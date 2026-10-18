"""Evidence-integrity regressions from the KRAS G12C run review (Phronesis_Run_Analysis_and_Claude_Code_Fix_Prompt.md,
findings E1-E3, E6): which evidence may support which claim, disease context, figure matching and regimen labels.

Offline: the verifier is exercised directly with hand-built tool results. Hand-built passages are parser and policy
fixtures, not scientific gold; real-record extraction is checked against tests/fixtures/primary/ in
tests/test_verifier_grounding.py.
"""

from __future__ import annotations

from typing import Any, Dict, List

from phronesis.phronesis.agents import verifier as vmod
from phronesis.phronesis.agents.verifier import ClaimRecord, ClinicalVerifier, VerificationReport
from phronesis.tools.registry import ToolResult
from phronesis.trials import trial_grounding
from phronesis.utils.text import fold_typography, fold_typography_with_map

Q = "KRAS G12C inhibitors in NSCLC and colorectal cancer: FDA-approved indications and clinical evidence"


def lit(passages: List[Dict[str, Any]]) -> ToolResult:
    return ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": passages})


def tool(name: str, data: Dict[str, Any]) -> ToolResult:
    return ToolResult(success=True, tool_name=name, data=data)


def claim_for(report: VerificationReport, needle: str) -> ClaimRecord:
    return next(c for c in report.claims if needle in c.claim_text)


def categories(report: VerificationReport) -> List[str]:
    return [d.category for d in report.defects]


# E2: approval claims need a regulatory record -----------------------------------------------------------------
APPROVAL = "Sotorasib received FDA accelerated approval for KRAS G12C-mutated NSCLC."
SIMULATION = tool("vivarix_simulate_time_course", {
    "model_name": "KRAS G12C sotorasib NSCLC signalling model", "description": "sotorasib accelerated approval NSCLC kras",
})
ASSOCIATION = tool("synapta_target_associations", {
    "associations": [{"target": "KRAS", "disease": "NSCLC", "score": 0.81, "note": "sotorasib accelerated approval kras nsclc"}],
})


def web(url: str, title: str) -> ToolResult:
    return tool("websurfer_search", {"query": "sotorasib approval", "results": [
        {"title": title, "url": url, "snippet": "Sotorasib accelerated approval for KRAS G12C-mutated NSCLC."},
    ]})


def test_simulation_and_target_association_cannot_support_an_approval_claim():
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"vivarix": SIMULATION, "synapta": ASSOCIATION})
    c = claim_for(report, "accelerated approval")
    assert c.status == "unsupported" and "no FDA label or approval record" in c.reason
    assert not any(e.source_key in ("tool:vivarix", "tool:synapta") for e in c.evidence)
    assert not report.passed


def test_an_fda_gov_page_or_openfda_record_supports_the_approval_claim():
    fda_page = web("https://www.fda.gov/drugs/resources-information-approved-drugs/fda-grants-accelerated-approval-sotorasib",
                   "FDA grants accelerated approval to sotorasib for KRAS G12C-mutated NSCLC")
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"synapta": ASSOCIATION, "websurfer": fda_page})
    c = claim_for(report, "accelerated approval")
    assert c.status == "supported" and c.evidence[0].source_key == "tool:websurfer"

    openfda = tool("openfda_drug_safety", {"drugs": [{"ingredient": "sotorasib", "approval": {
        "application_number": "NDA214665", "approval_type": "accelerated approval",
        "indication": "KRAS G12C-mutated locally advanced or metastatic NSCLC"}}]})
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"vivarix": SIMULATION, "agent.plan.3.openfda": openfda})
    assert claim_for(report, "accelerated approval").status == "supported"


def test_a_manufacturer_press_release_is_not_an_fda_record():
    release = web("https://www.amgen.com/newsroom/press-releases/2021/05/sotorasib-approval",
                  "Amgen announces FDA accelerated approval of sotorasib for KRAS G12C NSCLC")
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": release})
    assert claim_for(report, "accelerated approval").status == "unsupported"


def test_trial_paper_cited_for_an_approval_is_not_enough():
    paper = lit([{"pmid": "42636835", "passage_id": "p1", "title": "Ifebemtinib plus garsorasib in KRAS G12C colorectal cancer",
                  "passage_text": "Garsorasib received accelerated approval for KRAS G12C-mutated NSCLC in China."}])
    answer = "Garsorasib received accelerated approval for KRAS G12C-mutated NSCLC (PMID: 42636835)."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": paper})
    assert claim_for(report, "accelerated approval").status == "unsupported"


