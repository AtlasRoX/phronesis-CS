"""Shared state of one inquiry run.

``RunContext`` holds what used to be the locals of ``AuthoritativeOrchestrator._execute_inquiry`` and the small
closures that read them (``emit``, ``send``, ``record``, ``tracked``, ...). The stage functions in this package
read and update it in order. Names that tests patch on the orchestrator module (``get_llm``,
``get_tool_registry``, ``build_citation_map``, ...) are looked up through that module at call time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from ...agentic import config as agent_config
from ...agentic.control import RunCancelled, RunControl
from ...tools.registry import ErrorType, ToolResult

# Log records keep the orchestrator's logger name, as before the split.
logger = logging.getLogger("phronesis.agents.authoritative_orchestrator")

EventCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


def heartbeat_interval() -> float:
    """Seconds between liveness re-emits of a step waiting on a long model call (PHRONESIS_PROGRESS_HEARTBEAT_S,
    default 5; 0 switches them off)."""
    try:
        return max(0.0, float(os.getenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "") or 5.0))
    except ValueError:
        return 5.0


def cancel_grace_seconds() -> float:
    """How long a cancelled or deadline-expired model call is waited for before it is abandoned
    (PHRONESIS_CANCEL_GRACE_S, default 2)."""
    try:
        return max(0.0, float(os.getenv("PHRONESIS_CANCEL_GRACE_S", "") or 2.0))
    except ValueError:
        return 2.0


def orchestrator_module() -> Any:
    """The orchestrator module, looked up at call time so patches on it take effect in every stage."""
    from .. import authoritative_orchestrator

    return authoritative_orchestrator


@dataclass
class RunContext:
    """One inquiry: its arguments, what it has gathered so far and the event plumbing."""

    orch: Any  # the AuthoritativeOrchestrator running this inquiry
    rec: Any
    start_time: float
    pending: Dict[str, Any]
    query: str
    thread_id: str
    model_name: str
    enable_web_search: bool
    api_key: Optional[str]
    event_callback: Optional[EventCallback]
    user_id: str
    dataset_ids: Optional[List[str]]
    persist_memory: bool
    recall_memory: bool
    framework: str = agent_config.CLASSIC
    project_id: Optional[str] = None
    agent_options: Optional[Dict[str, Any]] = None
    control: Optional[RunControl] = None

    # Accumulated over the run.
    thoughts: List[Dict[str, Any]] = field(default_factory=list)
    thought_index: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reasoning_parts: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    selected_agents: List[str] = field(default_factory=list)
    tool_results: Dict[str, ToolResult] = field(default_factory=dict)
    execution_artifacts: Dict[str, Any] = field(default_factory=dict)
    client_gone: bool = False
    show_reasoning: bool = False

    # plan
    registry: Any = None
    clean_query: str = ""
    resolved_query: str = ""
    ctx: Any = None  # the QueryContext
    memory_pkg: Any = None
    historical_identifiers: Dict[str, Any] = field(default_factory=dict)
    memory_summary: str = ""
    agent_rt: Any = None  # AgenticRuntime (agentic frameworks only)
    agent_state: Any = None  # AgentState (agentic frameworks only)
    routing: Optional[Dict[str, Any]] = None  # Auto runs: the router's decision (execution_artifacts.framework_routing)
    evaluated_sources: List[Any] = field(default_factory=list)
    active_sources: List[Any] = field(default_factory=list)
    title_ctx: Dict[str, Any] = field(default_factory=dict)

    # gather
    task_names: List[str] = field(default_factory=list)
    requested_plan: Any = None  # the SimulationPlan as asked, kept for the task status even if rejected
    successful_sources: List[Any] = field(default_factory=list)
    discovery_report: Any = None

    # draft
    vivarix_summary: str = ""
    lumicell_summary: str = ""
    lithoscribe_summary: str = ""
    synapta_summary: str = ""
    scope_notes: List[str] = field(default_factory=list)
    synthesis_prompt: str = ""
    system_prompt: str = ""
    synthesis_model: str = ""
    llm_ok: bool = True
    candidate_response: str = ""
    streamed_draft: str = ""

    # verify
    entailment_judge: Any = None
    context_identifiers: Dict[str, Set[str]] = field(default_factory=dict)
    verification_report: Any = None
    citation_map: Dict[str, Any] = field(default_factory=dict)

    # progress: seconds since the run started of the last real progress (a step update or streamed text, not a
    # heartbeat), and the wall time of each pipeline stage ({phase, stage: "pipeline", status, seconds, at}).
    last_progress_s: float = 0.0
    pipeline_phases: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ recording

    def record(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Call a run-recorder method; recording problems never affect the answer."""
        if self.rec is None:
            return
        try:
            getattr(self.rec, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run recorder %s failed: %s", method, exc)

    async def finalize_manifest(self, response: str, duration: float, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if self.rec is None:
            return None
        try:
            return await self.rec.finalize(response=response, duration=duration, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run manifest finalization failed: %s", exc)
            return None

    # ------------------------------------------------------------------ events

    async def send(self, event: Dict[str, Any]) -> None:
        # A failing callback (client disconnected) must not abort the run; cancellation still propagates.
        if not self.event_callback or self.client_gone:
            return
        try:
            await self.event_callback(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.client_gone = True
            logger.info("Event delivery stopped (%s: %s); the run continues.", type(exc).__name__, exc)

    async def emit(
        self,
        step_id: str,
        title: str,
        text: str = "",
        status: str = "done",
        agent: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Start or update one activity step. Re-emitting an id updates that step in place."""
        now_elapsed = round(time.perf_counter() - self.start_time, 2)
        th = self.thought_index.get(step_id)
        if th is None:
            th = {"id": step_id, "stage": step_id, "started_at": now_elapsed, "extra": {}}
            self.thought_index[step_id] = th
            self.thoughts.append(th)
        th["title"] = self.orch._sanitize_agent_names(title)
        th["text"] = self.orch._sanitize_agent_names(text)
        th["status"] = status
        th["elapsed_seconds"] = now_elapsed
        if agent is not None:
            th["agent"] = agent
        beat = bool(extra and extra.get("heartbeat"))
        if not beat:
            self.last_progress_s = now_elapsed
            if th["extra"].get("heartbeat"):
                th["extra"] = {**th["extra"], "heartbeat": False}
        if extra:
            th["extra"] = {**th["extra"], **extra}
        started = th["started_at"]
        th["duration_seconds"] = None if status == "running" else round(now_elapsed - float(started), 2)
        await self.send({"type": "thought", "data": dict(th)})

    async def close_open_steps(self) -> None:
        """End of the run: no step may keep spinning. A step still marked running is resolved as done."""
        now_elapsed = round(time.perf_counter() - self.start_time, 2)
        for th in self.thoughts:
            if th.get("status") != "running":
                continue
            th["status"] = "done"
            th["elapsed_seconds"] = now_elapsed
            th["duration_seconds"] = round(now_elapsed - float(th.get("started_at") or 0.0), 2)
            await self.send({"type": "thought", "data": dict(th)})

    async def on_reasoning(self, delta: str) -> None:
        self.reasoning_parts.append(delta)
        if self.show_reasoning:
            await self.send({"type": "reasoning_delta", "delta": delta})

    async def on_text(self, delta: str) -> None:
        self.last_progress_s = round(time.perf_counter() - self.start_time, 2)
        await self.send({"type": "content_delta", "delta": delta})

    # ------------------------------------------------------------------ liveness, cancellation, stage timing

    async def watched(self, step_id: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """Await a long model call while keeping its step alive, the run cancellable and its deadline enforced.

        Every heartbeat_interval() seconds the running step is re-emitted unchanged except for its extra
        {"heartbeat": true, "last_progress_s": <run seconds of the last real progress>, "waiting_s": <seconds in
        this call>}: the worker is alive, which is not the same as progress. A cancel of the run's control stops the
        call within about half a second (RunCancelled). When the run has an absolute time budget the call is raced
        against it too (audit F4): at the deadline the call is cancelled, the budget is marked exhausted and
        RunDeadlineExceeded raised. A provider call that ignores cancellation is waited for at most
        PHRONESIS_CANCEL_GRACE_S seconds, then abandoned (its HTTP request may still finish in the background)."""
        from ...agentic.budget import RunDeadlineExceeded

        interval = heartbeat_interval()
        task = asyncio.ensure_future(coro)
        t0 = time.perf_counter()
        last_beat = t0
        budget = getattr(self.agent_state, "budget", None) if self.agent_state is not None else None
        deadline = (t0 + budget.remaining_seconds()) if budget is not None and getattr(budget, "time_limited", False) else None
        poll = 0.5 if self.control is not None else (interval or None)
        if poll is not None and interval:
            poll = min(poll, interval)
        try:
            while True:
                wait_for = poll
                if deadline is not None:
                    left = max(0.0, deadline - time.perf_counter())
                    wait_for = left if wait_for is None else min(wait_for, left)
                done, _ = await asyncio.wait({task}, timeout=wait_for)
                if task in done:
                    return task.result()
                if self.control is not None and self.control.cancelled:
                    await self._abandon(task, step_id, "cancel")
                    raise RunCancelled(self.control.cancel_reason or "cancelled")
                now = time.perf_counter()
                if deadline is not None and now >= deadline:
                    await self._abandon(task, step_id, "deadline")
                    budget.exhausted()  # records "time budget of Ns used"
                    raise RunDeadlineExceeded(budget.exhausted_reason or "time budget used")
                if interval and now - last_beat >= interval:
                    last_beat = now
                    await self._heartbeat(step_id, now - t0)
        finally:
            if not task.done():
                task.cancel()

    async def _abandon(self, task: "asyncio.Future[Any]", step_id: str, why: str) -> None:
        """Cancel a call and wait for it a short grace period only: a provider that ignores cancellation must not
        hold the run."""
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=cancel_grace_seconds())
        if task in done:
            if not task.cancelled():
                task.exception()  # retrieve it so it is not logged as never retrieved
            return
        logger.warning("Model call for %s did not stop within %.1fs after %s; abandoned", step_id, cancel_grace_seconds(), why)

    async def _heartbeat(self, step_id: str, waiting: float) -> None:
        th = self.thought_index.get(step_id)
        if th is None or th.get("status") != "running":
            return
        await self.emit(
            step_id,
            th.get("title") or "",
            th.get("text") or "",
            status="running",
            extra={"heartbeat": True, "last_progress_s": self.last_progress_s, "waiting_s": round(waiting, 1)},
        )

    def note_pipeline_stage(self, name: str, seconds: float, status: str = "ok", at: Optional[float] = None) -> None:
        """Record one pipeline stage's wall time ({phase, stage: "pipeline", status, seconds, at})."""
        entry: Dict[str, Any] = {"phase": name, "stage": "pipeline", "status": status, "seconds": round(max(0.0, seconds), 2)}
        if at is not None:
            entry["at"] = round(at, 2)
        self.pipeline_phases.append(entry)

    async def timed(self, name: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """Await one pipeline stage and record its wall time."""
        t0 = time.perf_counter()
        status = "ok"
        try:
            return await coro
        except RunCancelled:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self.note_pipeline_stage(name, time.perf_counter() - t0, status, at=t0 - self.start_time)

    def timed_sync(self, name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.perf_counter()
        status = "ok"
        try:
            return fn()
        except Exception:
            status = "error"
            raise
        finally:
            self.note_pipeline_stage(name, time.perf_counter() - t0, status, at=t0 - self.start_time)

    # ------------------------------------------------------------------ tools and artifacts

    async def tracked(
        self,
        name: str,
        agent: str,
        coro: Coroutine[Any, Any, ToolResult],
        running_title: Optional[str] = None,
        describer: Optional[Callable[[ToolResult], Tuple[str, str]]] = None,
    ) -> ToolResult:
        """Run one tool as a visible step: shown as running, then updated with what it returned."""
        ao = orchestrator_module()
        orch = self.orch
        title_ctx = self.title_ctx
        t0 = time.perf_counter()
        title = running_title or orch._RUNNING_TITLES.get(name, name).format_map(defaultdict(str, title_ctx))
        await self.emit(name, title, status="running", agent=agent)
        try:
            res = await coro
        except Exception as exc:
            res = ToolResult(success=False, tool_name=name, error=str(exc), error_type=ErrorType.INTERNAL_ERROR)
        if not res.execution_time_seconds:
            res.execution_time_seconds = time.perf_counter() - t0
        try:
            title, detail = describer(res) if describer else orch._describe_tool_result(name, res, title_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Step description for %s failed: %s", name, exc)
            title, detail = orch._describe_tool_result(name, res, title_ctx)
        extra_parts: List[str] = []
        for augment in ao.get_step_augmenters(name):
            try:
                override, parts = augment(res, title_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Step augmenter for %s failed: %s", name, exc)
                continue
            if override:
                title = override
            extra_parts.extend(p for p in (parts or []) if p)
        if extra_parts:
            detail = " · ".join(p for p in [detail, *extra_parts] if p)
        await self.emit(name, title, detail, status="done" if res.success else "error", agent=agent)
        return res

    def postprocess(self, fn: Callable[[ToolResult, Any], ToolResult], res: ToolResult) -> ToolResult:
        try:
            out = fn(res, self.ctx)
            return out if isinstance(out, ToolResult) else res
        except Exception as exc:  # noqa: BLE001
            logger.warning("Result post-processing %s failed: %s", getattr(fn, "__name__", fn), exc)
            return res

    def merge_artifact(self, key: str, value: Any) -> None:
        try:
            value = json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError) as exc:
            logger.warning("Artifact %s is not JSON-serializable: %s", key, exc)
            return
        current = self.execution_artifacts.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            self.execution_artifacts[key] = {**current, **value}
        else:
            self.execution_artifacts[key] = value

    # ------------------------------------------------------------------ verification

    async def before_verification(self, text: str) -> None:
        """Optional agentic hook (e.g. NLI precompute for a model-backed judge) awaited before each audit."""
        hook = self.agent_state.bag.get("before_verification") if self.agent_state is not None else None
        if hook is None:
            return
        try:
            await hook(text)
        except RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("before_verification hook failed: %s", exc)
