"""Stage 4: the evidence summaries and scope notes, the synthesis prompt and the first, streamed draft."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from ...agentic.budget import BudgetExhausted, RunDeadlineExceeded
from ...agentic.control import RunCancelled
from .context import RunContext, logger, orchestrator_module


class DraftBudgetStop(RuntimeError):
    """The run budget could not admit the first draft: the answer is the deterministic evidence summary."""


def first_draft_output_cap() -> int:
    """Output tokens (reasoning included) allocated to the first draft in agentic runs
    (PHRONESIS_FIRST_DRAFT_MAX_OUTPUT_TOKENS, default 16000): it is admitted from the run budget and sent to the
    model as max_tokens, like every other agentic model call (audit F1)."""
    try:
        return max(256, int(os.getenv("PHRONESIS_FIRST_DRAFT_MAX_OUTPUT_TOKENS", "") or 16_000))
    except ValueError:
        return 16_000


def retrieval_date() -> str:
    """Today's date in UTC (YYYY-MM-DD), from the server clock: what time-sensitive facts are dated "as of"."""
    return datetime.now(timezone.utc).date().isoformat()


# A rewrite edits the previous draft rather than writing a new answer: unchanged sections stay verbatim, so a round
# costs the changed claims, not a whole new ~2,000-word answer, and verified text is not reworded for nothing.
REVISION_INSTRUCTION = (
    "Revise the previous draft to address this critique; keep what is supported. Change only the sentences, table "
    "rows or sections that the new evidence or the critique affects; copy every other part of the previous draft "
    "verbatim (same headings, wording, order and citations). If nothing needs to change, return the previous draft "
    "unchanged:"
)


