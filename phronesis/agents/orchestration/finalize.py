"""Stage 6: persistent memory consolidation, the task status and the assembled result."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from ..query_context import TaskOutcome
from .context import RunContext, logger, orchestrator_module


def turn_fully_supported(report: Any) -> bool:
    """Whether a turn's evidence may be stored as verified: the check passed AND no claim was left unsupported or
    contradicted. "passed" alone tolerates unsupported claims (score >= 0.8), which must not make the turn's tool
    evidence VERIFIED_EMPIRICAL in later answers."""
    if report is None or not getattr(report, "passed", False):
        return False
    bad = {"unsupported", "contradicted"}
    for c in getattr(report, "claims", None) or []:
        status = c.get("status") if isinstance(c, dict) else getattr(c, "status", "")
        if str(status or "") in bad:
            return False
    for attr in ("unsupported_claims", "contradicted_claims"):
        value = getattr(report, attr, 0)
        n = len(value) if isinstance(value, (list, tuple, set)) else value
        if isinstance(n, (int, float)) and n > 0:
            return False
    return True


def _verification_counts(report: Any) -> Dict[str, Any]:
    """The report fields session memory needs to decide what may be stored as verified."""
    out: Dict[str, Any] = {"passed": bool(getattr(report, "passed", False))}
    for attr in ("fully_verified", "unsupported_claims", "contradicted_claims", "unchecked_claims"):
        value = getattr(report, attr, None)
        if value is not None:
            out[attr] = value
    return out


async def consolidate_memory(run: RunContext) -> None:
    """Write the turn's findings and its active subject to session memory (unless the run is not persisted)."""
    orch = run.orch
    persist_memory, ctx, user_id, thread_id, query = run.persist_memory, run.ctx, run.user_id, run.thread_id, run.query
    candidate_response, tool_results, verification_report = run.candidate_response, run.tool_results, run.verification_report
    emit = run.emit

    # -------------------------------------------------------------
    # STAGE 6: PERSISTENT MEMORY CONSOLIDATION (Mem0)
    # -------------------------------------------------------------
    if persist_memory:
        try:
            active_subj: Dict[str, str] = {}
            if ctx.primary_target:
                active_subj["gene"] = ctx.primary_target
            if ctx.drugs:
                active_subj["drug"] = ctx.drugs[0].label
            if ctx.diseases:
                active_subj["disease"] = ctx.diseases[0].label
            if ctx.biomodel_id:
                active_subj["biomodel_id"] = ctx.biomodel_id
            if active_subj:
                orch.memory.record_active_subject(user_id, thread_id, active_subj)
            consolidation = await orch.memory.consolidate_turn(
                thread_id=thread_id,
                query=query,
                response=candidate_response,
                tool_results={k: v.data for k, v in tool_results.items() if v.success},
                failure_lesson=verification_report.failure_lesson,
                is_turn_verified=turn_fully_supported(verification_report),
                verification=_verification_counts(verification_report),
                user_id=user_id,
                active_subject=active_subj if active_subj else None,
            )
            await emit(
                "memory",
                title="Saved findings to session memory",
                text=f"{consolidation['nodes_created']} memory node(s) written",
                extra={"thread_id": thread_id, "entities_updated": consolidation["entities_updated"]},
            )
        except Exception as mem_err:
            logger.warning("Memory consolidation error: %s", mem_err)


async def _record_routing_outcome(run: RunContext, task_status: Dict[str, Any]) -> None:
    """Feed the router's outcome history (phronesis/agentic/router_store.py) from every agentic research run."""
    agent_state = run.agent_state
    if agent_state is None or run.ctx is None or run.ctx.scope != "research":
        return
    try:
        from ...agentic import router

        core = agent_state.core_phase_status() or {}
        claims = (run.execution_artifacts or {}).get("claim_confidence") or {}
        await asyncio.to_thread(
            router.record_outcome,
            query=run.resolved_query or run.query,
            ctx=run.ctx,
            framework=agent_state.framework,
            routing=run.routing,
            verification_score=getattr(run.verification_report, "verification_score", None),
            answer_confidence=claims.get("answer_confidence") if isinstance(claims, dict) else None,
            task_status=str(task_status.get("task_status") or ""),
            core_status=str(core.get("status") or "ok"),
            llm_calls=agent_state.budget.llm_calls,
            llm_limit=agent_state.budget.max_llm_calls,
        )
    except Exception as exc:  # noqa: BLE001 - learning must never break a turn
        logger.info("Routing outcome not recorded: %s", exc)


