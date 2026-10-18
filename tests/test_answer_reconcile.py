"""Post-verification answer edits: the stated confidence level follows the computed answer confidence, and a named
trial's primary report is cited next to a review cited for the trial's figures."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agents.orchestration import verify as verify_stage
from phronesis.phronesis.agents.verifier import (
    COMPUTED_CONFIDENCE_NOTE,
    prefer_primary_trial_reports,
    reconcile_confidence_line,
)

# ---------------------------------------------------------------------------------------------------------------
# Confidence line
# ---------------------------------------------------------------------------------------------------------------
ANSWER = (
    "**Bottom line.** Olaparib maintenance prolongs progression-free survival in BRCA-mutated ovarian cancer.\n\n"
    "**Confidence:** high (one phase III RCT; OS immature)\n\n"
    "### Trial evidence\nSOLO-1 randomized 391 patients.\n"
)


def test_differing_level_is_rewritten_and_reason_kept():
    out = reconcile_confidence_line(ANSWER, "medium")
    assert "**Confidence:** moderate (one phase III RCT; OS immature) (computed from claim support)\n" in out
    assert out.replace("moderate", "high", 1).replace(f" {COMPUTED_CONFIDENCE_NOTE}", "") == ANSWER


def test_matching_level_is_untouched():
    assert reconcile_confidence_line(ANSWER, "high") == ANSWER
    text = "Confidence: moderate (two RCTs)"
    assert reconcile_confidence_line(text, "medium") == text  # medium and moderate are the same level


def test_no_computed_confidence_leaves_text():
    assert reconcile_confidence_line(ANSWER, None) == ANSWER
    assert reconcile_confidence_line(ANSWER, "") == ANSWER


def test_reconcile_is_idempotent():
    once = reconcile_confidence_line(ANSWER, "low")
    assert "**Confidence:** low (one phase III RCT" in once
    assert reconcile_confidence_line(once, "low") == once
    assert once.count(COMPUTED_CONFIDENCE_NOTE) == 1


@pytest.mark.parametrize(
    "text, band, expected",
    [
        ("- Confidence: **High** — one RCT.", "low", "- Confidence: **Low** (computed from claim support) — one RCT."),
        ("Overall confidence: moderate-to-high (pooled data)", "high",
         "Overall confidence: high (pooled data) (computed from claim support)"),
        ("Benefit is consistent. Confidence: low (case series only).", "medium",
         "Benefit is consistent. Confidence: moderate (case series only) (computed from claim support)."),
        ("**Confidence: high** (phase III (SOLO-1) data)", "medium",
         "**Confidence: moderate** (phase III (SOLO-1) data) (computed from claim support)"),
    ],
)
def test_rating_forms(text, band, expected):
    assert reconcile_confidence_line(text, band) == expected


def test_only_the_bottom_line_rating_and_not_prose():
    text = "We have high confidence: high doses work.\n\nConfidence: high (RCT)\n\n### Safety\nConfidence: low (FAERS)\n"
    out = reconcile_confidence_line(text, "medium")
    assert out.startswith("We have high confidence: high doses work.")  # mid-sentence prose is not a rating
    assert "Confidence: moderate (RCT) (computed from claim support)" in out
    assert out.endswith("Confidence: low (FAERS)\n")  # a section's own rating is left alone


class _Run(SimpleNamespace):
    pass


def _run(answer: str, artifacts: Dict[str, Any]) -> Any:
    events: List[Dict[str, Any]] = []

    async def send(ev: Dict[str, Any]) -> None:
        events.append(ev)

    async def emit(*_a: Any, **_k: Any) -> None:
        return None

    return _Run(
        candidate_response=answer, execution_artifacts=artifacts, agent_state=None, send=send, emit=emit,
        thought_index={}, events=events,
        verification_report=SimpleNamespace(passed=True, fully_verified=False, claims=[], verification_score=1.0, defects=[]),
    )


@pytest.mark.asyncio
async def test_report_result_reconciles_and_resends_the_answer(monkeypatch):
    rebuilt: List[str] = []
    monkeypatch.setattr(verify_stage, "build_citations", lambda run: rebuilt.append(run.candidate_response))
    run = _run(ANSWER, {"claim_confidence": {"answer_confidence": 0.66, "claims": []}})
    await verify_stage.report_result(run)
    assert "**Confidence:** moderate (one phase III RCT; OS immature) (computed from claim support)" in run.candidate_response
    shown = ""
    for ev in run.events:
        if ev["type"] == "content_reset":
            shown = ""
        elif ev["type"] == "content_delta":
            shown += ev["delta"]
    assert shown == run.candidate_response  # the client shows the corrected text
    assert rebuilt == [run.candidate_response]  # citations follow the answer as shown


@pytest.mark.asyncio
async def test_classic_run_without_confidence_is_untouched(monkeypatch):
    monkeypatch.setattr(verify_stage, "build_citations", lambda run: pytest.fail("no rebuild expected"))
    run = _run(ANSWER, {})
    await verify_stage.report_result(run)
    assert run.candidate_response == ANSWER and run.events == []


@pytest.mark.asyncio
async def test_agreeing_confidence_sends_nothing(monkeypatch):
    monkeypatch.setattr(verify_stage, "build_citations", lambda run: pytest.fail("no rebuild expected"))
    run = _run(ANSWER, {"claim_confidence": {"answer_confidence": 0.81}})
    await verify_stage.report_result(run)
    assert run.candidate_response == ANSWER and run.events == []


def test_confidence_from_agent_state_artifacts():
    run = _run(ANSWER, {})
    run.agent_state = SimpleNamespace(artifacts={"claim_confidence": {"answer_confidence": 0.2}})
    assert verify_stage.computed_confidence_band(run) == "low"
    run.agent_state.artifacts["claim_confidence"]["answer_confidence"] = None
    assert verify_stage.computed_confidence_band(run) is None


# ---------------------------------------------------------------------------------------------------------------
# Primary trial report
# ---------------------------------------------------------------------------------------------------------------
PRIMARY = {
    "pmid": "30345884",
    "title": "Maintenance Olaparib in Patients with Newly Diagnosed Advanced Ovarian Cancer",
    "passage_text": (
        "We conducted an international, randomized, double-blind, phase 3 trial to evaluate the efficacy of olaparib "
        "as maintenance therapy. The risk of disease progression or death was 70% lower with olaparib than with "
        "placebo (hazard ratio for disease progression or death, 0.30; 95% confidence interval, 0.23 to 0.41; "
        "P<0.001). (Funded by AstraZeneca and Merck; SOLO1 ClinicalTrials.gov number, NCT01844986.)"
    ),
    "evidence_grade": {"study_type": "rct"},
    "pub_types": ["Randomized Controlled Trial", "Clinical Trial, Phase III"],
}
REVIEW = {
    "pmid": "42180878",
    "title": "PARP inhibitors in BRCA-mutated ovarian cancer: a narrative review",
    "passage_text": "In SOLO-1, olaparib maintenance markedly reduced the risk of progression (HR 0.30).",
    "evidence_grade": {"study_type": "review"},
}
SENTENCE = (
    "In SOLO-1, olaparib maintenance reduced the risk of progression or death (HR 0.30; 95% CI 0.23–0.41) "
    "(PMID: 42180878; narrative review)."
)


def _tools(*passages: Dict[str, Any]) -> Dict[str, Any]:
    return {"lithoscribe": SimpleNamespace(success=True, data={"ranked_passages": list(passages)})}


def test_primary_report_is_cited_before_the_review():
    out, changes = prefer_primary_trial_reports(SENTENCE, _tools(REVIEW, PRIMARY))
    assert "(PMID: 30345884; phase III RCT; PMID: 42180878; narrative review)" in out
    assert changes == [{"trial": "SOLO-1", "primary": "PMID:30345884", "review": "PMID:42180878"}]


def test_citation_without_level_gets_bare_pmid():
    text = "SOLO-1 showed an HR of 0.30 (95% CI 0.23-0.41) (PMID: 42180878)."
    out, _ = prefer_primary_trial_reports(text, _tools(REVIEW, PRIMARY))
    assert "(PMID: 30345884; PMID: 42180878)" in out


def test_figure_mismatch_leaves_text():
    text = "In SOLO-1, the 7-year overall survival HR was 0.55 (95% CI 0.40-0.76) (PMID: 42180878; narrative review)."
    assert prefer_primary_trial_reports(text, _tools(REVIEW, PRIMARY)) == (text, [])


def test_no_figures_or_no_trial_name_leaves_text():
    for text in (
        "SOLO-1 established olaparib maintenance (PMID: 42180878; narrative review).",
        "Olaparib maintenance reduced risk (HR 0.30; 95% CI 0.23-0.41) (PMID: 42180878; narrative review).",
    ):
        assert prefer_primary_trial_reports(text, _tools(REVIEW, PRIMARY)) == (text, [])


def test_primary_already_cited_or_cited_source_not_a_review():
    text = "In SOLO-1 the HR was 0.30 (0.23-0.41) (PMID: 30345884; PMID: 42180878)."
    assert prefer_primary_trial_reports(text, _tools(REVIEW, PRIMARY)) == (text, [])
    unclassified = {**REVIEW, "evidence_grade": {"study_type": "unclassified"}}
    assert prefer_primary_trial_reports(SENTENCE, _tools(unclassified, PRIMARY)) == (SENTENCE, [])


def test_candidate_must_be_a_trial_report_naming_the_trial():
    other_review = {**PRIMARY, "pmid": "11111111", "evidence_grade": {"study_type": "systematic_review"}, "pub_types": []}
    assert prefer_primary_trial_reports(SENTENCE, _tools(REVIEW, other_review)) == (SENTENCE, [])
    unnamed = {**PRIMARY, "passage_text": PRIMARY["passage_text"].replace("SOLO1", "the")}
    assert prefer_primary_trial_reports(SENTENCE, _tools(REVIEW, unnamed)) == (SENTENCE, [])


def test_other_sentences_are_untouched():
    text = "BRCA2 loss sensitizes cells to PARP inhibition (PMID: 42180878). " + SENTENCE + "\n\nConfidence: high (RCT)"
    out, changes = prefer_primary_trial_reports(text, _tools(REVIEW, PRIMARY))
    assert len(changes) == 1
    assert out.startswith("BRCA2 loss sensitizes cells to PARP inhibition (PMID: 42180878). ")
    assert out.count("PMID: 30345884") == 1


def test_verify_helper_keeps_text_on_failure(monkeypatch):
    from phronesis.phronesis.agents import verifier as verifier_mod

    def boom(*_a: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(verifier_mod, "prefer_primary_trial_reports", boom)
    assert verify_stage.prefer_primary_reports(SENTENCE, _tools(REVIEW, PRIMARY)) == SENTENCE


def test_prompt_asks_for_the_primary_report():
    from phronesis.agents.prompts import SYNTHESIS_SYSTEM_PROMPT

    assert "primary report is in <evidence>, cite that report for its results" in SYNTHESIS_SYSTEM_PROMPT
