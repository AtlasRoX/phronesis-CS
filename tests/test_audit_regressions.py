"""
Offline regression tests for the defects reported in AUDIT_REPORT.md / AUDIT_FINDINGS.json.
No network or LLM access is required: tool executors and the LLM are replaced with recording fakes,
while the real tool input schemas, verifier, discovery engine, and memory engine are exercised.
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Tuple

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator
from phronesis.agents.discovery import BioDiscoveryEngine
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools import registry as registry_module
from phronesis.tools.registry import (
    AdapterType,
    BiomedicalToolRegistry,
    ErrorType,
    ToolResult,
    ToolUniverseEntry,
)
from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine


# =============================================================================
# Fakes (shared with the feature tests in tests/fakes.py; re-exported for older imports)
# =============================================================================

from tests.fakes import _TOOL_SCHEMAS, _fake_output, build_recording_registry, make_offline_orchestrator  # noqa: E402,F401


@pytest.fixture
def offline_orchestrator(monkeypatch, tmp_path):
    return make_offline_orchestrator(monkeypatch, tmp_path)


def _called(calls, tool_name):
    return [kw for name, kw in calls if name == tool_name]


# =============================================================================
# FINDING-01 / FINDING-02: routing
# =============================================================================

@pytest.mark.asyncio
async def test_single_cell_runs_without_target_gene(offline_orchestrator):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry("Analyze this uploaded test_atlas.h5ad dataset.", thread_id="t-sc", enable_web_search=False)
    assert _called(calls, "lumicell_analyze_single_cell") == [{"dataset_path": "test_atlas.h5ad", "target_gene": None, "dataset_name": None, "max_pct_mt": 20.0, "n_neighbors": None, "resolution": 0.5}]
    assert "Lumicell" in res.selected_agents
    assert not _called(calls, "vivarix_simulate_time_course")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Find KRAS papers only.",
        "Review recent Phase 2 clinical trials for HER2 antibody-drug conjugates in breast cancer without running simulations.",
        "Summarize KRAS literature, do not simulate anything.",
    ],
)
async def test_literature_only_requests_skip_simulation(offline_orchestrator, query):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry(query, thread_id="t-lit", enable_web_search=False)
    assert not _called(calls, "vivarix_simulate_time_course")
    assert "Vivarix" not in res.selected_agents
    assert _called(calls, "lithoscribe_literature_synthesis")


@pytest.mark.asyncio
async def test_gene_mention_alone_does_not_trigger_simulation(offline_orchestrator):
    orch, calls = offline_orchestrator
    await orch.execute_inquiry("Analyze KRAS G12D signaling and downstream targets.", thread_id="t-gene", enable_web_search=False)
    assert not _called(calls, "vivarix_simulate_time_course")


@pytest.mark.asyncio
async def test_explicit_simulation_request_still_runs(offline_orchestrator):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry("Simulate KRAS using BIOMD0000000048 for 100 seconds.", thread_id="t-sim", enable_web_search=False)
    sims = _called(calls, "vivarix_simulate_time_course")
    assert sims and sims[0]["biomodel_id"] == "BIOMD0000000048" and sims[0]["duration"] == 100.0
    assert "Vivarix" in res.selected_agents
    assert not _called(calls, "vivarix_compute_steady_state")


@pytest.mark.asyncio
async def test_steady_state_request_runs_root_finding(offline_orchestrator):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry("Compute the steady state of BIOMD0000000010.", thread_id="t-ss", enable_web_search=False)
    assert _called(calls, "vivarix_compute_steady_state")
    assert res.execution_artifacts["vivarix"]["steady_state"]["steady_state_concentrations"] == {"Mos": 30.0}


@pytest.mark.asyncio
async def test_single_cell_intent_without_dataset_reports_dataset_required(offline_orchestrator):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry(
        "Perform single-cell transcriptomic clustering for the KRAS tumor microenvironment.", thread_id="t-nods", enable_web_search=False
    )
    assert not _called(calls, "lumicell_analyze_single_cell")
    assert "lumicell" not in res.execution_artifacts


# =============================================================================
# FINDING-03: KEGG argument contract
# =============================================================================

@pytest.mark.asyncio
async def test_kegg_called_with_target_symbol_and_validates(offline_orchestrator):
    orch, calls = offline_orchestrator
    res = await orch.execute_inquiry("What KEGG pathways involve KRAS?", thread_id="t-kegg", enable_web_search=False)
    assert _called(calls, "synapta_kegg_pathways") == [{"target_symbol": "KRAS"}]
    assert "MAPK signaling pathway" in res.response


@pytest.mark.asyncio
async def test_kegg_schema_rejects_legacy_query_argument():
    reg, _ = build_recording_registry()
    bad = await reg.execute("synapta_kegg_pathways", query="pathway/KRAS")
    assert bad.success is False and bad.error_type == ErrorType.VALIDATION_ERROR


# =============================================================================
# FINDING-04: discovery scores reflect observations
# =============================================================================

def _vivarix(initial, final):
    return ToolResult(success=True, tool_name="vivarix", data={"initial_concentrations": initial, "final_concentrations": final})


def test_discovery_kinetic_score_tracks_flux_change():
    small = BioDiscoveryEngine.prioritize_hypotheses("q", "ERK2", {"vivarix": _vivarix({"Erk2-PP": 10.0}, {"Erk2-PP": 10.1})})
    large = BioDiscoveryEngine.prioritize_hypotheses("q", "ERK2", {"vivarix": _vivarix({"Erk2-PP": 10.0}, {"Erk2-PP": 200.0})})
    k_small = small.candidates[0].observed_components["pathway_kinetics"]
    k_large = large.candidates[0].observed_components["pathway_kinetics"]
    assert k_small < 0.05 < 0.9 < k_large
    assert k_small != 0.75 and k_large != 0.75


def test_discovery_single_cell_score_tracks_log_fold_change():
    def lum(lfc, padj):
        return ToolResult(
            success=True,
            tool_name="lumicell",
            data={"top_markers_by_cluster": {"0": [{"gene": "KRAS", "cluster": "0", "log_fold_change": lfc, "p_adj": padj}]}},
        )

    weak = BioDiscoveryEngine.prioritize_hypotheses("q", "KRAS", {"lumicell": lum(0.5, 0.01), "vivarix": _vivarix({"A": 1}, {"A": 2})})
    strong = BioDiscoveryEngine.prioritize_hypotheses("q", "KRAS", {"lumicell": lum(5.0, 0.01), "vivarix": _vivarix({"A": 1}, {"A": 2})})
    insignificant = BioDiscoveryEngine.prioritize_hypotheses("q", "KRAS", {"lumicell": lum(5.0, 0.4), "vivarix": _vivarix({"A": 1}, {"A": 2})})
    assert weak.candidates[0].observed_components["single_cell_marker_evidence"] == pytest.approx(0.125)
    assert strong.candidates[0].observed_components["single_cell_marker_evidence"] == pytest.approx(1.0)
    assert insignificant.candidates[0].observed_components["single_cell_marker_evidence"] is None


# =============================================================================
# FINDING-05 / FINDING-06 / FINDING-07 / FINDING-08 / FIX-13: verifier
# =============================================================================

def _sim_result(units="nmol"):
    return ToolResult(
        success=True,
        tool_name="vivarix_simulate_time_course",
        canonical_ids={"biomodel_id": "BIOMD0000000010"},
        data={
            "model_id": "BIOMD0000000010",
            "simulation_type": "TIME_COURSE",
            "units": {"time": "s", "quantity": units},
            "initial_concentrations": {"Mos": 100.0, "Mos-P": 0.0},
            "final_concentrations": {"Mos": 34.13, "Mos-P": 65.87},
        },
    )


def test_verifier_no_false_positive_on_phosphorylated_prefix():
    report = ClinicalVerifier().verify_response(
        query="Simulate Mos phosphorylation",
        response_markdown="Mos-P reached 65.87 nM while Mos was 34.13 nM.\n| **Mos** | 100.000 nM | 34.130 nM | -65.9% |",
        tool_results={"vivarix": _sim_result()},
    )
    assert report.numerical_checks_failed == 0
    assert not [d for d in report.defects if d.category == "NUMERICAL"]


def test_verifier_no_false_positive_with_unicode_hyphens():
    # Models typeset "Mos‑P" with U+2011 and use narrow no-break spaces; that must not read as "Mos".
    report = ClinicalVerifier().verify_response(
        query="Simulate Mos phosphorylation",
        response_markdown="- Mos‑P = **65.87 nM**\n- Mos = **34.13 nM**\nActive Mos‑P rises to 65.87 nM.",
        tool_results={"vivarix": _sim_result()},
    )
    assert not [d for d in report.defects if d.category == "NUMERICAL"]


def test_verifier_still_catches_errors_written_with_unicode_hyphens():
    report = ClinicalVerifier().verify_response(
        query="Simulate Mos phosphorylation",
        response_markdown="- Mos‑P = **12.00 nM**",
        tool_results={"vivarix": _sim_result()},
    )
    assert [d for d in report.defects if d.category == "NUMERICAL"]


def test_verifier_catches_unit_and_sign_errors():
    verifier = ClinicalVerifier()
    wrong_unit = verifier.verify_response("Mos simulation", "Final Mos concentration: 34.13 mM.", {"vivarix": _sim_result()})
    assert any(d.category == "NUMERICAL" for d in wrong_unit.defects)
    negative = verifier.verify_response("Mos simulation", "Mos: -34.13 nM at the end of the run.", {"vivarix": _sim_result()})
    assert any(d.category == "NUMERICAL" for d in negative.defects)
    converted = verifier.verify_response("Mos simulation", "Mos ended at 0.03413 µM.", {"vivarix": _sim_result()})
    assert not any(d.category == "NUMERICAL" for d in converted.defects)


def test_verifier_flags_unobserved_ensembl_and_uniprot():
    verifier = ClinicalVerifier()
    tool_results = {"synapta": ToolResult(success=True, tool_name="synapta", canonical_ids={"ensembl_id": "ENSG00000133703"}, data={"x": 1})}
    report = verifier.verify_response(
        "KRAS identifiers",
        "KRAS maps to ENSG00000133703 but also ENSG99999999999 and UniProt P99999.",
        tool_results,
    )
    flagged = {d.claim for d in report.defects if d.category == "IDENTIFIER"}
    assert {"ENSG99999999999", "P99999"} <= flagged
    assert "ENSG00000133703" not in flagged
    assert "ENSEMBL:ENSG00000133703" in report.verified_identifiers


def _passages(*texts):
    return ToolResult(
        success=True,
        tool_name="lithoscribe_literature_synthesis",
        data={"ranked_passages": [{"pmid": str(i), "passage_text": t} for i, t in enumerate(texts, 1)]},
    )


def test_verifier_rejects_clinical_claim_backed_by_unrelated_passages():
    report = ClinicalVerifier().verify_response(
        "sotorasib efficacy",
        "Phase 3 trial readouts confirmed an 85% objective response rate and curative efficacy for sotorasib.",
        {"lithoscribe": _passages("Glycolytic enzymes regulate hepatic metabolism.", "Endometrial repair depends on stromal remodeling.")},
    )
    assert any(d.category == "CLINICAL_CLAIM" for d in report.defects)
    assert report.passed is False


def test_verifier_accepts_clinical_claim_grounded_in_passage():
    report = ClinicalVerifier().verify_response(
        "sotorasib efficacy in NSCLC",
        "In the phase 2 CodeBreaK 100 trial, sotorasib achieved a 37.1% objective response rate in KRAS G12C NSCLC.",
        {"lithoscribe": _passages(
            "In CodeBreaK 100, sotorasib produced an objective response rate of 37.1% in previously treated KRAS G12C-mutant NSCLC.",
            "Unrelated passage about metabolism.",
        )},
    )
    assert not any(d.category == "CLINICAL_CLAIM" for d in report.defects)


def test_verifier_allows_honest_limitation_statements():
    report = ClinicalVerifier().verify_response(
        "PF-99999999 phase 3 overall survival",
        "No Phase 3 overall survival data are available for PF-99999999; the compound is unverified.",
        {"lithoscribe": _passages("KRAS biology review.")},
    )
    assert not any(d.category == "CLINICAL_CLAIM" for d in report.defects)


def test_verifier_steady_state_distinction():
    verifier = ClinicalVerifier()
    asserted = verifier.verify_response("Mos simulation", "Mos reaches a steady state of 34.13 nM.", {"vivarix": _sim_result()})
    assert any(d.category == "MODELING_DISTINCTION" for d in asserted.defects)
    hedged = verifier.verify_response("Mos simulation", "Mos ends at 34.13 nM; this is not a steady state.", {"vivarix": _sim_result()})
    assert not any(d.category == "MODELING_DISTINCTION" for d in hedged.defects)


def test_verifier_suggested_experiment_is_not_a_steady_state_claim():
    # Found by the evaluation harness: a "Suggested next experiments" bullet was flagged as a modelling claim.
    report = ClinicalVerifier().verify_response(
        "Mos simulation",
        "Mos ends at 34.13 nM.\n\n### Suggested next experiments\n"
        "- Run a systematic parameter sweep of V1 and record the steady-state concentrations of Mos-P.",
        {"vivarix": _sim_result()},
    )
    assert not any(d.category == "MODELING_DISTINCTION" for d in report.defects)


def test_verifier_complex_name_is_not_a_value_of_its_component():
    # Found by the evaluation harness: "(EGF_EGFR)2-P" was read as species EGF_EGFR with value 2.
    sim = _sim_result()
    sim.data["final_concentrations"] = {"EGF_EGFR": 17.43, "(EGF_EGFR)2-P": 3.61, "(EGF_EGFR)_Shc-P": 1.84}
    report = ClinicalVerifier().verify_response(
        "EGFR simulation",
        "| Species | Concentration (nmol) |\n|---|---|\n| EGF_EGFR | 17.43 |\n| (EGF_EGFR)2‑P | 3.61 |\n| (EGF_EGFR)_Shc‑P | 1.84 |",
        {"vivarix": sim},
    )
    assert not [d for d in report.defects if d.category == "NUMERICAL"], [d.description for d in report.defects]


def test_faers_counts_with_unicode_thousands_separators():
    # Found by the evaluation harness: "272 899 reports" (narrow no-break space) was read as 899.
    from phronesis.drugintel.checks import COUNT_RE, _as_int

    assert _as_int(COUNT_RE.search("mentioned in 272 899 reports").group(1)) == 272899
    assert _as_int(COUNT_RE.search("seen in 1,234 serious reports").group(1)) == 1234


def test_label_and_development_status_sentences_are_not_misread():
    # Found by the evaluation harness: label wording ("contraindicated due to the risk of lactic acidosis") was
    # read as a FAERS causation claim, and "X has advanced to phase 3 (CTR...)" as a preclinical paper's design.
    from phronesis.drugintel.checks import _LABEL_RE, _REPORTS_RE
    from phronesis.extensions.grading import _DEVELOPMENT_STATUS_RE

    label = "The FDA label contraindicates metformin in severe renal impairment due to the risk of lactic acidosis (openFDA label set_id: x)."
    assert _LABEL_RE.search(label) and not _REPORTS_RE.search(label)
    assert _REPORTS_RE.search("FAERS reports show 120 cases caused by the drug.")
    assert _DEVELOPMENT_STATUS_RE.search("HRS-4642 has advanced to phase 3 trials (CTR20254212)")
    assert not _DEVELOPMENT_STATUS_RE.search("In a phase 3 trial, the drug improved survival")


def test_verifier_accepts_identifiers_from_verified_memory():
    verifier = ClinicalVerifier()
    text = "As established previously, BIOMD0000000010 captured MAPK ultrasensitivity."
    without = verifier.verify_response("previous MAPK conclusion", text, {})
    assert any(d.category == "IDENTIFIER" for d in without.defects)
    with_memory = verifier.verify_response(
        "previous MAPK conclusion", text, {}, historical_identifiers={"biomodels": {"BIOMD0000000010"}}
    )
    assert not any(d.category == "IDENTIFIER" for d in with_memory.defects)


# =============================================================================
# FINDING-09: tenant isolation in the memory engine
# =============================================================================

@pytest.mark.asyncio
async def test_memory_scoped_by_user(tmp_path):
    mem = ClinicalMemoryEngine(storage_dir=str(tmp_path))
    data = {"vivarix": {"model_id": "BIOMD0000000010", "final_concentrations": {"Mos": 1.0}}}
    await mem.consolidate_turn("shared-thread", "KRAS query", "resp", data, user_id="alice", is_turn_verified=True)
    await mem.consolidate_turn("shared-thread", "KRAS query", "resp", data, user_id="bob", is_turn_verified=True)

    assert mem.get_verified_identifiers("shared-thread", user_id="alice")["biomodels"] == {"BIOMD0000000010"}
    mem.clear_all_memory(user_id="alice")
    assert mem.get_memory_stats(user_id="alice")["total_nodes"] == 0
    assert mem.get_memory_stats(user_id="bob")["total_nodes"] > 0
    assert not mem.search_memory("KRAS", user_id="alice")
    assert mem.get_entity_vault(user_id="bob")


# =============================================================================
# FINDING-09: API authentication
# =============================================================================

@pytest.fixture
def api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    engine = ClinicalMemoryEngine(storage_dir=str(tmp_path))
    monkeypatch.setattr(backend_main, "get_clinical_memory_engine", lambda: engine)
    monkeypatch.delenv("PHRONESIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PHRONESIS_JWT_SECRET", raising=False)
    monkeypatch.setenv("PHRONESIS_API_TOKENS", "alice-token:alice,bob-token:bob")
    return TestClient(backend_main.app), engine


def test_api_rejects_missing_or_invalid_token(api_client):
    client, _ = api_client
    assert client.get("/api/memory/stats").status_code == 401
    assert client.delete("/api/memory/clear", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_api_scopes_memory_to_authenticated_user(api_client):
    client, engine = api_client
    asyncio.run(engine.consolidate_turn("t1", "KRAS query", "resp", {}, user_id="bob", is_turn_verified=True))
    alice = {"Authorization": "Bearer alice-token"}
    bob = {"Authorization": "Bearer bob-token"}
    assert client.get("/api/memory/thread/t1", headers=alice).json()["count"] == 0
    assert client.get("/api/memory/thread/t1", headers=bob).json()["count"] > 0
    assert client.delete("/api/memory/clear", headers=alice).status_code == 200
    assert client.get("/api/memory/thread/t1", headers=bob).json()["count"] > 0


def test_api_fails_closed_when_auth_unconfigured(api_client, monkeypatch):
    client, _ = api_client
    monkeypatch.delenv("PHRONESIS_API_TOKENS", raising=False)
    assert client.get("/api/memory/stats").status_code == 503


def test_api_accepts_jwt_subject(api_client, monkeypatch):
    import jwt

    client, _ = api_client
    monkeypatch.setenv("PHRONESIS_JWT_SECRET", "test-secret-with-enough-length-0123456789")
    token = jwt.encode({"sub": "carol", "exp": int(time.time()) + 60}, "test-secret-with-enough-length-0123456789", algorithm="HS256")
    resp = client.get("/api/memory/stats", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    expired = jwt.encode({"sub": "carol", "exp": int(time.time()) - 60}, "test-secret-with-enough-length-0123456789", algorithm="HS256")
    assert client.get("/api/memory/stats", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


# =============================================================================
# FIX-14: synchronous executors do not block the event loop
# =============================================================================

@pytest.mark.asyncio
async def test_sync_executor_runs_off_event_loop_and_respects_timeout():
    reg = BiomedicalToolRegistry()
    seen = {}

    def slow(query: str, max_results: int = 3):
        seen["thread"] = threading.get_ident()
        time.sleep(0.5)
        return {"query": query}

    reg.register(
        ToolUniverseEntry(
            name="slow_tool", description="slow", domain="test", adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=registry_module.WebSurferSearchInput, canonical_id_types=[], timeout_seconds=0.1, tags=[],
        ),
        executor=slow,
    )
    res = await reg.execute("slow_tool", query="x")
    assert res.success is False and res.error_type == ErrorType.TIMEOUT
    assert seen["thread"] != threading.get_ident()
