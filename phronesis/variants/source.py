"""EvidenceSource 'variant_intel': Synapta's ClinVar/CIViC/cBioPortal/gnomAD lookup for up to three named variants.

Each variant is looked up concurrently with one tool call; the combined result keeps the first variant's data at
the top level (the original single-variant shape) and the others under data['additional_variants'].
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..agents.query_context import QueryContext, SourceDecision
from ..sources import CitationRecord, EvidenceSource, SourceRunState
from .models import HGVS_C_PATTERN, PROTEIN_CHANGE_PATTERN
from .parse import GENE_ALIASES, extract_cancer_keyword, parse_variant_mentions
from .render import (
    compact_artifact,
    describe_variant_result,
    gnomad_qc_warning,
    render_variant_evidence,
    variant_datas,
    variant_label,
    variant_markdown_summary,
)

TOOL_NAME = "synapta_variant_intelligence"
SOURCE_KEY = "variant_intel"
_PLAN_KEY = "variant_intel_plan"
_GENE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")
MAX_VARIANTS = 3
_LOOKUP_KEYS = ("gene", "protein_change", "rsid", "hgvs_c", "clinvar_vcv")


def _data(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


def _plans_from_entities(ctx: QueryContext) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in ctx.variants:
        gene = GENE_ALIASES.get((e.gene or "").upper(), (e.gene or "").upper())
        change = e.protein_change if e.protein_change and re.fullmatch(PROTEIN_CHANGE_PATTERN, e.protein_change) else None
        rsid = str(e.ids.get("rsid") or "").lower() or None
        if gene and _GENE_RE.match(gene) and (change or rsid):
            out.append({"gene": gene, "protein_change": change, "rsid": rsid if rsid and re.fullmatch(r"rs\d{1,12}", rsid) else None})
    return out


def _plans_from_text(ctx: QueryContext) -> Tuple[List[Dict[str, Any]], List[str]]:
    mentions = parse_variant_mentions(ctx.clean_query or ctx.raw_query)
    out: List[Dict[str, Any]] = []
    for m in mentions:
        hgvs = m.hgvs or m.cdna_change
        hgvs = hgvs if hgvs and re.fullmatch(HGVS_C_PATTERN, hgvs) else None
        if m.gene and (m.protein_change or m.rsid or hgvs or m.clinvar_vcv):
            out.append({"gene": m.gene, "protein_change": m.protein_change, "rsid": m.rsid, "hgvs_c": hgvs, "clinvar_vcv": m.clinvar_vcv})
    return out, [m.display for m in mentions]


def _change(p: Dict[str, Any]) -> Optional[str]:
    return p.get("protein_change") or p.get("hgvs_c") or p.get("rsid") or p.get("clinvar_vcv")


def _merge_plans(plans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Distinct variants in order, at most MAX_VARIANTS. An rsID/VCV-only mention of a gene that already has a
    protein-level variant is left out: 'KRAS G12D (rs121913529)' names one variant, not two."""
    out: List[Dict[str, Any]] = []
    for raw in plans:
        p = {k: v for k, v in raw.items() if v}
        key = (p["gene"], str(_change(p) or "").upper())
        if any((o["gene"], str(_change(o) or "").upper()) == key for o in out):
            continue
        if not (p.get("protein_change") or p.get("hgvs_c")) and any(o["gene"] == p["gene"] for o in out):
            continue
        p["label"] = f"{p['gene']} {_change(p)}"
        out.append(p)
    return out[:MAX_VARIANTS]


