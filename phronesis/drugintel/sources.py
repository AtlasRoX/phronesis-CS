"""
The 'chembl' (Synapta) and 'openfda' (Lithoscribe) EvidenceSources. Both share one chembl_resolve_drugs
call per inquiry through SourceRunState.once('drug_resolve', ...).

When a question asks about safety but names no drug ("... repurposed drugs ... real-world safety data"),
openfda queries FAERS and the label for up to MAX_SAFETY_CANDIDATES candidate drugs taken from evidence produced
earlier in the same run: the approved drugs ChEMBL lists for the target, else the top approved PrimeKG
repurposing candidates. It reads those results through SourceRunState.source_result (both sources have a lower
`order`), so nothing runs twice and the orchestrator is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..agents.query_context import QueryContext, SourceDecision
from ..sources import CitationRecord, EvidenceSource, SourceRunState
from ..sources.names import labels as source_labels
from ..tools.registry import ErrorType, ToolResult
from .drugsfda import COVERAGE_NOTE, approval_phrase
from .engine import format_phase
from .names import (
    CHEMBL_ID_RE,
    drug_display_names,
    drug_mentions_for,
    has_candidate_safety_intent,
    has_degrader_intent,
    has_drug_intent,
    has_ehr_intent,
    has_label_intent,
    has_regulatory_intent,
    has_safety_intent,
    normalize_target_symbol,
    query_alleles,
    split_mentions,
)
from .render import (
    EHR_NOTE,
    approved_failure_phrase,
    approved_lookup_failed,
    approved_unknown_text,
    bioactive_summary,
    chembl_evidence_xml,
    chembl_fallback_markdown,
    faers_alias,
    join_names,
    label_product,
    safety_entries,
    safety_evidence_xml,
    safety_fallback_markdown,
)

RESOLVE_TOOL = "chembl_resolve_drugs"
PROFILE_TOOL = "chembl_drug_profile"
SAFETY_TOOL = "openfda_drug_safety"

# Candidate drugs for a safety question that names no drug.
MAX_SAFETY_CANDIDATES = 3
CANDIDATE_SOURCES = ("chembl", "primekg")  # in order of preference; both have a lower `order` than openfda
CANDIDATE_WAIT_S = 110.0  # how long openfda waits for them (ChEMBL resolve + profile tool timeouts are 40 + 60 s)
NO_CANDIDATE_REASON = "safety asked but no drug candidate was found"
_SOURCE_NAMES = source_labels("public", CANDIDATE_SOURCES)  # {"chembl": "ChEMBL", "primekg": "PrimeKG"}


def _query_text(ctx: QueryContext) -> str:
    return ctx.clean_query or ctx.raw_query or ""


def _candidate_purpose(text: str) -> Optional[str]:
    """Why openFDA looks up candidate drugs when none is named: "safety", "regulatory" or None (it does not)."""
    if has_candidate_safety_intent(text):
        return "safety"
    if has_regulatory_intent(text):
        return "regulatory"
    return None


def _no_candidate_reason(purpose: Optional[str]) -> str:
    if purpose == "regulatory":
        return "regulatory status asked but no drug candidate was found"
    return NO_CANDIDATE_REASON


def _ehr_note(purpose: str) -> Dict[str, str]:
    """The FAERS-is-not-EHR note belongs to safety questions only."""
    return {"ehr_note": EHR_NOTE} if purpose == "safety" else {}


def _prefer_allele(candidates: List[Dict[str, Any]], alleles: List[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Candidates whose ChEMBL mechanism names the question's variant ("KRAS G12C inhibitor") first, each marked
    allele_specific True/False, plus a note when none names it (target-level candidates, not variant-specific)."""
    if not alleles:
        return candidates, None
    out = []
    for c in candidates:
        text = f"{c.get('basis') or ''} {c.get('mechanism_of_action') or ''}".upper()
        out.append({**c, "allele_specific": any(a in text for a in alleles)})
    out.sort(key=lambda c: not c["allele_specific"])
    if any(c["allele_specific"] for c in out):
        return out, None
    return out, (f"no candidate's ChEMBL mechanism names {', '.join(alleles)}; these are drugs acting on the target, "
                 f"not necessarily {'/'.join(alleles)}-selective")


def _target_symbol(ctx: QueryContext) -> Optional[str]:
    return normalize_target_symbol(ctx.primary_symbol)


