"""
DGIdb drug-gene interactions for the genes a drug, repurposing, druggability or target question names.

Reference: Cannon M. et al., "DGIdb 5.0: rebuilding the drug-gene interaction database for precision medicine and
drug discovery platforms", Nucleic Acids Res. 2024;52(D1):D1227-D1235, doi:10.1093/nar/gkad1040.

API: the DGIdb v5 GraphQL endpoint https://dgidb.org/api/graphql (free, no key):
    query($names:[String!]!){genes(names:$names){nodes{name conceptId longName
      interactions{drug{name conceptId approved} interactionScore interactionTypes{type directionality}
                   publications{pmid} sources{sourceDbName}}}}}
An unknown gene is simply absent from `nodes`. A malformed query answers 200 with {"errors": [...]}, reported as a
failure. DGIdb aggregates about 40 sources (ChEMBL, CIViC, OncoKB, DrugBank-derived sets, Guide to Pharmacology,
...): an interaction can be a biomarker association (KRAS mutation predicting resistance to cetuximab) rather than
binding, `approved` means approved by some regulator (not necessarily FDA), and the interaction score is DGIdb's
evidence-weighted score relative to the gene's other interactions, not affinity or efficacy.

The same drug can appear under several concept ids (rxcui and chembl); rows are merged by drug name. Up to
MAX_INTERACTIONS drugs per gene are kept: approved drugs first, then by interaction score.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import httpx
from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext
from ..utils.http_limits import TTLCache
from . import common

TOOL_NAME = "dgidb_drug_gene_interactions"
SOURCE_KEY = "dgidb"
ENDPOINT = "https://dgidb.org/api/graphql"
GENE_URL = "https://dgidb.org/results?searchType=gene&searchTerms={gene}"
MAX_GENES = 6
MAX_INTERACTIONS = 25
MAX_PMIDS_PER_DRUG = 5
MAX_PMID_CITATIONS = 30
TIMEOUT_S = 15.0

QUERY = (
    "query($names:[String!]!){genes(names:$names){nodes{name conceptId longName "
    "interactions{drug{name conceptId approved} interactionScore interactionTypes{type directionality} "
    "publications{pmid} sources{sourceDbName}}}}}"
)

INTENT_RE = re.compile(
    r"\b(drugs?|drugg?ab\w*|tractab\w*|repurpos\w*|inhibitors?|antagonists?|agonists?|small[\s-]+molecules?|"
    r"compounds?|ligands?|therap(?:y|ies|eutics?)|treatments?|targetab\w*|actionab\w*|targets?|targeting|targeted|"
    r"leads?\s+(?:compounds?|optimi[sz]ation|discovery|series|generation)|dgidb|drug[\s-]+gene)\b",
    re.I,
)
NAME_RE = re.compile(r"\b(dgidb|drug[\s-]+gene\s+interaction\s+database)\b", re.I)
_DRUG_DRUG_RE = re.compile(r"\bdrug[\s-]+drug\s+interactions?\b", re.I)
_GENE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")

_CACHE = TTLCache(maxsize=256, ttl_s=24 * 3600)


class DgidbInput(BaseModel):
    genes: List[str] = Field(..., min_length=1, max_length=MAX_GENES, description="HGNC symbols")
    limit: int = Field(default=MAX_INTERACTIONS, ge=1, le=50, description="drugs kept per gene (approved first, by score)")


# -----------------------------------------------------------------------------
# GraphQL transport (shared with pharos.py)
# -----------------------------------------------------------------------------

async def graphql_post(
    url: str, query: str, *, source: str, variables: Optional[Dict[str, Any]] = None, timeout_s: float = TIMEOUT_S
) -> Dict[str, Any]:
    """POST a GraphQL query and return its `data`. Retries once on a timeout, a connection error or a 5xx; GraphQL
    `errors` and non-JSON bodies raise common.UpstreamError (a failure, never an empty result)."""
    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    last: Optional[common.UpstreamError] = None
    for attempt in range(2):
        try:
            async with common.client(timeout_s) as http:
                resp = await http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            last = common.UpstreamError(f"{source} timed out", "timeout")
            last.__cause__ = exc
        except httpx.TransportError as exc:
            last = common.UpstreamError(f"{source} unreachable ({type(exc).__name__})")
            last.__cause__ = exc
        else:
            code = resp.status_code
            if code >= 500:
                last = common.UpstreamError(f"{source} is unavailable (HTTP {code})", "upstream", code)
            elif code == 429:
                raise common.UpstreamError(f"{source} rate limited the request (HTTP 429)", "upstream", code)
            elif code >= 400:
                raise common.UpstreamError(f"{source} rejected the request (HTTP {code})", "bad_request", code)
            else:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise common.UpstreamError(f"{source} returned a non-JSON response (HTTP {code})", "upstream", code) from exc
                if not isinstance(body, dict):
                    raise common.UpstreamError(f"{source} returned an unexpected response", "upstream", code)
                errors = body.get("errors")
                if errors:
                    first = errors[0] if isinstance(errors, list) and errors else errors
                    msg = str(first.get("message") if isinstance(first, dict) else first)[:160]
                    raise common.UpstreamError(f"{source} GraphQL error: {msg}", "upstream", code)
                data = body.get("data")
                if not isinstance(data, dict):
                    raise common.UpstreamError(f"{source} returned no data", "upstream", code)
                return data
        if attempt == 0:
            await asyncio.sleep(0.5)
    assert last is not None
    raise last


# -----------------------------------------------------------------------------
# Tool
# -----------------------------------------------------------------------------

def _pmids(values: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for v in values:
        s = str(v or "").strip()
        if s.isdigit() and s not in out:
            out.append(s)
    return out


def merge_interactions(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per drug name (DGIdb lists a drug once per concept id), approved first, then by score."""
    merged: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        drug = r.get("drug") or {}
        name = re.sub(r"\s+", " ", str(drug.get("name") or "")).strip().upper()
        if not name:
            continue
        m = merged.setdefault(name, {"drug": name, "approved": False, "score": None, "types": [], "pmids": [],
                                     "sources": [], "concept_ids": []})
        m["approved"] = m["approved"] or bool(drug.get("approved"))
        score = r.get("interactionScore")
        if isinstance(score, (int, float)):
            m["score"] = max(float(score), m["score"]) if m["score"] is not None else float(score)
        for t in r.get("interactionTypes") or []:
            t = str((t or {}).get("type") or "").strip().lower()
            if t and t not in m["types"]:
                m["types"].append(t)
        for pmid in _pmids([(p or {}).get("pmid") for p in r.get("publications") or []]):
            if pmid not in m["pmids"]:
                m["pmids"].append(pmid)
        for s in r.get("sources") or []:
            s = str((s or {}).get("sourceDbName") or (s or {}).get("fullName") or "").strip()
            if s and s not in m["sources"]:
                m["sources"].append(s)
        cid = str(drug.get("conceptId") or "").strip()
        if cid and cid not in m["concept_ids"]:
            m["concept_ids"].append(cid)
    out = list(merged.values())
    for m in out:
        m["weak"] = is_weak_interaction(m)
    # Supported interactions first: a single assay-database record with no publication (HCV protease inhibitors
    # listed as KRAS "inhibitors" from one ChEMBL screen) sorts after every curated or published interaction.
    out.sort(key=lambda m: (m["weak"], not m["approved"], -(m["score"] or 0.0), m["drug"]))
    return out