def test_simulation_cannot_support_a_patient_outcome_claim():
    sim = tool("vivarix_simulate_time_course", {"model_name": "sotorasib docetaxel survival model",
                                                 "description": "sotorasib docetaxel overall survival response"})
    answer = "Sotorasib improved the objective response rate over docetaxel in pretreated patients."
    report = ClinicalVerifier().verify_response(Q, answer, {"vivarix": sim})
    c = claim_for(report, "objective response rate")
    assert c.status == "unsupported" and "simulation and target-association outputs" in c.reason


def test_bare_years_in_an_approvals_table_are_not_species_concentrations():
    sim = tool("vivarix_simulate_time_course", {"final_concentrations": {"KRAS": 5.0}, "initial_concentrations": {"KRAS": 1.0}})
    answer = "| Target | First approval year |\n|---|---|\n| KRAS | 2021 |\n"
    report = ClinicalVerifier().verify_response(Q, answer, {"vivarix": sim})
    assert "NUMERICAL" not in categories(report)
    sim_table = "| Species | Final (nmol) |\n|---|---|\n| KRAS | 9.0 |\n"
    assert "NUMERICAL" in categories(ClinicalVerifier().verify_response(Q, sim_table, {"vivarix": sim}))


# Cited source that does not support the claim -----------------------------------------------------------------
def test_support_from_another_paper_than_the_cited_one_is_recorded():
    passages = lit([
        {"pmid": "111", "passage_id": "a", "title": "Adagrasib pharmacokinetics in healthy volunteers",
         "passage_text": "Adagrasib exposure increased with dose in healthy volunteers receiving adagrasib."},
        {"pmid": "222", "passage_id": "b", "title": "Adagrasib in KRAS G12C NSCLC",
         "passage_text": "Adagrasib produced an objective response rate of 42.9% in pretreated KRAS G12C NSCLC."},
    ])
    answer = "Adagrasib produced an objective response rate of 42.9% in pretreated KRAS G12C NSCLC (PMID: 111)."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": passages})
    c = claim_for(report, "42.9%")
    assert c.status == "supported" and c.evidence[0].source_key == "PMID:222"
    assert c.cited_source_supported is False and "not by the cited PMID:111" in c.reason
    assert "CITATION_MISMATCH" in categories(report)

    right = answer.replace("PMID: 111", "PMID: 222")
    report = ClinicalVerifier().verify_response(Q, right, {"lit": passages})
    assert claim_for(report, "42.9%").cited_source_supported is True and "CITATION_MISMATCH" not in categories(report)


# E1: disease context --------------------------------------------------------------------------------------------
NSCLC_PAPER = {
    "pmid": "00000004", "passage_id": "n1",
    "title": "KRAS mutations and resistance to EGFR tyrosine kinase inhibitors in advanced NSCLC",
    "passage_text": "In advanced non-small cell lung cancer, KRAS mutations were associated with resistance to gefitinib "
                    "and erlotinib; the objective response rate to erlotinib was 5.0% in KRAS-mutant tumours.",
}


def test_an_nsclc_paper_cannot_support_a_crc_claim_even_when_the_question_covers_both():
    answer = "In colorectal cancer, KRAS mutations confer resistance to gefitinib and erlotinib (PMID: 00000004)."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": lit([NSCLC_PAPER])})
    assert claim_for(report, "gefitinib").status == "unsupported"
    defect = next(d for d in report.defects if d.category == "POPULATION_MISMATCH")
    assert "lung cancer study" in defect.description and "colorectal cancer" in defect.description


def test_correct_numbers_in_the_wrong_disease_fail_and_in_the_right_disease_pass():
    wrong = "In colorectal cancer, the objective response rate to erlotinib was 5.0% in KRAS-mutant tumours (PMID: 00000004)."
    report = ClinicalVerifier().verify_response(Q, wrong, {"lit": lit([NSCLC_PAPER])})
    assert claim_for(report, "5.0%").status == "unsupported" and "POPULATION_MISMATCH" in categories(report)
    right = wrong.replace("In colorectal cancer", "In advanced NSCLC")
    report = ClinicalVerifier().verify_response(Q, right, {"lit": lit([NSCLC_PAPER])})
    assert claim_for(report, "5.0%").status == "supported" and "POPULATION_MISMATCH" not in categories(report)


def test_a_source_that_also_discusses_the_claims_disease_is_accepted():
    both = dict(NSCLC_PAPER, passage_text=NSCLC_PAPER["passage_text"] + " Similar resistance is reported in colorectal cancer.")
    answer = "In colorectal cancer, KRAS mutations confer resistance to gefitinib and erlotinib (PMID: 00000004)."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": lit([both])})
    assert "POPULATION_MISMATCH" not in categories(report)


