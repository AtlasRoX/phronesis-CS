"""
Scientific Validation & Regression Test Suite for Phronesis.

These are local integration and regression tests. They are *inspired by* the task categories of public
benchmarks (BFCL for function calling, STaRK for knowledge retrieval, BixBench for computational biology),
but they do NOT run those benchmarks' datasets or harnesses and produce no benchmark scores.

Coverage:
- Tool registry: tool selection, canonical IDs, schema validation
- Synapta: OpenTargets & KEGG target-disease retrieval (live APIs)
- Vivarix COPASI ODEs & Lumicell AnnData/Scanpy execution
- PaperQA2 & OpenScholar: Query decomposition, citation grounding, missing evidence handling
- Reflexion: Deterministic verification, defect audits, numerical checks
- Mem0: Thread isolation, contradiction resolution, true BM25 retrieval
- Concurrency & Cancellation tests
"""

import asyncio
import os
import time
import pytest
from typing import Dict, Any

from phronesis.tools.registry import (
    BiomedicalToolRegistry,
    ErrorType,
    ToolResult,
    ToolUniverseEntry,
    AdapterType,
    get_tool_registry,
)
from phronesis.utils.biocontext import BioContextMCPClient
from phronesis.vivarix.engine import VivarixSimulationEngine
from phronesis.lumicell.engine import LumicellAnalysisEngine
from phronesis.lithoscribe.synthesis_engine import LiteratureSynthesisEngine
from phronesis.phronesis.agents.verifier import ClinicalVerifier, DefectSeverity
from phronesis.utils.memory import MemoryType, VerificationStatus
from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine
from phronesis.agents.discovery import BioDiscoveryEngine
from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator
from pydantic import BaseModel, Field

from tests.fakes import make_offline_orchestrator


@pytest.fixture
def memory(tmp_path):
    """A private memory vault per test (audit TST-002).

    The process-wide engine used to live in <repo>/.phronesis_memory and these tests left user-scoped rows there
    (cleanup covered only the default user and was skipped when an assertion failed). A fresh store per test is
    hermetic, needs no cleanup, and makes counters such as the contradiction total exact.
    """
    return ClinicalMemoryEngine(storage_dir=str(tmp_path / "memory"))


# =============================================================================
# 1. TOOL REGISTRY, CANONICAL IDENTIFIERS & SCHEMA INTEGRITY (BFCL-inspired)
# =============================================================================

class MockVivarixInput(BaseModel):
    biomodel_id: str = Field(..., pattern=r"^BIOMD\d{10}$")
    duration: float = Field(default=50.0, ge=1.0, le=1000.0)


@pytest.mark.asyncio
async def test_registry_tool_registry_validation_and_rejection():
    """Regression test: verify valid arguments pass and invalid schemas/identifiers are rejected."""
    registry = BiomedicalToolRegistry()

    async def mock_sim(biomodel_id: str, duration: float = 50.0):
        return {"simulated": True, "model": biomodel_id}

    entry = ToolUniverseEntry(
        name="test_vivarix_sim",
        description="Simulate kinetic ODE model",
        domain="vivarix",
        adapter_type=AdapterType.LOCAL_COMPUTE,
        input_schema=MockVivarixInput,
        canonical_id_types=["biomodel_id"],
    )
    registry.register(entry, mock_sim)

    # 1. Valid execution
    res_valid = await registry.execute("test_vivarix_sim", biomodel_id="BIOMD0000000010", duration=60.0)
    assert res_valid.success is True
    assert res_valid.data["simulated"] is True

    # 2. Invalid schema (bad accession format)
    res_invalid_id = await registry.execute("test_vivarix_sim", biomodel_id="INVALID_MODEL_123")
    assert res_invalid_id.success is False
    assert res_invalid_id.error_type == ErrorType.VALIDATION_ERROR

    # 3. Invalid range (duration < 1.0)
    res_invalid_dur = await registry.execute("test_vivarix_sim", biomodel_id="BIOMD0000000010", duration=0.1)
    assert res_invalid_dur.success is False
    assert res_invalid_dur.error_type == ErrorType.VALIDATION_ERROR

    # 4. Unregistered tool
    res_unreg = await registry.execute("non_existent_tool")
    assert res_unreg.success is False
    assert res_unreg.error_type == ErrorType.NOT_FOUND