def is_weak_interaction(m: Dict[str, Any]) -> bool:
    """One source and no publication: a screening or text-mined record, not curated evidence of an interaction."""
    return len(m.get("sources") or []) <= 1 and not (m.get("pmids") or [])


def _chembl_id(concept_ids: Sequence[str]) -> Optional[str]:
    return next((c.split(":", 1)[1].upper() for c in concept_ids if c.lower().startswith("chembl:")), None)


def _item(gene: str, m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "gene": gene,
        "drug": m["drug"],
        "approved": m["approved"],
        "score": round(m["score"], 3) if m["score"] is not None else None,
        "interaction_types": m["types"],
        "pmids": m["pmids"][:MAX_PMIDS_PER_DRUG],
        "pmids_total": len(m["pmids"]),
        "sources": m["sources"],
        "chembl_id": _chembl_id(m["concept_ids"]),
        "evidence": "single source, no publication" if m.get("weak") else "curated or published",
    }


def _citations(items: List[Dict[str, Any]], genes: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [
        {"type": "url", "id": f"DGIdb:{g}", "title": f"DGIdb 5 drug-gene interactions for {g}",
         "url": GENE_URL.format(gene=g), "section": "DGIdb interaction"}
        for g in genes
    ]
    seen: Set[str] = set()
    for it in items:
        types = "/".join(it["interaction_types"]) or "interaction"
        for pmid in it["pmids"][:2]:
            if pmid in seen or len(seen) >= MAX_PMID_CITATIONS:
                continue
            seen.add(pmid)
            out.append({
                "type": "pmid", "id": pmid, "title": f"DGIdb evidence: {it['gene']}-{it['drug']} {types}",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "section": "DGIdb interaction publication",
            })
    return out


async def dgidb_interactions(genes: List[str], limit: int = MAX_INTERACTIONS) -> Any:
    """Approved and investigational drugs interacting with up to three genes, from DGIdb 5."""
    symbols = list(dict.fromkeys(g.strip().upper() for g in genes if g and _GENE_RE.match(g.strip().upper())))[:MAX_GENES]
    subject = ", ".join(symbols)
    if not symbols:
        return common.failure(TOOL_NAME, common.UpstreamError("no valid gene symbol", "bad_request"), subject)
    key = (tuple(symbols), int(limit))
    data = _CACHE.get(key)
    if data is None:
        try:
            raw = await graphql_post(ENDPOINT, QUERY, source="DGIdb", variables={"names": symbols})
        except Exception as exc:  # noqa: BLE001
            return common.failure(TOOL_NAME, exc, subject)
        nodes = ((raw.get("genes") or {}).get("nodes")) if isinstance(raw.get("genes"), dict) else None
        if not isinstance(nodes, list):
            return common.failure(TOOL_NAME, common.UpstreamError("DGIdb returned an unexpected response"), subject)
        by_name = {str(n.get("name") or "").upper(): n for n in nodes if isinstance(n, dict)}
        found = [g for g in symbols if g in by_name]
        missing = [g for g in symbols if g not in by_name]
        if not found:
            return common.failure(TOOL_NAME, common.UpstreamError(f"DGIdb has no gene record for {subject}", "not_found"), subject)
        per_gene: List[Dict[str, Any]] = []
        items: List[Dict[str, Any]] = []
        for g in found:
            node = by_name[g]
            merged = merge_interactions(node.get("interactions") or [])
            kept = [_item(g, m) for m in merged[:limit]]
            items.extend(kept)
            per_gene.append({
                "gene": g,
                "concept_id": node.get("conceptId"),
                "long_name": node.get("longName"),
                "drugs_total": len(merged),
                "approved_total": sum(1 for m in merged if m["approved"]),
                "shown": len(kept),
            })
        data = {
            "subject": subject,
            "genes": per_gene,
            "missing_genes": missing,
            "items": items,
            "limit": limit,
            "citation_records": _citations(items, found),
        }
        _CACHE.set(key, data)
    # The PMIDs and ChEMBL ids DGIdb returns are evidence a run can cite: record them as canonical ids too, so
    # the run manifest and evaluation citation checks see them (not only the verifier's allowed-identifier hook).
    rows = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    pmids = sorted({str(p) for it in rows for p in it.get("pmids") or []})
    chembl = sorted({str(it["chembl_id"]) for it in rows if it.get("chembl_id")})
    return common.ok(TOOL_NAME, dict(data), {"pmids": ",".join(pmids), "chembl_ids": ",".join(chembl)})


# -----------------------------------------------------------------------------
# Approved drugs acting on several genes at once (SynLethDB partner enrichment)
# -----------------------------------------------------------------------------

# Interaction types that mean the drug lowers the gene product's activity (the useful direction for a
# synthetic-lethal partner); other types are kept but ranked after these.
INHIBITORY_TYPES = {
    "inhibitor", "antagonist", "blocker", "negative modulator", "inverse agonist", "antibody", "suppressor",
    "allosteric modulator", "inhibitory allosteric modulator", "channel blocker", "gating inhibitor", "cleavage",
    "degrader", "antisense oligonucleotide", "antibody drug conjugate",
}
BATCH_GENES = 10  # genes per GraphQL request; larger batches are split and sent concurrently


def interaction_class(types: Sequence[str]) -> str:
    """'inhibits' (an inhibitor/antagonist-type interaction), 'other' (typed, another direction) or 'untyped'
    (DGIdb gives no type: often a biomarker or response association, not binding)."""
    ts = {str(t).lower() for t in types or []}
    if ts & INHIBITORY_TYPES:
        return "inhibits"
    return "other" if ts else "untyped"


def _approved_drug(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": m["drug"],
        "chembl_id": _chembl_id(m["concept_ids"]),
        "approved": bool(m["approved"]),
        "interaction_types": list(m["types"]),
        "interaction_class": interaction_class(m["types"]),
        "score": round(m["score"], 3) if m["score"] is not None else None,
        "sources": list(m["sources"])[:6],
        "pmids": list(m["pmids"])[:MAX_PMIDS_PER_DRUG],
    }


async def approved_drugs_by_gene(
    genes: Sequence[str], *, per_gene: int = 5, timeout_s: float = 12.0
) -> Dict[str, List[Dict[str, Any]]]:
    """{GENE: approved drugs DGIdb lists as interacting with it}, in batched GraphQL requests (BATCH_GENES genes
    each, sent concurrently). Only approved drugs with curated or published support are kept (a single-source,
    unpublished screening record is dropped), inhibitor/antagonist-type interactions first, then other typed ones,
    then untyped ones, each by DGIdb score. A gene DGIdb does not know maps to []. Raises common.UpstreamError
    only when every batch failed (a partial answer is returned as is)."""
    symbols = list(dict.fromkeys(g.strip().upper() for g in genes if g and _GENE_RE.match(g.strip().upper())))
    if not symbols:
        return {}
    key = ("approved_by_gene", tuple(symbols), per_gene)
    cached = _CACHE.get(key)
    if isinstance(cached, dict):
        return dict(cached)
    batches = [symbols[i:i + BATCH_GENES] for i in range(0, len(symbols), BATCH_GENES)]
    outs = await asyncio.gather(
        *(graphql_post(ENDPOINT, QUERY, source="DGIdb", variables={"names": b}, timeout_s=timeout_s) for b in batches),
        return_exceptions=True,
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[BaseException] = []
    for batch, raw in zip(batches, outs):
        if isinstance(raw, BaseException):
            errors.append(raw)
            continue
        nodes = ((raw.get("genes") or {}).get("nodes")) if isinstance(raw.get("genes"), dict) else None
        if not isinstance(nodes, list):
            errors.append(common.UpstreamError("DGIdb returned an unexpected response"))
            continue
        by_name = {str(n.get("name") or "").upper(): n for n in nodes if isinstance(n, dict)}
        for g in batch:
            merged = merge_interactions((by_name.get(g) or {}).get("interactions") or [])
            kept = [m for m in merged if m["approved"] and not m["weak"]]
            rank = {"inhibits": 0, "other": 1, "untyped": 2}
            kept.sort(key=lambda m: (rank[interaction_class(m["types"])], -(m["score"] or 0.0), m["drug"]))
            out[g] = [_approved_drug(m) for m in kept[:per_gene]]
    if errors and not out:
        first = errors[0]
        raise first if isinstance(first, common.UpstreamError) else common.UpstreamError(
            f"DGIdb lookup failed ({type(first).__name__})")
    if not errors:
        _CACHE.set(key, out)
    return out


def loss_of_function_genes(genes: Sequence[str], question: str = "") -> List[str]:
    """The genes that are tumour suppressors, or that the question describes as lost/deficient ("BRCA2 loss")."""
    try:
        from ..agentic.hypothesis import is_loss_of_function_target
    except Exception:  # noqa: BLE001 - the labelling is advisory; without the rule nothing is labelled
        return []
    return [g for g in genes if g and is_loss_of_function_target(g, question or "")]


def lost_gene_label(gene: str) -> str:
    return f"drug–gene associations (mostly biomarker/indirect), not drugs that act on {gene}"


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "DGIdb 5 drug-gene interactions for up to three genes: approved and investigational drugs, interaction "
            "types (inhibitor, antagonist, ...), DGIdb interaction score, supporting PMIDs and source databases."
        ),
        domain="synapta",
        adapter_type=AdapterType.DIRECT_GRAPHQL,
        input_schema=DgidbInput,
        canonical_id_types=["pmid", "chembl_id"],
        timeout_seconds=40.0,  # two attempts of up to 15 s each
        tags=["drug_gene_interaction", "dgidb", "druggability", "repurposing", "drugs", "target", "upstream:dgidb"],
        is_async=True,
    )


