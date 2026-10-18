"""
Pharos / TCRD target development level (TDL) for the genes of a druggability, target or lead question.

Reference: Kelleher K.J. et al., "Pharos 2023: an integrated resource for the understudied human proteome",
Nucleic Acids Res. 2023;51(D1):D1405-D1416, doi:10.1093/nar/gkac1033 (TDL definitions: Oprea T.I. et al., Nat. Rev.
Drug Discov. 2018;17:317-332, doi:10.1038/nrd.2018.14).

API: the Pharos GraphQL endpoint https://pharos-api.ncats.io/graphql (free, no key). One request asks for every
gene through aliases: {t0: target(q:{sym:"MYC"}){name sym uniprot tdl fam novelty} t1: ...}. An unknown symbol
answers null. The match is case-insensitive; a record whose symbol differs from the one asked for is treated as
not found.

Target development levels:
  Tclin  at least one approved drug acts on the target through a known mechanism of action
  Tchem  small molecules in ChEMBL/DrugCentral meet the family potency cut-offs (kinases <= 30 nM, GPCRs and
         nuclear receptors <= 100 nM, ion channels <= 10 uM, others <= 1 uM), but no approved drug with a known
         mechanism acts on it
  Tbio   no qualifying small molecule, but the biology is studied (GO experimental terms, OMIM phenotype, or a
         Jensen text-mining score >= 5)
  Tdark  little is known: none of the above
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext
from ..utils.http_limits import TTLCache
from . import common
from .dgidb import graphql_post, query_genes

TOOL_NAME = "pharos_target_development_level"
SOURCE_KEY = "pharos"
ENDPOINT = "https://pharos-api.ncats.io/graphql"
TARGET_URL = "https://pharos.nih.gov/targets/{gene}"
MAX_GENES = 3
FIELDS = "name sym uniprot tdl fam novelty"

TDL_MEANING = {
    "Tclin": "at least one approved drug acts on it through a known mechanism of action",
    "Tchem": "has ChEMBL/DrugCentral small molecules meeting the potency cut-offs, but no approved drug with a known "
             "mechanism of action on it",
    "Tbio": "studied biology (GO experimental terms, OMIM phenotype or literature) but no small molecule meeting the "
            "potency cut-offs",
    "Tdark": "understudied: little is known about it and no qualifying small molecule",
}

INTENT_RE = re.compile(
    r"\b(drugg?ab\w*|undrugg\w*|tractab\w*|targetab\w*|ligandab\w*|"
    r"target[\s-]+to[\s-]+(?:lead|hit|drug)|hit[\s-]+to[\s-]+lead|"
    r"leads?\s+(?:compounds?|optimi[sz]ation|discovery|series|generation|identification)|lead[\s-]+like|"
    r"(?:drug|therapeutic|druggable|novel|new|good|viable|promising|attractive|potential|candidate)\s+targets?|"
    r"target\s+(?:development\s+level|class|assessment|validation|prioriti[sz]ation|selection|tractability)|"
    r"chemical\s+(?:matter|probes?|starting\s+points?)|pharos|tcrd|t(?:clin|chem|bio|dark)|"
    r"target\s+development)\b",
    re.I,
)
NAME_RE = re.compile(r"\b(pharos|tcrd|target\s+development\s+level|t(?:clin|chem|bio|dark))\b", re.I)
_GENE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")

_CACHE = TTLCache(maxsize=256, ttl_s=24 * 3600)


class PharosInput(BaseModel):
    genes: List[str] = Field(..., min_length=1, max_length=MAX_GENES, description="HGNC symbols")


def build_query(symbols: List[str]) -> str:
    """Aliased target lookups; symbols are validated against _GENE_RE first, so inlining them is safe."""
    return "{" + " ".join(f't{i}:target(q:{{sym:"{s}"}}){{{FIELDS}}}' for i, s in enumerate(symbols)) + "}"


def tdl_phrase(tdl: Optional[str]) -> str:
    return TDL_MEANING.get(str(tdl or ""), "development level not stated")


async def pharos_targets(genes: List[str]) -> Any:
    """Pharos target development level, family and novelty for up to three genes."""
    symbols = list(dict.fromkeys(g.strip().upper() for g in genes if g and _GENE_RE.match(g.strip().upper())))[:MAX_GENES]
    subject = ", ".join(symbols)
    if not symbols:
        return common.failure(TOOL_NAME, common.UpstreamError("no valid gene symbol", "bad_request"), subject)
    key = tuple(symbols)
    data = _CACHE.get(key)
    if data is None:
        try:
            raw = await graphql_post(ENDPOINT, build_query(symbols), source="Pharos")
        except Exception as exc:  # noqa: BLE001
            return common.failure(TOOL_NAME, exc, subject)
        items: List[Dict[str, Any]] = []
        missing: List[str] = []
        for i, sym in enumerate(symbols):
            rec = raw.get(f"t{i}")
            if not isinstance(rec, dict) or str(rec.get("sym") or "").upper() != sym:
                missing.append(sym)
                continue
            novelty = rec.get("novelty")
            items.append({
                "gene": sym,
                "name": rec.get("name"),
                "tdl": rec.get("tdl"),
                "tdl_meaning": tdl_phrase(rec.get("tdl")),
                "family": rec.get("fam"),
                "novelty": float(novelty) if isinstance(novelty, (int, float)) else None,
                "uniprot": rec.get("uniprot"),
                "url": TARGET_URL.format(gene=sym),
            })
        if not items:
            return common.failure(TOOL_NAME, common.UpstreamError(f"Pharos has no target record for {subject}", "not_found"), subject)
        data = {
            "subject": subject,
            "items": items,
            "missing_genes": missing,
            "citation_records": [
                {"type": "url", "id": f"Pharos:{it['gene']}", "title": f"Pharos target {it['gene']} ({it['tdl']})",
                 "url": it["url"], "section": "Pharos/TCRD target development level"}
                for it in items
            ],
        }
        _CACHE.set(key, data)
    return common.ok(TOOL_NAME, dict(data))


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "Pharos/TCRD target development level (Tclin, Tchem, Tbio, Tdark), protein family and novelty for up to "
            "three human genes: whether a target has approved drugs, potent small molecules, or is understudied."
        ),
        domain="synapta",
        adapter_type=AdapterType.DIRECT_GRAPHQL,
        input_schema=PharosInput,
        canonical_id_types=["uniprot_id"],
        timeout_seconds=40.0,  # two attempts of up to 15 s each
        tags=["pharos", "tcrd", "druggability", "target_development_level", "tdl", "tractability", "upstream:pharos"],
        is_async=True,
    )


class PharosSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "Pharos target development level lookup"
    tool_name = TOOL_NAME
    artifact_name = "pharos"
    label = "Pharos"
    subject_kind = "gene"
    nothing_found = "target records"
    name_re = NAME_RE
    order = 24

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        genes = query_genes(ctx, MAX_GENES)
        return {"genes": genes, "_subject": ", ".join(genes)} if genes else None

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        title = "Pharos: " + "; ".join(f"{it['gene']} {it.get('tdl') or 'TDL unknown'}" for it in d.get("items") or [])
        detail = "; ".join(f"{it['gene']}: {it.get('tdl_meaning')}" for it in d.get("items") or [])
        if d.get("missing_genes"):
            detail += f" · not in Pharos: {', '.join(d['missing_genes'])}"
        return title, detail

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        items = [{k: it.get(k) for k in ("gene", "tdl", "family", "novelty", "tdl_meaning")} for it in data.get("items") or []]
        return {"items": items, "missing_genes": ", ".join(data.get("missing_genes") or []) or None}

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        lines = [
            '<pharos_targets source="Pharos/TCRD (pharos-api.ncats.io GraphQL)" note="Target development level (TDL): '
            'Tclin = approved drug with a known mechanism acts on the target; Tchem = small molecules meet potency '
            'cut-offs but no approved drug; Tbio = studied biology, no qualifying small molecule; Tdark = understudied. '
            'novelty is the TIN-X novelty score (lower = more studied). Quote the TDL with Pharos as the source.">'
        ]
        for it in d.get("items") or []:
            attrs = {k: it.get(k) for k in ("gene", "name", "tdl", "family", "novelty", "uniprot")}
            if isinstance(attrs["novelty"], float):
                attrs["novelty"] = f"{attrs['novelty']:.8f}".rstrip("0").rstrip(".")
            lines.append(
                "<target " + " ".join(f'{k}="{common.attr(v)}"' for k, v in attrs.items() if v is not None) + ">"
                + common.text(f"{it['gene']}: Pharos TDL {it.get('tdl')} ({it.get('tdl_meaning')})") + "</target>"
            )
        for m in d.get("missing_genes") or []:
            lines.append(f'<target_not_found gene="{common.attr(m)}">Pharos has no target record for {common.text(m)}.</target_not_found>')
        lines.append("</pharos_targets>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        return {"uniprot": {str(it["uniprot"]) for it in common.data_of(result).get("items") or [] if it.get("uniprot")}}

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is not None and result.success:
            missing = common.data_of(result).get("missing_genes") or []
            if missing:
                notes.append(f"Pharos has no target record for {', '.join(missing)}; do not state a Pharos TDL for it.")
        return notes


def clear_cache() -> None:
    _CACHE.clear()