# =============================================================================
# 2. SYNAPTA KNOWLEDGE RETRIEVAL & ASSOCIATION EVIDENCE (STaRK-inspired)
# =============================================================================

@pytest.mark.network
@pytest.mark.asyncio
async def test_synapta_target_disease_association_retrieval():
    """Regression test: verify target entity resolution and quantified association scoring."""
    client = BioContextMCPClient()

    # Search entity
    search_res = await client.async_query_opentargets_search("KRAS", size=2)
    assert search_res.get("source") == "OpenTargets (Search)"
    assert len(search_res.get("results", [])) > 0
    top_hit = search_res["results"][0]
    assert top_hit["name"] == "KRAS"
    assert top_hit["id"].startswith("ENSG")

    # Retrieve real associations for Ensembl ID
    ensembl_id = top_hit["id"]
    assoc_res = await client.async_query_opentargets_associations(ensembl_id, size=3)
    assert assoc_res.get("source") == "OpenTargets (Associations)"
    assert assoc_res.get("approved_symbol") == "KRAS"
    assocs = assoc_res.get("associations", [])
    assert len(assocs) > 0

    first_assoc = assocs[0]
    assert "disease_name" in first_assoc
    assert "overall_association_score" in first_assoc
    assert 0.0 <= first_assoc["overall_association_score"] <= 1.0
    assert "genetic_association_score" in first_assoc


@pytest.mark.network
@pytest.mark.asyncio
async def test_synapta_ambiguous_entity_handling():
    """Regression test: verify ambiguous or non-existent gene query returns graceful empty state."""
    client = BioContextMCPClient()
    res = await client.async_query_opentargets_associations("ENSG99999999999", size=3)
    assert len(res.get("associations", [])) == 0


# =============================================================================
# 3. VIVARIX COPASI ODE SIMULATION & PARAMETER SCANS (BixBench-inspired)
# =============================================================================

def test_vivarix_ode_simulation(offline_biomd10):
    """Regression test: verify reproducible COPASI LSODA integration and artifact generation."""
    engine = VivarixSimulationEngine()
    res = engine.simulate_time_course("BIOMD0000000010", duration=40.0, intervals=20)

    assert res.success is True
    assert res.tool_name == "vivarix_simulate_time_course"
    assert res.canonical_ids.get("biomodel_id") == "BIOMD0000000010"

    data = res.data
    assert data["model_id"] == "BIOMD0000000010"
    assert "LSODA" in data["solver_method"]
    assert "Mek1" in data["species_list"]
    assert "Erk2-PP" in data["species_list"]
    assert len(data["final_concentrations"]) > 0

    # Numerical consistency check: Erk2-PP should be positive float
    assert data["final_concentrations"]["Erk2-PP"] > 0.0
    assert data["plot_base64_png"] is not None
    assert len(data["plot_base64_png"]) > 100


def test_vivarix_steady_state(offline_biomd10):
    """Regression test: verify COPASI steady-state computation."""
    engine = VivarixSimulationEngine()
    res = engine.compute_steady_state("BIOMD0000000010")
    assert res.success is True
    assert "steady_state_concentrations" in res.data
    assert len(res.data["steady_state_concentrations"]) > 0


def test_vivarix_parameter_scan(offline_biomd10):
    """Regression test: verify parameter scan sensitivity curve."""
    engine = VivarixSimulationEngine()
    res = engine.run_parameter_scan("BIOMD0000000010", parameter_name="V1", start_val=1.0, end_val=5.0, num_steps=3)
    assert res.success is True
    assert len(res.data["scan_results"]) == 3


# =============================================================================
# 4. LUMICELL SINGLE-CELL TRANSCRIPTOMICS ANALYTICS (BixBench-inspired)
# =============================================================================