# E3: figures that are in the abstract are found ---------------------------------------------------------------
# A hand-built Lancet-style passage for the parser (middle-dot decimals, "N=a/b", interval before the unit). The
# identifiers and values are synthetic on purpose; the real-record positive control (Johnson et al., Lung Cancer 2024)
# is in tests/test_verifier_grounding.py with its provenance in tests/fixtures/primary/.
SYNTHETIC_ABSTRACT = {
    "pmid": "00000001", "passage_id": "abs", "section": "abstract",
    "title": "Synthetic 2024: drug A versus drug B in a synthetic KRAS G12C NSCLC cohort",
    "passage_text": "Among 266 patients, 102 received sotorasib and 164 received docetaxel. Median overall survival was "
                    "10·2 months (95% CI 8·1–12·6) with sotorasib versus 11·3 months with docetaxel (hazard ratio "
                    "1·02, 95% CI 0·75–1·38).",
}
SYNTHETIC_CLAIM = (
    "In Synthetic 2024 (N=102/164), median overall survival was 10.2 months with sotorasib versus 11.3 months with "
    "docetaxel (HR 1.02, 95% CI 0.75–1.38) (PMID: 00000001)."
)


def test_lancet_style_middle_dot_values_are_supported():
    report = ClinicalVerifier().verify_response(Q, SYNTHETIC_CLAIM, {"lit": lit([SYNTHETIC_ABSTRACT])})
    c = claim_for(report, "10.2 months")
    assert c.status == "supported", c.reason
    assert c.cited_source_supported is True


def test_both_arm_sizes_are_checked():
    metrics = ClinicalVerifier._claim_metrics("N=102/164")
    assert sorted(m.value for m in metrics if m.kind == "n") == [102.0, 164.0]
    wrong = SYNTHETIC_CLAIM.replace("N=102/164", "N=102/170")
    report = ClinicalVerifier().verify_response(Q, wrong, {"lit": lit([SYNTHETIC_ABSTRACT])})
    assert claim_for(report, "10.2 months").status == "unsupported"


def test_figures_split_across_two_passages_of_one_article_are_supported():
    passages = lit([
        {"pmid": "5", "passage_id": "p1", "title": "Sotorasib versus docetaxel",
         "passage_text": "Median overall survival was 10.2 months with sotorasib and 11.3 months with docetaxel."},
        {"pmid": "5", "passage_id": "p2", "title": "Sotorasib versus docetaxel",
         "passage_text": "For sotorasib versus docetaxel the hazard ratio for death was 1.02 (95% CI 0.75-1.38)."},
    ])
    answer = "Median overall survival was 10.2 months with sotorasib versus docetaxel (HR 1.02, 95% CI 0.75-1.38) (PMID: 5)."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": passages})
    assert claim_for(report, "10.2 months").status == "supported"
    # Passages of a different article never pool with this one.
    other = lit([dict(passages.data["ranked_passages"][0]),
                 dict(passages.data["ranked_passages"][1], pmid="6")])
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": other})
    assert claim_for(report, "10.2 months").status == "unsupported"


def test_unit_written_once_after_an_interval_or_a_comparator():
    assert vmod.attach_units("10.2 (95% ci 8.1-12.0) months") == "10.2 months (95% ci 8.1-12.0)"
    assert vmod.attach_units("10.2 vs 8.1 months") == "10.2 months vs 8.1 months"
    doc = vmod._Doc("PMID:7", "median overall survival with sotorasib was 10.2 (95% ci 8.1-12.0) months.", None, "")
    report_status = ClinicalVerifier()._clinical_verdict(
        "Median overall survival with sotorasib was 10.2 months (PMID: 7).", {"PMID:7"}, [doc]
    )
    assert report_status[0] == "supported"


def test_middle_dot_decimals_fold_only_between_digits():
    assert fold_typography("10·2 months · 8 passages") == "10.2 months · 8 passages"
    folded, offsets = fold_typography_with_map("a 1·5 b")
    assert folded == "a 1.5 b" and len(offsets) == len(folded) + 1


def test_registry_figures_with_thousands_separators_match():
    assert trial_grounding._figure_in("1234", "enrolled 1,234 participants")
    assert trial_grounding._figure_in("10.2", "median 10·2 months")


# E6: combination described as monotherapy ---------------------------------------------------------------------
def test_a_combination_called_monotherapy_is_flagged():
    answer = "Ifebemtinib plus garsorasib monotherapy achieved an objective response rate of 90.3% in KRAS G12C CRC."
    report = ClinicalVerifier().verify_response(Q, answer, {})
    defect = next(d for d in report.defects if d.category == "REGIMEN_MISMATCH")
    assert "ifebemtinib + garsorasib" in defect.description


def test_comparing_monotherapy_with_a_combination_is_not_flagged():
    for answer in (
        "Sotorasib monotherapy versus sotorasib plus panitumumab was compared in chemorefractory CRC.",
        "Adagrasib monotherapy produced responses; adagrasib with cetuximab produced more.",
    ):
        assert vmod.regimen_mismatch(answer) is None, answer
