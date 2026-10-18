"""Stages 1-3: evidence sources, computational tools (Vivarix, Lumicell), execution artifacts and hypothesis
prioritization."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Coroutine, Dict, List, Optional, Tuple

from ...sources import EvidenceSource, SourceRunState
from ...tools.registry import ErrorType, ToolResult
from .. import query_planner
from .context import RunContext, logger, orchestrator_module


async def run_evidence(run: RunContext) -> None:
    """Stage 1: the built-in evidence tools and the registered evidence sources, run concurrently."""
    ao = orchestrator_module()
    ctx, registry, pending, resolved_query = run.ctx, run.registry, run.pending, run.resolved_query
    steps, selected_agents, tool_results = run.steps, run.selected_agents, run.tool_results
    active_sources, tracked = run.active_sources, run.tracked

    # -------------------------------------------------------------
    # STAGE 1: PARALLEL EVIDENCE GATHERING (LLMCompiler Stage 1)
    # -------------------------------------------------------------
    stage1_tasks: List[Coroutine[Any, Any, Any]] = []
    task_names: List[str] = []

    # 1.1 Synapta Task (Executed when target is identified and not a pure accession ODE simulation)
    if ctx.should_run("synapta"):
        async def run_synapta() -> ToolResult:
            t0 = time.perf_counter()
            steps.append({"agent": "Synapta", "role": "assistant", "action": "OpenTargets & KEGG Association"})
            target_symbol = ctx.primary_symbol
            # Per-upstream coverage: ok | success_empty | error | not_run. An outage is never reported as
            # "no association exists", and all upstreams failing makes the whole lookup a failure.
            sources: Dict[str, Dict[str, Any]] = {}

            def note(key: str, res: Optional[ToolResult], found: int = 0, reason: str = "") -> None:
                if res is None:
                    sources[key] = {"status": "not_run", **({"reason": reason} if reason else {})}
                elif not res.success:
                    sources[key] = {
                        "status": "error",
                        "error_type": getattr(res.error_type, "value", res.error_type) or "ERROR",
                        "error": (res.error or "")[:200],
                    }
                else:
                    sources[key] = {"status": "ok" if found else "success_empty", "count": found}

            async def opentargets() -> Tuple[List[Dict[str, Any]], Optional[str], Dict[str, Any]]:
                if ctx.primary_ensembl_id:
                    hits = [{"id": ctx.primary_ensembl_id, "name": ctx.primary_symbol, "entity": "target"}]
                    note("opentargets_search", None, reason="target already resolved")
                else:
                    search_res = await registry.execute("synapta_opentargets_search", query=target_symbol, size=3)
                    hits = search_res.data.get("results", []) if search_res.success and isinstance(search_res.data, dict) else []
                    note("opentargets_search", search_res, len(hits))
                ensembl_id = hits[0].get("id") if hits and str(hits[0].get("id")).startswith("ENSG") else None
                assoc_data: Dict[str, Any] = {}
                if ensembl_id:
                    # With a disease in the question, fetch a wider list so that disease can be put first
                    # (KRAS's global top association is Noonan syndrome, not the pancreatic cancer asked about).
                    q_diseases = ao.question_disease_words(ctx)
                    assoc_res = await registry.execute(
                        "synapta_opentargets_associations", ensembl_id=ensembl_id, size=25 if q_diseases else 5
                    )
                    if assoc_res.success and isinstance(assoc_res.data, dict):
                        assoc_data = dict(assoc_res.data)
                        assoc_data["associations"] = ao.prioritise_question_diseases(
                            assoc_data.get("associations") or [], q_diseases
                        )
                    note("associations", assoc_res, len(assoc_data.get("associations") or []))
                else:
                    note("associations", None, reason="no Ensembl target id")
                return hits, ensembl_id, assoc_data

            try:
                # KEGG does not depend on the OpenTargets chain, so both run at once.
                (hits, ensembl_id, assoc_data), kegg_res = await asyncio.gather(
                    opentargets(), registry.execute("synapta_kegg_pathways", target_symbol=target_symbol)
                )
                if not kegg_res.success:
                    logger.warning("KEGG pathway lookup failed for %s: %s", target_symbol, kegg_res.error)
                pathways = kegg_res.data.get("results", []) if kegg_res.success and isinstance(kegg_res.data, dict) else []
                note("kegg", kegg_res, len(pathways))

                ran = [s for s in sources.values() if s["status"] != "not_run"]
                evidence_count = len(assoc_data.get("associations") or []) + len(pathways)
                if ran and all(s["status"] == "error" for s in ran):
                    aggregate = "unavailable"
                elif any(s["status"] == "error" for s in ran):
                    aggregate = "partial"
                elif evidence_count == 0:
                    aggregate = "success_empty"
                else:
                    aggregate = "ok"
                coverage = {"status": aggregate, "sources": sources, "evidence_count": evidence_count}
                data = {
                    "search_hits": hits,
                    "associations": assoc_data.get("associations", []),
                    "pathways": pathways[:5],
                    "coverage": coverage,
                }
                if aggregate == "unavailable":
                    failed = ", ".join(f"{k}: {v.get('error_type')}" for k, v in sources.items() if v["status"] == "error")
                    return ToolResult(
                        success=False,
                        tool_name="synapta_opentargets_associations",
                        data=data,
                        error=f"Every OpenTargets/KEGG upstream failed ({failed}); no association evidence is available.",
                        error_type=ErrorType.UPSTREAM_API_ERROR,
                        canonical_ids={"target_symbol": target_symbol or ""},
                        execution_time_seconds=time.perf_counter() - t0,
                        metadata={"adapter": "DIRECT_GRAPHQL", "coverage": coverage},
                    )

                return ToolResult(
                    success=True,
                    tool_name="synapta_opentargets_associations",
                    data=data,
                    canonical_ids={"ensembl_id": ensembl_id or "", "target_symbol": target_symbol or ""},
                    execution_time_seconds=time.perf_counter() - t0,
                    metadata={"adapter": "DIRECT_GRAPHQL", "coverage": coverage},
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    tool_name="synapta_opentargets_associations",
                    error=str(e),
                    error_type=ErrorType.UPSTREAM_API_ERROR,
                    execution_time_seconds=time.perf_counter() - t0,
                )

        stage1_tasks.append(tracked("synapta", "Synapta", run_synapta()))
        task_names.append("synapta")
        selected_agents.append("Synapta")

    # The search plan (deterministic, upgraded by the planner model when it arrives in time) drives both the
    # literature and the web search; it is resolved once and shared.
    search_plan_task: Optional["asyncio.Future[Any]"] = None
    if ctx.should_run("lithoscribe") or ctx.should_run("websurfer"):
        search_plan_task = asyncio.ensure_future(query_planner.finalize_search_plan(ctx))
        pending["search_plan_task"] = search_plan_task
    else:
        query_planner.cancel_planner(ctx)

    # 1.2 Lithoscribe Task (PaperQA2 Literature Retrieval)
    if ctx.should_run("lithoscribe"):
        async def run_lithoscribe() -> ToolResult:
            steps.append({"agent": "Lithoscribe", "role": "assistant", "action": "PaperQA2 Literature Retrieval"})
            plan = await search_plan_task if search_plan_task is not None else None
            return await registry.execute("lithoscribe_literature_synthesis", **ao.literature_request(ctx, resolved_query, plan))

        stage1_tasks.append(tracked("lithoscribe", "Lithoscribe", run_lithoscribe()))
        task_names.append("lithoscribe")
        selected_agents.append("Lithoscribe")

    # 1.3 WebSurfer Task (if enabled)
    if ctx.should_run("websurfer"):
        async def run_websurfer() -> ToolResult:
            steps.append({"agent": "WebSurfer", "role": "assistant", "action": "Live Search"})
            web_query = ctx.web_query()  # a simulation question searches for the pathway's kinetics
            if not ctx.is_simulation_intent and search_plan_task is not None:
                web_query = (await search_plan_task).web_query or web_query
            return await registry.execute("websurfer_search", query=web_query, max_results=3)

        stage1_tasks.append(tracked("websurfer", "WebSurfer", run_websurfer()))
        task_names.append("websurfer")
        selected_agents.append("WebSurfer")

    # 1.4 Registered evidence sources (ClinicalTrials.gov, ChEMBL/openFDA, variant databases, ...)
    source_state = SourceRunState(registry, ctx)
    for src in active_sources:
        async def run_source(s: EvidenceSource = src) -> ToolResult:
            steps.append({"agent": s.agent, "role": "assistant", "action": s.action})
            return await s.run(ctx, registry, source_state)

        try:
            running = src.running_title(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s running title failed: %s", src.key, exc)
            running = None
        stage1_tasks.append(
            tracked(
                src.key,
                src.agent,
                run_source(),
                running_title=running,
                describer=lambda r, s=src: s.describe(r, ctx),
            )
        )
        task_names.append(src.key)
        selected_agents.append(src.agent)

    stage1_results = await asyncio.gather(*stage1_tasks, return_exceptions=True)
    query_planner.cancel_planner(ctx)  # the searches have used (or given up on) the planner model by now
    for name, res in zip(task_names, stage1_results):
        if isinstance(res, Exception):
            tool_results[name] = ToolResult(
                success=False,
                tool_name=name,
                error=str(res),
                error_type=ErrorType.INTERNAL_ERROR,
            )
        else:
            tool_results[name] = res  # type: ignore[assignment]  # gather returns BaseException | T
    run.task_names = task_names


async def run_computations(run: RunContext) -> None:
    """Stage 2: computational systems biology (Vivarix simulation, Lumicell single-cell analysis)."""
    ao = orchestrator_module()
    orch = run.orch
    ctx, registry = run.ctx, run.registry
    steps, selected_agents, tool_results, title_ctx = run.steps, run.selected_agents, run.tool_results, run.title_ctx
    emit, record, tracked, postprocess = run.emit, run.record, run.tracked, run.postprocess

    # -------------------------------------------------------------
    # STAGE 2: COMPUTATIONAL SYSTEMS BIOLOGY (Vivarix & Lumicell)
    # -------------------------------------------------------------
    stage2_tasks: List[Coroutine[Any, Any, Any]] = []
    stage2_names: List[str] = []

    bm_id: Optional[str] = None

    # 2.1 Vivarix runs only on explicit simulation intent (model accession, uploaded model, or kinetic/ODE
    # keywords), never merely because a gene symbol was named, and never when the user asked to skip simulation.
    model_att = ctx.model_attachment
    sim_plan = orch._simulation_plan(ctx) if ctx.should_run("vivarix") else None
    if sim_plan is not None and getattr(ctx.simulation_constraints, "simulation_forbidden", False):
        sim_plan = None  # an explicit "do not simulate" always wins
    requested_plan = sim_plan  # what was asked, kept for the task status even if it is rejected below
    if sim_plan is not None:
        selected_agents.append("Vivarix")
        plan_errors = orch._plan_errors(sim_plan)
        if plan_errors:
            # The request as stated cannot run (e.g. 10 ms is below the 1 s minimum). Nothing is substituted:
            # no default duration, no other model, no unchanged simulation.
            msg = "; ".join(str(e.get("message") or e.get("code")) for e in plan_errors)
            tool_results["vivarix"] = ToolResult(
                success=False,
                tool_name="vivarix_simulate_time_course" if sim_plan.action != "parameter_scan" else "vivarix_parameter_scan",
                error=f"Simulation request rejected before execution: {msg}",
                error_type=ErrorType.VALIDATION_ERROR,
                metadata={
                    "validation_code": plan_errors[0].get("code"),
                    "validation_errors": plan_errors,
                    "requested_operation": sim_plan.action,
                    "requested_duration_seconds": sim_plan.duration_seconds,
                },
            )
            await emit("vivarix", "Simulation request rejected before running", msg[:300], status="error", agent="Vivarix")
            record("note_step", "simulation_validation", sim_plan.model_dump(), {"errors": plan_errors})
            sim_plan = None
    if sim_plan is not None:
        if model_att is not None:
            bm_id = model_att.id
        elif sim_plan.biomodel_id or ctx.biomodel_id:
            bm_id = sim_plan.biomodel_id or ctx.biomodel_id
        else:
            # Search EMBL-EBI BioModels for a model matching the question. No match means no simulation:
            # an unrelated default model is never substituted.
            search_term = ctx.biomodel_search_term()
            await emit("biomodel_search", f"Searching BioModels for {search_term[:60]}", status="running", agent="Vivarix")
            search_error: Optional[str] = None
            try:
                dynamic_hits = await asyncio.to_thread(orch.vivarix.search_models, search_term, 3) or []
            except Exception as exc:  # noqa: BLE001
                dynamic_hits, search_error = [], f"{type(exc).__name__}: {str(exc)[:200]}"
            if dynamic_hits and dynamic_hits[0].get("accession"):
                bm_id = dynamic_hits[0]["accession"]
                others = ", ".join(str(h.get("accession")) for h in dynamic_hits[1:] if h.get("accession"))
                await emit(
                    "biomodel_search",
                    f"Selected {bm_id}",
                    f"{dynamic_hits[0].get('name') or ''}" + (f" · other matches: {others}" if others else ""),
                    agent="Vivarix",
                )
            elif search_error is not None:
                await emit("biomodel_search", "BioModels search failed", search_error, status="error", agent="Vivarix")
                tool_results["vivarix"] = ToolResult(
                    success=False,
                    tool_name="vivarix_simulate_time_course",
                    error=f"The BioModels search failed ({search_error}), so no model could be selected and nothing was simulated.",
                    error_type=ErrorType.UPSTREAM_API_ERROR,
                    metadata={"reason_code": "MODEL_SEARCH_FAILED", "search_term": search_term},
                )
            else:
                await emit(
                    "biomodel_search",
                    "No BioModels match; nothing simulated",
                    f"Search for '{search_term[:60]}' found no model. Name a BioModels accession or upload an SBML model.",
                    status="error",
                    agent="Vivarix",
                )
                tool_results["vivarix"] = ToolResult(
                    success=False,
                    tool_name="vivarix_simulate_time_course",
                    error=(
                        f"No BioModels model matched '{search_term[:60]}'. A simulation needs a model: give a BioModels "
                        "accession (BIOMD##########) or upload an SBML file. No unrelated default model was used."
                    ),
                    error_type=ErrorType.NOT_FOUND,
                    metadata={"reason_code": "MODEL_REQUIRED", "search_term": search_term},
                )
            record(
                "note_step",
                "biomodel_search",
                {"search_term": search_term, "limit": 3},
                {
                    "hits": [h.get("accession") for h in (dynamic_hits or []) if isinstance(h, dict)],
                    "selected": bm_id,
                    "fallback": False,
                    "error": search_error,
                },
            )
    if sim_plan is not None and bm_id is not None:
        if model_att is None:
            record("start_biomodel_resolution", bm_id)
        title_ctx["model"] = model_att.display_name if model_att is not None else bm_id
        title_ctx["scan_parameter"] = sim_plan.scan_parameter or "a parameter"

        model_kwargs: Dict[str, Any] = {}
        if model_att is not None:
            model_kwargs = {
                "sbml_path": model_att.model_path,
                "model_label": model_att.display_name,
                "model_provenance": dict(model_att.public_provenance),
            }
        overrides = {k: float(v) for k, v in (sim_plan.parameter_overrides or {}).items()}
        if overrides:
            model_kwargs["parameter_overrides"] = overrides
        intervals = max(5, min(500, sim_plan.intervals or 40))

        if sim_plan.action == "parameter_scan":
            async def run_vivarix_scan() -> ToolResult:
                steps.append({"agent": "Vivarix", "role": "assistant", "action": "COPASI Parameter Scan"})
                res = await registry.execute(
                    "vivarix_parameter_scan",
                    biomodel_id=bm_id,
                    parameter_name=sim_plan.scan_parameter,
                    start_val=sim_plan.scan_start,
                    end_val=sim_plan.scan_end,
                    num_steps=sim_plan.scan_points,
                    target_species=sim_plan.target_species[0] if len(sim_plan.target_species) == 1 else None,
                    duration=sim_plan.duration_seconds,
                    intervals=intervals,
                    report_all_species=True,
                    **{k: v for k, v in model_kwargs.items() if k != "parameter_overrides"},
                )
                return postprocess(ao.postprocess_vivarix_result, res)

            stage2_tasks.append(tracked("vivarix_scan", "Vivarix", run_vivarix_scan()))
            stage2_names.append("vivarix_scan")
        else:
            async def run_vivarix() -> ToolResult:
                steps.append({"agent": "Vivarix", "role": "assistant", "action": "COPASI ODE Simulation"})
                res = await registry.execute(
                    "vivarix_simulate_time_course",
                    biomodel_id=bm_id,
                    duration=sim_plan.duration_seconds,
                    intervals=intervals,
                    **model_kwargs,
                )
                return postprocess(ao.postprocess_vivarix_result, res)

            # For a steady-state request the time course runs as a labelled diagnostic trajectory next to
            # the root-finding result (artifact role "diagnostic_time_course").
            stage2_tasks.append(tracked("vivarix", "Vivarix", run_vivarix()))
            stage2_names.append("vivarix")

            if sim_plan.action == "steady_state":
                async def run_vivarix_steady_state() -> ToolResult:
                    steps.append({"agent": "Vivarix", "role": "assistant", "action": "COPASI Steady-State Root Finding"})
                    res = await registry.execute("vivarix_compute_steady_state", biomodel_id=bm_id, **model_kwargs)
                    return postprocess(ao.postprocess_vivarix_result, res)

                stage2_tasks.append(tracked("vivarix_steady_state", "Vivarix", run_vivarix_steady_state()))
                stage2_names.append("vivarix_steady_state")

    # 2.2 Lumicell Task (Scanpy Single-Cell Transcriptomics)
    # Gated on a referenced or attached dataset, independent of whether a target gene was named.
    if ctx.should_run("lumicell"):
        dataset_att = ctx.single_cell_attachment

        async def run_lumicell() -> ToolResult:
            steps.append({"agent": "Lumicell", "role": "assistant", "action": "Scanpy Single-Cell QC & Clustering"})
            lum_kwargs: Dict[str, Any] = {"target_gene": ctx.lumicell_target_gene(), "dataset_path": ctx.single_cell_dataset_path}
            if dataset_att is not None:
                lum_kwargs["dataset_name"] = dataset_att.display_name
            res = await registry.execute("lumicell_analyze_single_cell", **lum_kwargs)
            res = postprocess(ao.postprocess_lumicell_result, res)
            record("start_dataset_hash", dataset_att if dataset_att is not None else ctx.dataset_path)
            return res

        stage2_tasks.append(tracked("lumicell", "Lumicell", run_lumicell()))
        stage2_names.append("lumicell")
        selected_agents.append("Lumicell")
    elif (
        ctx.has_single_cell_intent
        and not ctx.single_cell_dataset_path
        and ctx.source_allowed("lumicell")
        and not ctx.extras.get("single_cell_forbidden")
    ):
        # Only an explicit analysis request without a dataset is a failed component (ORC-003); a request the
        # researcher excluded ("papers only", "no single-cell analysis") is not.
        tool_results["lumicell"] = ToolResult(
            success=False,
            tool_name="lumicell_analyze_single_cell",
            error="Single-cell transcriptomic analysis requires an uploaded dataset file (.h5ad / .zarr) with biological provenance. No dataset was referenced, and no synthetic data is generated.",
            error_type=ErrorType.DATASET_REQUIRED,
        )

    if stage2_tasks:
        stage2_results = await asyncio.gather(*stage2_tasks, return_exceptions=True)
        for name, res in zip(stage2_names, stage2_results):
            if isinstance(res, Exception):
                tool_results[name] = ToolResult(
                    success=False,
                    tool_name=name,
                    error=str(res),
                    error_type=ErrorType.INTERNAL_ERROR,
                )
            else:
                tool_results[name] = res  # type: ignore[assignment]  # gather returns BaseException | T
    run.requested_plan = requested_plan


def collect_artifacts(run: RunContext) -> None:
    """Execution artifacts from the tool results, the evidence sources and the registered artifact builders."""
    ao = orchestrator_module()
    ctx, tool_results, execution_artifacts = run.ctx, run.tool_results, run.execution_artifacts
    active_sources, merge_artifact = run.active_sources, run.merge_artifact

    # Collect execution artifacts
    if tool_results.get("vivarix") and tool_results["vivarix"].success and tool_results["vivarix"].data:
        execution_artifacts["vivarix"] = dict(tool_results["vivarix"].data)
        ss_res = tool_results.get("vivarix_steady_state")
        if "vivarix_steady_state" in tool_results:
            execution_artifacts["vivarix"]["role"] = "diagnostic_time_course"
        if ss_res and ss_res.success and isinstance(ss_res.data, dict):
            execution_artifacts["vivarix"]["steady_state"] = ss_res.data
    scan_res = tool_results.get("vivarix_scan")
    if scan_res is not None and isinstance(scan_res.data, dict):
        # Kept even when every point failed: the per-point failures are the result.
        execution_artifacts["vivarix_scan"] = {**scan_res.data, "success": scan_res.success}
    if tool_results.get("lumicell") and tool_results["lumicell"].success and tool_results["lumicell"].data:
        execution_artifacts["lumicell"] = tool_results["lumicell"].data


    successful_sources = [
        s for s in active_sources if tool_results.get(s.key) is not None and tool_results[s.key].success
    ]
    for src in successful_sources:
        try:
            art = src.artifact(tool_results[src.key])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s artifact failed: %s", src.key, exc)
            continue
        if art:
            merge_artifact(art[0], art[1])
    for builder in ao.get_artifact_builders():
        try:
            built = builder(tool_results, ctx) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Artifact builder failed: %s", exc)
            continue
        for key, value in built.items():
            merge_artifact(key, value)
    run.successful_sources = successful_sources


async def prioritize(run: RunContext) -> None:
    """Stage 3: evidence-weighted hypothesis prioritization (BioDiscovery)."""
    ao = orchestrator_module()
    ctx, clean_query, tool_results, execution_artifacts = run.ctx, run.clean_query, run.tool_results, run.execution_artifacts
    emit = run.emit

    # -------------------------------------------------------------
    # STAGE 3: HYPOTHESIS PRIORITIZATION (BioDiscoveryAgent)
    # -------------------------------------------------------------
    try:
        discovery_report = ao.BioDiscoveryEngine.prioritize_hypotheses(
            inquiry=clean_query,
            primary_target=ctx.primary_target,
            tool_results=tool_results,
            target_aliases=ctx.primary_aliases(),
        )
    except Exception as exc:  # noqa: BLE001
        # Prioritization is optional: a malformed tool payload must not end the whole inquiry.
        logger.warning("Hypothesis prioritization failed: %s", exc)
        discovery_report = ao.DiscoveryHypothesisReport(
            inquiry=clean_query,
            primary_target=ctx.primary_target or "Unspecified",
            status="INSUFFICIENT_EVIDENCE",
            iterative_update_summary=f"Prioritization unavailable ({type(exc).__name__}); no scores were generated.",
        )
    execution_artifacts["discovery"] = discovery_report.model_dump()

    if discovery_report.candidates:
        ranked = " · ".join(
            f"{c.target} ({c.composite_priority_score:.2f})" for c in discovery_report.candidates[:3]
        )
        await emit(
            "target_prioritization",
            title=(
                f"Ranked {len(discovery_report.candidates)} candidate "
                f"hypothes{'is' if len(discovery_report.candidates) == 1 else 'es'}"
            ),
            text=f"Evidence-weighted priority: {ranked}",
            agent="BioDiscovery",
            extra={"top_candidate": discovery_report.candidates[0].target},
        )

    run.discovery_report = discovery_report
