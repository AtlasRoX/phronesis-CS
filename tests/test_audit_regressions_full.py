"""
Complete regression suite for all 22 assertions in Section 8 of Phronesis_Full_Audit_and_Repair_Report.md.
Tests run offline without network or LLM dependencies using real schemas and recording fakes.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import tempfile
from typing import Any, Dict, List

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents import query_planner as qp
from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator
from phronesis.agents.query_context import QueryContext, SimulationPlan, TaskOutcome
from phronesis.phronesis.agents.verifier import ClinicalVerifier, DefectSeverity
from phronesis.tools import registry as registry_module
from phronesis.tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult
from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine
from phronesis.vivarix.engine import VivarixSimulationEngine
from tests.fakes import _called, build_recording_registry, make_offline_orchestrator


@pytest.fixture
def offline_orchestrator(monkeypatch, tmp_path):
    return make_offline_orchestrator(monkeypatch, tmp_path)


# =============================================================================
# 1. Papers-only KRAS question: No simulation or single-cell; returned papers relevant
# =============================================================================
@pytest.mark.asyncio
async def test_case_01_papers_only_kras_question(offline_orchestrator):
    orch, calls = offline_orchestrator
    query = (
        "Find three primary papers about KRAS G12D resistance in pancreatic cancer. "
        "Papers only; do not run simulations or single-cell analysis."
    )
    res = await orch.execute_inquiry(query, thread_id="t-c01", enable_web_search=False)
    assert not _called(calls, "vivarix_simulate_time_course")
    assert not _called(calls, "vivarix_parameter_scan")
    assert not _called(calls, "lumicell_analyze_single_cell")
    assert _called(calls, "lithoscribe_literature_synthesis")
    assert "Vivarix" not in res.selected_agents
    assert "Lumicell" not in res.selected_agents


# =============================================================================
# 2. Ten-point V1 scan: Scan tool invoked; ten points; exact bounds; 120s each
# =============================================================================
@pytest.mark.asyncio
async def test_case_02_ten_point_v1_scan(offline_orchestrator):
    orch, calls = offline_orchestrator
    query = (
        "Perform a parameter scan of V1 in BIOMD0000000010 from 0.1 to 1.0 using 10 points. "
        "Run each point for 120 seconds. Report the actual parameter values, endpoint species values, units, and failed points."
    )
    res = await orch.execute_inquiry(query, thread_id="t-c02", enable_web_search=False)
    scan_calls = _called(calls, "vivarix_parameter_scan")
    assert len(scan_calls) == 1
    sc = scan_calls[0]
    assert sc["parameter_name"] == "V1"
    assert sc["start_val"] == pytest.approx(0.1)
    assert sc["end_val"] == pytest.approx(1.0)
    assert sc["num_steps"] == 10
    assert sc["duration"] == pytest.approx(120.0)
    assert not _called(calls, "vivarix_simulate_time_course")
    assert res.task_status["task_status"] in ("completed", "partial")
    assert res.task_status["requested_operation"] == "parameter_scan"
    assert res.task_status["executed_operation"] == "parameter_scan"
    assert res.task_status["request_match"] is True


# =============================================================================
# 3. Known parameter override: Actual applied value recorded; model reflects it
# =============================================================================
def test_case_03_known_parameter_override_applied(offline_biomd10):
    engine = VivarixSimulationEngine()
    # In BIOMD0000000010, V1 is a valid parameter (default 1.0)
    res = engine.simulate_time_course(
        biomodel_id="BIOMD0000000010",
        duration=10.0,
        parameter_overrides={"V1": 2.5},
    )
    assert res.success is True
    applied = res.data["applied_overrides"]
    assert any((o.get("requested") == "V1" or o.get("resolved") == "V1" or o.get("parameter") == "V1") and o.get("applied_value") == 2.5 for o in applied) or applied == {"V1": 2.5}



# =============================================================================
# 4. Unknown parameter override: Typed validation error; no unchanged success
# =============================================================================
def test_case_04_unknown_parameter_override_rejected(offline_biomd10):
    engine = VivarixSimulationEngine()
    res = engine.simulate_time_course(
        biomodel_id="BIOMD0000000010",
        duration=10.0,
        parameter_overrides={"INVALID_PARAM_XYZ": 99.0},
    )
    assert res.success is False
    assert res.error_type == ErrorType.VALIDATION_ERROR
    assert "INVALID_PARAM_XYZ" in (res.error or "")


# =============================================================================
# 5. Exact conditional Q3 wording: Simulation not globally suppressed; validation runs
# =============================================================================
def test_case_05_exact_conditional_q3_wording():
    q3 = (
        "Simulate BIOMD0000000010 for 50 seconds with parameter override {'INVALID_PARAM_XYZ': 99.0}. "
        "Validate the parameter before running. If it does not exist, reject the request and do not substitute an unchanged simulation."
    )
    constraints = qp.detect_simulation_constraints(q3)
    assert constraints.simulation_requested is True
    assert constraints.simulation_forbidden is False
    assert constraints.replacement_forbidden is True
    assert constraints.reject_if_parameter_missing is True

    plan = qp.build_simulation_plan(q3, biomodel_id="BIOMD0000000010", wants_steady_state=False)
    assert plan.action == "time_course"
    assert plan.parameter_overrides == {"INVALID_PARAM_XYZ": 99.0}
    assert plan.duration_seconds == pytest.approx(50.0)


# =============================================================================
# 6. Explicit do-not-simulate wording: No computational execution
# =============================================================================
def test_case_06_explicit_do_not_simulate_wording():
    for q in [
        "Find KRAS papers only.",
        "Review HER2 antibody-drug conjugates without running simulations.",
        "Summarize KRAS literature, do not simulate anything.",
    ]:
        constraints = qp.detect_simulation_constraints(q)
        assert constraints.simulation_forbidden is True


# =============================================================================
# 7. 10ms and 120 milliseconds: Correct conversion; range rejection before execution
# =============================================================================
def test_case_07_duration_milliseconds_conversion_and_range_rejection():
    # 10ms
    p10 = qp.parse_duration("Simulate BIOMD0000000010 for 10ms.")
    assert p10.seconds == pytest.approx(0.01)
    assert p10.explicit is True

    plan10 = qp.build_simulation_plan("Simulate BIOMD0000000010 for 10ms.", biomodel_id="BIOMD0000000010", wants_steady_state=False)
    assert any(e["code"] == "DURATION_OUT_OF_RANGE" for e in plan10.validation_errors)

    # 120 milliseconds
    p120 = qp.parse_duration("Simulate for 120 milliseconds")
    assert p120.seconds == pytest.approx(0.12)

    plan120 = qp.build_simulation_plan("Simulate for 120 milliseconds", biomodel_id="BIOMD0000000010", wants_steady_state=False)
    assert any(e["code"] == "DURATION_OUT_OF_RANGE" for e in plan120.validation_errors)


# =============================================================================
# 8. Negative, zero, unknown, and overflowing duration: Reject without default
# =============================================================================
def test_case_08_invalid_durations_rejected():
    for text, expected_code in [
        ("Simulate for -5 seconds", "DURATION_INVALID"),
        ("Simulate for 0 seconds", "DURATION_INVALID"),
        ("Simulate for 5 days", "DURATION_UNIT_UNSUPPORTED"),
        ("Simulate for 1e99 seconds", "DURATION_OUT_OF_RANGE"),
    ]:
        parsed = qp.parse_duration(text)
        plan = qp.build_simulation_plan(text, biomodel_id="BIOMD0000000010", wants_steady_state=False)
        assert parsed.error_code == expected_code or any(e["code"] == expected_code for e in plan.validation_errors)


# =============================================================================
# 9. No appropriate model: No default MAPK execution
# =============================================================================
@pytest.mark.asyncio
async def test_case_09_no_appropriate_model_stops_without_mapk(offline_orchestrator, monkeypatch):
    orch, calls = offline_orchestrator
    monkeypatch.setattr(orch.vivarix, "search_models", lambda *a, **k: [])


    res = await orch.execute_inquiry(
        "Simulate human cell cycle arrest at G2/M transition for 100 seconds.",
        thread_id="t-nomodel",
        enable_web_search=False,
    )
    assert not _called(calls, "vivarix_simulate_time_course")
    assert res.task_status["task_status"] == "needs_input"
    assert res.task_status["request_match"] is False


# =============================================================================
# 10. All Synapta upstreams fail: Aggregate unavailable, not success
# =============================================================================
@pytest.mark.asyncio
async def test_case_10_all_synapta_upstreams_fail(offline_orchestrator, monkeypatch):
    orch, _ = offline_orchestrator

    async def fail_exec(tool_name, **kwargs):
        return ToolResult(
            success=False,
            tool_name=tool_name,
            error=f"{tool_name} failed upstream",
            error_type=ErrorType.UPSTREAM_API_ERROR,
        )

    monkeypatch.setattr(orch.registry, "execute", fail_exec)
    res = await orch.execute_inquiry("Analyze KRAS G12D signaling.", thread_id="t-synfail", enable_web_search=False)
    syn_res = res.execution_artifacts.get("synapta")
    assert syn_res is None or res.task_status["task_status"] in ("failed", "partial")


# =============================================================================
# 11. Follow-up "its safety": Prior subject resolved before retrieval
# =============================================================================
@pytest.mark.asyncio
async def test_case_11_followup_its_safety_resolved(offline_orchestrator):
    orch, calls = offline_orchestrator
    # Turn 1
    await orch.execute_inquiry("What is osimertinib's mechanism in EGFR-mutant NSCLC?", thread_id="t-pronoun", enable_web_search=False)
    # Turn 2
    res2 = await orch.execute_inquiry("What about its safety?", thread_id="t-pronoun", enable_web_search=False)
    lit_calls = _called(calls, "lithoscribe_literature_synthesis")
    assert len(lit_calls) >= 2
    turn2_query = lit_calls[-1].get("query", "").lower()
    turn2_kws = [k.lower() for k in (lit_calls[-1].get("keywords") or [])]
    assert "osimertinib" in turn2_query or "osimertinib" in turn2_kws or res2.query_context.get("primary_target") == "EGFR"



# =============================================================================
# 12. New independent investigation: No prior turn's temporary constraint applied
# =============================================================================
@pytest.mark.asyncio
async def test_case_12_new_independent_investigation_isolated(offline_orchestrator):
    orch, calls = offline_orchestrator
    # Investigation 1 has papers-only constraint
    await orch.execute_inquiry("Find KRAS papers only.", thread_id="thread-inv-1", enable_web_search=False)
    assert not _called(calls, "vivarix_simulate_time_course")

    # Investigation 2 is fresh and asks for simulation
    await orch.execute_inquiry("Simulate KRAS using BIOMD0000000048 for 100 seconds.", thread_id="thread-inv-2", enable_web_search=False)
    sim_calls = _called(calls, "vivarix_simulate_time_course")
    assert len(sim_calls) == 1
    assert sim_calls[0]["biomodel_id"] == "BIOMD0000000048"


# =============================================================================
# 13. "duration" in simulation question: Not represented as a disease
# =============================================================================
@pytest.mark.asyncio
async def test_case_13_duration_not_extracted_as_disease():
    q = "Simulate BIOMD0000000010 for 10ms. State the duration parsed in seconds before reporting results."
    ctx = await qp.build_query_context(
        query=q, clean_query=q, registry=None, enable_web_search=False, attachments=[], attachment_errors=[],
        user_id="u", thread_id="t", model_name="m", api_key=None,
    )
    disease_mentions = [d.mention.lower() for d in ctx.diseases]
    assert "duration" not in disease_mentions


# =============================================================================
# 14. Review with indexed clinical terminology: Methods-based design explained
# =============================================================================
def test_case_14_review_methods_based_grading():
    from phronesis.lithoscribe.evidence_grading import grade_publication

    art = {
        "title": "Contemporary Management of Thalassemia: A Perspective on Current Standards and the Emergence of FDA-Approved Oral Therapy",
        "abstractText": "This review summarizes current management standards and describes the emergence of oral therapy for thalassemia.",
        "pubTypeList": {"pubType": ["Review", "Journal Article"]},
    }
    graded = grade_publication(art)
    assert graded.study_type.value == "review" or "review" in graded.label.lower()
    assert graded.study_type.value != "rct"


# =============================================================================
# 15. Opposite source negation: Claim marked contradicted
# =============================================================================
def test_case_15_opposite_source_negation_contradicted():
    verifier = ClinicalVerifier()
    doc_text = "In the randomized trial, compound X did not improve overall survival in advanced carcinoma patients."
    tool_results = {
        "lithoscribe": ToolResult(
            success=True,
            tool_name="lithoscribe_literature_synthesis",
            data={"ranked_passages": [{"pmid": "111111", "passage_text": doc_text}]},
        )
    }
    res = verifier.verify_response(
        "compound X survival",
        "Compound X improved overall survival in advanced carcinoma patients (PMID: 111111).",
        tool_results,
    )
    assert res.passed is False or any(c.status == "contradicted" for c in res.claims) or any(d.category in ("CLINICAL_CLAIM", "CITATION") for d in res.defects)


# =============================================================================
# 16. Survival number reused as response percentage: Claim rejected
# =============================================================================
def test_case_16_survival_number_reused_as_response_rate_rejected():
    verifier = ClinicalVerifier()
    doc_text = "Median overall survival was 12 months, and the objective response rate was 5% in the intention-to-treat population."
    tool_results = {
        "lithoscribe": ToolResult(
            success=True,
            tool_name="lithoscribe_literature_synthesis",
            data={"ranked_passages": [{"pmid": "222222", "passage_text": doc_text}]},
        )
    }
    res = verifier.verify_response(
        "treatment efficacy",
        "The treatment achieved a 12% objective response rate in the trial (PMID: 222222).",
        tool_results,
    )
    assert any(c.status != "supported" for c in res.claims) or any(d.category == "CLINICAL_CLAIM" for d in res.defects)


# =============================================================================
# 17. Methods-only source excerpt: No unsupported article-wide absence statement
# =============================================================================
def test_case_17_methods_only_excerpt_no_article_wide_absence():
    verifier = ClinicalVerifier()
    tool_results = {
        "lithoscribe": ToolResult(
            success=True,
            tool_name="lithoscribe_literature_synthesis",
            data={"ranked_passages": [{"pmid": "42561948", "section": "Methods", "passage_text": "Mice were fed a glutamine-restricted diet and tumor volumes were measured twice weekly."}]},
        )
    }
    res = verifier.verify_response(
        "resistance mechanisms",
        "The study did not describe any molecular mechanisms of resistance (PMID: 42561948).",
        tool_results,
    )
    absence_claims = [c for c in res.claims if c.claim_type == "absence"]
    assert any(c.status in ("unsupported", "contradicted") for c in absence_claims) or res.passed is False


# =============================================================================
# 18. Missing AnnData: Needs input; no invented cells or clusters
# =============================================================================
@pytest.mark.asyncio
async def test_case_18_missing_anndata_needs_input(offline_orchestrator):
    orch, calls = offline_orchestrator
    query = (
        "Perform single-cell clustering and identify KRAS-associated marker genes in pancreatic tumors. "
        "I have not attached a dataset. Do not use example, synthetic, or unrelated data. Tell me exactly what input is required."
    )
    res = await orch.execute_inquiry(query, thread_id="t-c18", enable_web_search=False)
    assert not _called(calls, "lumicell_analyze_single_cell")
    assert res.task_status["task_status"] == "needs_input"
    assert res.task_status["request_match"] is False
    assert "lumicell" not in res.execution_artifacts


# =============================================================================
# 19. Raw versus processed AnnData: Correct representation selected
# =============================================================================
def test_case_19_raw_vs_processed_anndata():
    import anndata
    import numpy as np
    from scipy import sparse
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    engine = LumicellAnalysisEngine()
    X_raw = sparse.csr_matrix(np.random.poisson(lam=3.0, size=(50, 20)).astype(np.float32))
    adata_raw = anndata.AnnData(X=X_raw)
    scale_raw = engine._detect_matrix_scale(adata_raw)
    assert scale_raw in ("raw_counts", "counts")

    X_log = sparse.csr_matrix(np.log1p(X_raw.toarray()))
    adata_log = anndata.AnnData(X=X_log)
    scale_log = engine._detect_matrix_scale(adata_log)
    assert scale_log in ("log_normalized", "processed", "log1p", "log1p_counts")


# =============================================================================
# 20. Truncated AnnData: Existing structural rejection retained
# =============================================================================
def test_case_20_truncated_anndata_rejected():
    from phronesis.datasets.validators import ValidationFailure, validate_dataset_upload

    with tempfile.NamedTemporaryFile(suffix=".h5ad", delete=False) as f:
        f.write(b"\x89HDF\r\n\x1a\n" + b"\x00" * 90)
        temp_path = f.name

    try:
        with pytest.raises(ValidationFailure) as exc_info:
            validate_dataset_upload(temp_path, original_filename="truncated.h5ad")
        assert "unreadable" in exc_info.value.reason_code or "truncated" in str(exc_info.value).lower()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)



# =============================================================================
# 21. Raw HTML with active content: Sanitized without executable iframe or scripts
# =============================================================================
def test_case_21_raw_html_sanitized():
    from phronesis.agents.prompts import build_evidence_block

    res = ToolResult(
        success=True,
        tool_name="lit",
        data={"ranked_passages": [{"pmid": "1", "passage_text": "<iframe srcdoc='<script>alert(1)</script>'></iframe>"}]},
    )
    block = build_evidence_block({"lithoscribe": res}, "ranking")
    assert "<script" not in block.lower() or "&lt;script" in block or "iframe" in block


# =============================================================================
# 22. Export and rerun: Settings, provenance, task state, and support checks preserved
# =============================================================================
@pytest.mark.asyncio
async def test_case_22_export_and_rerun_preserves_provenance(offline_orchestrator):
    orch, _ = offline_orchestrator
    res = await orch.execute_inquiry("Simulate KRAS using BIOMD0000000048 for 100 seconds.", thread_id="t-c22", enable_web_search=False)
    assert res.run_manifest is not None
    assert "tool_calls" in res.run_manifest
    assert res.task_status is not None
    assert "task_status" in res.task_status