def variant_plan(ctx: QueryContext) -> Optional[Dict[str, Any]]:
    """The variants to look up, cached on ctx. The top-level keys describe the first variant (gene, protein_change,
    rsid, hgvs_c, clinvar_vcv, label); 'variants' lists every planned variant (at most 3), 'labels' their labels,
    plus cancer_keyword and not_looked_up (mentions that were not planned)."""
    if _PLAN_KEY in ctx.extras:
        return ctx.extras[_PLAN_KEY] or None
    text_plans, mentions = _plans_from_text(ctx)
    variants = _merge_plans(_plans_from_entities(ctx) + text_plans)
    plan: Dict[str, Any] = {}
    if variants:
        keyword = extract_cancer_keyword(ctx.clean_query or ctx.raw_query)
        if not keyword:
            keyword = next((k for k in (extract_cancer_keyword(d.label) for d in ctx.diseases) if k), None)
        plan = {k: v for k, v in variants[0].items()}
        plan["cancer_keyword"] = keyword
        plan["variants"] = variants
        plan["labels"] = [v["label"] for v in variants]
        plan["not_looked_up"] = [m for m in mentions if m not in plan["labels"]]
    ctx.extras[_PLAN_KEY] = plan
    ctx.extras["variant_intel_mentions"] = mentions
    return plan or None


