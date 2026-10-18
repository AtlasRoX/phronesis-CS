"""
PubTator3 entity-based literature search plus LitVar2 variant summaries: an extra, modest literature channel.

References: Wei C.-H. et al., "PubTator 3.0: an AI-powered literature resource for unlocking biomedical knowledge",
Nucleic Acids Res. 2024;52(W1):W540-W546, doi:10.1093/nar/gkae235. Allot A. et al., "Tracking genetic variants in
the biomedical literature using LitVar 2.0", Nat. Genet. 2023;55:901-903, doi:10.1038/s41588-023-01414-x.

APIs (no key; NCBI asks for at most ~3 requests per second, enforced here by a shared throttle):
- `GET {PUBTATOR}/entity/autocomplete/?query=pancreatic cancer&concept=disease` ->
  [{"_id": "@DISEASE_Pancreatic_Neoplasms", "biotype": "disease", "db_id": "D010190", "name": ...}]
- `GET {PUBTATOR}/search/?text=@GENE_KRAS AND @DISEASE_Pancreatic_Neoplasms` -> {"results": [{"pmid", "title",
  "journal", "authors", "date", "doi", "text_hl": "... @<m>GENE_KRAS</m> @GENE_3845 @@@KRAS@@@ ..."}], "count"}.
  Results come ranked by relevance with title, journal and DOI, so no second lookup is needed for them.
- `GET {LITVAR}/variant/autocomplete/?query=EGFR T790M` -> [{"_id": "litvar@rs121434569##", "rsid", "gene", "name":
  "p.T790M", "pmids_count", "data_clinical_significance": [...]}]
- `GET {LITVAR}/variant/get/{litvar_id}/publications` -> {"pmids": [...]} (unranked); used only when the PubTator3
  search for the variant returns nothing, with titles from `{PUBTATOR}/publications/export/biocjson?pmids=...`.

Gene ids are "@GENE_<SYMBOL>", variant ids "@VARIANT_<p.change>_<GENE>_human"; the disease id is looked up from the
resolved disease label (a few simplified forms are tried, e.g. "adenocarcinoma" -> "carcinoma"). When no disease id
is found, the disease is searched as a quoted phrase. At most 10 PMIDs are contributed; each one is returned through
canonical_ids and allowed_identifiers so citing it passes the verifier's identifier check.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, field_validator

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "pubtator_literature"
SOURCE_KEY = "pubtator"
PUBTATOR = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"
LITVAR = "https://www.ncbi.nlm.nih.gov/research/litvar2-api"
MAX_PMIDS = 10
MIN_INTERVAL_S = 0.34  # ~3 requests per second across this process

NAME_RE = re.compile(r"\b(pubtator|litvar)\b", re.I)
_HL_ENTITY_RE = re.compile(r"(?:@(?!@@)\S+\s+)+@@@(.*?)@@@")
_TAG_RE = re.compile(r"</?m>")
_PROTEIN_CHANGE_RE = re.compile(r"^(?:p\.)?([A-Z])(\d{1,4})([A-Z*])$")

_lock: Optional[asyncio.Lock] = None
_last = 0.0


class PubtatorInput(BaseModel):
    gene: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9\-\.]{0,19}$")
    disease: Optional[str] = Field(default=None, max_length=120)
    variants: List[str] = Field(default_factory=list, max_length=2, description="protein changes, e.g. T790M")
    max_results: int = Field(default=MAX_PMIDS, ge=1, le=MAX_PMIDS)

    @field_validator("variants")
    @classmethod
    def _variants(cls, values: List[str]) -> List[str]:
        out = []
        for v in values:
            v = v.strip().upper().removeprefix("P.")
            if not _PROTEIN_CHANGE_RE.match(v):
                raise ValueError(f"unsupported protein change: {v[:20]!r}")
            out.append(v)
        return out


async def _throttle() -> None:
    global _lock, _last
    if _lock is None:
        _lock = asyncio.Lock()
    try:
        async with _lock:
            wait = _last + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _last = time.monotonic()
    except RuntimeError:  # a lock bound to an event loop that has since closed
        _lock = None


class _Throttle:
    """The shared throttle as an async context manager, so the HTTP helper enters it before the request."""

    async def __aenter__(self) -> "_Throttle":
        await _throttle()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


_THROTTLE = _Throttle()


async def _get(http: httpx.AsyncClient, url: str, source: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return await common.get_json(http, url, source=source, params=params, limiter=_THROTTLE)


def clean_highlight(text: str) -> str:
    """PubTator3 text_hl with its entity markup replaced by the surface text."""
    out = _HL_ENTITY_RE.sub(lambda m: m.group(1), text or "")
    return re.sub(r"\s+", " ", _TAG_RE.sub("", out)).strip()


def disease_queries(label: str) -> List[str]:
    """The label plus simplified forms PubTator's autocomplete knows ("pancreatic ductal adenocarcinoma" ->
    "pancreatic ductal carcinoma" -> "pancreatic cancer")."""
    base = re.sub(r"\s+", " ", (label or "").strip())
    if not base:
        return []
    out = [base]
    out.append(re.sub(r"\badenocarcinoma\b", "carcinoma", base, flags=re.I))
    out.append(re.sub(r"\s+", " ", re.sub(r"\b(?:ductal\s+)?(?:adeno)?carcinomas?\b|\bneoplasms?\b|\btumou?rs?\b|\bmalignan\w+\b",
                                           "cancer", base, flags=re.I)).strip())
    return list(dict.fromkeys(q for q in out if q))[:3]


async def resolve_disease(http: httpx.AsyncClient, label: str) -> Optional[Dict[str, str]]:
    for q in disease_queries(label):
        hits = await _get(http, f"{PUBTATOR}/entity/autocomplete/", "PubTator3", {"query": q, "concept": "disease", "limit": "3"})
        for h in hits if isinstance(hits, list) else []:
            if isinstance(h, dict) and str(h.get("_id") or "").startswith("@DISEASE_"):
                return {"id": str(h["_id"]), "name": str(h.get("name") or q), "mesh": str(h.get("db_id") or "")}
    return None


async def litvar_variant(http: httpx.AsyncClient, gene: Optional[str], change: str) -> Optional[Dict[str, Any]]:
    query = f"{gene} {change}" if gene else change
    hits = await _get(http, f"{LITVAR}/variant/autocomplete/", "LitVar2", {"query": query})
    best = None
    for h in hits if isinstance(hits, list) else []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or h.get("hgvs") or "")
        genes = [str(g).upper() for g in h.get("gene") or []]
        if name.upper().removeprefix("P.") != change or (gene and gene.upper() not in genes):
            continue
        score = (1 if h.get("rsid") else 0, int(h.get("pmids_count") or 0))
        if best is None or score > best[0]:
            best = (score, h)
    if best is None:
        return None
    h = best[1]
    return {
        "litvar_id": str(h.get("_id") or ""),
        "rsid": h.get("rsid"),
        "name": h.get("name"),
        "gene": [g for g in h.get("gene") or []],
        "pmids_count": h.get("pmids_count"),
        "clinical_significance": [s for s in h.get("data_clinical_significance") or []],
        "query": change,
    }


def _article(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pmid = str(r.get("pmid") or r.get("_id") or "").strip()
    if not pmid.isdigit():
        return None
    authors = [str(a) for a in r.get("authors") or []]
    snippet = clean_highlight(str(r.get("text_hl") or ""))
    return {
        "pmid": pmid,
        "title": re.sub(r"\s+", " ", str(r.get("title") or "")).strip(),
        "journal": r.get("journal"),
        "year": str(r.get("meta_date_publication") or r.get("date") or "")[:4] or None,
        "doi": r.get("doi") or None,
        "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
        "snippet": snippet[:400].rsplit(" ", 1)[0] + " [...]" if len(snippet) > 400 else snippet,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


async def search(http: httpx.AsyncClient, text: str, limit: int) -> Tuple[int, List[Dict[str, Any]]]:
    doc = await _get(http, f"{PUBTATOR}/search/", "PubTator3", {"text": text})
    if not isinstance(doc, dict):
        raise common.UpstreamError("PubTator3 returned an unexpected response", "upstream")
    arts = [a for a in (_article(r) for r in doc.get("results") or [] if isinstance(r, dict)) if a]
    return int(doc.get("count") or 0), arts[:limit]


async def litvar_publications(http: httpx.AsyncClient, litvar_id: str, limit: int) -> Tuple[int, List[Dict[str, Any]]]:
    doc = await _get(http, f"{LITVAR}/variant/get/{quote(litvar_id, safe='')}/publications", "LitVar2")
    pmids = [str(p) for p in (doc or {}).get("pmids") or [] if str(p).isdigit()]
    if not pmids:
        return 0, []
    export = await _get(http, f"{PUBTATOR}/publications/export/biocjson", "PubTator3", {"pmids": ",".join(pmids[:limit])})
    docs = (export or {}).get("PubTator3") if isinstance(export, dict) else export
    arts: List[Dict[str, Any]] = []
    for d in docs if isinstance(docs, list) else []:
        if not isinstance(d, dict):
            continue
        passages = [p for p in d.get("passages") or [] if isinstance(p, dict)]
        title = next((p.get("text") for p in passages if (p.get("infons") or {}).get("type") == "title"), "")
        abstract = next((p.get("text") for p in passages if (p.get("infons") or {}).get("type") == "abstract"), "")
        infons = (passages[0].get("infons") or {}) if passages else {}
        art = _article({"pmid": d.get("pmid") or d.get("id"), "title": title, "journal": d.get("journal"),
                        "meta_date_publication": infons.get("year"), "authors": d.get("authors") or [],
                        "text_hl": abstract})
        if art:
            arts.append(art)
    return len(pmids), arts


async def pubtator_literature(
    gene: Optional[str] = None, disease: Optional[str] = None, variants: Optional[List[str]] = None, max_results: int = MAX_PMIDS
) -> Any:
    """Top PubTator3 articles for gene AND disease (AND variant), plus LitVar2 summaries of the variants."""
    gene = (gene or "").strip().upper() or None
    variants = [v.upper().removeprefix("P.") for v in (variants or [])][:2]
    limit = max(1, min(max_results or MAX_PMIDS, MAX_PMIDS))
    subject = " ".join(x for x in [gene, *variants, disease or ""] if x)
    if not (gene or variants):
        return common.failure(TOOL_NAME, common.UpstreamError("a gene or variant is required", "bad_request"), subject)
    try:
        async with common.client(20.0) as http:
            disease_entity = await resolve_disease(http, disease) if disease else None
            variant_rows: List[Dict[str, Any]] = []
            for v in variants:
                try:
                    row = await litvar_variant(http, gene, v)
                except common.UpstreamError as exc:  # a LitVar2 outage must not hide the PubTator3 search
                    variant_rows.append({"query": v, "status": "error", "detail": exc.detail})
                    continue
                variant_rows.append(row or {"query": v, "status": "not_found"})
            disease_term = disease_entity["id"] if disease_entity else (f'"{disease}"' if disease else None)
            found = [r for r in variant_rows if r.get("litvar_id")]
            vterms = [f"@VARIANT_p.{r['query']}_{gene}_human" for r in found] if gene else []
            queries: List[List[Optional[str]]] = []
            if len(vterms) > 1:
                queries.append([*vterms, disease_term])  # e.g. T790M AND C797S together first
            if vterms:
                queries.append([vterms[0], disease_term])
            if gene and (disease_term or not found):
                queries.append([f"@GENE_{gene}", disease_term])
            total, articles, used = 0, [], ""
            for parts in queries:
                q = " AND ".join(t for t in parts if t)
                total, articles = await search(http, q, limit)
                used = q
                if articles:
                    break
            channel = "pubtator3_search"
            if not articles and found:
                total, articles = await litvar_publications(http, found[0]["litvar_id"], limit)
                used, channel = found[0]["litvar_id"], "litvar2_publications"
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)
    pmids = [a["pmid"] for a in articles]
    dois = [a["doi"] for a in articles if a.get("doi")]
    data = {
        "subject": subject,
        "gene": gene,
        "disease": disease,
        "disease_entity": disease_entity,
        "query": used,
        "channel": channel,
        "total_hits": total,
        "variants": variant_rows,
        "items": articles,
        "citation_records": [
            {"type": "pmid", "id": a["pmid"], "title": a["title"] or None, "url": a["url"], "passage": a["snippet"] or None,
             "authors": a["authors"] or None, "journal": a.get("journal"), "year": a.get("year"),
             "section": "PubTator3 search" if channel == "pubtator3_search" else "LitVar2 variant publications"}
            for a in articles
        ],
    }
    return common.ok(TOOL_NAME, data, {"pmids": ",".join(pmids), "dois": ",".join(dois),
                                       "rsids": ",".join(str(r["rsid"]) for r in variant_rows if r.get("rsid"))})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "PubTator3 entity-based PubMed search (gene AND disease, or a variant) returning the top PMIDs with titles, "
            "plus LitVar2 variant summaries (rsID, publication count, clinical significance)."
        ),
        domain="literature",
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=PubtatorInput,
        canonical_id_types=["pmids", "dois", "rsids"],
        timeout_seconds=40.0,
        tags=["pubtator", "litvar", "literature", "pubmed", "papers", "variant", "gene", "disease", "upstream:ncbi"],
        is_async=True,
    )


def _variant_changes(ctx: QueryContext, gene: Optional[str]) -> List[str]:
    out: List[str] = []
    for v in ctx.variants:
        pc = (v.protein_change or "").strip().upper()
        if pc and _PROTEIN_CHANGE_RE.match(pc) and (not gene or not v.gene or v.gene.upper() == gene):
            out.append(pc)
    return list(dict.fromkeys(out))[:2]


class PubtatorSource(common.OmicsSource):
    key = SOURCE_KEY
    agent = "Lithoscribe"
    action = "PubTator3 entity search and LitVar2 variant summary"
    tool_name = TOOL_NAME
    artifact_name = "pubtator"
    label = "PubTator3/LitVar2"
    subject_kind = "gene with a disease, or a variant"
    nothing_found = "articles"
    name_re = NAME_RE
    order = 68

    def intent(self, ctx: QueryContext) -> bool:
        # An extra literature channel for gene + disease and variant questions (no wording needed).
        return bool(self.build_plan(ctx))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        variants = _variant_changes(ctx, gene)
        _, disease = common.disease_trait(ctx)
        disease = (disease or "").strip()[:120] or None
        if not gene or not (disease or variants):
            return None
        plan: Dict[str, Any] = {"gene": gene, "_subject": " ".join(x for x in [gene, *variants, disease or ""] if x)}
        if disease:
            plan["disease"] = disease
        if variants:
            plan["variants"] = variants
        return plan

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        vs = [f"{v.get('name')} {v.get('rsid') or ''} ({v.get('pmids_count')} papers)".replace("  ", " ")
              for v in d.get("variants") or [] if v.get("litvar_id")]
        detail = f"query {d.get('query')}; {d.get('total_hits')} hits"
        if vs:
            detail += "; LitVar2: " + "; ".join(vs)
        return f"PubTator3: {len(self.items(d))} articles for {d.get('subject')}", detail

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("gene", "disease", "disease_entity", "query", "channel", "total_hits", "variants", "items")
        return {k: data.get(k) for k in keys}

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        lines = [
            f'<pubtator_literature source="PubTator3 / LitVar2 (NCBI)" query="{common.attr(d.get("query"))}" '
            f'channel="{common.attr(d.get("channel"))}" total_hits="{common.attr(d.get("total_hits"))}" '
            'note="Top PubMed articles by entity co-mention (PubTator3 relevance), with titles and a highlighted '
            'sentence; a co-mention is not a finding. Cite by PMID and state only what the title or sentence says. '
            'LitVar2 counts are publications mentioning the variant, not evidence of effect.">'
        ]
        for v in d.get("variants") or []:
            if v.get("litvar_id"):
                lines.append(
                    f'<litvar_variant query="{common.attr(v.get("query"))}" name="{common.attr(v.get("name"))}" '
                    f'rsid="{common.attr(v.get("rsid") or "")}" gene="{common.attr(", ".join(v.get("gene") or []))}" '
                    f'pmids_count="{common.attr(v.get("pmids_count"))}" '
                    f'clinical_significance="{common.attr(", ".join(v.get("clinical_significance") or []))}"/>'
                )
            else:
                lines.append(f'<litvar_variant query="{common.attr(v.get("query"))}" status="{common.attr(v.get("status") or "not_found")}"/>')
        for a in self.items(d):
            lines.append(
                f'<article pmid="{common.attr(a.get("pmid"))}" year="{common.attr(a.get("year") or "")}" '
                f'journal="{common.attr(a.get("journal") or "")}" doi="{common.attr(a.get("doi") or "")}" '
                f'title="{common.attr(a.get("title"))}">{common.text(a.get("snippet"))}</article>'
            )
        lines.append("</pubtator_literature>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        d = common.data_of(result)
        items = self.items(d)
        dois: Set[str] = set()
        for a in items:
            if a.get("doi"):
                dois.update({str(a["doi"]), str(a["doi"]).lower()})
        return {
            "pmids": {str(a["pmid"]) for a in items if a.get("pmid")},
            "dois": dois,
            "rsid": {str(v["rsid"]).lower() for v in d.get("variants") or [] if v.get("rsid")},
        }

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is not None and result.success:
            rows = common.data_of(result).get("variants") or []
            missing = [v["query"] for v in rows if not v.get("litvar_id") and v.get("status") != "error"]
            failed = [v["query"] for v in rows if v.get("status") == "error"]
            if missing:
                notes.append(f"LitVar2 has no entry for {', '.join(missing)}; do not state LitVar2 data for it.")
            if failed:
                notes.append(
                    f"LitVar2 could not be queried for {', '.join(failed)} (an outage, not an absence of publications); "
                    "do not state LitVar2 data for it."
                )
        return notes
