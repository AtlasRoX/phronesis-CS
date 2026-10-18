"""Extension module owned by TRIALS (feature 1, ClinicalTrials.gov v2). Registers that feature's hooks, sources and tools.

Lithoscribe searches the ClinicalTrials.gov registry in Stage 1 when the question has clinical intent, names
an NCT ID, or names a drug. The verifier gets per-trial evidence documents and a figure check; the synthesis
prompt gets a component line and a rule, registered with the tool so the prompt never describes a registry
search the registry cannot run.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Set, Tuple

from ..agents.query_context import QueryContext, SourceDecision
from ..sources import CitationRecord, EvidenceSource, SourceRunState, register_source
from ..trials.trial_grounding import NCT_RE, audit_trial_figures, trial_evidence_docs
from ..trials.trial_query import TrialQueryPlan, has_clinical_intent, plan_trial_query
from . import hooks

TOOL_NAME = "lithoscribe_clinical_trials_search"
SOURCE_KEY = "clinical_trials"
_PLAN_KEY = "trials.plan"

PROMPT_COMPONENT = "- Lithoscribe also searches the ClinicalTrials.gov registry (protocol metadata and posted results)"
PROMPT_RULE = (
    "Clinical figures (response rates, PFS/OS, hazard ratios, p-values) may come only from a <posted_result> "
    "inside <clinical_trials> or from a retrieved passage. Quote each value exactly as given, with its unit, the "
    "arm name, and the NCT ID or cited publication (PMID/DOI/URL) in the same sentence or table row; never derive, "
    "convert or round a figure. A trial with no posted results has no reportable registry efficacy; do not report "
    "numbers from memory. State which arm an effect estimate (hazard ratio, odds ratio, difference) favours only from "
    "the <analysis>'s estimate_comment or group_description, never from the order of its groups; when confirmatory_test=\"false\", "
    "do not present its p-value as confirmatory. Registry fields (phase, status, enrollment, sponsor) describe the protocol, "
    "not efficacy: ESTIMATED enrollment is planned size. When trials are in evidence, include a 'Trial landscape' table: "
    "NCT ID | Phase | Status | Enrollment | Lead sponsor (at most 8 rows) and, if results are posted, a second table "
    "NCT ID | Endpoint | Arm | Value. The interface shows the full registry list, so do not repeat every trial."
)
NOT_SEARCHED_NOTE = (
    "ClinicalTrials.gov was not searched: no drug, target or NCT ID could be identified; do not cite NCT identifiers."
)


def _plan(ctx: QueryContext) -> Optional[TrialQueryPlan]:
    """The trial query plan for this inquiry, computed once and kept on the context as a JSON-safe dict."""
    extras = ctx.extras if isinstance(ctx.extras, dict) else {}
    if _PLAN_KEY not in extras:
        plan = plan_trial_query(ctx.clean_query, ctx.primary_display, ctx)
        extras[_PLAN_KEY] = asdict(plan) if plan is not None else None
    stored = extras.get(_PLAN_KEY)
    return TrialQueryPlan(**stored) if isinstance(stored, dict) else None


def _clinical_intent(ctx: QueryContext) -> bool:
    return has_clinical_intent(ctx.clean_query) or bool({"clinical", "clinical_trials"} & set(ctx.intents or set()))


def _data(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


class ClinicalTrialsSource(EvidenceSource):
    key = SOURCE_KEY
    agent = "Lithoscribe"
    action = "ClinicalTrials.gov v2 Registry Search"
    required_tools = (TOOL_NAME,)
    order = 10

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        plan = _plan(ctx)
        if plan is not None:
            return SourceDecision(run=True, reason=f"registry search for {plan.label}")
        if _clinical_intent(ctx):
            return SourceDecision(run=False, reason="no drug, target or NCT ID identified")
        return SourceDecision(run=False, reason="no clinical-trial question")

    def running_title(self, ctx: QueryContext) -> str:
        plan = _plan(ctx)
        return f"Searching ClinicalTrials.gov for {plan.label if plan else 'trials'}"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> Any:
        plan = _plan(ctx)
        return await registry.execute(TOOL_NAME, **(plan.to_tool_kwargs() if plan else {}))

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return "ClinicalTrials.gov search failed", f"{err_type}: {str(result.error or '')[:240]}"
        from ..trials.render import describe_result

        plan = _plan(ctx)
        return describe_result(result.data, plan.label if plan else "")

    def evidence_xml(self, result: Any) -> str:
        from ..trials.render import render_trials_evidence

        return render_trials_evidence(_data(result))

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        # whyStopped text can name other trials ("combined with NCT05815173"), so scan the whole payload.
        serialized = json.dumps(_data(result), ensure_ascii=False, default=str)
        return {"nct": {m.upper() for m in NCT_RE.findall(serialized)}}

    def citations(self, result: Any) -> List[CitationRecord]:
        out: List[CitationRecord] = []
        for rec in _data(result).get("citation_records") or []:
            if isinstance(rec, dict) and rec.get("id"):
                out.append(
                    CitationRecord(
                        type="nct",
                        id=str(rec["id"]),
                        title=rec.get("title"),
                        url=rec.get("url"),
                        source_agent="Lithoscribe",
                        source_tool=TOOL_NAME,
                    )
                )
        return out

    def artifact(self, result: Any) -> Optional[Tuple[str, Any]]:
        data = _data(result)
        if not data:
            return None
        payload = {k: v for k, v in data.items() if k != "citation_records"}
        try:
            from ..trials.clinical_trials import ClinicalTrialsReport

            # Null fields are dropped to keep the persisted message small; the client types mark them optional.
            return SOURCE_KEY, ClinicalTrialsReport.model_validate(payload).model_dump(exclude_none=True)
        except Exception:  # noqa: BLE001
            return SOURCE_KEY, payload

    def fallback_markdown(self, result: Any) -> str:
        from ..trials.render import fallback_markdown

        return fallback_markdown(_data(result))

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        if result is None:
            return [NOT_SEARCHED_NOTE] if _clinical_intent(ctx) and _plan(ctx) is None else []
        if result.success and not _data(result).get("trials"):
            plan = _plan(ctx)
            return [
                f"ClinicalTrials.gov returned no registered trials for {plan.label if plan else 'this question'}; "
                f"do not cite NCT identifiers that are not in the evidence."
            ]
        return []

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        plan = _plan(ctx)
        return f"Trials: ClinicalTrials.gov ({plan.label})" if plan else None


def register_hooks() -> None:
    """Source, verifier evidence docs and figure check; idempotent, run at import."""
    register_source(ClinicalTrialsSource())
    hooks.register_evidence_doc_provider("trials", trial_evidence_docs)
    hooks.register_verification_check("trial_figures", audit_trial_figures)


register_hooks()


def register_prompt_hooks() -> None:
    hooks.register_prompt_component("trials", PROMPT_COMPONENT)
    hooks.register_prompt_rule("trials", PROMPT_RULE)


def register_tools(registry: Any) -> None:
    from ..tools.registry import AdapterType, ToolUniverseEntry
    from ..trials.clinical_trials import ClinicalTrialsSearchInput, search_trials

    registry.register(
        ToolUniverseEntry(
            name=TOOL_NAME,
            description=(
                "Lithoscribe ClinicalTrials.gov API v2 registry search: protocol metadata (phase, status, enrollment, "
                "sponsor, conditions, interventions, primary outcomes) plus posted primary and efficacy results, "
                "values kept exactly as registered."
            ),
            domain="lithoscribe",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=ClinicalTrialsSearchInput,
            canonical_id_types=["nct_id", "pmid"],
            timeout_seconds=25.0,
            tags=["clinical_trials", "clinicaltrials.gov", "nct", "posted_results"],
        ),
        executor=search_trials,
    )
    register_prompt_hooks()
