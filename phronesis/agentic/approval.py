"""
Plan approval (feature 15, human in the loop; stage approve, order 10).

Magentic-UI (Mozannar et al. 2025, arXiv:2507.22358): co-planning (the researcher sees and edits the plan
before anything runs) and action guards (approval before expensive or irreversible work). Here the plan is
the source routing in `ctx.decisions`; expensive work is a kinetic simulation or parameter scan (Vivarix),
single-cell analysis (Lumicell), model calibration, docking or a sandboxed code run.

When it runs:
- interactive runs, and runs a human was attached to (a background job whose watcher left): when
  `agent_options.require_plan_approval` is set or expensive work is routed;
- never-interactive runs (plain /api/chat, MCP, jobs nobody watched): only with `require_plan_approval`,
  and then the plan is approved automatically and recorded with source "auto" (RunControl semantics).

Protocol: `{"type":"approval_request","run_id","approval_id","kind":"plan","payload":{"sources":[{key,label,run,
reason}],"expensive":[...],"question"}}` goes out through `state.send`, and the run waits on
`RunControl.request_approval` until the researcher answers through `POST /api/agent/runs/{run_id}/approve`.

Fail closed (AGT-005). No answer within `PHRONESIS_APPROVAL_TIMEOUT_S` (default 300 s), the last watcher leaving,
or an error while asking is not an approval: the expensive work (simulation, parameter scan, calibration,
single-cell analysis, sandboxed code, docking) is withheld (`withhold_expensive`) and the run continues with the
cheap, read-only source searches. `PHRONESIS_APPROVAL_ON_TIMEOUT=reject` treats it as a full rejection instead
(literature search only). Nothing is ever approved on the researcher's behalf once a researcher could answer.

Decisions: an edited payload can only switch sources off (`ctx.decisions[key] = SourceDecision(run=False,
reason="switched off by the researcher")`); it never switches on a source the routing policy turned off. A
rejected plan switches every optional source off except the literature search (lithoscribe) and withholds the
expensive work. Artifact `plan_approval`: {approval_id, status: approved|edited|rejected|auto|timeout|unattended|
error, sources, switched_off, expensive, withheld, message}.

`request_approval_event(state, kind, payload)` is the reusable helper (the F1 plan DAG uses it with kind "dag").
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .config import env_float
from .control import ApprovalDecision
from .guardrails import neutralise
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import AgentState
from ..sources.names import labels as source_labels

logger = logging.getLogger(__name__)

ALWAYS_ON = ("lithoscribe",)
SWITCHED_OFF_REASON = "switched off by the researcher"
WITHHELD_REASON = "withheld: the expensive work was not approved"
# Sources whose routed work is itself expensive (kinetic simulation / parameter scan, single-cell analysis).
EXPENSIVE_SOURCES = ("vivarix", "lumicell")
# Decision sources that are a refusal by default rather than an answer from the researcher.
FAIL_CLOSED_SOURCES = ("timeout", "unattended", "error")

# The approval card's label for each source switch (phronesis.sources.names: SourceName.description).
SOURCE_LABELS: Dict[str, str] = source_labels("description")

_CALIBRATION_RE = re.compile(r"\b(calibrat\w*|fit(?:ting)?\s+(?:the\s+)?(?:model|parameters?)|estimat\w*\s+(?:the\s+)?(?:kinetic\s+)?parameters?|parameter\s+estimation)\b", re.I)
_ANALYSIS_RE = re.compile(r"\b(analy[sz]e|analysis|regression|statistic\w*|differential\s+expression|cluster\w*|correlat\w*|plot)\b", re.I)
_DOCKING_RE = re.compile(r"\b(dock(?:ing)?|generate\s+(?:analogs?|analogues|molecules))\b", re.I)


def approval_timeout() -> float:
    return env_float("PHRONESIS_APPROVAL_TIMEOUT_S", 300.0, 5.0, 3600.0)


def on_no_answer() -> str:
    """PHRONESIS_APPROVAL_ON_TIMEOUT: withhold_expensive (default) or reject. There is no approving option."""
    value = (os.getenv("PHRONESIS_APPROVAL_ON_TIMEOUT", "") or "").strip().lower()
    return "reject" if value == "reject" else "withhold_expensive"


def expensive_work(state: AgentState) -> List[str]:
    """The expensive or long-running work this turn is routed to (empty when there is none)."""
    ctx = state.ctx
    out: List[str] = []
    should = getattr(ctx, "should_run", None)
    if callable(should) and should("vivarix"):
        plan = getattr(ctx, "simulation_plan", None)
        out.append("parameter scan" if plan is not None and getattr(plan, "action", "") == "parameter_scan" else "kinetic simulation")
    if callable(should) and should("lumicell"):
        out.append("single-cell analysis")
    query = state.clean_query or ""
    attachments = list(getattr(ctx, "attachments", None) or [])
    has_table = any(getattr(a, "kind", "") in ("h5ad", "table") for a in attachments)
    has_model = bool(getattr(ctx, "biomodel_id", None) or getattr(ctx, "model_attachment", None))
    if _CALIBRATION_RE.search(query) and has_table and has_model:
        out.append("model calibration")
    elif has_table and _ANALYSIS_RE.search(query) and state.options.get("enable_code_analysis", True):
        out.append("sandboxed code analysis")
    if _DOCKING_RE.search(query):
        out.append("docking or molecule generation")
    return list(dict.fromkeys(out))


def source_rows(ctx: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key, decision in (getattr(ctx, "decisions", None) or {}).items():
        rows.append(
            {
                "key": key,
                "label": SOURCE_LABELS.get(key, key.replace("_", " ").capitalize()),
                "run": bool(getattr(decision, "run", False)) and (ctx.source_allowed(key) if hasattr(ctx, "source_allowed") else True),
                "reason": str(getattr(decision, "reason", "") or ""),
            }
        )
    return rows


async def request_approval_event(
    state: AgentState, kind: str, payload: Dict[str, Any], timeout_seconds: Optional[float] = None
) -> Tuple[Optional[str], ApprovalDecision]:
    """Ask for approval and tell the client: the approval_request event carries the id RunControl created.

    Non-interactive runs get RunControl's automatic approval and no event. RunControl registers the request
    before calling announce, so the id in the event is already answerable through the run-control API.
    """
    control = state.control
    timeout = approval_timeout() if timeout_seconds is None else timeout_seconds
    announced: List[str] = []

    async def announce(approval_id: str) -> None:
        announced.append(approval_id)
        await state.send(
            {"type": "approval_request", "run_id": control.run_id, "approval_id": approval_id, "kind": kind, "payload": payload}
        )

    decision = await control.request_approval(kind, payload, timeout_seconds=timeout, announce=announce)
    return (announced[0] if announced else None), decision


def _edited_off(original: List[Dict[str, Any]], edited: Any) -> List[str]:
    """Keys the researcher switched off in an edited payload (lists of rows or {key: bool} maps)."""
    wanted: Dict[str, bool] = {}
    rows = edited.get("sources") if isinstance(edited, dict) else None
    if isinstance(rows, dict):
        wanted = {str(k): bool(v) for k, v in rows.items()}
    elif isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("key") is not None:
                wanted[str(row["key"])] = bool(row.get("run", True))
    running = {r["key"] for r in original if r["run"]}
    return [k for k in running if wanted.get(k) is False]


def _switch_off(ctx: Any, keys: List[str], reason: str = SWITCHED_OFF_REASON) -> List[str]:
    from ..agents.query_context import SourceDecision

    done: List[str] = []
    for key in keys:
        current = ctx.decisions.get(key)
        if current is not None and current.run:
            ctx.decisions[key] = SourceDecision(run=False, reason=reason)
            done.append(key)
    if done:
        if reason == SWITCHED_OFF_REASON:
            prior = list(ctx.extras.get("switched_off_by_researcher") or [])
            ctx.extras["switched_off_by_researcher"] = sorted(set(prior) | set(done))
        if "vivarix" in done:
            ctx.suppress_simulation = True  # agentic phases must not simulate behind the researcher's back either
        if "lumicell" in done:
            ctx.extras["single_cell_forbidden"] = True
    return done


def withhold_expensive(state: AgentState, expensive: List[str]) -> List[str]:
    """Keep the expensive work from running (fail closed): switch its sources off and set the hard guards the
    simulation, calibration, single-cell and code-analysis phases check. Returns the source keys switched off."""
    ctx = state.ctx
    if not expensive:
        return []
    decisions = getattr(ctx, "decisions", None) or {}
    switched = _switch_off(ctx, [k for k in EXPENSIVE_SOURCES if k in decisions], reason=WITHHELD_REASON) if decisions else []
    if any(w in expensive for w in ("kinetic simulation", "parameter scan", "model calibration")):
        try:
            ctx.suppress_simulation = True  # simulation_block_reason(): no simulation or calibration this turn
        except Exception:  # noqa: BLE001
            pass
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict):
        if "single-cell analysis" in expensive:
            extras["single_cell_forbidden"] = True
        extras["expensive_work_withheld"] = sorted(set(extras.get("expensive_work_withheld") or []) | set(expensive))
    if "sandboxed code analysis" in expensive:
        state.options["enable_code_analysis"] = False
    state.scope_notes.append(f"Not run because it was not approved: {', '.join(expensive)}.")
    state.brief_notes.append(
        "The researcher did not approve the expensive work proposed for this question ("
        + ", ".join(expensive)
        + "); it was not run. Do not present results from it, and say it can be run once approved."
    )
    return switched


def _needs_decision(control: Any) -> bool:
    flag = getattr(control, "needs_human_decision", None)
    return bool(flag) if flag is not None else bool(getattr(control, "interactive", False))


class PlanApprovalPhase(AgentPhase):
    key = "approval"
    stage = "approve"
    order = 10

    def applies(self, state: AgentState) -> bool:
        required = bool(state.options.get("require_plan_approval"))
        if not _needs_decision(state.control):
            return required
        return required or bool(expensive_work(state))

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        ctx = state.ctx
        expensive = expensive_work(state)
        sources = source_rows(ctx)
        payload: Dict[str, Any] = {
            "sources": sources,
            "expensive": expensive,
            "question": state.clean_query[:500],
        }
        if state.control.interactive:
            await state.step(
                "agent_approval",
                "Waiting for your approval of the plan",
                ", ".join(r["label"] for r in sources if r["run"]) or "No source routed",
                status="running",
                agent="Planner",
            )
        try:
            approval_id, decision = await request_approval_event(state, "plan", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - asking failed: fail closed, never run the guarded work unasked
            logger.warning("Plan approval request failed: %s", exc)
            approval_id, decision = None, ApprovalDecision(
                approved=False, message=f"approval could not be requested ({type(exc).__name__})", source="error"
            )
        state.control.checkpoint()  # a cancel resolves the request as rejected; stop here instead

        switched: List[str] = []
        withheld: List[str] = []
        if decision.source == "auto" and decision.approved:
            status = "auto"
        elif decision.source in FAIL_CLOSED_SOURCES and not decision.approved:
            status = decision.source
            withheld = list(expensive)
            switched = withhold_expensive(state, withheld)
            if on_no_answer() == "reject":
                optional = [r["key"] for r in sources if r["run"] and r["key"] not in ALWAYS_ON]
                switched += _switch_off(ctx, optional, reason=WITHHELD_REASON)
        elif not decision.approved:
            status = "rejected"
            optional = [r["key"] for r in sources if r["run"] and r["key"] not in ALWAYS_ON]
            switched = _switch_off(ctx, optional)
            withheld = list(expensive)
            switched += withhold_expensive(state, withheld)
        else:
            switched = _switch_off(ctx, _edited_off(sources, decision.payload)) if decision.payload else []
            status = "edited" if switched else "approved"

        final = source_rows(ctx)
        note = neutralise(decision.message or "")[:500] if decision.source == "user" else ""
        if switched and status in ("rejected", "edited"):
            labels = ", ".join(SOURCE_LABELS.get(k, k) for k in switched)
            state.brief_notes.append(f"The researcher switched these sources off before the search: {labels}. Do not present results from them.")
            state.scope_notes.append(f"Not searched at the researcher's request: {labels}.")
        if note and decision.approved:
            state.brief_notes.append(f"The researcher's note on the plan: {note}")
        artifact = {
            "approval_id": approval_id,
            "status": status,
            "sources": final,
            "switched_off": switched,
            "expensive": expensive,
            "withheld": withheld,
            "message": note,
        }
        state.artifacts["plan_approval"] = artifact
        titles = {
            "approved": "Plan approved",
            "edited": f"Plan approved with {len(switched)} source(s) switched off",
            "rejected": "Plan rejected: literature search only",
            "auto": "Plan approved automatically (non-interactive run)",
            "timeout": "No answer in time: expensive work withheld" if withheld else "No answer in time: no expensive work to withhold",
            "unattended": "Nobody is watching: expensive work withheld" if withheld else "Nobody is watching: no expensive work to withhold",
            "error": "Approval could not be obtained: expensive work withheld",
        }
        await state.step("agent_approval", titles[status], ", ".join(r["label"] for r in final if r["run"]), agent="Planner")
        return PhaseOutcome(artifacts={"plan_approval": artifact})


register_phase(PlanApprovalPhase())

__all__ = ["PlanApprovalPhase", "expensive_work", "request_approval_event", "source_rows", "withhold_expensive"]
