"""
SynLethDB synthetic-lethal (SL) partners of one gene from a local, user-provided bulk file.

References: Wang J. et al., "SynLethDB 2.0: a web-based knowledge graph database on synthetic lethality for novel
anticancer drug discovery", Database (Oxford) 2022;2022:baac030, doi:10.1093/database/baac030. SynLethDB 3.0
(https://www.synlethdb.com; the old https://synlethdb.sist.shanghaitech.edu.cn redirects there) distributes its
tables on Zenodo (record 22843223, doi:10.5281/zenodo.22843223, open access).

Bulk download (verified 2026-10-16, about 3.7 MB, tab separated):
    https://zenodo.org/records/22843223/files/Human.SL.detailed.tsv?download=1
Its header is `x:START_ID x_type x_name x_source y:END_ID y_type y_name y_source relation :TYPE rel_source edge_index
cell_line pubmed_id cancer`; rel_source is the evidence type (CRISPR/CRISPRi, GenomeRNAi, High Throughput, Low
Throughput, Text Mining, Computational Prediction, Decipher, ...). `scripts/download_synlethdb.py` fetches it to
~/.cache/phronesis/synlethdb. The SynLethDB 2.0 CSV layout (n1.name, n2.name, r.source, r.statistic_score,
r.pubmed_id, r.cell_line) and simple gene_a/gene_b/evidence/score/pmid tables are read too.

Point PHRONESIS_SYNLETHDB_DIR at the directory holding the file. Without it the source is skipped with a scope note
saying SL partners cannot be listed from SynLethDB, so no partner is ever invented. PHRONESIS_SYNLETHDB_RELEASE names
the release in citations.
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext, SourceDecision
from . import common
from .depmap import SYNTHETIC_LETHAL_RE

TOOL_NAME = "synlethdb_partners"
SOURCE_KEY = "synlethdb"
DOWNLOAD_URL = "https://zenodo.org/records/22843223/files/Human.SL.detailed.tsv?download=1"
FILE_NAMES = ("Human.SL.detailed.tsv", "Human_SL.csv", "Human_SL.tsv", "synlethdb_human_sl.tsv", "synlethdb_human_sl.csv")
MAX_PARTNERS = 20
MAX_PMIDS_PER_PARTNER = 5
# Druggable-partner enrichment: the top experimental partners are looked up in DGIdb (batched) and the first
# approval dates of the leading drugs in Drugs@FDA, within a budget well under the tool timeout. Any failure keeps
# the plain partner list and says the lookup failed.
ENRICH_PARTNERS = 20
DRUGS_PER_PARTNER = 5
FDA_DATE_DRUGS = 6
TOOL_TIMEOUT_S = 60.0
ENRICH_BUDGET_S = 30.0
DGIDB_TIMEOUT_S = 12.0
FDA_BUDGET_S = 8.0

NAME_RE = re.compile(r"\bsynleth(?:db)?\b", re.I)
# Evidence types that are predictions or literature mining rather than a screen or experiment.
PREDICTED = {"computational prediction", "text mining", "predicted", "prediction"}

_COLUMNS = {
    "gene_a": ("x_name", "gene_a", "gene_a.name", "n1.name", "genea", "gene1", "gene_a_name", "symbol_a"),
    "gene_b": ("y_name", "gene_b", "gene_b.name", "n2.name", "geneb", "gene2", "gene_b_name", "symbol_b"),
    "evidence": ("rel_source", "evidence", "source", "r.source", "evidence_type"),
    "score": ("score", "statistic_score", "r.statistic_score", "sl_score"),
    "pmid": ("pubmed_id", "pmid", "r.pubmed_id", "pmids"),
    "cell_line": ("cell_line", "r.cell_line"),
    "cancer": ("cancer", "r.cancer", "disease"),
    "relation": ("relation", "r.relation"),
}


class SynlethdbInput(BaseModel):
    gene: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9\-\.]{0,19}$", description="HGNC symbol")
    disease: Optional[str] = Field(default=None, max_length=120, description="rank partners reported in this cancer first")
    druggable_partners: bool = Field(
        default=True,
        description="also look up approved drugs acting on the top experimental partners (DGIdb, Drugs@FDA dates)",
    )


def synlethdb_dir() -> Optional[Path]:
    raw = os.getenv("PHRONESIS_SYNLETHDB_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def sl_file() -> Optional[Path]:
    d = synlethdb_dir()
    if d is None or not d.is_dir():
        return None
    for name in FILE_NAMES:
        if (d / name).is_file():
            return d / name
    candidates = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".tsv", ".csv")
                        and re.search(r"\bsl\b|_sl|\.sl\.", p.name, re.I) and not re.search(r"non[._-]?sl|computed", p.name, re.I))
    return candidates[0] if candidates else None


_INDEX: Dict[Tuple[str, float, int], Dict[str, List[Dict[str, str]]]] = {}
_LOCK = threading.Lock()


def _column_map(header: List[str]) -> Dict[str, int]:
    norm = [h.strip().strip('"').lower() for h in header]
    out: Dict[str, int] = {}
    for key, names in _COLUMNS.items():
        for n in names:
            if n in norm:
                out[key] = norm.index(n)
                break
    return out


def _clean(value: str) -> str:
    v = (value or "").strip().strip('"').strip()
    return "" if v.upper() in ("", "NA", "NAN", "NONE", "TBD", "NULL") else v


def load_index(path: Path) -> Dict[str, List[Dict[str, str]]]:
    """SYMBOL -> rows naming it (cached per file version). Raises ValueError when the gene columns are missing."""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    with _LOCK:
        if key in _INDEX:
            return _INDEX[key]
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    index: Dict[str, List[Dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delim)
        cols = _column_map(next(reader))
        if "gene_a" not in cols or "gene_b" not in cols:
            raise ValueError("no gene columns (expected x_name/y_name, n1.name/n2.name or gene_a/gene_b)")
        for parts in reader:
            row = {k: _clean(parts[i]) if i < len(parts) else "" for k, i in cols.items()}
            rel = row.get("relation", "")
            if rel and rel.upper() not in ("SL", "SYNTHETIC_LETHALITY", "SYNTHETIC LETHALITY"):
                continue
            a, b = row["gene_a"].upper(), row["gene_b"].upper()
            if not a or not b or a == b:
                continue
            index.setdefault(a, []).append(row)
            index.setdefault(b, []).append(row)
    with _LOCK:
        _INDEX.clear()
        _INDEX[key] = index
    return index


def _pmids(raw: str) -> List[str]:
    return [p for p in re.split(r"[;,|\s]+", raw or "") if p.isdigit()]


def partners_of(index: Dict[str, List[Dict[str, str]]], gene: str, disease: Optional[str] = None) -> List[Dict[str, Any]]:
    symbol = gene.upper()
    words = [w for w in re.findall(r"[a-z]{4,}", (disease or "").lower()) if w not in ("cancer", "carcinoma", "tumor", "tumour", "neoplasm")]
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in index.get(symbol) or []:
        partner = row["gene_b"] if row["gene_a"].upper() == symbol else row["gene_a"]
        g = grouped.setdefault(partner.upper(), {"partner": partner.upper(), "evidence": set(), "pmids": [],
                                                 "cell_lines": set(), "cancers": set(), "scores": []})
        if row.get("evidence"):
            g["evidence"].add(row["evidence"])
        for p in _pmids(row.get("pmid", "")):
            if p not in g["pmids"]:
                g["pmids"].append(p)
        for c in re.split(r"[;,]", row.get("cell_line", "")):
            if c.strip():
                g["cell_lines"].add(c.strip())
        if row.get("cancer"):
            g["cancers"].add(re.sub(r"_[A-Z]?\d{4,}$", "", row["cancer"]).strip())
        try:
            if row.get("score"):
                g["scores"].append(float(row["score"]))
        except ValueError:
            pass
    out: List[Dict[str, Any]] = []
    for g in grouped.values():
        evidence = sorted(g["evidence"])
        experimental = [e for e in evidence if e.lower() not in PREDICTED]
        cancers = sorted(g["cancers"])
        out.append({
            "partner": g["partner"],
            "evidence_types": evidence,
            "experimental": bool(experimental),
            "prediction_only": bool(evidence) and not experimental,
            "pmids": g["pmids"][:MAX_PMIDS_PER_PARTNER],
            "n_pmids": len(g["pmids"]),
            "cell_lines": sorted(g["cell_lines"])[:5],
            "cancers": cancers[:5],
            "max_score": round(max(g["scores"]), 4) if g["scores"] else None,
            "in_question_disease": bool(words) and any(w in c.lower() for c in cancers for w in words),
        })
    out.sort(key=lambda p: (not p["in_question_disease"], not p["experimental"], -len([e for e in p["evidence_types"] if e.lower() not in PREDICTED]),
                            -p["n_pmids"], -(p["max_score"] or 0.0), p["partner"]))
    return out


def synlethdb_partners(gene: str, disease: Optional[str] = None, druggable_partners: bool = False) -> Any:
    """Synthetic-lethal partners of one gene from the local SynLethDB file (sync; runs in a worker).

    With druggable_partners (the registry passes the input schema's default, True) and experimental partners found,
    the return value is an awaitable the registry awaits on the event loop: it adds the approved drugs acting on the
    top experimental partners (see add_druggable_partners). A direct call keeps the default False and gets the plain,
    offline ToolResult."""
    started = time.monotonic()
    result, partners = _partners_result(gene, disease)
    if not (druggable_partners and result.success):
        return result
    data = common.data_of(result)
    targets, excluded = enrichment_targets(partners, data.get("gene") or "")
    if not targets:
        result.data = _with_lookup(data, [], {"status": "skipped", "detail": "no experimental partner to look up",
                                              "excluded_tumour_suppressors": excluded})
        return result
    budget = min(ENRICH_BUDGET_S, TOOL_TIMEOUT_S - (time.monotonic() - started) - 5.0)
    if budget < 3.0:
        result.data = _with_lookup(data, [], {"status": "skipped", "detail": "reading the SynLethDB file used the time budget"})
        return result
    return add_druggable_partners(result, targets, excluded, budget)


def enrichment_targets(partners: List[Dict[str, Any]], subject: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(the top ENRICH_PARTNERS experimental partners, best evidence first; the tumour suppressors left out). A lost
    tumour suppressor partner (BRCA1 for BRCA2) has biomarker associations in DGIdb, not drugs that act on it."""
    from .dgidb import loss_of_function_genes

    experimental = [p for p in partners if p.get("experimental") and p.get("partner") and p["partner"] != subject.upper()]
    suppressors = set(loss_of_function_genes([p["partner"] for p in experimental]))
    kept = [p for p in experimental if p["partner"] not in suppressors]
    return kept[:ENRICH_PARTNERS], sorted(suppressors)


def _with_lookup(data: Dict[str, Any], rows: List[Dict[str, Any]], lookup: Dict[str, Any]) -> Dict[str, Any]:
    """data with druggable_partners and druggable_lookup placed before the long partner list (compact JSON views
    truncate from the end)."""
    head = {k: v for k, v in data.items() if k not in ("items", "citation_records")}
    return {**head, "druggable_partners": rows, "druggable_lookup": lookup,
            "items": data.get("items") or [], "citation_records": data.get("citation_records") or []}


async def _fda_dates(rows: List[Dict[str, Any]], budget_s: float) -> int:
    """First approval dates from Drugs@FDA for the leading FDA_DATE_DRUGS drugs (each partner's first
    inhibitor-type drug before any second drug); sets each drug's `fda` record and returns how many resolved."""
    from ..drugintel.drugsfda import DRUGSFDA_LIMIT, drugsfda_search, summarize_approval
    from ..drugintel.openfda_client import OpenFdaClient

    names: List[str] = []
    depth = 0
    while len(names) < FDA_DATE_DRUGS and any(depth < len(r["drugs"]) for r in rows):
        for r in rows:
            if depth < len(r["drugs"]) and r["drugs"][depth]["name"] not in names and len(names) < FDA_DATE_DRUGS:
                names.append(r["drugs"][depth]["name"])
        depth += 1
    client = OpenFdaClient(transport=common.TRANSPORT)

    async def one(name: str) -> Dict[str, Any]:
        search = drugsfda_search(name)
        body, found = await client.drugsfda(search, DRUGSFDA_LIMIT)
        a = summarize_approval(name, body, found, search)
        return {"status": a.status, "first_approval_date": a.first_approval_date,
                "application_number": a.application_number, "brand_names": list(a.brand_names or [])[:2]}

    tasks = {n: asyncio.ensure_future(one(n)) for n in names}
    if tasks:
        _, pending = await asyncio.wait(list(tasks.values()), timeout=budget_s)
        for t in pending:
            t.cancel()
    resolved = 0
    for r in rows:
        for d in r["drugs"]:
            task = tasks.get(d["name"])
            if task is None:
                d["fda"] = {"status": "not_checked"}
            elif task.done() and not task.cancelled() and task.exception() is None:
                d["fda"] = task.result()
                resolved += 1 if d["fda"]["status"] == "approved" else 0
            else:
                d["fda"] = {"status": "error"}
    return resolved


async def find_druggable_partners(targets: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(partners with approved drugs, lookup record) for SynLethDB partners: one batched DGIdb query, then Drugs@FDA
    first approval dates for the leading drugs. Raises when DGIdb could not be queried at all."""
    from . import dgidb

    names = [p["partner"] for p in targets]
    by_gene = await dgidb.approved_drugs_by_gene(names, per_gene=DRUGS_PER_PARTNER, timeout_s=DGIDB_TIMEOUT_S)
    rows: List[Dict[str, Any]] = []
    for p in targets:
        drugs = [dict(d) for d in by_gene.get(p["partner"]) or []]
        if drugs:
            rows.append({"partner": p["partner"], "evidence": list(p.get("evidence_types") or []),
                         "sl_pmids": list(p.get("pmids") or [])[:3], "drugs": drugs})
    unchecked = [n for n in names if n not in by_gene]
    lookup: Dict[str, Any] = {
        "status": "partial" if unchecked else "ok",
        "partners_checked": [n for n in names if n in by_gene],
        "partners_not_checked": unchecked,
    }
    if rows:
        try:
            resolved = await _fda_dates(rows, FDA_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 - dates are optional; the drug pipeline can still resolve them
            for r in rows:
                for d in r["drugs"]:
                    d.setdefault("fda", {"status": "error"})
            lookup["fda_dates"] = f"Drugs@FDA lookup failed ({type(exc).__name__}); approval dates not resolved here"
        else:
            lookup["fda_dates"] = (
                f"Drugs@FDA first approval dates looked up for the leading {FDA_DATE_DRUGS} drugs ({resolved} FDA-approved); "
                "other drugs' FDA status and dates were not checked here"
            )
    return rows, lookup


async def add_druggable_partners(result: Any, targets: List[Dict[str, Any]], excluded: List[str], budget_s: float) -> Any:
    """The SynLethDB result with druggable_partners added; a failed or slow lookup keeps the partner list and records
    the failure (never fails the tool)."""
    data = common.data_of(result)
    try:
        rows, lookup = await asyncio.wait_for(find_druggable_partners(targets), timeout=budget_s)
    except asyncio.TimeoutError:
        result.data = _with_lookup(data, [], {"status": "failed", "detail": f"approved-drug lookup exceeded {budget_s:g}s",
                                              "excluded_tumour_suppressors": excluded})
        return result
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {str(exc)[:120]}"
        result.data = _with_lookup(data, [], {"status": "failed", "detail": f"DGIdb lookup failed ({detail})",
                                              "excluded_tumour_suppressors": excluded})
        return result
    lookup["excluded_tumour_suppressors"] = excluded
    pmids = list(dict.fromkeys(p for r in rows for d in r["drugs"] for p in d.get("pmids") or []))[:20]
    known = {c.get("id") for c in data.get("citation_records") or []}
    data["citation_records"] = [
        *(data.get("citation_records") or []),
        *({"type": "pmid", "id": p, "url": f"https://pubmed.ncbi.nlm.nih.gov/{p}/",
           "section": "DGIdb drug-gene interaction for a synthetic-lethal partner"} for p in pmids if p not in known),
        *({"type": "url", "id": f"DGIdb:{r['partner']}", "title": f"DGIdb 5 drug-gene interactions for {r['partner']}",
           "url": f"https://dgidb.org/results?searchType=gene&searchTerms={r['partner']}",
           "section": "DGIdb interaction (synthetic-lethal partner)"} for r in rows),
    ]
    result.data = _with_lookup(data, rows, lookup)
    ids = dict(result.canonical_ids or {})
    all_pmids = list(dict.fromkeys([*(ids.get("pmids") or "").split(","), *pmids]))
    ids["pmids"] = ",".join(p for p in all_pmids if p)
    chembl = sorted({d["chembl_id"] for r in rows for d in r["drugs"] if d.get("chembl_id")})
    if chembl:
        ids["chembl_ids"] = ",".join(chembl)
    result.canonical_ids = {k: v for k, v in ids.items() if v}
    return result


def _partners_result(gene: str, disease: Optional[str] = None) -> Tuple[Any, List[Dict[str, Any]]]:
    """(the ToolResult, every partner ranked) for one gene; the partner list is empty on a failure."""
    from ..tools.registry import ErrorType, ToolResult

    symbol = gene.strip().upper()
    path = sl_file()
    if path is None:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.DATASET_REQUIRED,
            error="No SynLethDB human SL file configured (set PHRONESIS_SYNLETHDB_DIR)",
            data={"status": "skipped", "subject": symbol},
        ), []
    try:
        index = load_index(path)
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration, ValueError) as exc:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.INTERNAL_ERROR,
            error=f"SynLethDB file could not be read: {type(exc).__name__}: {str(exc)[:120]}",
            data={"status": "error", "subject": symbol},
        ), []
    if symbol not in index:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.NOT_FOUND,
            error=f"{symbol} has no synthetic-lethal pair in the local SynLethDB file ({path.name})",
            data={"status": "not_found", "subject": symbol},
        ), []
    partners = partners_of(index, symbol, disease)
    shown = partners[:MAX_PARTNERS]
    release = os.getenv("PHRONESIS_SYNLETHDB_RELEASE", "").strip() or path.name
    pmids = list(dict.fromkeys(p for row in shown for p in row["pmids"]))
    data = {
        "subject": symbol,
        "gene": symbol,
        "disease": disease,
        "release": release,
        "file": path.name,
        "n_partners": len(partners),
        "n_experimental": sum(1 for p in partners if p["experimental"]),
        "n_prediction_only": sum(1 for p in partners if p["prediction_only"]),
        "items": shown,
        "citation_records": [
            *({"type": "pmid", "id": p, "url": f"https://pubmed.ncbi.nlm.nih.gov/{p}/",
               "section": f"SynLethDB synthetic-lethal pair evidence for {symbol}"} for p in pmids),
            {"type": "url", "id": f"SynLethDB:{symbol}", "title": f"SynLethDB synthetic-lethal partners of {symbol} ({release})",
             "url": "https://www.synlethdb.com", "section": "local SynLethDB bulk file"},
        ],
    }
    return ToolResult(success=True, tool_name=TOOL_NAME, data=data, canonical_ids={"pmids": ",".join(pmids)} if pmids else {},
                      metadata={"source_release": f"SynLethDB {release}"}), partners


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "SynLethDB synthetic-lethal partners of a gene from the local bulk file (PHRONESIS_SYNLETHDB_DIR): each "
            "partner with its evidence types (CRISPR, RNAi, high/low throughput, text mining, prediction), cell lines, "
            "cancers and PMIDs, plus the approved drugs (DGIdb) acting on the top experimental partners, with Drugs@FDA "
            "first approval dates for the leading ones."
        ),
        domain="genetics",
        adapter_type=AdapterType.LOCAL_COMPUTE,
        input_schema=SynlethdbInput,
        canonical_id_types=["pmids"],
        timeout_seconds=60.0,
        tags=["synlethdb", "synthetic_lethality", "synthetic_lethal", "sl_partners", "genetic_interaction", "cancer"],
        is_async=False,
    )


class SynlethdbSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "SynLethDB synthetic-lethal partners (local bulk file)"
    tool_name = TOOL_NAME
    artifact_name = "synlethdb"
    label = "SynLethDB"
    nothing_found = "synthetic-lethal partners"
    name_re = NAME_RE
    order = 69

    def intent(self, ctx: QueryContext) -> bool:
        return bool(SYNTHETIC_LETHAL_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        if not gene:
            return None
        _, disease = common.disease_trait(ctx)
        plan: Dict[str, Any] = {"gene": gene, "_subject": gene}
        if disease:
            plan["disease"] = disease[:120]
        return plan

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        decision = super().should_run(ctx)
        if decision.run and sl_file() is None:
            return SourceDecision(run=False, reason="no SynLethDB file configured (PHRONESIS_SYNLETHDB_DIR)")
        return decision

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        detail = f"{d.get('n_experimental')} with screen or experimental evidence, {d.get('n_prediction_only')} prediction or text mining only"
        rows = [r for r in d.get("druggable_partners") or [] if isinstance(r, dict)]
        lookup = d.get("druggable_lookup") or {}
        if rows:
            shown = [f"{r['partner']} ({', '.join(x['name'] for x in (r.get('drugs') or [])[:3])})" for r in rows[:5]]
            detail += f" · druggable partners (approved drugs, DGIdb): {'; '.join(shown)}" + (f" +{len(rows) - 5} more" if len(rows) > 5 else "")
        elif lookup.get("status") == "failed":
            detail += f" · approved-drug lookup for partners failed ({str(lookup.get('detail') or '')[:120]})"
        elif lookup.get("status") in ("ok", "partial"):
            detail += f" · no approved drug in DGIdb for the top {len(lookup.get('partners_checked') or [])} experimental partners"
        return f"SynLethDB: {d.get('n_partners')} synthetic-lethal partners of {d.get('gene')}", detail

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("gene", "disease", "release", "n_partners", "n_experimental", "n_prediction_only", "items")
        out = {k: data.get(k) for k in keys}
        if "druggable_partners" in data:
            out["druggable_partners"] = [
                {"partner": r.get("partner"), "evidence": r.get("evidence") or [],
                 "drugs": [{"name": x.get("name"), "chembl_id": x.get("chembl_id"), "interaction_types": x.get("interaction_types") or [],
                            "interaction_class": x.get("interaction_class"), "pmids": x.get("pmids") or [],
                            "fda_status": (x.get("fda") or {}).get("status"),
                            "fda_first_approval": (x.get("fda") or {}).get("first_approval_date")}
                           for x in r.get("drugs") or []]}
                for r in data.get("druggable_partners") or [] if isinstance(r, dict)
            ]
            lookup = data.get("druggable_lookup") or {}
            out["druggable_lookup"] = {k: lookup.get(k) for k in ("status", "detail", "fda_dates") if lookup.get(k)}
        return out

    @staticmethod
    def druggable_xml(d: Dict[str, Any]) -> List[str]:
        """<druggable_partners>: approved drugs acting on the experimental partners, or why they were not looked up."""
        lookup = d.get("druggable_lookup")
        if not isinstance(lookup, dict):
            return []
        gene = common.attr(d.get("gene"))
        status = lookup.get("status")
        excluded = ", ".join(lookup.get("excluded_tumour_suppressors") or [])
        excluded_note = (f" Tumour-suppressor partners ({common.attr(excluded)}) were not looked up: their DGIdb drugs are "
                         "biomarker associations, not drugs acting on them.") if excluded else ""
        if status == "failed":
            return [f'<druggable_partners status="failed" note="The approved-drug lookup for the synthetic-lethal partners '
                    f'failed ({common.attr(lookup.get("detail"))}). This is an outage, not evidence that the partners have '
                    f'no approved drugs: say the lookup failed rather than that none exist.{excluded_note}"/>']
        if status == "skipped":
            return [f'<druggable_partners status="skipped" note="{common.attr(lookup.get("detail"))}.{excluded_note}"/>']
        checked = lookup.get("partners_checked") or []
        rows = [r for r in d.get("druggable_partners") or [] if isinstance(r, dict)]
        lines = [
            f'<druggable_partners source="DGIdb 5 (approved drugs) + Drugs@FDA (first approval dates)" status="{common.attr(status)}" '
            f'partners_checked="{common.attr(", ".join(checked))}" '
            f'note="Approved drugs DGIdb lists as interacting with the top experimental synthetic-lethal partners of {gene} '
            '(partners checked are listed; a checked partner absent here has no approved drug with curated or published '
            'support in DGIdb). approved means approved by some regulator in DGIdb; FDA approval and its date are '
            'established only where fda_status=&quot;approved&quot; (Drugs@FDA first_approval date). For drugs with '
            'fda_status=&quot;not_checked&quot; the FDA status and date were not looked up here: take them from the drug '
            'sources if present, otherwise say they were not retrieved. class=&quot;inhibits&quot; is an '
            'inhibitor/antagonist-type interaction; class=&quot;untyped&quot; may be a biomarker association, do not call '
            f'it inhibition. A synthetic-lethal pair plus an approved drug is a hypothesis for {gene}-deficient tumours, '
            f'not evidence of clinical benefit; cite the SL PMIDs and the drug PMIDs as given.{excluded_note}">'
        ]
        for r in rows:
            lines.append(f'<partner name="{common.attr(r.get("partner"))}" sl_evidence="{common.attr("; ".join(r.get("evidence") or []))}" '
                         f'sl_pmids="{common.attr(",".join(r.get("sl_pmids") or []))}">')
            for x in r.get("drugs") or []:
                fda = x.get("fda") or {}
                attrs = {
                    "name": x.get("name"),
                    "class": x.get("interaction_class"),
                    "types": ", ".join(x.get("interaction_types") or []) or "not stated",
                    "chembl_id": x.get("chembl_id"),
                    "pmids": ", ".join(x.get("pmids") or []) or None,
                    "sources": ", ".join(x.get("sources") or []) or None,
                    "fda_status": fda.get("status") or "not_checked",
                    "fda_first_approval": fda.get("first_approval_date"),
                    "fda_application": fda.get("application_number"),
                }
                lines.append("<drug " + " ".join(f'{k}="{common.attr(v)}"' for k, v in attrs.items() if v not in (None, "")) + "/>")
            lines.append("</partner>")
        lines.append("</druggable_partners>")
        return lines

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        lines = [
            f'<synlethdb_partners source="SynLethDB (local bulk file)" gene="{common.attr(d.get("gene"))}" '
            f'release="{common.attr(d.get("release"))}" n_partners="{common.attr(d.get("n_partners"))}" '
            f'n_experimental="{common.attr(d.get("n_experimental"))}" '
            'note="Reported synthetic-lethal pairs with their evidence types and PMIDs. Computational Prediction and '
            'Text Mining rows are not experimental evidence: say so. Name only partners listed here, with their PMIDs; '
            'cell-line SL does not establish clinical benefit.">'
        ]
        for p in self.items(d):
            fields = {
                "partner": p.get("partner"),
                "evidence_types": "; ".join(p.get("evidence_types") or []),
                "prediction_only": str(bool(p.get("prediction_only"))).lower(),
                "pmids": ",".join(p.get("pmids") or []),
                "n_pmids": p.get("n_pmids"),
                "cell_lines": "; ".join(p.get("cell_lines") or []),
                "cancers": "; ".join(p.get("cancers") or []),
                "max_score": p.get("max_score"),
            }
            lines.append("<sl_partner " + " ".join(f'{k}="{common.attr(v)}"' for k, v in fields.items() if v not in (None, "")) + "/>")
        lines.extend(self.druggable_xml(d))
        lines.append("</synlethdb_partners>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        d = common.data_of(result)
        rows = [r for r in d.get("druggable_partners") or [] if isinstance(r, dict)]
        pmids = {p for row in self.items(d) for p in row.get("pmids") or []}
        pmids |= {p for r in rows for p in r.get("sl_pmids") or []}
        pmids |= {p for r in rows for x in r.get("drugs") or [] for p in x.get("pmids") or []}
        out: Dict[str, Set[str]] = {"pmids": pmids}
        chembl = {str(x["chembl_id"]) for r in rows for x in r.get("drugs") or [] if x.get("chembl_id")}
        if chembl:
            out["chembl"] = chembl
        return out

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        if result is None:
            if (self.intent(ctx) or self.named(ctx)) and common.gene_symbol(ctx) and sl_file() is None and not common.simulation_only(ctx):
                return [
                    "SynLethDB was not consulted: no local SynLethDB file is configured (PHRONESIS_SYNLETHDB_DIR), so "
                    "synthetic-lethal partners cannot be listed from SynLethDB. Do not name SL partners as SynLethDB "
                    "pairs, and name a partner at all only when another retrieved source reports it, with that source."
                ]
            return []
        if not result.success and getattr(result.error_type, "value", result.error_type) == "NOT_FOUND":
            subject = self.subject(ctx) or common.data_of(result).get("subject") or "this gene"
            return [
                f"The local SynLethDB file lists no synthetic-lethal pair for {subject}. That is the absence of a "
                "curated pair in this release, not evidence that none exists; do not invent partners."
            ]
        notes = super().scope_notes(ctx, result)
        lookup = common.data_of(result).get("druggable_lookup") if result.success else None
        if isinstance(lookup, dict) and lookup.get("status") == "failed":
            notes.append(
                f"Approved drugs acting on the SynLethDB partners could not be looked up ({str(lookup.get('detail') or '')[:160]}). "
                "That is an outage, not evidence the partners are undruggable: name a drug for a partner only when "
                "another retrieved source (DGIdb, ChEMBL, the drug sources) reports it."
            )
        return notes