def test_lumicell_requires_real_dataset_without_fabrication(tmp_path):
    """Regression test: Lumicell must reject unprovided dataset with DATASET_REQUIRED, and only analyze real AnnData files."""
    engine = LumicellAnalysisEngine()

    # 1. Product path without dataset must fail honestly
    res_no_data = engine.execute_single_cell_pipeline(target_gene="KRAS")
    assert res_no_data.success is False
    assert res_no_data.error_type == ErrorType.DATASET_REQUIRED

    # 2. Test-isolated fixture AnnData file with real counts matrix
    import anndata
    import numpy as np
    import pandas as pd

    genes = ["KRAS", "EGFR", "GAPDH", "ACTB", "TP53", "BRAF", "MYC", "CDK4", "MT-CO1", "MT-CO2"]
    np.random.seed(42)
    X = np.random.poisson(lam=3.0, size=(40, len(genes))).astype(np.float32)
    var = pd.DataFrame(index=genes)
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(40)])
    obs["donor_id"] = ["donor_1"] * 20 + ["donor_2"] * 20
    adata = anndata.AnnData(X=X, obs=obs, var=var)
    adata_path = str(tmp_path / "test_tissue_atlas.h5ad")
    adata.write_h5ad(adata_path)

    res_with_data = engine.execute_single_cell_pipeline(dataset_path=adata_path, target_gene="KRAS", max_pct_mt=80.0, n_neighbors=5, resolution=0.5)
    assert res_with_data.success is True
    assert res_with_data.tool_name == "lumicell_analyze_single_cell"
    assert res_with_data.data["dataset_path"] == adata_path
    assert res_with_data.data["n_cells_post_qc"] > 0
    assert "top_markers_by_cluster" in res_with_data.data


# =============================================================================
# 5. PaperQA2 / OpenScholar BENCHMARK: LITERATURE SYNTHESIS & PASSAGE CITATIONS
# =============================================================================

@pytest.mark.network
@pytest.mark.asyncio
async def test_paperqa2_literature_synthesis():
    """PaperQA2 test: verify query decomposition, passage ranking, and DOI/PMID tracking."""
    engine = LiteratureSynthesisEngine()
    res = await engine.execute_literature_synthesis("KRAS G12D inhibitor resistance mechanisms in colorectal cancer", max_articles=4)

    assert res.success is True
    assert res.tool_name == "lithoscribe_literature_synthesis"

    data = res.data
    assert len(data["decomposed_subqueries"]) >= 2
    assert data["total_articles_screened"] > 0
    assert len(data["ranked_passages"]) > 0

    top_psg = data["ranked_passages"][0]
    assert "title" in top_psg
    assert "passage_text" in top_psg
    assert top_psg["experimental_setting"] in ["human-clinical", "in-vivo", "in-vitro", "computational"]
    assert top_psg["pub_type"] in ["clinical-trial", "review", "research-article", "meta-analysis"]


# =============================================================================
# 6. REFLEXION BENCHMARK: DETERMINISTIC DEFECT AUDIT & REJECTION
# =============================================================================

def test_reflexion_verifier_catches_unsupported_conclusion_and_vendor_leak():
    """Reflexion test: verify verifier detects fake positive conclusions and vendor names."""
    verifier = ClinicalVerifier()

    # Tool execution failed
    tool_results = {
        "vivarix": ToolResult(success=False, tool_name="vivarix", error="Solver divergence", error_type=ErrorType.SIMULATION_FAILED),
        "synapta": ToolResult(success=True, tool_name="synapta", canonical_ids={"ensembl_id": "ENSG00000133703"}),
    }

    # Text that asserts unsupported positive conclusion and leaks vendor name
    hallucinated_text = (
        "Cross-domain analysis confirms strong target validation and positive clinical trial progress. "
        "We used LangGraph and OpenAI to run this evaluation on BIOMD9999999999."
    )

    report = verifier.verify_response(
        query="Investigate target",
        response_markdown=hallucinated_text,
        tool_results=tool_results,
    )

    assert report.passed is False
    assert report.verification_score < 0.70

    categories = [d.category for d in report.defects]
    assert "INTEGRITY" in categories  # for positive conclusion when tool failed, and for LangGraph/OpenAI leak
    assert "IDENTIFIER" in categories  # for fake BIOMD9999999999

    feedback = report.to_feedback_prompt()
    assert "REFLEXION VERIFICATION AUDIT FAILED" in feedback
    assert report.failure_lesson is not None


