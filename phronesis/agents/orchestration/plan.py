"""Stage 0: read the question, recall memory, run the agentic pre-evidence phases and decide the sources."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from ...agentic import config as agent_config
from ...utils.memory.clinical_memory import MemoryContextPackage
from .. import query_planner
from ..query_context import SourceDecision, TaskOutcome
from .context import RunContext, logger, orchestrator_module


async def start(run: RunContext) -> Optional[Any]:
    """Announce an agentic run and answer a greeting directly. Returns the result when the run ends here."""
    ao = orchestrator_module()
    if run.control is not None:
        # First event of an agentic run: the client needs the run id to approve, steer or cancel.
        await run.send(
            {"type": "run_control", "run_id": run.control.run_id, "framework": run.framework, "interactive": run.control.interactive}
        )

    # -------------------------------------------------------------
    # STAGE 0.1: CASUAL / GREETING INTENT (Ordinary Conversation)
    # -------------------------------------------------------------
    is_casual = any(p.match(run.query.strip()) for p in ao._CASUAL_RES)
    if is_casual:
        # A fixed introduction: nothing was reasoned about or looked up, so no activity is shown.
        resp = ao._INTRODUCTION
        duration = round(time.perf_counter() - run.start_time, 2)
        return ao.AuthoritativeOrchestrationResult(
            thread_id=run.thread_id,
            response=resp,
            thoughts=run.thoughts,
            steps=[],
            selected_agents=[],
            duration_seconds=duration,
            model_used=run.model_name,
            framework_used="conversational-direct",
            verification_report={"passed": True, "verification_score": 1.0, "defects": []},
            run_manifest=await run.finalize_manifest(resp, duration, framework="conversational-direct"),
            task_status=TaskOutcome(
                requested_operation="conversation", executed_operation="conversation", reason="Greeting answered."
            ).model_dump(),
        )
    return None


async def read_question(run: RunContext) -> Optional[Any]:
    """Build the query context, recall memory and run the agentic pre-evidence phases.

    Returns the result when the run ends here (a clarification request or a non-research message)."""
    ao = orchestrator_module()
    orch = run.orch
    query, thread_id, user_id, model_name, api_key = run.query, run.thread_id, run.user_id, run.model_name, run.api_key
    recall_memory = run.recall_memory
    pending = run.pending
    emit = run.emit

    registry = ao.get_tool_registry()
    clean_query = query.strip().strip('"\'')
    prior_subj = orch.memory.get_active_subject(user_id, thread_id) if recall_memory else None
    run.registry, run.clean_query, run.resolved_query = registry, clean_query, clean_query
    try:
        ao.load_extensions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Extensions failed to load: %s", exc)

    await emit("plan", "Reading the question", status="running")
    try:
        resolution = await ao.resolve_attachments(user_id, list(run.dataset_ids or []), clean_query)
        attachments, attachment_errors = list(resolution.attachments), list(resolution.errors)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Attachment resolution failed: %s", exc)
        attachments = []
        attachment_errors = [f"Attached dataset {i} not found" for i in (run.dataset_ids or [])]
    ctx = await ao.build_query_context(
        query=clean_query,
        clean_query=clean_query,
        registry=registry,
        enable_web_search=run.enable_web_search,
        attachments=attachments,
        attachment_errors=attachment_errors,
        user_id=user_id,
        thread_id=thread_id,
        model_name=model_name,
        api_key=api_key,
        prior_subject=prior_subj,
        llm_factory=lambda **kw: ao.get_llm(**kw),
    )
    pending["ctx"] = ctx
    run.ctx = ctx
    ctx.effort_level = (run.agent_options or {}).get("effort_level", "medium")

    # -------------------------------------------------------------
    # STAGE 0: RECALL FROM PERSISTENT MEMORY (Mem0 Principles)
    # -------------------------------------------------------------
    if recall_memory:
        memory_pkg = orch.memory.get_context_package(thread_id, user_id=user_id)
        historical_identifiers = orch.memory.get_verified_identifiers(thread_id, user_id=user_id)
    else:
        memory_pkg = MemoryContextPackage(thread_id=thread_id)
        historical_identifiers = {}
    memory_summary = memory_pkg.to_system_prompt_injection()
    run.memory_pkg, run.historical_identifiers, run.memory_summary = memory_pkg, historical_identifiers, memory_summary

    # -------------------------------------------------------------
    # STAGE 0.2: NOT A RESEARCH QUESTION (no source is searched for it)
    # -------------------------------------------------------------
    if ctx.scope != "research" and recall_memory:
        # In a thread with history a vague message ("why?", "and its safety?") is a follow-up; only
        # unmistakable small talk is answered directly there.
        has_history = bool(
            memory_pkg.verified_evidence or memory_pkg.active_hypotheses or memory_pkg.computed_artifacts
            or memory_pkg.user_instructions or orch.memory.get_conversation_history(thread_id, user_id=user_id)
        )
        if has_history:
            ctx.scope = "conversational" if query_planner.is_small_talk(clean_query) else "research"
            query_planner.decide_builtin_sources(ctx)
    # -------------------------------------------------------------
    # AUTO: choose the architecture for this question before the agent state is built
    # -------------------------------------------------------------
    if run.framework == agent_config.AUTO:
        await _choose_framework(run)
    # -------------------------------------------------------------
    # AGENTIC RUNTIME (agentic frameworks only): clarify, decompose, recall project memory
    # -------------------------------------------------------------
    stopped = await _agentic_preflight(run)
    if stopped is not None:
        return stopped

    if ctx.scope != "research":
        return await _answer_non_research(run)
    return None


async def _choose_framework(run: RunContext) -> None:
    """Auto: route the question to one agentic framework and say why (docs/agentic/auto-routing.md)."""
    from ...agentic import router

    ao = orchestrator_module()
    research = run.ctx.scope == "research"
    if research:
        await run.emit("route", "Choosing the approach", status="running", agent="Router")
    # AGT-007: the run budget exists before routing, so the router's model calls are charged to it ("router" phase)
    # and a tightened request budget limits them; _agentic_preflight reuses this budget.
    budget = None
    if run.control is not None:
        effort_level = (run.agent_options or {}).get("effort_level", "medium")
        budget = run.pending["agent_budget"] = ao.RunBudget.from_env((run.agent_options or {}).get("budget"), effort_level=effort_level)
    decision = await router.choose_framework(
        run.resolved_query,
        run.ctx,
        llm_factory=(lambda **kw: ao.get_llm(**kw)) if research else None,
        model_name=run.model_name,
        api_key=run.api_key,
        budget=budget,
    )
    run.framework = decision.framework
    run.routing = decision.to_artifact()
    if research:
        label = router.FRAMEWORK_LABELS.get(decision.framework, decision.framework)
        await run.emit("route", f"Chose {label}", decision.reason, agent="Router",
                       extra={"framework": decision.framework, "rule": decision.rule})


async def _agentic_preflight(run: RunContext) -> Optional[Any]:
    ao = orchestrator_module()
    control, agent_options, framework = run.control, run.agent_options, run.framework
    ctx, model_name, api_key = run.ctx, run.model_name, run.api_key
    run.agent_rt = None
    run.agent_state = None
    if control is not None:
        effort_level = (agent_options or {}).get("effort_level", "medium")
        budget = run.pending.pop("agent_budget", None) or ao.RunBudget.from_env((agent_options or {}).get("budget"), effort_level=effort_level)
        agent_state = ao.AgentState(
            query=run.query,
            clean_query=run.resolved_query,
            ctx=ctx,
            registry=run.registry,
            user_id=run.user_id,
            thread_id=run.thread_id,
            model_name=model_name,
            api_key=api_key,
            framework=framework,
            budget=budget,
            control=control,
            llm=ao.AgentLLM(lambda **kw: ao.get_llm(**kw), agent_config.agent_model(model_name), api_key, budget),
            tool_results=run.tool_results,
            emit=run.emit,
            send=run.send,
            project_id=run.project_id,
            options=dict(agent_options or {}),
            memory_summary=run.memory_summary,
        )
        if run.routing is not None:
            agent_state.bag["routing"] = run.routing  # the same dict: escalation is recorded on the run's routing
        agent_rt = ao.AgenticRuntime(agent_state)
        run.agent_state, run.agent_rt = agent_state, agent_rt
        ctx.extras["agentic"] = {"framework": framework, "run_id": control.run_id}
        pre = await agent_rt.run_stage("before_evidence")
        if pre.stop_response:
            query_planner.cancel_planner(ctx)
            run.record("set_routing", ctx)
            duration = round(time.perf_counter() - run.start_time, 2)
            stop_artifacts = {**agent_state.artifacts, **pre.artifacts, "agent_run": agent_state.summary()}
            if run.routing is not None:
                stop_artifacts["framework_routing"] = run.routing
            return ao.AuthoritativeOrchestrationResult(
                thread_id=run.thread_id,
                response=pre.stop_response,
                thoughts=run.thoughts,
                steps=[],
                selected_agents=[],
                duration_seconds=duration,
                model_used=model_name,
                framework_used=framework,
                verification_report={"passed": True, "verification_score": 1.0, "defects": []},
                execution_artifacts=json.loads(json.dumps(stop_artifacts, default=str)),
                run_manifest=await run.finalize_manifest(pre.stop_response, duration, framework=framework),
                query_context=ctx.to_artifact(),
                task_status=pre.stop_task_status
                or TaskOutcome(task_status="needs_input", reason="The question needs clarification.").model_dump(),
            )
        if agent_state.project_memory:
            run.memory_summary = f"{run.memory_summary}\n\n{agent_state.project_memory}".strip()
    return None


async def _answer_non_research(run: RunContext) -> Any:
    ao = orchestrator_module()
    ctx, thread_id = run.ctx, run.thread_id
    query_planner.cancel_planner(ctx)
    run.record("set_routing", ctx)
    resp = ao._non_research_reply(run.clean_query, ctx.scope)
    await run.emit(
        "plan",
        title="Read the question",
        text="Not a research question · no sources searched"
        if ctx.scope == "conversational"
        else "No biomedical topic found · no sources searched",
        extra={"thread_id": thread_id, **ctx.to_step_extra()},
    )
    duration = round(time.perf_counter() - run.start_time, 2)
    return ao.AuthoritativeOrchestrationResult(
        thread_id=thread_id,
        response=resp,
        thoughts=run.thoughts,
        steps=[],
        selected_agents=[],
        duration_seconds=duration,
        model_used=run.model_name,
        framework_used="conversational-direct",
        verification_report={"passed": True, "verification_score": 1.0, "defects": []},
        run_manifest=await run.finalize_manifest(resp, duration, framework="conversational-direct"),
        query_context=ctx.to_artifact(),
        task_status=TaskOutcome(
            task_status="completed" if ctx.scope == "conversational" else "needs_input",
            requested_operation="conversation" if ctx.scope == "conversational" else None,
            executed_operation="conversation" if ctx.scope == "conversational" else None,
            request_match=ctx.scope == "conversational",
            reason="Not a research question; no sources were searched."
            if ctx.scope == "conversational"
            else "No biomedical topic was found in the question.",
        ).model_dump(),
    )


async def route_sources(run: RunContext) -> None:
    """Decide which registered evidence sources run, report the plan and (agentic) wait for its approval."""
    ao = orchestrator_module()
    ctx, registry, memory_pkg, thread_id = run.ctx, run.registry, run.memory_pkg, run.thread_id
    emit = run.emit

    run.record("set_routing", ctx)
    run.record(
        "set_memory_context",
        n_evidence=len(memory_pkg.verified_evidence),
        n_instructions=len(memory_pkg.user_instructions),
        memory_summary=run.memory_summary,
    )

    # Registered evidence sources: skipped silently when their tools are not registered.
    try:
        all_sources = [s for s in ao.get_sources() if s.stage == 1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Evidence sources unavailable: %s", exc)
        all_sources = []
    evaluated_sources: List[Any] = []
    active_sources: List[Any] = []
    for src in all_sources:
        if any(registry.get_entry(t) is None for t in src.required_tools):
            ctx.decisions[src.key] = SourceDecision(run=False, reason="tool not available")
            continue
        try:
            decision = src.should_run(ctx)
            if not isinstance(decision, SourceDecision):
                decision = SourceDecision(run=bool(decision))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s routing failed: %s", src.key, exc)
            decision = SourceDecision(run=False, reason=f"routing error: {type(exc).__name__}")
        # ORC-002: the turn's source restriction ("papers only", "trials only") is enforced here for every
        # registered source, not left to each source's own should_run.
        allowed = ctx.source_allowed(src.key) or ctx.source_allowed(getattr(src, "agent", "").lower())
        if decision.run and not allowed:
            decision = SourceDecision(run=False, reason=ctx.source_restriction_reason or "excluded at your request")
        ctx.decisions[src.key] = decision
        evaluated_sources.append(src)
        if decision.run:
            active_sources.append(src)

    # Report what was actually parsed from the question and recalled, so routing is inspectable.
    plan_facts: List[str] = list(ctx.plan_facts())
    for src in active_sources:
        try:
            fact = src.plan_fact(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s plan fact failed: %s", src.key, exc)
            fact = None
        if fact:
            plan_facts.append(fact)
    if memory_pkg.verified_evidence or memory_pkg.user_instructions:
        plan_facts.append(
            f"Recalled {len(memory_pkg.verified_evidence)} evidence item(s) and "
            f"{len(memory_pkg.user_instructions)} instruction(s) from this session"
        )
    if ctx.attachments or ctx.attachment_errors:
        used = [f"{a.display_name}" + (f" ({a.dims})" if a.dims else "") for a in ctx.attachments]
        if ctx.attachment_errors:
            n_err = len(ctx.attachment_errors)
            title = ctx.attachment_errors[0] if n_err == 1 else f"{n_err} attached datasets not found"
            detail = ctx.attachment_errors[1:] + ([f"Using {', '.join(used)}"] if used else [])
            await emit("attachments", title, " · ".join(detail), status="error")
        else:
            n = len(ctx.attachments)
            await emit("attachments", f"Using {n} uploaded dataset{'s' if n != 1 else ''}: {', '.join(used)}")
    await emit(
        "plan",
        title="Read the question",
        text=" · ".join(plan_facts),
        extra={"thread_id": thread_id, "memory_nodes_active": len(memory_pkg.verified_evidence), **ctx.to_step_extra()},
    )

    if run.agent_rt is not None:
        await run.agent_rt.run_stage("approve")
        # Plan approval may have switched sources off.
        active_sources = [s for s in active_sources if ctx.decisions.get(s.key, SourceDecision(run=True)).run]

    run.evaluated_sources, run.active_sources = evaluated_sources, active_sources
    title_ctx: Dict[str, Any] = {
        "target": ctx.primary_display,
        "model": ctx.biomodel_id or (ctx.model_attachment.display_name if ctx.model_attachment else None),
        "dataset": ctx.dataset_attachment.display_name if ctx.dataset_attachment else ctx.dataset_path,
    }
    run.title_ctx = title_ctx
