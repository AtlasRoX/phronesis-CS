"""
Agentic phase registry and runtime.

A phase is one capability (decomposition, plan-and-execute, gap re-retrieval, debate, ...) that runs at one
stage of a turn:

- before_evidence: after the question is parsed and memory recalled (clarify, decompose, project recall);
  runs for non-research scopes too, so a clarifying question can replace the generic "not research" reply
- approve: after source routing, before Stage 1 starts (plan approval may switch sources off in ctx.decisions)
- augment: after the pipeline's Stage 1 + 2 results exist (planned tool DAG, supervisor, hypotheses, ...)
- after_draft: after each draft answer, before verification (gaps, critique); may ask for a re-synthesis
- finalize: after verification (claim confidence, project memory, run summary)

Phases are registered by key (re-registering replaces), run in `order`, and only for the frameworks they
list (None = every agentic framework). A failing phase is logged and shown as an error step; it never ends
the turn. RunCancelled always propagates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .budget import BudgetExhausted
from .config import AGENTIC_FRAMEWORKS
from .control import RunCancelled
from .llm import AgentLLMUnavailable
from .state import AgentState

logger = logging.getLogger(__name__)

STAGES = ("before_evidence", "approve", "augment", "after_draft", "finalize")

# A phase may run this long past the run's remaining time budget before it is stopped (seconds).
PHASE_GRACE_SECONDS = 10.0
# after_draft phases that only score the current draft: skipped when an earlier phase of the same stage already
# asked for a rewrite that will be checked again (the scores would be for a draft about to be replaced).
SKIP_WHEN_REWRITE_PENDING = frozenset({"nli_precompute"})


@dataclass
class PhaseOutcome:
    # before_evidence only: end the turn with this reply (a clarifying question, a refusal, ...)
    stop_response: Optional[str] = None
    stop_task_status: Optional[Dict[str, Any]] = None
    # after_draft only: write the answer again with the new evidence and/or this feedback
    resynthesize: bool = False
    feedback: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def merge(self, other: Optional["PhaseOutcome"]) -> None:
        if other is None:
            return
        if other.stop_response and not self.stop_response:
            self.stop_response, self.stop_task_status = other.stop_response, other.stop_task_status
        self.resynthesize = self.resynthesize or other.resynthesize
        self.feedback.extend(other.feedback)
        self.artifacts.update(other.artifacts)


class AgentPhase:
    key: str = ""
    stage: str = "augment"
    order: int = 100
    frameworks: Optional[Tuple[str, ...]] = None  # None = every agentic framework
    needs_llm: bool = False  # skipped (with a note) when the agent model is unavailable

    def applies(self, state: AgentState) -> bool:
        return True

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        raise NotImplementedError


_PHASES: Dict[str, AgentPhase] = {}


def register_phase(phase: AgentPhase) -> None:
    if not phase.key or phase.stage not in STAGES:
        raise ValueError(f"phase needs a key and a stage in {STAGES}")
    _PHASES.pop(phase.key, None)
    _PHASES[phase.key] = phase


def unregister_phase(key: str) -> None:
    _PHASES.pop(key, None)


def get_phases(stage: str, framework: str) -> List[AgentPhase]:
    out = [
        p for p in _PHASES.values()
        if p.stage == stage and (framework in (p.frameworks or AGENTIC_FRAMEWORKS))
    ]
    return sorted(out, key=lambda p: (p.order, p.key))


def snapshot_phases() -> Dict[str, AgentPhase]:
    return dict(_PHASES)


def restore_phases(state: Dict[str, AgentPhase]) -> None:
    _PHASES.clear()
    _PHASES.update(state)


def _phase_title(key: str) -> str:
    return key.replace("_", " ").strip().capitalize() or "Agent step"


class AgenticRuntime:
    def __init__(self, state: AgentState) -> None:
        self.state = state

    async def run_stage(self, stage: str) -> PhaseOutcome:
        merged = PhaseOutcome()
        st = self.state
        for phase in get_phases(stage, st.framework):
            st.control.checkpoint()
            try:
                if not phase.applies(st):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agent phase %s applies() failed: %s", phase.key, exc)
                continue
            if phase.needs_llm and not st.llm.available:
                st.bag.setdefault("skipped_phases", []).append({"phase": phase.key, "reason": st.llm.unavailable_reason})
                continue
            # A used-up budget skips the phases that need it. Finalize phases that make no required model call
            # (counter-evidence recheck, deterministic claim confidence, memory) still run: they make the answer's
            # status and memory correct and cost nothing (an optional model call inside them is refused and they
            # take their deterministic path). Once only the finalization reserve is left, phases that need the agent
            # model are skipped too.
            if st.budget.exhausted() and not (stage == "finalize" and not phase.needs_llm):
                st.bag.setdefault("skipped_phases", []).append({"phase": phase.key, "reason": st.budget.exhausted_reason})
                continue
            if phase.needs_llm and st.budget.optional_exhausted():
                st.bag.setdefault("skipped_phases", []).append({"phase": phase.key, "reason": st.budget.stop_reason()})
                continue
            if (
                stage == "after_draft"
                and phase.key in SKIP_WHEN_REWRITE_PENDING
                and merged.resynthesize
                and st.draft_round + 1 < st.budget.max_draft_rounds
            ):
                st.bag.setdefault("skipped_phases", []).append(
                    {"phase": phase.key, "reason": "a rewrite of this draft is pending; the next draft is scored instead"}
                )
                continue
            t0 = time.perf_counter()
            status = "ok"
            before = set(st.running_step_ids())
            failure = ""
            reason = ""
            limit = st.budget.remaining_seconds() + PHASE_GRACE_SECONDS
            try:
                outcome = await asyncio.wait_for(phase.run(st), timeout=limit)
                merged.merge(outcome)
            except RunCancelled:
                raise
            except BudgetExhausted as exc:
                status = "budget"
                failure = f"Stopped: agent budget used up ({exc})"
            except asyncio.TimeoutError as exc:
                if time.perf_counter() - t0 < limit - 0.5:  # a timeout raised inside the phase, not the bound
                    status = "error"
                    failure = f"TimeoutError: {str(exc)[:200]}"
                else:
                    # wait_for cancelled the phase: treat it like a used-up budget, keep whatever it already stored.
                    status = "budget"
                    failure = f"Stopped: the run's time budget ran out after {limit:.0f}s"
                    st.budget.exhausted_reason = (
                        st.budget.exhausted_reason or f"time budget of {st.budget.max_seconds:.0f}s used"
                    )
            except AgentLLMUnavailable as exc:
                status = "llm_unavailable"
                failure = f"Model call failed, so this step did not run ({str(exc)[:160]})"
                st.bag.setdefault("skipped_phases", []).append({"phase": phase.key, "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001
                status = "error"
                failure = f"{type(exc).__name__}: {str(exc)[:200]}"
                logger.warning("Agent phase %s failed: %s", phase.key, exc, exc_info=True)
            # A phase that handled its own failure (no critic replied, planner unavailable, deterministic fallback)
            # reports it with state.mark_phase; that status is what the run summary and task status show.
            mark = st.pop_phase_mark(phase.key)
            if mark and status == "ok":
                status, reason = mark["status"], mark.get("reason", "")
            elif failure:
                reason = failure
            # Steps the phase opened and never closed: resolve them in place (same id) rather than adding a
            # separate "<phase> failed" row next to a spinner that never stops.
            left_open = [sid for sid in st.running_step_ids() if sid not in before]
            if failure:
                if left_open:
                    await st.close_steps(left_open, status="error", title_suffix="did not finish", text=failure)
                else:
                    await st.step(f"agent_{phase.key}", f"{_phase_title(phase.key)} did not run", failure, status="error")
            elif left_open:
                await st.close_steps(left_open, status="done")
            entry = {"phase": phase.key, "stage": stage, "status": status, "seconds": round(time.perf_counter() - t0, 2)}
            if reason:
                entry["reason"] = reason[:240]
            st.bag.setdefault("phases_ran", []).append(entry)
        return merged