def test_reflexion_verifier_passes_grounded_response():
    """Reflexion test: verify properly grounded scientific text passes."""
    verifier = ClinicalVerifier()
    tool_results = {
        "vivarix": ToolResult(
            success=True,
            tool_name="vivarix",
            canonical_ids={"biomodel_id": "BIOMD0000000010"},
            data={"final_concentrations": {"Erk2-PP": 7.46}},
        ),
        "synapta": ToolResult(success=True, tool_name="synapta", canonical_ids={"ensembl_id": "ENSG00000133703"}),
    }

    grounded_text = (
        "Target evaluation via Synapta maps KRAS (ENSG00000133703) to downstream cascades. "
        "Dynamic kinetic ODE simulation using Vivarix on BIOMD0000000010 reaches Erk2-PP steady state at 7.46 nM."
    )

    report = verifier.verify_response(
        query="Investigate KRAS and Erk2-PP kinetics",
        response_markdown=grounded_text,
        tool_results=tool_results,
    )
    assert report.passed is True
    assert report.verification_score >= 0.70
    assert "BIOMD0000000010" in report.verified_identifiers


# =============================================================================
# 7. Mem0 BENCHMARK: THREAD ISOLATION, CONTRADICTION RESOLUTION & BM25
# =============================================================================

@pytest.mark.asyncio
async def test_mem0_thread_isolation_and_bm25_retrieval(memory):
    """Mem0 test: verify strict thread isolation and true probabilistic BM25 search."""
    th_a = "thread_eval_alpha"
    th_b = "thread_eval_beta"

    # Consolidate turn in Thread A
    await memory.consolidate_turn(
        thread_id=th_a,
        query="Evaluate KRAS G12D mutation",
        response="KRAS G12D is an oncogenic driver in PDAC.",
        tool_results={"synapta": "KRAS G12D interacts with PIK3CA and BRAF"},
    )

    # Consolidate turn in Thread B
    await memory.consolidate_turn(
        thread_id=th_b,
        query="Evaluate HER2 amplification in breast cancer",
        response="HER2 is a receptor tyrosine kinase.",
        tool_results={"synapta": "HER2 heterodimerizes with HER3"},
    )

    # 1. Thread A search should retrieve KRAS but NOT HER2
    search_a = memory.search_memory("KRAS G12D", thread_id=th_a)
    assert len(search_a) > 0
    assert any("KRAS" in s["content"] for s in search_a)
    assert not any("HER2" in s["content"] for s in search_a)

    # 2. Thread B search should retrieve HER2 but NOT KRAS
    search_b = memory.search_memory("HER2 receptor", thread_id=th_b)
    assert len(search_b) > 0
    assert any("HER2" in s["content"] for s in search_b)
    assert not any("KRAS G12D" in s["content"] for s in search_b)

    # 3. Clean up
    memory.clear_thread_memory(th_a)
    memory.clear_thread_memory(th_b)


@pytest.mark.asyncio
async def test_mem0_contradiction_detection(memory):
    """Mem0 test: verify opposing assertions trigger contradiction demotion to REFUTED."""
    th_contra = "thread_contra_test"

    # 1. Record provisional finding claiming sensitivity
    await memory.consolidate_turn(
        thread_id=th_contra,
        query="Is KRAS sensitive to monotherapy?",
        response="Draft assessment",
        tool_results={"pilot_study": "Tumor model is sensitive to KRAS switch-II monotherapy"},
    )

    # 2. Record empirical finding showing resistance
    await memory.consolidate_turn(
        thread_id=th_contra,
        query="Check persistent resistance",
        response="Second draft",
        tool_results={"vivarix": "Tumor model is resistant to KRAS switch-II monotherapy through SOS1 rebound"},
    )

    # Verify contradiction was caught and logged. The store is private to this test, so the count is exact
    # (it used to be ">= 1" on a shared workspace store, which earlier runs had already satisfied).
    stats = memory.get_memory_stats()
    assert stats["total_contradictions_resolved"] == 1

    # ...and the prior "sensitive" finding was demoted to REFUTED, as the docstring claims.
    import sqlite3

    with sqlite3.connect(memory.db_path) as conn:
        rows = conn.execute(
            "SELECT content, verification_status, is_contradicted FROM memory_nodes WHERE thread_id = ?",
            (th_contra,),
        ).fetchall()
    refuted = [r for r in rows if r[1] == VerificationStatus.REFUTED.value]
    assert len(refuted) == 1 and refuted[0][2] == 1
    assert "sensitive to KRAS switch-II monotherapy" in refuted[0][0]

    memory.clear_thread_memory(th_contra)


