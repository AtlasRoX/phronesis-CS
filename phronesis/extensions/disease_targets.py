"""
Extension: disease-level target evidence (Open Targets). Answers "which targets for disease X" questions, where no
gene is named, with the disease's most strongly associated targets, the evidence behind each (human genetics,
clinical, animal model, literature), tractability, and the drugs and clinical candidates studied for the disease.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from phronesis.agents.query_context import QueryContext, SourceDecision
from phronesis.extensions import hooks
from phronesis.sources import register_source
from phronesis.sources.base import CitationRecord, EvidenceSource, SourceRunState
from phronesis.tools.registry import ErrorType, ToolResult

TOOL = "opentargets_disease_targets"
logger = logging.getLogger(__name__)

# Questions about a disease's targets, treatments or drug pipeline (the disease must be named and resolved).
_DISEASE_TARGET_RE = re.compile(
    r"\b(?:targets?|druggab\w*|therapeutic|therap(?:y|ies)|treat\w*|drugs?|pipeline|candidates?|inhibitors?|"
    r"agonists?|antagonists?|mechanisms?|pathogenesis|disease[- ]modifying|novel approaches?)\b",
    re.IGNORECASE,
)

PROMPT_COMPONENTS = {
    "disease_targets": "- Synapta (Open Targets): a disease's top associated targets with evidence types, and its clinical candidates",
}
PROMPT_RULES = {
    "disease_targets_evidence": "Rank a disease's targets by tier (validated, tested and not failed, genetics), one "
    "class per drug's target set; loss-of-function risk genes are mechanisms, not targets. A phase is not efficacy.",
}


class DiseaseTargetsInput(BaseModel):
    efo_id: str = Field(pattern=r"^[A-Za-z]+_\d+$")
    target_limit: int = Field(default=15, ge=1, le=50)
    candidate_limit: int = Field(default=40, ge=0, le=150)


def _disease(ctx: QueryContext) -> Optional[Any]:
    """The first named disease with an ontology id from the resolver."""
    for e in ctx.diseases:
        if e.ids.get("ontology_id") and e.source in ("OpenTargets", "HGNC", "EFO", "MONDO") and e.validated:
            return e
    return None


class DiseaseTargetsSource(EvidenceSource):
    key = "disease_targets"
    agent = "Synapta"
    action = "Open Targets disease-target evidence"
    required_tools = (TOOL,)
    order = 15

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        disease = _disease(ctx)
        if disease is None:
            return SourceDecision(run=False, reason="no resolved disease named")
        if ctx.primary_target:
            return SourceDecision(run=False, reason=f"target {ctx.primary_symbol} named (target view used)")
        qtype = ctx.search_plan.question_type if ctx.search_plan is not None else "general"
        if qtype in ("target", "treatment", "guideline", "mechanism") or _DISEASE_TARGET_RE.search(ctx.clean_query):
            return SourceDecision(run=True, reason=f"targets and therapies for {disease.label}")
        return SourceDecision(run=False, reason="no target or therapy question")

    def running_title(self, ctx: QueryContext) -> str:
        d = _disease(ctx)
        return f"Ranking targets and clinical candidates for {d.label if d else 'the disease'} in Open Targets"

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        d = _disease(ctx)
        return f"Disease targets: {d.label} ({d.ids.get('ontology_id')})" if d else None

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        d = _disease(ctx)
        if d is None:
            return ToolResult(success=False, tool_name=TOOL, error="no resolved disease", error_type=ErrorType.NOT_FOUND)
        return await registry.execute(TOOL, efo_id=d.ids["ontology_id"], target_limit=15, candidate_limit=25)

    def describe(self, result: ToolResult, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            err = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return "Open Targets disease lookup failed", f"{err}: {str(result.error or '')[:200]}"
        data = result.data or {}
        name = (data.get("disease") or {}).get("name") or "the disease"
        targets = data.get("targets") or []
        if not targets:
            return f"No associated targets found for {name}", ""
        tiers = data.get("tiers") or {}
        validated = [v["drug"].title() for v in tiers.get("validated") or []]
        tested = tiers.get("clinically_tested") or []
        failed = sum(1 for c in tested if c.get("outcome", "").startswith(("failed", "stopped")))
        parts = []
        if validated:
            parts.append(f"Approved: {', '.join(validated)}")
        if tested:
            parts.append(f"{len(tested)} clinically tested ({failed} failed or stopped)")
        if tiers.get("genetic_risk_mechanisms"):
            parts.append(f"Genetics: {', '.join(tiers['genetic_risk_mechanisms'][:5])}")
        return f"Found target evidence for {name}", " · ".join(parts)

    def evidence_xml(self, result: ToolResult) -> str:
        data = result.data or {}
        d = data.get("disease") or {}
        tiers = data.get("tiers") or {}
        a = lambda v: escape(str(v if v is not None else ""), quote=True)  # noqa: E731
        mapped = data.get("ontology_id_mapping") or {}
        requested = f'requested_id="{a(mapped.get("requested_id"))}" ' if mapped.get("requested_id") else ""
        lines = [f'<disease_targets source="Open Targets Platform" disease="{a(d.get("name"))}" ontology_id="{a(d.get("id"))}" '
                 f'{requested}associated_targets_total="{a(data.get("n_associated_targets"))}">',
                 "<how_to_read>Targets are grouped by how far they are validated for this disease. The association list "
                 "at the end orders evidence that a gene is involved, not its value as a drug target; genes whose risk "
                 "variants cause loss of function are mechanisms to restore, not targets to inhibit. A trial phase reached "
                 "is not efficacy: each tested drug carries its recorded outcome. When asked for the top targets, treat the "
                 "targets one drug hits together as one target class, cover all three tiers (validated, the most advanced "
                 "tested drugs that have not failed, genetic mechanisms), and cite failed programmes as cautionary evidence. "
                 "A drug without annotated targets is still a candidate: name it and say its target is not given here."
                 "</how_to_read>"]
        # KG-010: approval is inferred from a trial-registry record, not a regulatory label; the notes say so.
        lines.append("<validated note=\"drugs with a ClinicalTrials.gov record for this disease in status 'approved for "
                     "marketing' (an expanded-access status, not a regulatory label: confirm the indication), and the "
                     "targets they act on\">")
        for v in tiers.get("validated") or []:
            lines.append(f'<drug chembl_id="{a(v.get("chembl_id"))}" targets="{a(", ".join(v.get("targets") or []) or "not annotated")}">'
                         f'{a(v.get("drug"))}: {a(v.get("mechanism") or "mechanism not annotated")}</drug>')
        lines.append("</validated>")
        lines.append("<clinically_tested note=\"drugs Open Targets lists for this disease whose most advanced stage is phase 2 "
                     "or later, with the recorded trial outcome. For an approved drug the stage may come from another "
                     "indication: approval_scope says so, and disease_trial_phase is the highest phase of its trials for "
                     "this disease recorded here\">")
        for c in tiers.get("clinically_tested") or []:
            stop = f' stop_reason="{a(c.get("stop_reason"))}"' if c.get("stop_reason") else ""
            phase = c.get("disease_trial_phase")
            extra = (f' disease_trial_phase="{a(phase if phase is None or phase % 1 else int(phase))}"' if phase is not None else "")
            extra += f' approval_scope="{a(c.get("approval_scope"))}"' if c.get("approval_scope") else ""
            lines.append(f'<drug chembl_id="{a(c.get("chembl_id"))}" stage="{a(c.get("stage"))}" outcome="{a(c.get("outcome"))}"{stop}{extra} '
                         f'targets="{a(", ".join(c.get("targets") or []))}">{a(c.get("drug"))}: {a(c.get("mechanism") or "mechanism not annotated")}</drug>')
        lines.append("</clinically_tested>")
        lines.append(f"<genetic_risk_mechanisms>{a(', '.join(tiers.get('genetic_risk_mechanisms') or []))}</genetic_risk_mechanisms>")
        lines.append("<associated_genes note=\"Open Targets association evidence; not a therapeutic ranking\">")
        targets = data.get("targets") or []
        # Genes validated through the same approved drug are one target class (nintedanib's kinases), not N targets.
        validated = [t for t in targets if t.get("evidence_class") == "clinically_validated"]
        if validated:
            lines.append(f'<gene_class evidence_class="clinically_validated" symbols="{a(", ".join(t["symbol"] for t in validated))}" '
                         f'ensembl_ids="{a(", ".join(t["ensembl_id"] for t in validated if t.get("ensembl_id")))}">one target class: '
                         "the targets of the approved drug(s) listed under validated</gene_class>")
        for t in targets:
            if t.get("evidence_class") == "clinically_validated":
                continue
            ev = " ".join(f'{k}="{v}"' for k, v in sorted((t.get("evidence") or {}).items()))
            trac = "; ".join(f"{m}: {', '.join(v)}" for m, v in (t.get("tractability") or {}).items())
            lines.append(
                f'<gene symbol="{a(t.get("symbol"))}" ensembl_id="{a(t.get("ensembl_id"))}" overall_score="{a(t.get("overall_score"))}" '
                f'evidence_class="{a(t.get("evidence_class"))}" {ev} tractability="{a(trac)}">{a(t.get("name"))}</gene>'
            )
        lines.append("</associated_genes></disease_targets>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: ToolResult) -> Dict[str, Set[str]]:
        data = result.data or {}
        return {
            "ensembl": {t["ensembl_id"] for t in data.get("targets") or [] if t.get("ensembl_id")},
            "chembl": {str(c["chembl_id"]).upper() for c in data.get("clinical_candidates") or [] if c.get("chembl_id")},
            "ontology": {str(i) for i in ((data.get("disease") or {}).get("id"), (data.get("ontology_id_mapping") or {}).get("requested_id")) if i},
        }

    def citations(self, result: ToolResult) -> List[CitationRecord]:
        data = result.data or {}
        efo = (data.get("disease") or {}).get("id")
        out = []
        for t in data.get("targets") or []:
            if t.get("ensembl_id"):
                out.append(CitationRecord(
                    type="ensembl", id=t["ensembl_id"], title=f"{t['symbol']}: {t.get('name') or ''}".strip(": "),
                    url=f"https://platform.opentargets.org/evidence/{t['ensembl_id']}/{efo}" if efo else None,
                    source_agent=self.agent, source_tool=TOOL,
                ))
        for c in data.get("clinical_candidates") or []:
            if c.get("chembl_id"):
                out.append(CitationRecord(
                    type="chembl", id=str(c["chembl_id"]).upper(), title=str(c.get("name") or "").title(),
                    url=f"https://www.ebi.ac.uk/chembl/compound_report_card/{c['chembl_id']}/",
                    source_agent=self.agent, source_tool=TOOL,
                ))
        return out

    def artifact(self, result: ToolResult) -> Optional[Tuple[str, Any]]:
        data = result.data or {}
        return ("disease_targets", data) if data.get("targets") else None

    def scope_notes(self, ctx: QueryContext, result: Optional[ToolResult]) -> List[str]:
        if result is None:
            return []
        if not result.success:
            return [f"Open Targets disease-target lookup failed ({str(result.error or '')[:160]}); no ranked target evidence is available."]
        data = result.data or {}
        total, limit = data.get("n_clinical_candidates"), data.get("candidate_limit")
        if isinstance(total, int) and isinstance(limit, int) and total > limit:
            return [f"Open Targets lists {total} drugs and clinical candidates for this disease; only {limit} are shown, "
                    "so the candidate list is not exhaustive."]
        return []


register_source(DiseaseTargetsSource())


def register_prompt_hooks() -> None:
    for key, line in PROMPT_COMPONENTS.items():
        hooks.register_prompt_component(key, line)
    for key, text in PROMPT_RULES.items():
        hooks.register_prompt_rule(key, text)


def register_tools(registry: Any) -> None:
    from phronesis.diseaseintel.opentargets import disease_targets
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    registry.register(
        ToolUniverseEntry(
            name=TOOL,
            description="Open Targets: the targets most associated with a disease (with evidence types and tractability) "
            "and the drugs and clinical candidates studied for it.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=DiseaseTargetsInput,
            canonical_id_types=["ensembl_id", "chembl_id"],
            timeout_seconds=30.0,
            tags=["disease", "targets", "opentargets", "drugs", "clinical"],
        ),
        executor=lambda efo_id, target_limit=15, candidate_limit=40: _run(disease_targets, efo_id, target_limit, candidate_limit),
    )
    register_prompt_hooks()


OLS_TERMS_URL = "https://www.ebi.ac.uk/ols4/api/ontologies/efo/terms"
OT_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"
REPLACEMENT_TIMEOUT_S = 8.0
_ONTOLOGY_IRI_RE = re.compile(r"/obo/([A-Za-z]+_\d+)$")
_OT_SEARCH = (
    "query($q: String!) { search(queryString: $q, entityNames: [\"disease\"], page: {index: 0, size: 5}) "
    "{ hits { id name entity } } }"
)


async def replacement_disease_id(ontology_id: str, transport: Any = None) -> Tuple[Optional[str], str]:
    """(current id, how) for a disease id Open Targets no longer carries. Open Targets moved many EFO disease terms
    to MONDO (EFO_0002618 pancreatic carcinoma -> MONDO_0005192): EBI OLS gives the EFO term's 'term replaced by';
    without one, the term's label is searched in Open Targets and only an exact name match is accepted."""
    import httpx

    prefix, _, number = ontology_id.partition("_")
    if not number:
        return None, ""
    async with httpx.AsyncClient(timeout=REPLACEMENT_TIMEOUT_S, transport=transport,
                                 headers={"User-Agent": "Phronesis-DiseaseTargets/1.0"}) as client:
        resp = await client.get(OLS_TERMS_URL, params={"obo_id": f"{prefix}:{number}"})
        terms = ((resp.json() or {}).get("_embedded") or {}).get("terms") or [] if resp.status_code == 200 else []
        if not terms:
            return None, ""
        term = terms[0]
        for iri in [term.get("term_replaced_by")] + list((term.get("annotation") or {}).get("term replaced by") or []):
            m = _ONTOLOGY_IRI_RE.search(str(iri or ""))
            if m and m.group(1) != ontology_id:
                return m.group(1), "EBI OLS 'term replaced by'"
        label = re.sub(r"^obsolete[_ ]", "", str(term.get("label") or ""), flags=re.I).strip()
        if not label:
            return None, ""
        resp = await client.post(OT_GRAPHQL_URL, json={"query": _OT_SEARCH, "variables": {"q": label}})
        hits = (((resp.json() or {}).get("data") or {}).get("search") or {}).get("hits") or [] if resp.status_code == 200 else []
        for hit in hits:
            if hit.get("entity") == "disease" and str(hit.get("name", "")).lower() == label.lower() and hit.get("id") != ontology_id:
                return str(hit["id"]), f"Open Targets search for the EFO label '{label}'"
    return None, ""