async def shared_resolve(ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
    """One ChEMBL resolve per inquiry, awaited by both drug sources."""
    names, chembl_ids = split_mentions(drug_mentions_for(ctx))
    target_symbol = _target_symbol(ctx)
    return await state.once(
        "drug_resolve",
        lambda: registry.execute(RESOLVE_TOOL, names=names, chembl_ids=chembl_ids, target_symbol=target_symbol),
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _queried_as(d: Dict[str, Any]) -> str:
    """The name the drug was asked about; the user's words or the preferred name when the planner passed its
    ChEMBL id."""
    query = str(d.get("query_name") or "")
    if CHEMBL_ID_RE.fullmatch(query):
        if d.get("mention"):
            return str(d["mention"])
        if d.get("pref_name"):
            return str(d["pref_name"]).lower()
    return query or str(d.get("chembl_id") or "")


def _mention_for(query_name: str, display: Dict[str, str]) -> Optional[str]:
    """The user's words for a drug resolved from query_name (None when they only gave a ChEMBL id)."""
    if query_name in display:
        return display[query_name]
    return None if CHEMBL_ID_RE.fullmatch(query_name or "") else (query_name or None)


def _citation_records(data: Any, agent: str, tool: str) -> List[CitationRecord]:
    out: List[CitationRecord] = []
    for rec in (data or {}).get("citation_records") or [] if isinstance(data, dict) else []:
        try:
            out.append(CitationRecord(**rec, source_agent=agent, source_tool=tool))
        except Exception:  # noqa: BLE001
            continue
    return out


def _compact(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if k != "citation_records"}


def _target_failure_phrase(data: Dict[str, Any]) -> str:
    return "timed out" if data.get("target_lookup_error_kind") == "timeout" else "failed"


def _error_detail(result: ToolResult) -> str:
    err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
    return f"{err_type}: {str(result.error or '')[:240]}"


class ChemblSource(EvidenceSource):
    key = "chembl"
    agent = "Synapta"
    action = "ChEMBL Drug Mechanism & Bioactivity"
    required_tools = (RESOLVE_TOOL, PROFILE_TOOL)
    order = 20

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        mentions = drug_mentions_for(ctx)
        if mentions:
            return SourceDecision(run=True, reason=f"drug named: {', '.join(mentions)}")
        symbol = _target_symbol(ctx)
        if symbol and has_drug_intent(_query_text(ctx)):
            return SourceDecision(run=True, reason=f"drug question about {symbol}")
        if symbol and has_regulatory_intent(_query_text(ctx)):
            # "FDA-approved indications for KRAS G12C inhibitors": the approved drugs acting on the target are the
            # candidates openFDA checks for approvals and labels.
            return SourceDecision(run=True, reason=f"regulatory question about drugs acting on {symbol}")
        return SourceDecision(run=False, reason="no drug named and no drug question about a target")

    def running_title(self, ctx: QueryContext) -> str:
        mentions = drug_mentions_for(ctx)
        if mentions:
            return f"Looking up {join_names(mentions)} in ChEMBL"
        return f"Looking up approved drugs for {_target_symbol(ctx)} in ChEMBL"

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        mentions = drug_mentions_for(ctx)
        if mentions:
            return f"Drugs: {', '.join(mentions)}"
        return f"Drug lookup: approved drugs for {_target_symbol(ctx)}"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        resolved = await shared_resolve(ctx, registry, state)
        if not resolved.success or not isinstance(resolved.data, dict):
            return resolved
        rdata = resolved.data
        drugs = rdata.get("drugs") or []
        target = rdata.get("target") or None
        carry = {
            "unresolved_names": list(rdata.get("unresolved_names") or []),
            "invalid_chembl_ids": list(rdata.get("invalid_chembl_ids") or []),
            "target_lookup": rdata.get("target_lookup"),
            "target_lookup_error": rdata.get("target_lookup_error"),
            "target_lookup_error_kind": rdata.get("target_lookup_error_kind"),
            "target_symbol_query": _target_symbol(ctx),
            "lookup_errors": list(rdata.get("lookup_errors") or []),
        }
        if not drugs and not target:
            return ToolResult(
                success=True,
                tool_name=PROFILE_TOOL,
                data={"chembl_release": rdata.get("chembl_release"), "target": None, "drugs": [], "target_approved_drugs": [], **carry},
                canonical_ids={},
                metadata={"chembl_release": rdata.get("chembl_release"), "source": "ChEMBL REST"},
            )
        display = drug_display_names(ctx)
        mentions = {d["chembl_id"]: m for d in drugs if (m := _mention_for(str(d.get("query_name") or ""), display))}
        profile = await registry.execute(
            PROFILE_TOOL,
            parent_chembl_ids=[d["chembl_id"] for d in drugs],
            target_chembl_id=(target or {}).get("target_chembl_id"),
            target_symbol=(target or {}).get("symbol_query"),
            target_pref_name=(target or {}).get("pref_name"),
            query_names={d["chembl_id"]: d["query_name"] for d in drugs},
            forms={d["chembl_id"]: list(d.get("forms") or []) for d in drugs},
            matched_via={d["chembl_id"]: d["matched_via"] for d in drugs},
            mentions=mentions,
        )
        if profile.success and isinstance(profile.data, dict):
            pdata = profile.data
            for d in pdata.get("drugs") or []:
                if not d.get("mention") and mentions.get(d.get("chembl_id")):
                    d["mention"] = mentions[d["chembl_id"]]
            if has_degrader_intent(_query_text(ctx)):
                pdata["degrader_question"] = True
            pdata["unresolved_names"] = list(dict.fromkeys([*carry["unresolved_names"], *(pdata.get("unresolved_names") or [])]))
            pdata["invalid_chembl_ids"] = carry["invalid_chembl_ids"]
            pdata["target_lookup"] = carry["target_lookup"]
            pdata["target_lookup_error"] = carry["target_lookup_error"]
            pdata["target_lookup_error_kind"] = carry["target_lookup_error_kind"]
            pdata["target_symbol_query"] = carry["target_symbol_query"]
            pdata["lookup_errors"] = [*carry["lookup_errors"], *(pdata.get("lookup_errors") or [])]
            by_id = {d["chembl_id"]: d for d in drugs}
            for d in pdata.get("drugs") or []:
                if not d.get("trade_names") and by_id.get(d.get("chembl_id"), {}).get("trade_names"):
                    d["trade_names"] = by_id[d["chembl_id"]]["trade_names"]
        return profile

    def describe(self, result: ToolResult, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return "ChEMBL lookup failed", _error_detail(result)
        data = result.data or {}
        drugs = data.get("drugs") or []
        target = data.get("target") or None
        symbol = (target or {}).get("symbol_query") or (target or {}).get("pref_name")
        unresolved = data.get("unresolved_names") or []
        invalid = data.get("invalid_chembl_ids") or []
        missing = [*unresolved, *invalid]
        if drugs:
            title = "; ".join(
                f"Resolved {_queried_as(d)} to {d.get('chembl_id')} (max phase {format_phase(d.get('max_phase'))})"
                for d in drugs
            )
            details: List[str] = []
            for d in drugs:
                part = _plural(len(d.get("mechanisms") or []), "mechanism")
                if target:
                    part += f" · {len(d.get('bioactivities') or [])} IC50/Ki/Kd records vs {symbol}"
                details.append(part if len(drugs) == 1 else f"{d.get('pref_name') or d.get('chembl_id')}: {part}")
            if missing:
                details.append(f"no exact ChEMBL match: {', '.join(missing)}")
            return title, " · ".join(details)
        if target:
            approved = data.get("target_approved_drugs") or []
            if approved_lookup_failed(data):
                # A timeout or HTTP error is "unknown", never "Found 0 approved drugs".
                title = f"ChEMBL lookup for {symbol} {approved_failure_phrase(data)}; approved drugs unknown"
            else:
                title = f"Found {_plural(len(approved), 'approved drug')} acting on {symbol} ({target.get('target_chembl_id')})"
            names = ", ".join(a.get("pref_name") or f"{a.get('chembl_id')} (name not retrieved)" for a in approved[:4])
            more = f" +{len(approved) - 4} more" if len(approved) > 4 else ""
            compounds = data.get("target_bioactive_compounds") or []
            if compounds:
                distinct = data.get("target_bioactive_distinct") or len(compounds)
                exact = (data.get("target_activity_records_total") or 0) <= (data.get("target_activity_records_fetched") or 0)
                title += f"; {'' if exact else 'at least '}{_plural(int(distinct), 'compound')} with measured activity"
                top = ", ".join(c.get("pref_name") or c.get("chembl_id") for c in compounds[:3])
                detail = f"{names}{more}" if names else ""
                return title, " · ".join(x for x in (detail, f"top compounds: {top}") if x)
            return title, f"{names}{more}" if names else ""
        if data.get("target_lookup") == "error" and data.get("target_symbol_query"):
            # The target could not be looked up: approved drugs are unknown, not absent.
            return (f"ChEMBL target lookup for {data['target_symbol_query']} {_target_failure_phrase(data)}; "
                    "approved drugs unknown"), str(data.get("target_lookup_error") or "")
        if missing:
            return f"No exact ChEMBL match for {join_names(missing)}", ""
        return "No ChEMBL records found", ""

    def evidence_xml(self, result: ToolResult) -> str:
        return chembl_evidence_xml(result.data or {})

    def allowed_identifiers(self, result: ToolResult) -> Dict[str, Set[str]]:
        text = json.dumps(_compact(result.data or {}), default=str)
        return {"chembl": {m.upper() for m in CHEMBL_ID_RE.findall(text)}}

    def citations(self, result: ToolResult) -> List[CitationRecord]:
        return _citation_records(result.data, self.agent, PROFILE_TOOL)

    def artifact(self, result: ToolResult) -> Optional[Tuple[str, Any]]:
        data = result.data or {}
        if not (data.get("drugs") or data.get("target_approved_drugs") or data.get("target_bioactive_compounds")
                or data.get("unresolved_names") or data.get("invalid_chembl_ids") or approved_lookup_failed(data)):
            return None
        return "drug_intel", {"chembl": _compact(data)}

    def fallback_markdown(self, result: ToolResult) -> str:
        return chembl_fallback_markdown(result.data or {})

    def scope_notes(self, ctx: QueryContext, result: Optional[ToolResult]) -> List[str]:
        if result is None:
            return []
        if not result.success:
            return [f"Synapta's ChEMBL lookup failed ({_error_detail(result)}); no drug mechanism or potency data is available."]
        data = result.data or {}
        notes: List[str] = []
        for name in data.get("unresolved_names") or []:
            notes.append(f"ChEMBL has no exact match for '{name}', so no ChEMBL data exists for it in this answer.")
        for cid in data.get("invalid_chembl_ids") or []:
            notes.append(f"{cid} is not a valid ChEMBL molecule ID (ChEMBL returned 404).")
        target = data.get("target") or None
        symbol = data.get("target_symbol_query")
        if data.get("target_lookup") == "not_found" and symbol:
            notes.append(f"ChEMBL has no human single-protein target for {symbol}; no bioactivity values were retrieved.")
        elif data.get("target_lookup") == "error" and symbol:
            notes.append(
                f"The ChEMBL target lookup for {symbol} {_target_failure_phrase(data)}; no bioactivity values or approved "
                f"drugs were retrieved, so whether any approved drug acts on {symbol} is unknown (not 0)."
            )
        errors = data.get("lookup_errors") or []
        for err in errors:
            notes.append(
                f"The ChEMBL lookup '{err.get('query')}' failed ({err.get('error')}); its data is missing from this answer, "
                "which is not evidence that no such data exists."
            )
        failed_queries = " ".join(str(e.get("query") or "") for e in errors)
        drugs = data.get("drugs") or []
        if target and not drugs and "compounds with measured activity" not in failed_queries:
            # Never let "no approved drug" read as "no compounds": the two are reported separately.
            notes.append(bioactive_summary(data) + " Do not say ChEMBL lists no compounds for this target unless both counts are 0.")
        if target and approved_lookup_failed(data):
            notes.append(
                f"{approved_unknown_text(data)}. Say that the approved drugs acting on "
                f"{symbol or target.get('pref_name') or target.get('target_chembl_id')} are unknown because the lookup "
                "failed; never report them as 0 or none."
            )
        if data.get("degrader_question") or has_degrader_intent(_query_text(ctx)):
            notes.append(
                "ChEMBL does not label compounds as PROTACs or degraders; the ChEMBL compounds listed are ranked by "
                "measured activity only. Do not call any of them a PROTAC or degrader unless another retrieved source says so."
            )
        unnamed = [str(a.get("chembl_id")) for a in data.get("target_approved_drugs") or [] if not a.get("pref_name")]
        if unnamed:
            notes.append(
                f"ChEMBL did not return the names of the approved drug(s) {', '.join(unnamed)}; refer to them by ChEMBL ID "
                "only. Their mechanism text (e.g. 'GTPase KRas inhibitor') is not a drug name: never write it as one."
            )
        if drugs and not target:
            notes.append("No target was resolved, so no ChEMBL IC50/Ki/Kd values were retrieved; do not quote potency values.")
        for d in drugs:
            if f"activities for {d.get('chembl_id')}" in failed_queries:
                continue
            if target and not d.get("bioactivities"):
                notes.append(
                    f"ChEMBL has no IC50/Ki/Kd records with a pChEMBL value for {d.get('pref_name') or d.get('chembl_id')} "
                    f"against {target.get('pref_name') or target.get('target_chembl_id')}."
                )
        if drugs:
            notes.append(
                "ChEMBL max phase 4 means approved by at least one regulator somewhere; it is not evidence of FDA approval."
            )
        return notes


def _ingredient(name: Any) -> str:
    """An openFDA-safe active-ingredient string (letters, digits, spaces, hyphens), lower case."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 \-]", " ", str(name or ""))).strip().lower()


def unnamed_approved_ids(res: Optional[ToolResult], limit: int = MAX_SAFETY_CANDIDATES) -> List[str]:
    """ChEMBL ids of the first `limit` approved drugs acting on the target whose names the profile lacks."""
    if res is None or not res.success or not isinstance(res.data, dict):
        return []
    approved = res.data.get("target_approved_drugs") or []
    return [str(a["chembl_id"]) for a in approved[:limit] if a.get("chembl_id") and not a.get("pref_name")]


def _chembl_candidates(
    res: Optional[ToolResult], names: Optional[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """(candidates, basis, why none) from the ChEMBL source's result: approved drugs acting on the target.
    names: preferred names looked up separately for approved drugs the profile returned without one."""
    if res is None:
        return [], None, None
    if not res.success or not isinstance(res.data, dict):
        return [], None, f"the ChEMBL lookup failed ({_error_detail(res)})"
    data = res.data
    names = names or {}
    target = data.get("target") or {}
    if not target:
        symbol = data.get("target_symbol_query") or "the target"
        if data.get("target_lookup") == "error":
            return [], None, f"the ChEMBL target lookup for {symbol} {_target_failure_phrase(data)}"
        return [], None, f"ChEMBL has no target for {symbol}"
    label = target.get("symbol_query") or target.get("pref_name") or target.get("target_chembl_id")
    if approved_lookup_failed(data):
        return [], None, f"the ChEMBL lookup of approved drugs acting on {label} {approved_failure_phrase(data)}"
    out: List[Dict[str, Any]] = []
    unnamed: List[str] = []
    for a in data.get("target_approved_drugs") or []:
        name = a.get("pref_name") or names.get(str(a.get("chembl_id")))
        ing = _ingredient(name)
        if len(ing) < 2:
            unnamed.append(str(a.get("chembl_id")))
            continue
        if any(c["ingredient"] == ing for c in out):
            continue
        moa = f": {a['mechanism_of_action']}" if a.get("mechanism_of_action") else ""
        out.append({"drug": ing, "ingredient": ing, "chembl_id": a.get("chembl_id"), "origin": "chembl",
                    "mechanism_of_action": a.get("mechanism_of_action"),
                    "basis": f"approved drug acting on {label} in ChEMBL (max phase 4{moa})"})
    if not out:
        if unnamed:
            return [], None, (f"the names of the approved drugs acting on {label} ({', '.join(unnamed[:5])}) could not "
                              "be retrieved from ChEMBL")
        return [], None, f"ChEMBL lists no approved drug acting on {label}"
    return out, f"ChEMBL approved drugs (max phase 4) acting on {label} ({target.get('target_chembl_id')})", None


# PrimeKG candidate tags that mark a drug as no anti-disease therapy for this question (symptom control, or an
# agent for a different tumour type); such candidates are not queried in openFDA.
_NON_THERAPEUTIC_TAG_RE = re.compile(r"supportive care|neuroendocrine|non[- ]therapeutic|diagnostic|nutrient", re.IGNORECASE)


def _approved(c: Dict[str, Any]) -> bool:
    groups = c.get("approval_groups") or []
    groups = [groups] if isinstance(groups, str) else groups
    return "approved" in {str(g).lower() for g in groups}


def _primekg_candidates(
    res: Optional[ToolResult],
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str], List[Dict[str, Any]]]:
    """(candidates, basis, why none, skipped) from the PrimeKG source's result: approved drugs, best first.

    Approved drugs from the mutated gene's pathway list (pathway_candidates: MEK, BRAF, mTOR inhibitors for KRAS)
    come first, then the ranked candidates tagged with that pathway, then the other ranked candidates. Candidates
    tagged supportive care or neuroendocrine-tumour agent are skipped and returned in `skipped`."""
    if res is None:
        return [], None, None, []
    if not res.success or not isinstance(res.data, dict):
        return [], None, "the PrimeKG analysis failed", []
    pathway: List[Dict[str, Any]] = []
    tagged: List[Dict[str, Any]] = []
    ranked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    diseases: List[str] = []
    seen: Set[str] = set()

    def add(bucket: List[Dict[str, Any]], c: Dict[str, Any], basis: str, disease: str) -> None:
        ing = _ingredient(c.get("name"))
        if len(ing) < 2 or ing in seen:
            return
        seen.add(ing)
        bad = [str(t) for t in c.get("tags") or [] if _NON_THERAPEUTIC_TAG_RE.search(str(t))]
        if bad:
            skipped.append({"drug": ing, "primekg_id": c.get("id"), "reason": f"tagged {', '.join(bad)}"})
            return
        bucket.append({"drug": ing, "ingredient": ing, "chembl_id": None, "primekg_id": c.get("id"), "origin": "primekg",
                       "basis": basis})
        if disease not in diseases:
            diseases.append(disease)

    analyses = [a for a in res.data.get("analyses") or []
                if isinstance(a, dict) and a.get("operation") == "repurposing" and not a.get("error")]
    for a in analyses:
        result = a.get("result") or {}
        disease = str((result.get("disease") or {}).get("name") or "the disease")
        genes = "/".join(result.get("mutant_genes") or []) or "mutated-gene"
        for c in result.get("pathway_candidates") or []:
            if _approved(c):
                targets = ", ".join((c.get("pathway_targets") or [])[:3])
                add(pathway, c, f"PrimeKG {genes}-pathway drug for {disease}"
                    f"{f' (targets {targets})' if targets else ''}; approved", disease)
    for a in analyses:
        result = a.get("result") or {}
        disease = str((result.get("disease") or {}).get("name") or "the disease")
        for rank, c in enumerate(result.get("candidates") or [], start=1):
            if not _approved(c):
                continue
            on_pathway = any("pathway" in str(t).lower() for t in c.get("tags") or [])
            add(tagged if on_pathway else ranked, c,
                f"PrimeKG repurposing candidate #{rank} for {disease} (score {c.get('score')}; approved)", disease)
    out = [*pathway, *tagged, *ranked]
    if not out:
        why = "PrimeKG ranked no approved repurposing candidate"
        if skipped:
            why += f" other than supportive-care or neuroendocrine-tumour agents ({', '.join(x['drug'] for x in skipped[:4])})"
        return [], None, why, skipped
    kind = ("approved PrimeKG pathway drugs and repurposing candidates" if pathway
            else "top approved PrimeKG repurposing candidates")
    return out, f"{kind} for {join_names(diseases)}", None, skipped


async def _names_for_ids(registry: Any, chembl_ids: List[str]) -> Dict[str, str]:
    """{ChEMBL id: preferred name} through chembl_resolve_drugs (molecule/{id}.json, retried); {} on failure."""
    ids = list(dict.fromkeys(chembl_ids))[:3]  # the resolve tool takes up to three ids
    if not ids:
        return {}
    try:
        res = await registry.execute(RESOLVE_TOOL, names=[], chembl_ids=ids, target_symbol=None)
    except Exception:  # noqa: BLE001 - a failed name lookup only means falling back to other candidates
        return {}
    if not res.success or not isinstance(res.data, dict):
        return {}
    out: Dict[str, str] = {}
    for d in res.data.get("drugs") or []:
        name = d.get("pref_name")
        if not name:
            continue
        for cid in (d.get("query_name"), d.get("chembl_id"), *(d.get("forms") or [])):
            if cid in ids:
                out[str(cid)] = str(name)
    return out


def approval_scope_notes(entry: Dict[str, Any]) -> List[str]:
    """What the answer may say about one drug's FDA approval, from its Drugs@FDA lookup."""
    approval = entry.get("approval") or {}
    name = entry.get("ingredient") or approval.get("ingredient")
    status = approval.get("status")
    if status == "approved":
        n = approval.get("efficacy_supplements_total") or 0
        return [
            f"Drugs@FDA: {name} is {approval_phrase(approval)}; {n} approved efficacy supplement"
            f"{'' if n == 1 else 's'} (new or changed indications, not named by Drugs@FDA). Quote the application "
            "number and dates exactly."
        ]
    if status == "combination_only":
        return [f"Drugs@FDA: {name} is {approval_phrase(approval)}. Do not call {name} FDA-approved as a single agent."]
    if status == "not_found":
        return [
            f"Drugs@FDA lists no approved NDA or BLA for {name}: do not call it FDA-approved (it may be investigational "
            f"or approved only outside the US). {COVERAGE_NOTE}"
        ]
    if status == "error":
        return [
            f"The Drugs@FDA lookup for {name} failed ({approval.get('error')}), so its FDA approval status is unknown "
            "from this source; do not state it is or is not FDA-approved on that basis."
        ]
    return []


def chembl_unavailable(result: Optional[ToolResult]) -> bool:
    """Whether a failed ChEMBL call failed upstream (5xx, timeout, open breaker), not on the request itself."""
    if result is None or result.success:
        return False
    et = str(getattr(result.error_type, "value", result.error_type) or "")
    return et in ("UPSTREAM_API_ERROR", "TIMEOUT")


def _by_name_queries(
    ctx: QueryContext, lookup_errors: List[Dict[str, Any]], taken: Set[str]
) -> Tuple[List[Tuple[Dict[str, Any], str]], List[Dict[str, Any]]]:
    """(openFDA queries, skipped) for drugs whose ChEMBL lookup failed upstream.

    openFDA (FAERS, label, Drugs@FDA) is searched by name, so each such drug is queried by the researcher's own
    name for it (the safety tool also retries the US adopted name: paracetamol -> acetaminophen). A drug given
    only as a ChEMBL id has no name to search by and is skipped. Drugs ChEMBL did answer for keep the max-phase
    rule (unapproved drugs are not queried); these could not be checked."""
    display = drug_display_names(ctx)
    queries: List[Tuple[Dict[str, Any], str]] = []
    skipped: List[Dict[str, Any]] = []
    seen = set(taken)
    for err in lookup_errors:
        query = str(err.get("query") or "")
        is_id = bool(CHEMBL_ID_RE.fullmatch(query))
        name = display.get(query) or (None if is_id else query)
        ing = _ingredient(name)
        if CHEMBL_ID_RE.fullmatch(ing):
            ing = ""
        if len(ing) < 2:
            skipped.append({"drug": query, "chembl_id": query.upper() if is_id else None, "max_phase": None,
                            "reason": "the ChEMBL lookup failed and there is no drug name to search openFDA by"})
            continue
        if ing in seen:
            continue
        seen.add(ing)
        record = {"chembl_id": query.upper() if is_id else None, "query_name": name, "matched_by_name": True,
                  "chembl_error": str(err.get("error") or "")[:240]}
        queries.append((record, ing))
    return queries, skipped


class OpenFdaSource(EvidenceSource):
    key = "openfda"
    agent = "Lithoscribe"
    action = "openFDA FAERS & Label Lookup"
    required_tools = (RESOLVE_TOOL, SAFETY_TOOL)
    order = 21

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        mentions = drug_mentions_for(ctx)
        text = _query_text(ctx)
        if not mentions:
            purpose = _candidate_purpose(text)
            if purpose is None:
                return SourceDecision(run=False, reason="no drug named")
            if "fda_sweep" in (getattr(ctx, "excluded_modules", None) or set()):
                # "No FDA sweep is needed": the candidate-drug sweep is skipped; a named drug is still looked up.
                return SourceDecision(run=False, reason="FDA candidate sweep excluded at your request")
            # Sources are routed in `order`, so the chembl and primekg decisions are already made.
            feeders = [k for k in CANDIDATE_SOURCES if ctx.should_run(k)]
            if not feeders:
                return SourceDecision(
                    run=False,
                    reason=f"{_no_candidate_reason(purpose)} (no drug named; no ChEMBL or PrimeKG drug lookup runs)",
                )
            what = "FAERS and label" if purpose == "safety" else "Drugs@FDA approvals and label indications"
            return SourceDecision(
                run=True,
                reason=f"{purpose} question naming no drug: {what} for up to {MAX_SAFETY_CANDIDATES} candidate "
                f"drugs from {' or '.join(_SOURCE_NAMES[k] for k in feeders)}",
            )
        if has_safety_intent(text):
            return SourceDecision(run=True, reason=f"safety question about {', '.join(mentions)}")
        if has_label_intent(text):
            return SourceDecision(run=True, reason=f"FDA label or approval question about {', '.join(mentions)}")
        # A mechanism or potency question needs no FAERS/label calls (4-7 openFDA requests per drug).
        return SourceDecision(run=False, reason=f"no safety, label or FDA-approval question about {', '.join(mentions)}")

    def running_title(self, ctx: QueryContext) -> str:
        mentions = drug_mentions_for(ctx)
        if not mentions:
            if _candidate_purpose(_query_text(ctx)) == "regulatory":
                return "Checking FDA approvals and labels for candidate drugs found earlier in this run"
            return "Checking FDA adverse-event reports and labels for candidate drugs found earlier in this run"
        return f"Checking FDA adverse-event reports and label for {join_names(mentions)}"

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        if not drug_mentions_for(ctx):
            if _candidate_purpose(_query_text(ctx)) == "regulatory":
                return f"Regulatory: Drugs@FDA + label for up to {MAX_SAFETY_CANDIDATES} candidate drugs (no drug named)"
            return f"Safety: FAERS + label for up to {MAX_SAFETY_CANDIDATES} candidate drugs (no drug named)"
        return "Safety: FAERS + label"

    async def _query_safety(
        self, registry: Any, queries: List[Tuple[Dict[str, Any], str]]
    ) -> Tuple[List[ToolResult], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """(results, entries, errors, citation records, label set ids) for (drug record, ingredient) pairs."""
        results = list(await asyncio.gather(*(registry.execute(SAFETY_TOOL, ingredient=ing, top_n=10) for _, ing in queries)))
        entries: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        citation_records: List[Dict[str, Any]] = []
        set_ids: List[str] = []
        for (d, ing), res in zip(queries, results):
            if res.success and isinstance(res.data, dict):
                entry = dict(res.data)
                entry["chembl_id"] = d.get("chembl_id")
                if d.get("molecule_type"):
                    entry["molecule_type"] = d.get("molecule_type")  # the approval check skips CBER-type products
                if d.get("origin"):
                    entry["candidate"] = True
                    entry["candidate_basis"] = d.get("basis")
                if d.get("matched_by_name"):
                    entry["matched_by_name"] = True
                    entry["chembl_error"] = d.get("chembl_error")
                citation_records.extend(entry.pop("citation_records", None) or [])
                entries.append(entry)
                if (entry.get("label") or {}).get("set_id"):
                    set_ids.append(entry["label"]["set_id"])
            else:
                errors.append({"drug": ing, "error": _error_detail(res)})
        return results, entries, errors, citation_records, set_ids

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        if not drug_mentions_for(ctx):
            return await self._run_candidates(ctx, registry, state)
        resolved = await shared_resolve(ctx, registry, state)
        if resolved.success and isinstance(resolved.data, dict):
            rdata: Dict[str, Any] = resolved.data
        elif chembl_unavailable(resolved):
            # ChEMBL is down (5xx, timeout, open breaker). openFDA searches by name, so every drug is queried by
            # the name the researcher used instead of failing the safety lookup (see _by_name_queries).
            rdata = {"drugs": [], "lookup_errors": [{"query": q, "error": str(resolved.error or "")[:240]}
                                                    for q in drug_mentions_for(ctx)]}
        else:
            return ToolResult(
                success=False,
                tool_name=SAFETY_TOOL,
                error=f"the drug could not be identified in ChEMBL first: {resolved.error}",
                error_type=resolved.error_type or ErrorType.UPSTREAM_API_ERROR,
            )
        drugs = rdata.get("drugs") or []
        skipped: List[Dict[str, Any]] = []
        queries: List[Tuple[Dict[str, Any], str]] = []
        for d in drugs:
            name = d.get("pref_name") or d.get("query_name") or d.get("chembl_id")
            phase = d.get("max_phase")
            if phase is None or float(phase) != 4.0:
                skipped.append({
                    "drug": name,
                    "chembl_id": d.get("chembl_id"),
                    "max_phase": phase,
                    "reason": f"ChEMBL max phase {format_phase(phase)} ({d.get('max_phase_meaning')}), not an approved drug",
                })
                continue
            ingredient = _ingredient(d.get("pref_name") or d.get("query_name"))
            if len(ingredient) < 2:
                skipped.append({"drug": name, "chembl_id": d.get("chembl_id"), "max_phase": phase, "reason": "no usable ingredient name"})
                continue
            queries.append((d, ingredient))
        for name in rdata.get("unresolved_names") or []:
            skipped.append({"drug": name, "chembl_id": None, "max_phase": None, "reason": "no exact ChEMBL match"})
        for cid in rdata.get("invalid_chembl_ids") or []:
            skipped.append({"drug": cid, "chembl_id": None, "max_phase": None, "reason": "not a valid ChEMBL molecule ID"})
        by_name, by_name_skipped = _by_name_queries(ctx, rdata.get("lookup_errors") or [], {ing for _, ing in queries})
        queries.extend(by_name)
        skipped.extend(by_name_skipped)

        results, entries, errors, citation_records, set_ids = await self._query_safety(registry, queries)
        if queries and not entries:
            first = results[0]
            return ToolResult(success=False, tool_name=SAFETY_TOOL, error=first.error, error_type=first.error_type)
        data: Dict[str, Any] = {"safety": entries, "skipped": skipped, "errors": errors, "citation_records": citation_records}
        if by_name:
            data["chembl_unavailable"] = {
                "drugs": [ing for _, ing in by_name],
                "error": by_name[0][0].get("chembl_error") or "",
            }
        if has_ehr_intent(_query_text(ctx)):
            data["ehr_note"] = EHR_NOTE
        return ToolResult(
            success=True,
            tool_name=SAFETY_TOOL,
            data=data,
            canonical_ids={"set_ids": ",".join(set_ids)} if set_ids else {},
            metadata={"source": "openFDA"},
        )

    async def _run_candidates(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        """No drug named, safety asked: FAERS + label for candidates from the ChEMBL result, else PrimeKG's."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CANDIDATE_WAIT_S
        candidates: List[Dict[str, Any]] = []
        basis: Optional[str] = None
        origin: Optional[str] = None
        why_none: List[str] = []
        names_looked_up: Dict[str, str] = {}
        skipped_kg: List[Dict[str, Any]] = []
        allele_note: Optional[str] = None
        for key in CANDIDATE_SOURCES:
            if not ctx.should_run(key):
                continue
            res = await state.source_result(key, requester=self, timeout_s=max(1.0, deadline - loop.time()))
            if res is None:
                why_none.append(f"the {_SOURCE_NAMES[key]} result was not available")
                continue
            if key == "chembl":
                # Approved drugs whose names the profile could not fetch are still the best candidates: look
                # their names up by id before falling back to PrimeKG.
                names_looked_up = await _names_for_ids(registry, unnamed_approved_ids(res))
                found, found_basis, reason = _chembl_candidates(res, names_looked_up)
            else:
                found, found_basis, reason, skipped_kg = _primekg_candidates(res)
            if found:
                found, allele_note = _prefer_allele(found, query_alleles(_query_text(ctx)))
                candidates, basis, origin = found[:MAX_SAFETY_CANDIDATES], found_basis, key
                break
            if reason:
                why_none.append(reason)
        purpose = _candidate_purpose(_query_text(ctx)) or "safety"
        selection: Dict[str, Any] = {
            "source": _SOURCE_NAMES.get(origin or ""),
            "basis": basis,
            "purpose": purpose,
            "drugs": [{k: c.get(k) for k in ("drug", "chembl_id", "primekg_id", "basis", "allele_specific") if k in c}
                      for c in candidates],
            "max_candidates": MAX_SAFETY_CANDIDATES,
        }
        if allele_note:
            selection["allele_note"] = allele_note
        if purpose == "regulatory":
            # Absence of an approval record for these candidates is not evidence that no drug is approved.
            selection["coverage_note"] = ("Only the candidate drugs listed were checked in Drugs@FDA; a drug outside "
                                          "this list may be approved.")
        if names_looked_up and origin == "chembl":
            selection["names_looked_up"] = names_looked_up
        if skipped_kg:
            selection["skipped"] = skipped_kg
        if why_none and candidates:
            selection["not_used"] = why_none  # why an earlier source gave no candidate
        if not candidates:
            selection.update(status="none", reason=_no_candidate_reason(purpose), details=why_none)
            return ToolResult(
                success=True,
                tool_name=SAFETY_TOOL,
                data={"safety": [], "skipped": [], "errors": [], "citation_records": [],
                      "candidate_selection": selection, **_ehr_note(purpose)},
                metadata={"source": "openFDA"},
            )
        selection["status"] = "selected"
        results, entries, errors, citation_records, set_ids = await self._query_safety(
            registry, [(c, c["ingredient"]) for c in candidates]
        )
        if not entries:
            first = results[0]
            return ToolResult(success=False, tool_name=SAFETY_TOOL, error=first.error, error_type=first.error_type)
        return ToolResult(
            success=True,
            tool_name=SAFETY_TOOL,
            data={"safety": entries, "skipped": [], "errors": errors, "citation_records": citation_records,
                  "candidate_selection": selection, **_ehr_note(purpose)},
            canonical_ids={"set_ids": ",".join(set_ids)} if set_ids else {},
            metadata={"source": "openFDA"},
        )

    def describe(self, result: ToolResult, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return "FDA adverse-event lookup failed", _error_detail(result)
        data = result.data or {}
        selection = data.get("candidate_selection") or None
        entries = safety_entries(data)
        if not entries:
            if selection and selection.get("status") == "none":
                return (f"Skipped the FDA lookup: {selection.get('reason') or NO_CANDIDATE_REASON}",
                        "; ".join(selection.get("details") or []) or "no drug named")
            skipped = data.get("skipped") or []
            if skipped:
                s = skipped[0]
                return f"Skipped the FDA lookup for {s.get('drug')}", str(s.get("reason") or "")
            return "No FDA adverse-event data", ""
        titles: List[str] = []
        details: List[str] = []
        if selection:
            details.append(f"candidates (no drug named): {selection.get('basis')}")
        for e in entries:
            total = int(e.get("total_reports") or 0)
            alias = faers_alias(e)
            if total:
                titles.append(f"FAERS: {total:,} reports mention {e.get('ingredient')}" + (f" (as {alias})" if alias else ""))
            else:
                titles.append(f"FAERS: no reports found for {e.get('ingredient')}")
            bits: List[str] = []
            top = (e.get("top_reactions") or [])[:1]
            if top:
                bits.append(f"top: {top[0]['term']} ({top[0]['count']})")
            approval = e.get("approval") or {}
            if approval.get("status") in ("approved", "not_found", "combination_only"):
                bits.append(approval_phrase(approval))
            label = e.get("label")
            if label:
                state = "present" if label.get("boxed_warning") else "none"
                bits.append(f"boxed warning: {state} on {label_product(label)}")
            elif e.get("label_error"):
                bits.append("FDA label lookup failed")
            else:
                bits.append("no FDA label found")
            details.append(" · ".join(bits))
        if (data.get("chembl_unavailable") or {}).get("drugs"):
            details.append("ChEMBL unavailable: matched by name ("
                           + ", ".join(str(d) for d in data["chembl_unavailable"]["drugs"]) + ")")
        for s in data.get("skipped") or []:
            details.append(f"skipped {s.get('drug')}: {s.get('reason')}")
        if data.get("ehr_note"):
            details.append("FAERS is spontaneous reporting, not EHR data; no EHR source is available")
        title = "; ".join(titles)
        if selection:
            title = f"Candidate drugs' {title}"
        return title, " · ".join(details)

    def evidence_xml(self, result: ToolResult) -> str:
        return safety_evidence_xml(result.data or {})

    def allowed_identifiers(self, result: ToolResult) -> Dict[str, Set[str]]:
        apps: Set[str] = set()
        set_ids: Set[str] = set()
        for entry in safety_entries(result.data or {}):
            label = entry.get("label") or {}
            if label.get("application_number"):
                apps.add(re.sub(r"[\s-]", "", str(label["application_number"])).upper())
            if label.get("set_id"):
                set_ids.add(str(label["set_id"]).lower())
            approval = entry.get("approval") or {}
            for a in approval.get("applications") or []:
                if a.get("application_number"):
                    apps.add(re.sub(r"[\s-]", "", str(a["application_number"])).upper())
            for x in approval.get("efficacy_supplements") or []:
                if x.get("application_number"):
                    apps.add(re.sub(r"[\s-]", "", str(x["application_number"])).upper())
        return {"fda_application": apps, "dailymed_setid": set_ids}

    def citations(self, result: ToolResult) -> List[CitationRecord]:
        return _citation_records(result.data, self.agent, SAFETY_TOOL)

    def artifact(self, result: ToolResult) -> Optional[Tuple[str, Any]]:
        data = result.data or {}
        entries = safety_entries(data)
        selection = data.get("candidate_selection") or None
        if not entries and not data.get("skipped") and not selection:
            return None
        out: Dict[str, Any] = {"safety": entries, "safety_skipped": list(data.get("skipped") or [])}
        if selection:
            out["safety_candidates"] = selection
        if data.get("ehr_note"):
            out["safety_ehr_note"] = data["ehr_note"]
        return "drug_intel", out

    def fallback_markdown(self, result: ToolResult) -> str:
        return safety_fallback_markdown(result.data or {})

    def scope_notes(self, ctx: QueryContext, result: Optional[ToolResult]) -> List[str]:
        if result is None:
            return []
        if not result.success:
            return [f"Lithoscribe's openFDA lookup failed ({_error_detail(result)}); no FDA adverse-event or label data is available."]
        data = result.data or {}
        notes: List[str] = []
        selection = data.get("candidate_selection") or None
        if selection and selection.get("status") == "none":
            why = "; ".join(selection.get("details") or [])
            notes.append(
                "The question asks about safety, but no drug was named and no drug candidate was found"
                f"{' (' + why + ')' if why else ''}, so no FDA adverse-event or label data is available."
            )
        elif selection:
            names = join_names([d.get("drug") for d in selection.get("drugs") or [] if d.get("drug")])
            notes.append(
                f"No drug was named in the question, so openFDA was queried for {names}: candidates taken from "
                f"{selection.get('basis')}. Present the FAERS counts as these candidates' FAERS reports, not as safety "
                "data for other drugs or for the question as a whole."
            )
            if selection.get("skipped"):
                notes.append(
                    "PrimeKG candidates not queried in openFDA (not anti-cancer therapies for this question): "
                    + "; ".join(f"{x.get('drug')} ({x.get('reason')})" for x in selection["skipped"][:5]) + "."
                )
        if data.get("ehr_note"):
            notes.append(str(data["ehr_note"]) + " Say that EHR safety data was not retrieved.")
        down = data.get("chembl_unavailable") or {}
        if down.get("drugs"):
            names = join_names([str(d) for d in down["drugs"]])
            notes.append(
                f"ChEMBL was unavailable ({str(down.get('error') or 'upstream error')[:160]}), so openFDA was queried "
                f"for {names} by the drug name as written: its ChEMBL identity and approval phase were not checked. "
                "Say that the drug was matched by name; base any FDA-approval statement only on Drugs@FDA or the label "
                "in the evidence."
            )
        for s in data.get("skipped") or []:
            not_approved = s.get("max_phase") is not None and float(s["max_phase"]) < 4
            notes.append(
                f"openFDA was not queried for {s.get('drug')}: {s.get('reason')}; no FDA adverse-event or label data "
                "exists for it in this answer."
                + (f" Its Drugs@FDA approval status was not checked either: do not call {s.get('drug')} FDA-approved "
                   "unless another retrieved source shows an FDA approval." if not_approved else "")
            )
        for err in data.get("errors") or []:
            notes.append(f"The openFDA lookup for {err.get('drug')} failed ({err.get('error')}).")
        for e in safety_entries(data):
            name = e.get("ingredient")
            notes.extend(approval_scope_notes(e))
            alias = faers_alias(e)
            if alias:
                notes.append(
                    f"FAERS has no reports under the name {name}; its reports were found under the US name {alias} "
                    f"({e.get('faers_match_field') or 'activesubstance'} field). Say so when citing the counts."
                )
            if not e.get("total_reports"):
                also = [x for x in (e.get("us_name"),) if x]
                searched = f" (also searched as {', '.join(also)} and by openFDA generic name)" if also else (
                    " (also searched by openFDA generic name)" if len(e.get("faers_searches_tried") or []) > 1 else "")
                notes.append(f"FAERS returned no reports for {name}{searched}; say that no reports were found, not that "
                             "the drug is safe.")
            label = e.get("label")
            if not label:
                if e.get("label_error"):
                    notes.append(
                        f"The FDA label lookup for {name} failed ({e['label_error']}), so whether a label exists is "
                        f"unknown; do not say it has no label and do not quote label warnings for it."
                    )
                else:
                    notes.append(f"No FDA label was found for {name}; do not quote label warnings for it.")
                continue
            if label.get("combination_product"):
                notes.append(
                    f"No single-ingredient NDA/BLA label was found for {name}; the label retrieved is for the combination "
                    f"product {label_product(label)}. Say so, and do not attribute its warnings to {name} alone."
                )
            if not label.get("harmonized"):
                notes.append(
                    f"The {name} label (set id {label.get('set_id')}) is not harmonized by openFDA, so its brand and "
                    "application number are unknown; it does not show FDA approval of a specific product."
                )
            if label.get("boxed_warning_truncated") or label.get("warnings_truncated") or label.get("contraindications_truncated"):
                notes.append(f"The {name} label sections are truncated excerpts; describe only the excerpted text.")
        return notes