# =============================================================================
# 8. BioDiscoveryAgent BENCHMARK: HYPOTHESIS PRIORITIZATION
# =============================================================================

def test_biodiscovery_hypothesis_prioritization():
    """BioDiscoveryAgent test: verify multi-factorial candidate prioritization and experimental proposals."""
    tool_results = {
        "synapta": ToolResult(
            success=True,
            tool_name="synapta",
            data={"associations": [{"disease_name": "PDAC", "overall_association_score": 0.88}]},
        )
    }

    report = BioDiscoveryEngine.prioritize_hypotheses(
        inquiry="Overcome adaptive resistance",
        primary_target="KRAS",
        tool_results=tool_results,
    )

    assert len(report.candidates) >= 1
    top = report.candidates[0]
    assert top.rank == 1
    assert 0.0 <= top.composite_priority_score <= 1.0
    assert "[PREDICTION:" in top.prediction_label
    assert len(top.proposed_validation_experiment) > 20
    assert "Western blot" in top.proposed_validation_experiment or "synergy screen" in top.proposed_validation_experiment


# =============================================================================
# 9. END-TO-END ORCHESTRATION & CANCELLATION BENCHMARK
# =============================================================================

@pytest.mark.network
@pytest.mark.asyncio
async def test_authoritative_orchestrator_end_to_end():
    """Verify authoritative orchestrator executes Stage 1 through Stage 6 with real artifacts and honest dataset state."""
    orch = AuthoritativeOrchestrator()
    th_id = f"test_e2e_{int(time.time())}"

    res = await orch.execute_inquiry(
        # Explicit simulation intent: naming a gene alone no longer dispatches Vivarix (AUDIT FINDING-02).
        query="Analyze the mechanistic rationale of combining KRAS G12D inhibition with EGFR targeted therapy to prevent feedback reactivations, and simulate the downstream kinetic response.",
        thread_id=th_id,
        enable_web_search=False,
    )

    assert len(res.response) > 100
    assert len(res.thoughts) >= 5
    assert "Synapta" in res.selected_agents
    assert "Vivarix" in res.selected_agents
    assert "Lithoscribe" in res.selected_agents
    assert res.verification_report is not None
    assert "vivarix" in res.execution_artifacts
    # Lumicell honestly returned DATASET_REQUIRED because no real .h5ad was provided
    assert "lumicell" not in res.execution_artifacts

    # Clean up
    orch.memory.clear_thread_memory(th_id)


@pytest.mark.network
@pytest.mark.asyncio
async def test_cancellation_resilience():
    """Verify task cancellation terminates coroutines cleanly without raising uncaught exceptions."""
    orch = AuthoritativeOrchestrator()
    th_id = "test_cancel_thread"

    task = asyncio.create_task(
        orch.execute_inquiry(
            query="Analyze long simulation",
            thread_id=th_id,
        )
    )

    # Let it start for 100ms then cancel
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# =============================================================================
# 10. SCIENTIFIC FIDELITY & REGRESSION TEST SUITE
# =============================================================================