def query_genes(ctx: QueryContext, limit: int = MAX_GENES) -> List[str]:
    """The question's gene symbols, the primary gene first."""
    out: List[str] = []
    first = common.gene_symbol(ctx)
    if first:
        out.append(first)
    for g in ctx.genes:
        sym = (g.label or "").strip().upper()
        if sym and _GENE_RE.match(sym) and sym not in out:
            out.append(sym)
    return out[:limit]


class DgidbSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "DGIdb drug-gene interaction lookup"
    tool_name = TOOL_NAME
    artifact_name = "dgidb"
    label = "DGIdb"
    subject_kind = "gene"
    nothing_found = "drug-gene interactions"
    name_re = NAME_RE
    order = 23

    def intent(self, ctx: QueryContext) -> bool:
        text = common.query_text(ctx)
        return bool(INTENT_RE.search(text)) and not (_DRUG_DRUG_RE.search(text) and not NAME_RE.search(text))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        genes = query_genes(ctx)
        return {"genes": genes, "_subject": ", ".join(genes)} if genes else None

    async def run(self, ctx: QueryContext, registry: Any, state: Any) -> Any:
        result = await super().run(ctx, registry, state)
        data = common.data_of(result)
        if getattr(result, "success", False) and data:
            # A lost tumour suppressor's DGIdb "drugs" (olaparib for BRCA2) are biomarker associations: label them
            # on the result so the step summary, the evidence text and the card all say so.
            genes = [str(g.get("gene") or "") for g in data.get("genes") or [] if isinstance(g, dict)]
            result.data = {**data, "loss_of_function_genes": loss_of_function_genes(genes, common.query_text(ctx))}
        return result

    @staticmethod
    def _lost(d: Dict[str, Any]) -> Set[str]:
        """Genes labelled loss-of-function on the result; without the label (a direct tool call), the tumour
        suppressors among the result's genes."""
        if isinstance(d.get("loss_of_function_genes"), list):
            return {str(g).upper() for g in d["loss_of_function_genes"]}
        return {g.upper() for g in loss_of_function_genes([str(x.get("gene") or "") for x in d.get("genes") or [] if isinstance(x, dict)])}

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        lost = self._lost(d)
        heads = [
            f"{g['gene']}: {g['drugs_total']} {lost_gene_label(g['gene'])}" if str(g.get("gene") or "").upper() in lost
            else f"{g['gene']}: {g['drugs_total']} drugs ({g['approved_total']} approved)"
            for g in d.get("genes") or []
        ]
        top = [f"{it['drug']}{' (approved)' if it.get('approved') else ''}" for it in (d.get("items") or [])[:3]]
        detail = ("top: " + ", ".join(top)) if top else "no interactions listed"
        if d.get("missing_genes"):
            detail += f" · not in DGIdb: {', '.join(d['missing_genes'])}"
        return f"DGIdb: {'; '.join(heads)}", detail

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        items = [
            {"gene": it["gene"], "drug": it["drug"], "approved": "yes" if it.get("approved") else "no",
             "score": it.get("score"), "type": ", ".join(it.get("interaction_types") or []) or "n/a",
             "pmids": ", ".join(it.get("pmids") or []), "sources": ", ".join((it.get("sources") or [])[:4])}
            for it in data.get("items") or []
        ]
        out = {"genes": ", ".join(g["gene"] for g in data.get("genes") or []), "items": items,
               "missing_genes": ", ".join(data.get("missing_genes") or []) or None}
        lost = [g["gene"] for g in data.get("genes") or [] if str(g.get("gene") or "").upper() in self._lost(data)]
        if lost:
            out["note"] = " ".join(f"{g}: {lost_gene_label(g)}." for g in lost)
        return out

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        lines = [
            f'<dgidb_interactions source="DGIdb 5 (dgidb.org GraphQL)" genes="{common.attr(d.get("subject"))}" '
            'note="Drug-gene interactions aggregated by DGIdb from about 40 sources. approved=true means DGIdb lists '
            'the drug as approved by a regulator (not necessarily the FDA). Some interactions are biomarker or '
            'response associations (a mutation predicting resistance), not binding: use the interaction type when '
            'given and do not call an untyped interaction inhibition. score is DGIdb\'s evidence-weighted interaction '
            'score relative to the gene\'s other interactions, not affinity or efficacy. evidence="single source, no '
            'publication" marks a screening-database record: do not present such a drug as acting on the gene. Cite '
            'the PMIDs as given.">'
        ]
        items = d.get("items") or []
        lost = self._lost(d)
        for g in d.get("genes") or []:
            role = ""
            if str(g.get("gene") or "").upper() in lost:
                role = (
                    f' role="lost tumour suppressor / loss-of-function gene" '
                    f'label="{common.attr(lost_gene_label(g.get("gene")))}" note_lost="These drugs are associated with '
                    f'{common.attr(g.get("gene"))} status (biomarker, response or indirect associations); they do not act '
                    f'on {common.attr(g.get("gene"))}. Do not call them drugs targeting it; drugs for a lost gene act on its '
                    f'synthetic-lethal partners or downstream pathways."'
                )
            lines.append(
                f'<gene symbol="{common.attr(g.get("gene"))}" concept_id="{common.attr(g.get("concept_id"))}" '
                f'drugs_total="{common.attr(g.get("drugs_total"))}" approved_total="{common.attr(g.get("approved_total"))}" '
                f'shown="{common.attr(g.get("shown"))}" order="curated or published first, then approved, then by score"{role}>'
            )
            for it in (i for i in items if i.get("gene") == g.get("gene")):
                attrs = {
                    "drug": it.get("drug"),
                    "approved": "true" if it.get("approved") else "false",
                    "score": it.get("score"),
                    "types": ", ".join(it.get("interaction_types") or []) or "not stated",
                    "pmids": ", ".join(it.get("pmids") or []) or None,
                    "pmids_total": it.get("pmids_total") if (it.get("pmids_total") or 0) > len(it.get("pmids") or []) else None,
                    "sources": ", ".join(it.get("sources") or []) or None,
                    "chembl_id": it.get("chembl_id"),
                    "evidence": it.get("evidence"),
                }
                lines.append("<interaction " + " ".join(f'{k}="{common.attr(v)}"' for k, v in attrs.items() if v is not None) + "/>")
            lines.append("</gene>")
        for m in d.get("missing_genes") or []:
            lines.append(f'<gene_not_found symbol="{common.attr(m)}">DGIdb has no gene record for {common.text(m)}.</gene_not_found>')
        lines.append("</dgidb_interactions>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        items = common.data_of(result).get("items") or []
        return {
            "pmids": {p for it in items for p in it.get("pmids") or []},
            "chembl": {str(it["chembl_id"]) for it in items if it.get("chembl_id")},
        }

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is not None and result.success:
            missing = common.data_of(result).get("missing_genes") or []
            if missing:
                notes.append(f"DGIdb has no gene record for {', '.join(missing)}; do not state DGIdb interactions for it.")
        return notes


def clear_cache() -> None:
    _CACHE.clear()
