"""Reported analysis design over indexed tags (E4), and memory that keeps unverified work unverified (E12)."""

from __future__ import annotations

from types import SimpleNamespace

from phronesis.agentic.project_memory import _verified, format_memory_block
from phronesis.extensions.grading import _claim_fits, _DESIGN_WORDS
from phronesis.lithoscribe.evidence_grading import StudyType, grade_article, legacy_setting
from phronesis.utils.memory.clinical_memory import (
    ClinicalMemoryEngine,
    MemoryType,
    VerificationStatus,
    turn_fully_verified,
)
from phronesis.utils.memory.project_store import FactInput, ProjectStore

RCT_TAGS = {"pubType": ["Randomized Controlled Trial", "Clinical Trial, Phase III", "Journal Article"]}


def test_maic_indexed_as_rct_is_graded_as_an_indirect_comparison_with_raw_tags_kept():
    g = grade_article({
        "title": "Sotorasib versus docetaxel in KRAS G12C NSCLC: a matching-adjusted indirect comparison",
        "abstractText": "We conducted an unanchored MAIC using individual patient data from CodeBreaK 200.",
        "pubTypeList": RCT_TAGS,
    })
    assert g.study_type == StudyType.INDIRECT_COMPARISON
    assert g.indexed_design == "rct" and g.mixed_design
    assert g.indexed_pub_types == RCT_TAGS["pubType"]
    assert "Indexed as Randomized Controlled Trial, Clinical Trial, Phase III" in g.design_note
    assert "indirect comparison" in g.design_note
    assert g.tier > 2 and g.weight < 0.9  # never the RCT tier
    assert legacy_setting(g) == "human-clinical"


def test_post_hoc_and_real_world_analyses_tagged_as_trials_are_flagged():
    post_hoc = grade_article({
        "title": "Outcomes by PD-L1 expression: a post hoc analysis of the CodeBreaK 200 randomized trial",
        "pubTypeList": {"pubType": ["Randomized Controlled Trial"]},
    })
    assert post_hoc.study_type == StudyType.CLINICAL_TRIAL and post_hoc.indexed_design == "rct"
    assert "post hoc" in post_hoc.design_note
    real_world = grade_article({
        "title": "Real-world outcomes of sotorasib after docetaxel",
        "abstractText": "This retrospective study included 164 patients.",
        "pubTypeList": {"pubType": ["Clinical Trial"]},
    })
    assert real_world.study_type == StudyType.OBSERVATIONAL and real_world.indexed_design == "clinical_trial"


def test_an_ordinary_rct_keeps_its_indexed_grade():
    g = grade_article({
        "title": "Sotorasib versus docetaxel for previously treated KRAS G12C NSCLC (CodeBreaK 200): a randomised, phase 3 trial",
        "abstractText": "Patients were randomly assigned 1:1. Indirect comparisons with other agents are limited. Real-world data are needed.",
        "pubTypeList": RCT_TAGS,
    })
    assert g.study_type == StudyType.RCT and g.indexed_design is None and g.design_note.startswith("Indexed publication type")


def test_network_meta_analysis_stays_distinct_from_a_pairwise_meta_analysis():
    nma = grade_article({"title": "KRAS G12C inhibitors: a systematic review and network meta-analysis", "pubTypeList": {"pubType": ["Meta-Analysis"]}})
    assert nma.study_type == StudyType.NETWORK_META_ANALYSIS
    pairwise = grade_article({"title": "A meta-analysis of sotorasib trials", "pubTypeList": {"pubType": ["Meta-Analysis"]}})
    assert pairwise.study_type == StudyType.META_ANALYSIS


def test_calling_an_indirect_comparison_randomized_does_not_fit_unless_the_method_is_named():
    assert not _claim_fits("randomized", "indirect_comparison")
    assert not _claim_fits("phase 3", "indirect_comparison")
    assert _DESIGN_WORDS["indirect_comparison"].search("a matching-adjusted indirect comparison of two randomized trials")
    assert _claim_fits("network meta-analysis", "network_meta_analysis")


def test_turn_verified_requires_no_unresolved_claims():
    assert turn_fully_verified(True) is True
    assert turn_fully_verified(False, {"passed": True}) is False
    assert turn_fully_verified(True, {"passed": True, "unsupported_claims": 3}) is False
    assert turn_fully_verified(True, {"passed": True, "unsupported_claims": 0, "contradicted_claims": 0, "unchecked_claims": 0}) is True
    assert turn_fully_verified(True, {"passed": True, "fully_verified": False}) is False