def apply_incomplete_status(task_status: Dict[str, Any], agent_state: Any) -> Dict[str, Any]:
    """Add `incomplete_reasons` and `unresolved` (lists, always present) to the task status, and make a run that
    stopped early or left sub-questions unanswered "partial" even when its computation (a time course) completed:
    the request is the whole brief, not its simulation step."""
    status: Dict[str, Any] = dict(task_status)
    reasons: List[str] = []
    unresolved: List[str] = []
    if agent_state is not None:
        try:
            info = agent_state.incomplete_status()
            reasons, unresolved = list(info.get("incomplete_reasons") or []), list(info.get("unresolved") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Incomplete-status check failed: %s", exc)
    status["incomplete_reasons"] = reasons
    status["unresolved"] = unresolved
    if (reasons or unresolved) and status.get("task_status") in ("completed", "partial"):
        status["task_status"] = "partial"
        parts = list(reasons)
        if unresolved:
            parts.append(f"{len(unresolved)} sub-question(s) not fully answered.")
        lead = " ".join(parts)
        reason = str(status.get("reason") or "")
        status["reason"] = lead if reason in ("", "Evidence gathered and synthesized.") or lead in reason else f"{lead} {reason}"
    return status


def merge_pipeline_phases(run: RunContext) -> None:
    """Put the pipeline stages' wall times next to the agent phases in the agent_run artifact (run order), and
    refresh its budget snapshot so it includes the verifier revision and the finalize phases."""
    art = (run.execution_artifacts or {}).get("agent_run")
    if not isinstance(art, dict) or run.agent_state is None:
        return
    try:
        phases = [p for p in art.get("phases") or [] if isinstance(p, dict) and p.get("stage") != "pipeline"]
        merged = phases + [dict(p) for p in run.pipeline_phases]
        if all("at" in p for p in merged):
            merged.sort(key=lambda p: float(p.get("at") or 0.0))
        art["phases"] = merged
        art["budget"] = run.agent_state.budget.to_dict()
        art["sub_questions"] = [q.to_dict() for q in run.agent_state.sub_questions]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pipeline phase timing merge failed: %s", exc)


async def assemble_result(run: RunContext) -> Any:
    """Close the open steps and build the AuthoritativeOrchestrationResult."""
    ao = orchestrator_module()
    orch = run.orch
    ctx, agent_state, requested_plan, tool_results = run.ctx, run.agent_state, run.requested_plan, run.tool_results
    verification_report, llm_ok, candidate_response = run.verification_report, run.llm_ok, run.candidate_response
    thread_id, model_name, framework, start_time = run.thread_id, run.model_name, run.framework, run.start_time
    thoughts, reasoning_parts, show_reasoning, steps = run.thoughts, run.reasoning_parts, run.show_reasoning, run.steps
    selected_agents, execution_artifacts, citation_map = run.selected_agents, run.execution_artifacts, run.citation_map
    record, close_open_steps, finalize_manifest = run.record, run.close_open_steps, run.finalize_manifest

    total_duration = round(time.perf_counter() - start_time, 2)
    try:
        query_context_artifact = ctx.to_artifact()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query context summary failed: %s", exc)
        query_context_artifact = None
    try:
        core_note = agent_state.core_phase_note() if agent_state is not None else ""
        outcome = orch._task_outcome(ctx, requested_plan, tool_results, verification_report, llm_ok, core_note)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Task outcome failed: %s", exc)
        outcome = TaskOutcome(task_status="partial", reason=f"Task status unavailable ({type(exc).__name__}).")
    task_status = apply_incomplete_status(outcome.model_dump(), agent_state)
    merge_pipeline_phases(run)
    record("note_step", "task_status", {}, task_status)
    await close_open_steps()
    await _record_routing_outcome(run, task_status)

    return ao.AuthoritativeOrchestrationResult(
        thread_id=thread_id,
        response=candidate_response,
        thoughts=thoughts,
        reasoning=orch._sanitize_agent_names("".join(reasoning_parts)) if show_reasoning else "",
        steps=steps,
        selected_agents=list(dict.fromkeys(selected_agents)),
        duration_seconds=total_duration,
        model_used=model_name,
        framework_used=framework,
        verification_report=verification_report.model_dump(),
        execution_artifacts=execution_artifacts,
        citations=citation_map,
        run_manifest=await finalize_manifest(candidate_response, total_duration, framework=framework),
        query_context=query_context_artifact,
        task_status=task_status,
    )