async def _replacement(efo_id: str, lookup: Any) -> Tuple[Optional[str], str]:
    try:
        return await asyncio.wait_for(lookup(efo_id), timeout=REPLACEMENT_TIMEOUT_S + 2.0)
    except Exception as exc:  # noqa: BLE001  # the original NOT_FOUND stands
        logger.info("No replacement id for %s: %s", efo_id, type(exc).__name__)
        return None, ""


async def _run(fn: Any, efo_id: str, target_limit: int, candidate_limit: int, lookup: Any = None) -> ToolResult:
    from phronesis.diseaseintel.opentargets import OpenTargetsError

    requested = efo_id
    mapping: Optional[Dict[str, str]] = None
    try:
        data = await fn(efo_id, target_limit=target_limit, candidate_limit=candidate_limit)
        if not data.get("disease"):
            # Open Targets dropped many EFO disease ids for MONDO ones: retry once with the replacement id.
            new_id, how = await _replacement(efo_id, lookup or replacement_disease_id)
            if new_id:
                retry = await fn(new_id, target_limit=target_limit, candidate_limit=candidate_limit)
                if retry.get("disease"):
                    data, efo_id = retry, new_id
                    mapping = {"requested_id": requested, "resolved_id": new_id, "via": how}
                    data["ontology_id_mapping"] = mapping
    except OpenTargetsError as exc:
        error_type = ErrorType.TIMEOUT if exc.kind == "timeout" else ErrorType.UPSTREAM_API_ERROR
        return ToolResult(success=False, tool_name=TOOL, error=str(exc)[:300], error_type=error_type)
    except Exception as exc:  # noqa: BLE001
        # Unexpected failures keep only the exception type: upstream text is not shown to the user.
        return ToolResult(success=False, tool_name=TOOL, error=f"Open Targets lookup failed ({type(exc).__name__})",
                          error_type=ErrorType.UPSTREAM_API_ERROR)
    if not data.get("disease"):
        return ToolResult(success=False, tool_name=TOOL, error=f"Open Targets has no disease {efo_id}", error_type=ErrorType.NOT_FOUND)
    meta: Dict[str, Any] = {"source": "Open Targets Platform GraphQL"}
    if mapping:
        meta["ontology_id_mapping"] = mapping
    return ToolResult(success=True, tool_name=TOOL, data=data, canonical_ids={"ontology_id": efo_id}, metadata=meta)