def prepare_context(run: RunContext) -> None:
    """Plain-text tool summaries (used by the no-model fallback) and the scope notes passed to the model."""
    ao = orchestrator_module()
    orch = run.orch
    ctx, tool_results, task_names, requested_plan = run.ctx, run.tool_results, run.task_names, run.requested_plan
    evaluated_sources = run.evaluated_sources

    # -------------------------------------------------------------
    # STAGE 4: EVIDENCE SYNTHESIS (PaperQA2 Grounded Context)
    # -------------------------------------------------------------
    # Prepare structured context from empirical tool executions
    vivarix_summary = ""
    if tool_results.get("vivarix") and tool_results["vivarix"].success and isinstance(tool_results["vivarix"].data, dict):
        v_data = tool_results["vivarix"].data
        units = v_data.get("units", {}) or {}
        convergence = (v_data.get("diagnostics", {}) or {}).get("convergence_status", "UNKNOWN")
        vivarix_summary = (
            f"- **Model**: {v_data.get('model_id')} ({v_data.get('model_name')})\n"
            f"- **Solver**: {v_data.get('solver_method')} (Duration: {v_data.get('duration')}{units.get('time', 's')})\n"
            f"- **Concentration Unit**: {v_data.get('concentration_unit') or units.get('quantity', 'unknown')}\n"
            f"- **Final Time-Course Concentrations (t={v_data.get('duration')}, endpoint dynamics: {convergence})**: "
            f"{json.dumps(v_data.get('final_concentrations', {}))}\n"
        )
        if v_data.get("applied_overrides"):
            vivarix_summary += f"- **Applied Parameter Overrides**: {json.dumps(v_data['applied_overrides'])}\n"
        ss_res = tool_results.get("vivarix_steady_state")
        if ss_res and ss_res.success and isinstance(ss_res.data, dict):
            vivarix_summary += (
                f"- **COPASI Steady-State Solution ({ss_res.data.get('steady_state_status')}, "
                f"stability: {ss_res.data.get('stability', 'unknown')})**: "
                f"{json.dumps(ss_res.data.get('steady_state_concentrations', {}))}\n"
            )
        else:
            vivarix_summary += (
                "- **Steady State**: NOT computed. Describe values only as final time-course concentrations, never as steady states.\n"
            )
    scan_res = tool_results.get("vivarix_scan")
    if scan_res is not None and isinstance(scan_res.data, dict):
        sd = scan_res.data
        rows = "\n".join(
            f"  - {sd.get('resolved_parameter')} = {p.get('applied_value', p.get('parameter_value'))}: "
            + (f"{sd.get('target_species')} = {p.get('target_response')}" if p.get("target_response") is not None
               else json.dumps(p.get("endpoint_species", {})))
            for p in sd.get("scan_results", [])
        )
        fails = "; ".join(f"{f.get('parameter_value')}: {f.get('error')}" for f in sd.get("failed_points", []))
        vivarix_summary += (
            f"- **Parameter Scan** of {sd.get('resolved_parameter')} in {sd.get('model_id')} "
            f"({sd.get('successful_points')}/{sd.get('total_points')} points, {sd.get('duration_seconds')} s each, "
            f"concentrations in {sd.get('concentration_unit')}):\n{rows}\n"
            + (f"- **Failed Points**: {fails}\n" if fails else "")
        )

    lumicell_summary = ""
    if tool_results.get("lumicell") and tool_results["lumicell"].success and isinstance(tool_results["lumicell"].data, dict):
        l_data = tool_results["lumicell"].data
        lumicell_summary = (
            f"- **Single-Cell Atlas**: {l_data.get('n_cells_post_qc')} cells across {l_data.get('n_clusters')} Leiden clusters\n"
            f"- **Top Marker Genes per Cluster**: {json.dumps({k: [m['gene'] for m in v[:3]] for k, v in l_data.get('top_markers_by_cluster', {}).items()})}\n"
            f"- **Target Expression**: {json.dumps(l_data.get('target_expression_summary', {}))}\n"
        )

    lithoscribe_summary = ""
    if tool_results.get("lithoscribe") and tool_results["lithoscribe"].success and isinstance(tool_results["lithoscribe"].data, dict):
        lit_data = tool_results["lithoscribe"].data
        top_psgs = lit_data.get("ranked_passages", [])[:4]
        passages_text = []
        for p in top_psgs:
            setting = p.get("grade_label") or p.get("experimental_setting") or "unclassified"
            passages_text.append(f"- **{p.get('citation') or p.get('citation_label') or 'Passage'}** ({setting}): {p.get('passage_text', '')[:200]}")
        lithoscribe_summary = "\n".join(passages_text)

    synapta_summary = ""
    if tool_results.get("synapta") and tool_results["synapta"].success and isinstance(tool_results["synapta"].data, dict):
        s_data = tool_results["synapta"].data
        assocs = s_data.get("associations", [])[:3]
        assoc_str = ", ".join(f"{a.get('disease_name')} (score: {a.get('overall_association_score')})" for a in assocs) if assocs else "Target-disease network evaluated"
        synapta_summary = f"- **Associated Diseases**: {assoc_str}"
        pathways = s_data.get("pathways", []) or []
        if pathways:
            pw_str = ", ".join(f"{p.get('description', p.get('id'))} ({p.get('id')})" for p in pathways[:5])
            synapta_summary += f"\n- **KEGG Pathway Memberships**: {pw_str}"

    scope_notes: List[str] = []
    # Notes about Vivarix/Synapta not running only matter to questions they could have informed.
    qtype = ctx.search_plan.question_type if ctx.search_plan is not None else "general"
    molecular = qtype in ("mechanism", "target", "genetics", "biomarker", "general")
    # A simulation that was not requested is simply not part of this answer: no note, so it is never listed as a
    # limitation. An explicit "no simulation" constraint is passed on so the answer respects it silently.
    if ctx.suppress_simulation:
        scope_notes.append(
            "The researcher asked not to run simulations, so none was run; do not discuss simulation and do not "
            "list it as a limitation."
        )
    if not ctx.enable_web_search:
        scope_notes.append("Live web search was disabled for this turn.")
    disease_view = ctx.should_run("disease_targets")  # Synapta's disease view ran instead of a target lookup
    if not ctx.primary_target and molecular and not disease_view:
        scope_notes.append(
            "No specific gene/protein target was identified in the question; Synapta target lookups were not run. "
            "Mention this only if the question is about a molecular target."
        )
    elif ctx.primary_target and "synapta" not in task_names:
        decision = ctx.decisions.get("synapta")
        reason = (decision.reason if decision is not None and decision.reason else "it was not selected for this question")
        scope_notes.append(
            f"Synapta (OpenTargets/KEGG) was not run ({reason}); "
            f"no target-disease association data exists for {ctx.primary_display or ctx.primary_target} in this answer."
        )
    syn_res = tool_results.get("synapta")
    syn_data = syn_res.data if syn_res is not None and isinstance(syn_res.data, dict) else {}
    syn_cov = syn_data.get("coverage") if isinstance(syn_data, dict) else None
    syn_cov_d: Dict[str, Any] = syn_cov if isinstance(syn_cov, dict) else {}
    if syn_cov_d.get("status") in ("partial", "unavailable"):
        failed = [k for k, v in (syn_cov_d.get("sources") or {}).items() if isinstance(v, dict) and v.get("status") == "error"]
        scope_notes.append(
            f"OpenTargets/KEGG coverage is {syn_cov_d['status']}: {', '.join(failed)} failed. A failed lookup is not "
            "evidence that an association or pathway does not exist; say the data was unavailable."
        )
    # Pre-synthesis check: the artifact must match the request, or the answer must say it does not.
    pre_outcome = orch._task_outcome(ctx, requested_plan, tool_results, None, True)
    if requested_plan is not None and pre_outcome.task_status in ("failed", "needs_input", "partial"):
        scope_notes.append(
            f"TASK STATUS ({pre_outcome.task_status.upper()}): the researcher requested a "
            f"{requested_plan.action.replace('_', ' ')}"
            + (f" but a {pre_outcome.executed_operation.replace('_', ' ')} ran" if pre_outcome.executed_operation
               and pre_outcome.executed_operation != requested_plan.action else "")
            + f". {pre_outcome.reason} Do not present any other computation as the requested result, do not "
            "substitute another model, duration or parameter, and state plainly what was not done and why."
        )
    for src in evaluated_sources:
        try:
            scope_notes.extend(n for n in (src.scope_notes(ctx, tool_results.get(src.key)) or []) if n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s scope notes failed: %s", src.key, exc)
    for provider in ao.get_scope_note_providers():
        try:
            scope_notes.extend(n for n in (provider(ctx, tool_results) or []) if n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scope note provider failed: %s", exc)
    run.vivarix_summary, run.lumicell_summary = vivarix_summary, lumicell_summary
    run.lithoscribe_summary, run.synapta_summary = lithoscribe_summary, synapta_summary
    run.scope_notes = scope_notes


def build_prompt(run: RunContext, feedback: Optional[List[str]] = None, previous_draft: str = "") -> str:
    """The synthesis request; agentic runs add the research brief, the critique to address and the previous draft."""
    ao = orchestrator_module()
    agent_state, tool_results, discovery_report, ctx = run.agent_state, run.tool_results, run.discovery_report, run.ctx
    scope_notes, memory_summary, query = run.scope_notes, run.memory_summary, run.query
    extra_sections: List[str] = []
    brief = ""
    if agent_state is not None:
        extra_sections = ao.render_agent_evidence(tool_results, agent_state.evidence_labels, agent_state.evidence_sections)
        brief = agent_state.research_brief()
        if feedback:
            brief += f"\n{REVISION_INSTRUCTION}\n" + "\n".join(f"- {f}" for f in feedback)
        if previous_draft:
            brief += f"\n<previous_draft>\n{previous_draft[:12000]}\n</previous_draft>"
    return ao.build_synthesis_request(
        evidence_block=ao.build_evidence_block(
            tool_results, discovery_report.to_markdown_summary(), query_context=ctx, extra_sections=extra_sections
        ),
        tool_status_block=ao.build_tool_status_block(
            tool_results, scope_notes + (agent_state.scope_notes if agent_state is not None else [])
        ),
        memory_summary=memory_summary,
        query=query,
        question_type=ctx.search_plan.question_type if ctx.search_plan is not None else "general",
        research_brief=brief,
        retrieval_date=retrieval_date(),  # UTC; dates approvals, trial status and FAERS counts
    )


async def write_first_draft(run: RunContext) -> None:
    """Stream the first draft from the synthesis model; on a model error, show the raw evidence instead."""
    ao = orchestrator_module()
    orch = run.orch
    agent_state, tool_results, model_name, api_key = run.agent_state, run.tool_results, run.model_name, run.api_key
    emit, send, record, on_reasoning, on_text = run.emit, run.send, run.record, run.on_reasoning, run.on_text
    successful_sources, clean_query, discovery_report, scope_notes = (
        run.successful_sources, run.clean_query, run.discovery_report, run.scope_notes
    )
    vivarix_summary, lumicell_summary = run.vivarix_summary, run.lumicell_summary
    lithoscribe_summary, synapta_summary = run.lithoscribe_summary, run.synapta_summary

    if agent_state is not None:
        agent_state.bag.setdefault("steering", []).extend(agent_state.control.drain_steering())
    synthesis_prompt = build_prompt(run)
    system_prompt = ao.get_synthesis_system_prompt()
    if agent_state is not None:
        system_prompt = f"{system_prompt}\n\n{ao.SPOTLIGHT_RULE}"

    effort_level = getattr(run.ctx, "effort_level", None) or (run.agent_options or {}).get("effort_level", "medium")
    from ...agentic.config import get_effort_profile
    effort_prof = get_effort_profile(effort_level)

    deliberation_prompts = {
        "concise": "\n[EFFORT LEVEL: LOW / FAST]\nPrioritize direct, concise synthesis without lengthy internal deliberation. Focus on primary conclusive points.",
        "standard": "\n[EFFORT LEVEL: MEDIUM / BALANCED]\nConduct standard scientific reasoning. Balance depth with clarity, highlighting core evidence, mechanisms, and caveats.",
        "deep": "\n[EFFORT LEVEL: HIGH / DEEP THINKING]\nEngage in deep, multi-angle clinical deliberation. Explicitly analyze alternative hypotheses, mechanistic discrepancies, evidentiary tension, biomarker stratification, and clinical trial design subtleties before synthesizing conclusions.",
        "exhaustive": "\n[EFFORT LEVEL: ULTRA / EXHAUSTIVE THINKING]\nEngage in exhaustive, maximal-rigor deliberation. Systematically interrogate every biological assumption, pharmacokinetic/pharmacodynamic pathway, edge-case safety signal, discordant publication readout, and statistical confidence limit. Stress-test contradictory evidence across in vitro, in vivo, and clinical phases with extreme depth.",
    }
    depth = effort_prof.get("deliberation_depth", "standard")
    delib_text = deliberation_prompts.get(depth, "")
    if delib_text:
        system_prompt = f"{system_prompt}\n{delib_text}"

    n_ok = sum(1 for r in tool_results.values() if r.success)
    sources_text = f"{n_ok} of {len(tool_results)} sources returned evidence"
    await emit(
        "synthesis",
        title="Reasoning over the evidence",
        text=sources_text,
        status="running",
        extra={"model": model_name, "effort_level": effort_level},
    )

    candidate_response = ""
    llm_ok = True
    fallback_error: Optional[str] = None
    llm: Any = None
    # The answer can be written by a larger model than the one that routes and plans (PHRONESIS_SYNTHESIS_MODEL).
    synthesis_model = os.getenv("PHRONESIS_SYNTHESIS_MODEL", "").strip() or model_name
    # Agentic runs admit the first draft from the run budget like every other model call (audit F1): it is charged,
    # sent its exact output allocation, reconciled afterwards, and never started when the budget cannot admit it.
    # The classic pipeline has no run budget: its draft stays unledgered (budget.py documents this).
    budget = getattr(agent_state, "budget", None) if agent_state is not None else None
    alloc = None
    draft_text = ""
    draft_reasoning = ""
    streamed_chars = [0]  # what reached the client before a failure: charged when the call is settled as failed

    async def counted_text(delta: str) -> None:
        streamed_chars[0] += len(delta or "")
        await on_text(delta)

    try:
        llm_kwargs: Dict[str, Any] = {}
        if budget is not None:
            want = first_draft_output_cap()
            try:
                alloc = budget.admit(
                    "first_draft", len(system_prompt) + len(synthesis_prompt), want, min_output=min(1024, want)
                )
            except BudgetExhausted as exc:
                raise DraftBudgetStop(str(exc)) from exc
            llm_kwargs["max_tokens"] = alloc.output_cap
        llm = ao.get_llm(
            model_name=synthesis_model,
            api_key=api_key,
            streaming=True,
            reasoning_effort=effort_prof.get("reasoning_effort"),
            **llm_kwargs,
        )
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=synthesis_prompt),
        ]
        stream_kwargs: Dict[str, Any] = {}
        if budget is not None and budget.time_limited:
            stream_kwargs["total_timeout"] = ao.stream_timeout(budget.remaining_seconds())
        draft_text, draft_reasoning = await run.watched(
            "synthesis",
            ao.astream_with_reasoning(llm, messages, on_reasoning=on_reasoning, on_text=counted_text, **stream_kwargs),
        )
        candidate_response = draft_text
        if alloc is not None:
            budget.settle(alloc, output_chars=len(draft_text or "") + len(draft_reasoning or ""))
        await emit(
            "synthesis",
            title="Reasoned over the evidence and wrote the answer",
            text=f"{sources_text} · {len(candidate_response.split())} words",
        )
    except BaseException as err:
        if alloc is not None:
            # Cancelled, failed or deadline: the prompt stays charged, the unused allocation is released.
            budget.settle(alloc, output_chars=streamed_chars[0] or None, failed=True)
        if isinstance(err, RunCancelled) or not isinstance(err, Exception):
            raise
        llm_ok = False
        fallback_error = f"{type(err).__name__}: {str(err)[:240]}"
        logger.warning("LLM synthesis error: %s. Generating deterministic evidence fallback.", err)
        if isinstance(err, DraftBudgetStop):
            title = "Agent budget used up before the answer could be written; showing the gathered evidence"
        elif isinstance(err, RunDeadlineExceeded):
            title = "Run time budget ran out while writing; showing the gathered evidence"
        else:
            title = "Model unavailable; showing the raw evidence instead"
        await emit(
            "synthesis",
            title=title,
            text=f"{type(err).__name__}: {str(err)[:240]}",
            status="error",
        )
        await send({"type": "content_reset", "reason": "model_error"})
        plugin_sections = []
        for src in successful_sources:
            try:
                md = src.fallback_markdown(tool_results[src.key])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Source %s fallback markdown failed: %s", src.key, exc)
                continue
            if md and md.strip():
                plugin_sections.append(md.strip() + "\n\n")
        # Deterministic, evidence-backed fallback without fake conclusions
        candidate_response = (
            f"### Evidence-Backed Clinical Evaluation: {clean_query}\n\n"
            f"#### Target Biology & Topological Architecture (🧬 Synapta)\n{synapta_summary}\n\n"
            f"#### Dynamic Kinetic Simulation (⚙️ Vivarix)\n{vivarix_summary}\n\n"
            f"#### Single-Cell Transcriptomic Heterogeneity (🔬 Lumicell)\n{lumicell_summary}\n\n"
            f"#### Verified Literature & Clinical Readouts (📚 Lithoscribe)\n{lithoscribe_summary}\n\n"
            f"{''.join(plugin_sections)}"
            f"{discovery_report.to_markdown_summary()}\n\n"
            f"#### Evidence Limitations & Evaluation Status\n"
            f"Synthesis generated from empirical tool outputs. Direct LLM completion encountered: {err}."
        )

    # What the client has on screen: the streamed draft (or, after a model error, nothing it must replace).
    raw_draft = candidate_response
    candidate_response = orch._sanitize_answer(candidate_response)
    streamed_draft = raw_draft if llm_ok else candidate_response
    record(
        "set_synthesis",
        system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        synthesis_prompt_sha256=hashlib.sha256(synthesis_prompt.encode("utf-8")).hexdigest(),
        scope_notes=list(scope_notes),
        llm_ok=llm_ok,
        fallback_error=fallback_error,
        response_words=len(candidate_response.split()),
        temperature=getattr(llm, "temperature", None) if llm_ok else None,
        # PRV-002: the model asked to write the answer and the one whose client actually served it.
        synthesis_model=synthesis_model,
        synthesis_served_model=getattr(llm, "model_name", None) if llm_ok else None,
    )
    run.synthesis_prompt, run.system_prompt, run.synthesis_model = synthesis_prompt, system_prompt, synthesis_model
    run.llm_ok, run.candidate_response, run.streamed_draft = llm_ok, candidate_response, streamed_draft