def test_regression_unsupported_clinical_claim_fails_verification():
    """Regression test: Unsupported clinical assertions without evidence must fail with score < 0.50 (NOT 1.00)."""
    verifier = ClinicalVerifier()
    # No literature tool evidence
    tool_results = {
        "synapta": ToolResult(success=True, tool_name="synapta", canonical_ids={"target_symbol": "KRAS"}),
    }
    response_text = (
        "Phase 3 clinical trial readouts confirm 92% overall survival and curative efficacy of this combination therapy in patient cohorts."
    )
    report = verifier.verify_response(
        query="Evaluate curative efficacy",
        response_markdown=response_text,
        tool_results=tool_results,
    )
    assert report.passed is False
    assert report.verification_score < 0.50
    defect_categories = [d.category for d in report.defects]
    assert "CLINICAL_CLAIM" in defect_categories


def test_regression_numerical_mismatch_14_92_vs_7_46_fails():
    """Regression test: 14.92 concentration must NOT pass when observed result was 7.46 (5% tolerance enforced)."""
    verifier = ClinicalVerifier()
    tool_results = {
        "vivarix": ToolResult(
            success=True,
            tool_name="vivarix",
            canonical_ids={"biomodel_id": "BIOMD0000000010"},
            data={"final_concentrations": {"Erk2-PP": 7.46}},
        ),
    }
    response_text = (
        "Kinetic ODE integration of BIOMD0000000010 establishes that steady state Erk2-PP reaches 14.92 nM."
    )
    report = verifier.verify_response(
        query="Check Erk2-PP steady state",
        response_markdown=response_text,
        tool_results=tool_results,
    )
    assert report.passed is False
    assert report.verification_score < 0.70
    defect_categories = [d.category for d in report.defects]
    assert "NUMERICAL" in defect_categories
    assert any("14.92" in d.description for d in report.defects)


def test_regression_invented_doi_and_pmid_fails():
    """Regression test: Unobserved DOI or PMID must trigger CITATION defect and fail verification."""
    verifier = ClinicalVerifier()
    tool_results = {
        "lithoscribe": ToolResult(
            success=True,
            tool_name="lithoscribe",
            data={"observed_citations": ["10.1038/s41586-020-2923-0", "32451500"]},
        ),
    }
    response_text = (
        "According to recent findings in DOI: 10.9999/fake.journal.2024 and PMID: 99999999, resistance is overcome."
    )
    report = verifier.verify_response(
        query="Check resistance literature",
        response_markdown=response_text,
        tool_results=tool_results,
    )
    assert report.passed is False
    defect_categories = [d.category for d in report.defects]
    assert "CITATION" in defect_categories


def test_regression_evidence_free_discovery_disabled():
    """Regression test: Zero tool evidence must return INSUFFICIENT_EVIDENCE with empty candidate ranking (NOT 0.87)."""
    report = BioDiscoveryEngine.prioritize_hypotheses(
        inquiry="Propose combination therapy",
        primary_target="KRAS",
        tool_results={},  # Zero tool evidence
    )
    assert report.status == "INSUFFICIENT_EVIDENCE"
    assert len(report.candidates) == 0


@pytest.mark.network
@pytest.mark.asyncio
async def test_regression_unrelated_query_no_kras_default():
    """Regression test: Questions about photosynthesis must NOT trigger KRAS lookup or Vivarix/Lumicell."""
    orch = AuthoritativeOrchestrator()
    th_id = f"test_photo_{int(time.time())}"

    res = await orch.execute_inquiry(
        query="What are the primary enzymes involved in the light-independent reactions of photosynthesis?",
        thread_id=th_id,
        enable_web_search=False,
    )

    # Must NOT default to KRAS or execute Vivarix/Lumicell
    assert "KRAS" not in res.selected_agents
    assert "Vivarix" not in res.selected_agents
    assert "Lumicell" not in res.selected_agents
    assert "vivarix" not in res.execution_artifacts
    assert "lumicell" not in res.execution_artifacts
    assert "kras" not in res.response.lower()

    orch.memory.clear_thread_memory(th_id)