def _nodes(mem: ClinicalMemoryEngine, user: str):
    with mem._connect() as conn:
        return conn.execute("SELECT memory_type, content, verification_status, sources_json FROM memory_nodes WHERE user_id = ?", (user,)).fetchall()


def test_unverified_turn_stores_tool_evidence_provisional_and_simulation_is_never_verified(tmp_path):
    mem = ClinicalMemoryEngine(storage_dir=str(tmp_path))
    data = {
        "lithoscribe": {"ranked_passages": [{"pmid": "39369609", "passage_text": "median OS 10.2 months"}]},
        "vivarix": {"model_id": "BIOMD0000000010", "final_concentrations": {"Mos": 1.0}},
    }
    # "passed" but with unsupported claims: nothing may become verified empirical evidence.
    mem.consolidate_turn_sync("t1", "q", "r", data, user_id="alice", is_turn_verified=True,
                              verification={"passed": True, "unsupported_claims": 8, "contradicted_claims": 0, "unchecked_claims": 0})
    rows = [r for r in _nodes(mem, "alice") if r["memory_type"] in (MemoryType.RETRIEVED_EVIDENCE.value, MemoryType.COMPUTED_RESULT.value)]
    assert rows and all(r["verification_status"] == VerificationStatus.PROVISIONAL.value for r in rows)

    # A fully verified turn: literature becomes verified, the kinetic model's output still does not.
    mem.consolidate_turn_sync("t2", "q", "r", data, user_id="bob", is_turn_verified=True,
                              verification={"passed": True, "unsupported_claims": 0, "contradicted_claims": 0, "unchecked_claims": 0})
    by_type = {r["memory_type"]: r for r in _nodes(mem, "bob")}
    assert by_type[MemoryType.RETRIEVED_EVIDENCE.value]["verification_status"] == VerificationStatus.VERIFIED_EMPIRICAL.value
    assert by_type[MemoryType.COMPUTED_RESULT.value]["verification_status"] == VerificationStatus.PROVISIONAL.value
    assert "not clinical evidence" in by_type[MemoryType.COMPUTED_RESULT.value]["content"]
    pkg = mem.get_context_package("t2", user_id="bob")
    assert not pkg.computed_artifacts  # never recalled as verified computed evidence
    # ...but the model id a verified turn observed is still an observed identifier for the verifier.
    assert mem.get_verified_identifiers("t2", user_id="bob")["biomodels"] == {"BIOMD0000000010"}
    assert mem.get_verified_identifiers("t1", user_id="alice")["biomodels"] == set()


def test_project_facts_keep_their_unverified_flag_and_verified_provenance(tmp_path):
    store = ProjectStore(str(tmp_path))
    pid = store.create_project("alice", "KRAS")["id"]
    store.upsert_facts(pid, "alice", [FactInput("sotorasib", "targets", "KRAS G12C", verified=True, confidence=0.6, source={"run_id": "verified-run"})])
    # A later unverified write with higher confidence must not replace the verified fact's provenance.
    store.upsert_facts(pid, "alice", [FactInput("sotorasib", "targets", "KRAS G12C", verified=False, confidence=0.9, source={"run_id": "unverified-run"})])
    fact = store.recall(pid, "alice", "sotorasib")[0]
    assert fact["verified"] and fact["source"]["run_id"] == "verified-run"

    store.upsert_facts(pid, "alice", [FactInput("adagrasib", "targets", "KRAS G12C", verified=False)])
    recalled = {f["subject"]: f for f in store.recall(pid, "alice", "adagrasib KRAS")}
    assert recalled["adagrasib"]["verified"] is False
    block = format_memory_block({"name": "KRAS"}, list(recalled.values()))
    assert "[unverified" in block and "never evidence" in block


def test_project_turn_with_unsupported_claims_is_not_verified():
    state = SimpleNamespace(verification={"passed": True, "unsupported_claims": 2, "contradicted_claims": 0, "unchecked_claims": 0})
    assert _verified(state) is False
    state.verification = SimpleNamespace(passed=True, fully_verified=True, unsupported_claims=0, contradicted_claims=0, unchecked_claims=0)
    assert _verified(state) is True
    state.verification = None
    assert _verified(state) is False