def _planned(plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not plan:
        return []
    return list(plan.get("variants") or [plan])


def combine_variant_results(results: List[Any], plans: List[Dict[str, Any]]) -> Any:
    """One ToolResult for several per-variant lookups: the first variant's data at the top level (the
    single-variant shape), the others under data['additional_variants'], citation records and ids merged."""
    from ..tools.registry import ErrorType, ToolResult

    datas: List[Dict[str, Any]] = []
    oks: List[bool] = []
    errors: List[str] = []
    error_type: Optional[Any] = None
    for res, p in zip(results, plans):
        data = None if isinstance(res, BaseException) else getattr(res, "data", None)
        ok = bool(not isinstance(res, BaseException) and getattr(res, "success", False))
        if not isinstance(data, dict):
            detail = f"{type(res).__name__}: {res}" if isinstance(res, BaseException) else str(getattr(res, "error", "") or "no data")
            data = {"gene": p.get("gene"), "protein_change": p.get("protein_change"), "rsid": p.get("rsid"),
                    "hgvs_c": p.get("hgvs_c"), "clinvar": [], "civic": None, "cbioportal": [], "gnomad": None,
                    "sources": [], "lookup_error": detail[:240]}
        if not ok:
            errors.append(f"{p.get('label') or variant_label(data)}: {getattr(res, 'error', None) or data.get('lookup_error') or 'no data'}")
            error_type = error_type or getattr(res, "error_type", None) or ErrorType.UPSTREAM_API_ERROR
        datas.append(data)
        oks.append(ok)

    top = dict(datas[0])
    top["additional_variants"] = datas[1:]
    records: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for d in datas:
        for rec in d.get("citation_records") or []:
            key = (str(rec.get("type")), str(rec.get("id")))
            if key not in seen:
                seen.add(key)
                records.append(rec)
    top["citation_records"] = records
    canonical: Dict[str, str] = {}
    for res in results:
        for k, v in (getattr(res, "canonical_ids", None) or {}).items():
            merged = [x for x in canonical.get(k, "").split(",") if x]
            merged += [x for x in str(v).split(",") if x and x not in merged]
            canonical[k] = ",".join(merged)
    elapsed = max([float(getattr(r, "execution_time_seconds", 0.0) or 0.0) for r in results if not isinstance(r, BaseException)] or [0.0])
    meta = {"adapter": "DIRECT_REST", "variants": [p.get("label") for p in plans],
            "sources": list((getattr(results[0], "metadata", None) or {}).get("sources") or []),
            # Release provenance of every per-variant lookup (DAT-010), in plan order.
            "source_releases": [(getattr(r, "metadata", None) or {}).get("source_releases") for r in results
                                if not isinstance(r, BaseException)]}
    if any(oks):
        return ToolResult(success=True, tool_name=TOOL_NAME, data=top, canonical_ids=canonical,
                          execution_time_seconds=elapsed, metadata=meta)
    return ToolResult(success=False, tool_name=TOOL_NAME, data=top, canonical_ids=canonical,
                      error="No variant database returned data: " + "; ".join(errors), error_type=error_type,
                      execution_time_seconds=elapsed, metadata=meta)


def _has_ok(data: Dict[str, Any]) -> bool:
    return any(s.get("status") == "ok" for s in data.get("sources") or [] if isinstance(s, dict))


class VariantIntelSource(EvidenceSource):
    key = SOURCE_KEY
    agent = "Synapta"
    action = "ClinVar/CIViC/cBioPortal/gnomAD variant lookup"
    required_tools = (TOOL_NAME,)
    order = 30

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        if ctx.biomodel_id and ctx.is_simulation_intent:
            return SourceDecision(run=False, reason="BioModel simulation requested")
        plan = variant_plan(ctx)
        if plan:
            labels = plan.get("labels") or [plan.get("label") or plan.get("gene")]
            return SourceDecision(run=True, reason=f"variant{'s' if len(labels) > 1 else ''} named: {', '.join(labels)}")
        if ctx.extras.get("variant_intel_mentions"):
            return SourceDecision(run=False, reason="variant named without a gene symbol")
        return SourceDecision(run=False, reason="no variant named")

    def running_title(self, ctx: QueryContext) -> str:
        plan = variant_plan(ctx) or {}
        labels = plan.get("labels") or [plan.get("label", "the variant")]
        return f"Looking up {', '.join(labels)} in ClinVar, CIViC, cBioPortal and gnomAD"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> Any:
        plan = variant_plan(ctx) or {}
        plans = _planned(plan)
        calls = []
        for p in plans or [plan]:
            kwargs = {k: p[k] for k in _LOOKUP_KEYS if p.get(k)}
            if plan.get("cancer_keyword"):
                kwargs["cancer_keyword"] = plan["cancer_keyword"]
            calls.append(kwargs)
        if len(calls) == 1:
            return await registry.execute(TOOL_NAME, **calls[0])
        results = await asyncio.gather(*(registry.execute(TOOL_NAME, **kw) for kw in calls), return_exceptions=True)
        return combine_variant_results(list(results), plans)

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        plan = variant_plan(ctx) or {}
        data = _data(result)
        datas = variant_datas(data)
        if len(datas) > 1:
            labels = [variant_label(d) for d in datas]
            parts = []
            for d, lbl in zip(datas, labels):
                _title, detail = describe_variant_result(d, lbl)
                parts.append(f"{lbl}: {detail or d.get('lookup_error') or 'no data'}")
            if not result.success:
                return f"Variant lookup for {', '.join(labels)} returned no data", " | ".join(parts)
            counts = [f"{lbl} ({sum(1 for s in d.get('sources') or [] if s.get('status') == 'ok')} of 4 databases)" for d, lbl in zip(datas, labels)]
            return f"Looked up {len(datas)} variants: {', '.join(counts)}", " | ".join(parts)
        label = plan.get("label") or data.get("gene") or "variant"
        if result.success:
            return describe_variant_result(data, label)
        if data.get("sources"):
            _title, detail = describe_variant_result(data, label)
            return f"Variant lookup for {label} returned no data", detail
        err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
        return f"Variant lookup for {label} failed", f"{err_type}: {str(result.error or '')[:240]}"

    def evidence_xml(self, result: Any) -> str:
        return "\n".join(x for x in (render_variant_evidence(d) for d in variant_datas(_data(result))) if x)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {"clinvar": set(), "rsid": set(), "civic": set()}
        for data in variant_datas(_data(result)):
            civic = data.get("civic") or {}
            out["clinvar"] |= {r["accession"] for r in data.get("clinvar") or [] if r.get("accession")}
            out["rsid"] |= {rs for r in data.get("clinvar") or [] for rs in r.get("rsids") or []}
            out["rsid"] |= set((data.get("gnomad") or {}).get("rsids") or [])
            out["civic"] |= {e["eid"] for e in civic.get("evidence") or []} | {a["aid"] for a in civic.get("assertions") or []}
        return out

    def citations(self, result: Any) -> List[CitationRecord]:
        out = []
        for rec in _data(result).get("citation_records") or []:
            try:
                out.append(CitationRecord(**rec))
            except Exception:  # noqa: BLE001
                continue
        return out

    def artifact(self, result: Any) -> Optional[Tuple[str, Any]]:
        data = _data(result)
        return (SOURCE_KEY, compact_artifact(data)) if data else None

    def fallback_markdown(self, result: Any) -> str:
        return "\n\n".join(variant_markdown_summary(d) for d in variant_datas(_data(result)))

    def _variant_notes(self, data: Dict[str, Any], label: str, ok: bool, multi: bool = False) -> List[str]:
        notes: List[str] = []
        if not ok:
            notes.append(
                f"Synapta's variant lookup for {label} returned no data; do not state ClinVar classifications, CIViC "
                f"evidence, cBioPortal prevalence or gnomAD frequencies for it."
            )
            return notes
        for s in data.get("sources") or []:
            name, status, detail = s.get("name"), s.get("status"), s.get("detail") or ""
            if status == "ok":
                continue
            if name == "gnomad" and status == "skipped" and "single-nucleotide" in detail:
                notes.append(f"gnomAD was not queried because {label} is not a single-nucleotide variant; no population allele frequency is available.")
            elif name == "gnomad":
                notes.append(f"gnomAD population frequency is unavailable for {label} ({detail or status}).")
            elif name == "clinvar":
                notes.append(f"ClinVar returned no classification for {label} ({detail or status}); do not state one.")
            elif name == "civic":
                notes.append(f"CIViC curated evidence is unavailable for {label} ({detail or status}).")
            elif name == "cbioportal":
                notes.append(f"cBioPortal prevalence is unavailable for {label} ({detail or status}).")
        if data.get("cbioportal") and not data.get("cancer_filter"):
            study = data["cbioportal"][0].get("study_name")
            scope = f" for {label}" if multi else ""
            notes.append(
                f"No cancer type was matched, so cBioPortal prevalence{scope} comes only from the pan-cancer {study} cohort; "
                f"do not present it as the frequency in a specific cancer."
            )
        g = data.get("gnomad")
        warning = gnomad_qc_warning(g) if isinstance(g, dict) else ""
        if warning:  # DAT-005: a QC-failed gnomAD call is not a clean population frequency
            notes.append(f"For {label}, {warning}.")
        return notes

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        plan = variant_plan(ctx)
        if result is None:
            if not plan and ctx.extras.get("variant_intel_mentions") and not (ctx.biomodel_id and ctx.is_simulation_intent):
                named = ", ".join(ctx.extras["variant_intel_mentions"][:3])
                return [f"A variant ({named}) was named without a gene symbol, so Synapta variant databases were not queried."]
            return []
        data = _data(result)
        datas = variant_datas(data)
        planned = _planned(plan)
        notes: List[str] = []
        if len(datas) > 1:
            for i, d in enumerate(datas):
                label = (planned[i].get("label") if i < len(planned) else None) or variant_label(d)
                notes.extend(self._variant_notes(d, label, _has_ok(d), multi=True))
        else:
            label = (plan or {}).get("label") or "the variant"
            notes.extend(self._variant_notes(data, label, bool(result.success)))
        # An rsID or VCV the lookup itself returned names the same variant ('KRAS G12D (rs121913529)').
        returned: Set[str] = set()
        for d in datas:
            returned |= {rs for r in d.get("clinvar") or [] for rs in r.get("rsids") or []}
            returned |= {str(r.get("accession") or "").split(".")[0] for r in d.get("clinvar") or []}
        looked = [p.get("label") for p in planned[: len(datas)] if p.get("label")] or [(plan or {}).get("label") or "the variant"]
        skipped = [p["label"] for p in planned[len(datas):] if p.get("label")]
        skipped += [m for m in (plan or {}).get("not_looked_up") or [] if m not in skipped]
        others = [o for o in skipped if not any(i and i in o.split() for i in returned)]
        if others:
            notes.append(
                f"Only {', '.join(looked)} {'was' if len(looked) == 1 else 'were'} looked up in the variant databases; "
                f"{', '.join(others[:4])} {'was' if len(others) == 1 else 'were'} not."
            )
        return notes

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        plan = variant_plan(ctx)
        if not plan:
            return None
        facts = []
        for p in _planned(plan):
            if p.get("protein_change"):
                fact = f"{p['gene']} p.{p['protein_change']}"
                facts.append(f"{fact} ({p['rsid']})" if p.get("rsid") else fact)
            else:
                facts.append(str(p.get("label") or p.get("gene")))
        return f"Variant{'s' if len(facts) > 1 else ''}: {', '.join(facts)}"