@pytest.mark.asyncio
async def test_regression_conversational_greeting_no_tools(monkeypatch, tmp_path):
    """Regression test: Casual greetings must not launch wet-lab or computational agents."""
    # Offline orchestrator (audit TST-008): the production registry made this "offline" test resolve entities
    # against Open Targets. The recording registry has the real tool schemas, records every call, and has no
    # resolver; the LLM is disabled and memory lives under tmp_path.
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    th_id = f"test_greet_{int(time.time())}"

    res = await orch.execute_inquiry(
        query="Hello! Who are you and what can you do?",
        thread_id=th_id,
        enable_web_search=False,
    )

    assert res.framework_used == "conversational-direct"
    assert len(res.selected_agents) == 0
    assert calls == []  # no tool ran
    assert "Phronesis" in res.response
    assert res.verification_report["passed"] is True

    orch.memory.clear_thread_memory(th_id)


@pytest.mark.asyncio
async def test_regression_cross_user_memory_isolation(memory):
    """Regression test: Memory search by user_id must NOT expose another user's investigation findings."""
    th_id = "shared_thread_isolation_check"

    await memory.consolidate_turn(
        thread_id=th_id,
        query="User A query on target EGFR",
        response="EGFR assessment for User A",
        tool_results={"synapta": "EGFR sensitivity data"},
        user_id="user_alpha",
    )

    await memory.consolidate_turn(
        thread_id=th_id,
        query="User B query on target BRAF",
        response="BRAF assessment for User B",
        tool_results={"synapta": "BRAF V600E mutation findings"},
        user_id="user_beta",
    )

    # User Alpha search must NOT see User Beta
    search_alpha = memory.search_memory("BRAF V600E", thread_id=th_id, user_id="user_alpha")
    assert not any("BRAF" in s["content"] for s in search_alpha)

    # User Beta search must see BRAF
    search_beta = memory.search_memory("BRAF V600E", thread_id=th_id, user_id="user_beta")
    assert any("BRAF" in s["content"] for s in search_beta)

    memory.clear_thread_memory(th_id)


@pytest.mark.asyncio
async def test_regression_provisional_memory_not_in_verified_context(memory):
    """Regression test: Provisional tool findings must stay out of verified_evidence context."""
    th_id = "test_provisional_context_filter"

    # Consolidate turn with is_turn_verified=False
    await memory.consolidate_turn(
        thread_id=th_id,
        query="Investigate provisional target",
        response="Unverified synthesis",
        tool_results={"synapta": "Provisional association for novel target"},
        is_turn_verified=False,
    )

    pkg = memory.get_context_package(th_id)
    # Must NOT enter verified_evidence
    assert len(pkg.verified_evidence) == 0
    # Must enter active_hypotheses as provisional
    assert len(pkg.active_hypotheses) > 0
    assert any("[Provisional Literature]" in h for h in pkg.active_hypotheses)

    memory.clear_thread_memory(th_id)


def test_regression_all_failed_parameter_scan_fails(offline_biomd10):
    """An unknown scan parameter is rejected once, before any point runs (typed VALIDATION_ERROR, no partial run)."""
    engine = VivarixSimulationEngine()
    # Scanning a nonexistent parameter on BIOMD0000000010
    res = engine.run_parameter_scan(
        biomodel_id="BIOMD0000000010",
        parameter_name="NONEXISTENT_PARAMETER_XYZ",
        start_val=1.0,
        end_val=5.0,
        num_steps=3,
    )
    assert res.success is False
    assert res.error_type == ErrorType.VALIDATION_ERROR
    assert res.metadata["validation_code"] == "UNKNOWN_PARAMETER"
    assert "not found" in res.error.lower()


def test_regression_invalid_parameter_override_fails(offline_biomd10):
    """Regression test: Invalid parameter override must fail simulation explicitly, not silently ignore it."""
    engine = VivarixSimulationEngine()
    res = engine.simulate_time_course(
        biomodel_id="BIOMD0000000010",
        duration=20.0,
        intervals=10,
        parameter_overrides={"COMPLETELY_INVALID_PARAM": 99.0},
    )
    assert res.success is False
    assert res.error_type == ErrorType.VALIDATION_ERROR
    assert res.metadata["validation_code"] == "UNKNOWN_PARAMETER"
    assert "invalid parameter override" in res.error.lower()
