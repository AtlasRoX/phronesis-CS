"""
Shared state of one agentic run: what every phase reads and writes.

The orchestrator owns the tool_results dict; phases add results only through call_tool/add_result so each
one is budget-charged, labelled (phase and purpose, shown in the evidence block) and visible to the verifier,
the citation map and memory exactly like a pipeline result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .budget import BudgetExhausted, RunBudget
from .config import AGENTIC, DEBATE, SUPERVISOR, SWARM
from .control import RunControl
from .llm import AgentLLM

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]

# The phase that IS each framework: when it does not run as asked, the run is reported as partial.
CORE_PHASES: Dict[str, str] = {AGENTIC: "plan_execute", SUPERVISOR: "supervisor", SWARM: "swarm", DEBATE: "debate"}
CORE_PHASE_LABELS: Dict[str, str] = {
    "plan_execute": "Plan & execute",
    "supervisor": "Supervisor",
    "swarm": "Swarm",
    "debate": "Debate",
}
SendFn = Callable[[Dict[str, Any]], Awaitable[None]]


# What each agent phase is called next to the source it searched ("web search (counter-evidence)").
_PHASE_LABELS: Dict[str, str] = {
    "gaps": "gap search",
    "plan_execute": "plan step",
    "plan_join": "plan step",
    "supervisor": "supervisor",
    "swarm": "swarm",
    "debate": "debate",
    "counter_evidence": "counter-evidence",
    "counter_evidence_recheck": "counter-evidence",
    "hypothesis": "hypothesis test",
    "calibrate": "calibration",
    "analyst": "analysis",
    "decompose": "decomposition",
}
_AGENT_KEY_RE = re.compile(r"^agent\.([a-z_]+)\.(\d+)\.([A-Za-z0-9_\-]+)$")


def source_label(short: str) -> str:
    """Public name of a tool or source key ("lithoscribe" -> "Europe PMC"); the key itself, readable, when unknown."""
    from ..sources.names import public_name

    name = public_name(short) or public_name(short.split("_", 1)[0])
    return name or short.replace("_", " ")


def evidence_key_label(key: str, round_no: Optional[int] = None) -> str:
    """A reader's name for an evidence key: "agent.gaps.4.lithoscribe" -> "Europe PMC (gap search)", with
    round_no (0-based) -> "Europe PMC (gap round 1)"; "tool:chembl" / "synlethdb" -> "ChEMBL" / "SynLethDB"."""
    key = str(key or "").strip()
    m = _AGENT_KEY_RE.match(key)
    if m is None:
        bare = key.split(":", 1)[1] if key.startswith("tool:") else key
        return source_label(bare) if bare and ":" not in bare else key
    phase, short = m.group(1), m.group(3)
    if phase == "gaps" and round_no is not None:
        where = f"gap round {round_no + 1}"
    else:
        where = _PHASE_LABELS.get(phase, phase.replace("_", " "))
    return f"{source_label(short)} ({where})"


@dataclass
class SubQuestion:
    id: str
    text: str
    focus: str = ""  # literature | target | simulation | single_cell | clinical | drug | variant | structure | omics | general
    depends_on: List[str] = field(default_factory=list)
    status: str = "open"  # open | answered | partial | unanswerable
    evidence_keys: List[str] = field(default_factory=list)
    note: str = ""
    # Readable names of evidence keys set by the phase that added them ("Europe PMC (gap round 1)"); a key without
    # one is named from the key itself (evidence_key_label).
    evidence_key_names: Dict[str, str] = field(default_factory=dict)

    def add_evidence(self, key: str, label: str = "") -> None:
        if key not in self.evidence_keys:
            self.evidence_keys.append(key)
        if label:
            self.evidence_key_names[key] = label

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "focus": self.focus,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "evidence_keys": list(self.evidence_keys),
            "evidence_labels": [self.evidence_key_names.get(k) or evidence_key_label(k) for k in self.evidence_keys],
            "note": self.note,
        }


@dataclass
class AgentState:
    query: str
    clean_query: str
    ctx: Any  # QueryContext
    registry: Any  # BiomedicalToolRegistry
    user_id: str
    thread_id: str
    model_name: str
    api_key: Optional[str]
    framework: str
    budget: RunBudget
    control: RunControl
    llm: AgentLLM
    tool_results: Dict[str, Any]
    emit: EmitFn
    send: SendFn
    project_id: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    memory_summary: str = ""
    project_memory: str = ""
    sub_questions: List[SubQuestion] = field(default_factory=list)
    evidence_labels: Dict[str, Dict[str, str]] = field(default_factory=dict)
    evidence_sections: List[str] = field(default_factory=list)  # extra XML sections written by phases
    brief_notes: List[str] = field(default_factory=list)  # lines for the <research_brief> in the synthesis request
    scope_notes: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    draft: str = ""
    draft_round: int = 0
    verification: Any = None
    bag: Dict[str, Any] = field(default_factory=dict)
    _counter: int = 0
    _running_steps: Dict[str, Dict[str, str]] = field(default_factory=dict)  # step id -> {title, agent} still spinning

    @property
    def effort_level(self) -> str:
        """The effort level for this agentic run (low, medium, high, ultra)."""
        return str(self.options.get("effort_level") or getattr(self.ctx, "effort_level", "medium") or "medium")

    # Tools ---------------------------------------------------------------------------------------------------
    def next_key(self, phase: str, tool_name: str) -> str:
        self._counter += 1
        short = tool_name.split("_", 1)[0] if tool_name else "tool"
        return f"agent.{phase}.{self._counter}.{short}"

    async def call_tool(
        self,
        tool_name: str,
        *,
        phase: str,
        purpose: str,
        key: Optional[str] = None,
        store: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Run one registry tool for a phase: checkpoint, charge, execute, label and (by default) store."""
        from ..tools.registry import ErrorType, ToolResult

        self.control.checkpoint()
        if tool_name.startswith("lumicell"):
            # The model never sees file paths: Lumicell always reads the researcher's own dataset, whatever the
            # plan wrote (a model-written path could point anywhere).
            from .plan_graph import single_cell_dataset_path

            kwargs["dataset_path"] = single_cell_dataset_path(self.ctx)
        elif tool_name == "lithoscribe_literature_synthesis":
            # Identifiers the researcher named are always looked up exactly, whatever the plan passed.
            named = list(getattr(self.ctx, "literature_ids", None) or [])
            if named:
                kwargs["pinned_ids"] = list(dict.fromkeys([*(kwargs.get("pinned_ids") or []), *named]))
        try:
            self.budget.charge_tool(phase)
        except BudgetExhausted as exc:
            return ToolResult(success=False, tool_name=tool_name, error=f"Agent budget exhausted: {exc}", error_type=ErrorType.TIMEOUT)
        try:
            from ..tools.reliability import run_deadline

            with run_deadline(self.budget.remaining_seconds()):
                res = await self.registry.execute(tool_name, _repair_arguments=True, **kwargs)  # agent-written arguments
        except Exception as exc:  # noqa: BLE001
            res = ToolResult(success=False, tool_name=tool_name, error=str(exc), error_type=ErrorType.INTERNAL_ERROR)
        self.trace.append(
            {
                "phase": phase,
                "tool": tool_name,
                "purpose": purpose[:200],
                "success": bool(getattr(res, "success", False)),
                "error_type": getattr(getattr(res, "error_type", None), "value", None),
                "seconds": round(float(getattr(res, "execution_time_seconds", 0.0) or 0.0), 3),
            }
        )
        if store:
            self.add_result(key or self.next_key(phase, tool_name), res, phase=phase, purpose=purpose)
        return res

    def add_result(self, key: str, result: Any, *, phase: str, purpose: str, render: bool = True) -> str:
        self.tool_results[key] = result
        self.evidence_labels[key] = {"phase": phase, "purpose": purpose[:240], "render": "1" if render else ""}
        return key

    def agent_result_keys(self) -> List[str]:
        return [k for k in self.tool_results if k in self.evidence_labels]

    # Steps ---------------------------------------------------------------------------------------------------
    async def step(self, step_id: str, title: str, text: str = "", status: str = "done", agent: str = "Agent", **extra: Any) -> None:
        if status == "running":
            self._running_steps[step_id] = {"title": title, "agent": agent}
        else:
            self._running_steps.pop(step_id, None)
        try:
            await self.emit(step_id, title, text, status=status, agent=agent, extra=extra or None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Agent step emit failed: %s", exc)

    def running_step_ids(self) -> List[str]:
        return list(self._running_steps)

    async def close_steps(self, step_ids: List[str], *, status: str, title_suffix: str = "", text: str = "") -> None:
        """Resolve steps a phase left spinning, so a failed phase never leaves a forever-running step."""
        for sid in step_ids:
            info = self._running_steps.get(sid)
            if info is None:
                continue
            title = info["title"] + (f" - {title_suffix}" if title_suffix else "")
            await self.step(sid, title, text, status=status, agent=info["agent"])

    # Phase status ----------------------------------------------------------------------------------------------
    def mark_phase(self, key: str, status: str, reason: str = "") -> None:
        """A phase that handled its own failure (e.g. no model reply, deterministic fallback) reports it here; the
        runtime records this status in phases_ran instead of "ok"."""
        self.bag.setdefault("_phase_marks", {})[key] = {"status": status, "reason": reason[:240]}

    def pop_phase_mark(self, key: str) -> Optional[Dict[str, str]]:
        return (self.bag.get("_phase_marks") or {}).pop(key, None)

    def core_phase_status(self) -> Optional[Dict[str, str]]:
        """{"phase", "label", "status", "reason"} of the framework's core phase; None for frameworks without one.

        status: ok | fallback | llm_unavailable | budget | error | skipped | not_run (the last run of the phase wins).
        """
        key = CORE_PHASES.get(self.framework)
        if key is None:
            return None
        label = CORE_PHASE_LABELS.get(key, key)
        ran = [p for p in self.bag.get("phases_ran") or [] if isinstance(p, dict) and p.get("phase") == key]
        if ran:
            last = ran[-1]
            return {"phase": key, "label": label, "status": str(last.get("status") or "ok"), "reason": str(last.get("reason") or "")}
        skipped = [p for p in self.bag.get("skipped_phases") or [] if isinstance(p, dict) and p.get("phase") == key]
        if skipped:
            return {"phase": key, "label": label, "status": "skipped", "reason": str(skipped[-1].get("reason") or "")}
        return {"phase": key, "label": label, "status": "not_run", "reason": ""}

    def core_phase_note(self) -> str:
        """The task-status reason when the framework's core phase did not run as asked ("" when it did)."""
        return core_phase_note(self.core_phase_status(), research=getattr(self.ctx, "scope", "research") == "research")

    def incomplete_status(self) -> Dict[str, List[str]]:
        """Why the request is only partly done, beyond the core phase: {"incomplete_reasons": [...], "unresolved":
        [sub-question texts]}. Both empty when the run finished what it set out to do.

        - the budget ran out (any limit, including the time budget), or optional checks stopped at the
          finalization reserve while sub-questions were still unanswered;
        - the planned tool DAG failed under a framework whose core phase is not plan-and-execute (Debate runs it
          first): the answer then rests on the standard pipeline's evidence;
        - sub-questions the evidence answers only partly or not at all (and, after an early stop, those never
          checked)."""
        reasons: List[str] = []
        budget = self.budget
        # A sub-question still "open" was never checked; that counts only when something stopped the run early.
        stopped_early = bool(budget.exhausted_reason or budget.optional_exhausted_reason)
        unresolved = [
            q.text for q in self.sub_questions
            if q.status in ("partial", "missing", "unanswerable") or (q.status == "open" and stopped_early)
        ]
        if budget.exhausted_reason:
            reasons.append(f"Agent budget exhausted: {budget.exhausted_reason}.")
        elif budget.optional_exhausted_reason and unresolved:
            reasons.append(f"Further evidence checks stopped early: {budget.optional_exhausted_reason}.")
        if CORE_PHASES.get(self.framework) != "plan_execute":
            plan = [p for p in self.bag.get("phases_ran") or [] if isinstance(p, dict) and p.get("phase") == "plan_execute"]
            if plan and str(plan[-1].get("status") or "ok") in ("llm_unavailable", "error", "fallback"):
                reasons.append("The planner model call failed; the standard pipeline's evidence was used instead.")
        return {"incomplete_reasons": reasons, "unresolved": unresolved}

    # Prompt pieces -------------------------------------------------------------------------------------------
    def research_brief(self) -> str:
        lines: List[str] = []
        if self.sub_questions:
            lines.append("Address each sub-question explicitly; say which ones the evidence cannot answer:")
            lines.extend(f"- [{q.id}] {q.text}" + (f" (status: {q.status})" if q.status != "open" else "") for q in self.sub_questions)
        steering = self.bag.get("steering") or []
        if steering:
            lines.append("The researcher added these instructions while the run was in progress (follow them):")
            lines.extend(f"- {m}" for m in steering)
        lines.extend(self.brief_notes)
        return "\n".join(lines)

    def summary(self) -> Dict[str, Any]:
        return {
            "framework": self.framework,
            "run_id": self.control.run_id,
            "sub_questions": [q.to_dict() for q in self.sub_questions],
            "budget": self.budget.to_dict(),
            "trace": self.trace[-80:],
            "decisions": list(self.control.decisions),
            "steering": list(self.bag.get("steering") or []),
            "phases": list(self.bag.get("phases_ran") or []),
            "draft_rounds": self.draft_round,
            "core_phase": self.core_phase_status(),
        }


_CORE_FAILURE_WORDS = {
    "llm_unavailable": "model call failed",
    "error": "the step failed",
    "budget": "the agent budget ran out",
    "skipped": "the agent model was unavailable",
    "not_run": "it was not reached",
}


def core_phase_note(core: Optional[Dict[str, Any]], *, research: bool = True) -> str:
    """Why the framework's core phase did not run as asked, for the task status ("" when it ran normally).

    "not_run" counts only for research questions (other scopes never reach the agent phases)."""
    if not isinstance(core, dict):
        return ""
    status = str(core.get("status") or "ok")
    label = str(core.get("label") or core.get("phase") or "The agent phase")
    if status == "ok" or (status == "not_run" and not research):
        return ""
    if status == "fallback":
        reason = str(core.get("reason") or "").strip()
        return f"{label} ran in fallback mode: {reason}" if reason else f"{label} ran in fallback mode (model calls failed)."
    why = _CORE_FAILURE_WORDS.get(status, "it did not complete")
    if status == "skipped" and "budget" in str(core.get("reason") or "").lower():
        why = "the agent budget ran out"
    return f"{label} did not run ({why}); the answer uses the standard pipeline."
