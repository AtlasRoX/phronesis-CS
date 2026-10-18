"""WS-3 claim checking repairs (docs/plans/2026-10-16-query-run-repair-plan.md, items 3.1-3.9).

Offline: verifier, confidence and counter-evidence are exercised directly with hand-built tool results; the
orchestrator test patches the model and the verifier so no network or model is used.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agentic import confidence as conf
from phronesis.agentic import counter_evidence as ce
from phronesis.agentic import nli
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.config import AGENTIC
from phronesis.agentic.control import RunControl
from phronesis.agentic.llm import AgentLLM, AgentLLMUnavailable
from phronesis.agentic.state import AgentState
from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.citations import _sentence_spans
from phronesis.phronesis.agents import verifier as vmod
from phronesis.phronesis.agents.verifier import (
    ClaimEvidence,
    ClaimRecord,
    ClinicalVerifier,
    DefectItem,
    DefectSeverity,
    VerificationReport,
)
from phronesis.tools.registry import ToolResult
from tests.fakes import make_offline_orchestrator

TABLE_ANSWER = """### Phase III trial outcomes in metastatic castration-sensitive prostate cancer

| Trial (NCT) | Intervention | Population | Primary Endpoint | Hazard Ratio (95% CI) | p-value |
|---|---|---|---|---|---|
| NCT04497844 (AMPLITUDE) | Niraparib + AAP | mCSPC HRR | Radiographic PFS (HRR-effector subgroup) | 0.571 (0.421-0.774) | 0.0003 |
| | | | Overall survival | 0.80 (0.61-1.04) | 0.12 |
"""


def lit(passages: List[Dict[str, Any]]) -> ToolResult:
    return ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": passages})


def by_text(report: VerificationReport, needle: str) -> ClaimRecord:
    return next(c for c in report.claims if needle in c.claim_text)


# 3.1 markdown-aware claim units ------------------------------------------------------------------------------
def test_headings_and_table_header_rows_are_not_claims():
    units = vmod.markdown_claim_units(TABLE_ANSWER)
    claims = [c for _raw, c, _t in units]
    assert not any("Phase III trial outcomes" in c for c in claims)
    assert not any(c.startswith("Trial (NCT)") or "Intervention | Population" in c for c in claims)
    assert claims[0] == (
        "NCT04497844 (AMPLITUDE) — Niraparib + AAP, mCSPC HRR, Radiographic PFS (HRR-effector subgroup): "
        "Hazard Ratio (95% CI) 0.571 (0.421-0.774); p-value 0.0003"
    )
    # A continuation row carries the trial from the first column forward.
    assert claims[1] == "NCT04497844 (AMPLITUDE) — Overall survival: Hazard Ratio (95% CI) 0.80 (0.61-1.04); p-value 0.12"
    assert all(from_table for _r, _c, from_table in units)
    # The raw sentence is the row itself, so claim_support lines up with the citation map's sentence spans.
    assert units[0][0].startswith("| NCT04497844")


def test_rows_without_figures_list_markers_and_abbreviations():
    text = (
        "| Drug | Target |\n|---|---|\n| Olaparib | PARP |\n\n"
        "- **Smith et al.** reported a benefit, e.g. in carriers, vs. placebo.\n"
        "> 1. Quoted point here.\n"
        "**Key findings**\n"
    )
    claims = [c for _raw, c, _t in vmod.markdown_claim_units(text)]
    assert claims == ["Smith et al. reported a benefit, e.g. in carriers, vs. placebo.", "Quoted point here."]
    # The sentence splitter (shared with the citation map) does not break after "et al." or "e.g.".
    spans = _sentence_spans("Smith et al. found it. Next one, e.g. this. Fig. 2 shows it.")
    assert len(spans) == 3


def test_table_claims_need_the_rows_figures_in_the_evidence():
    trial_doc = ToolResult(
        success=True,
        tool_name="clinical_trials",
        data={"studies": [{"nct_id": "NCT04497844", "title": "AMPLITUDE niraparib abiraterone",
                           "outcome": "radiographic progression-free survival hazard ratio 0.571 (0.421-0.774); p=0.0003"}]},
    )
    synapta = ToolResult(success=True, tool_name="synapta", data={"associations": [{"target": "PARP1", "score": 0.80}]})
    report = ClinicalVerifier().verify_response(
        "niraparib in mCSPC prostate cancer", TABLE_ANSWER, {"clinical_trials": trial_doc, "synapta": synapta}
    )
    rpfs = by_text(report, "Radiographic PFS")
    os_row = by_text(report, "Overall survival")
    assert rpfs.status == "supported"
    # The OS figures appear nowhere, so the row is not credited to an unrelated tool record.
    assert os_row.status != "supported"
    assert not any(c.claim_text.startswith("###") or "Trial (NCT)" in c.claim_text for c in report.claims)


# 3.2 no terms and no figures -> not checked ----------------------------------------------------------------
def test_claim_without_terms_or_figures_is_not_checked():
    v = ClinicalVerifier()
    docs = [vmod._Doc("tool:synapta", "anything at all about overall survival", None, "")]
    status, reason, doc = v._clinical_verdict("Overall survival.", set(), docs)
    assert status == "not_checked" and doc is None and "no distinctive" in reason


# 3.3 population mismatch ------------------------------------------------------------------------------------
MELANOMA_Q = "What is the safety of adjuvant nivolumab in resected stage IIB/IIC cutaneous melanoma?"
CRC_PASSAGE = {
    "pmid": "42583023",
    "title": "Dual versus single-agent immune checkpoint inhibitor therapy in microsatellite instability-high "
             "metastatic colorectal cancer",
    "passage_text": "Grade 3/4 immune-mediated adverse events occurred in 27 of 353 patients (7.6%) receiving nivolumab.",
    "passage_id": "p1",
}
CRC_CLAIM = (
    "Grade 3/4 immune-mediated adverse events occurred in 27 of 353 patients (7.6%) receiving nivolumab "
    "(PMID: 42583023)."
)


def test_population_mismatch_is_flagged_for_figures_from_another_disease():
    report = ClinicalVerifier().verify_response(MELANOMA_Q, f"Adjuvant nivolumab is tolerable. {CRC_CLAIM}", {"lit": lit([CRC_PASSAGE])})
    defect = next(d for d in report.defects if d.category == "POPULATION_MISMATCH")
    assert defect.severity == DefectSeverity.MAJOR
    assert defect.description.startswith("This figure comes from a colorectal cancer study, not melanoma")
    assert defect.sentence == CRC_CLAIM
    assert by_text(report, "27 of 353").status == "unsupported"
    assert not report.passed


def test_population_named_in_the_claim_or_matching_question_is_not_flagged():
    named = CRC_CLAIM.replace("receiving nivolumab", "receiving nivolumab in MSI-H colorectal cancer")
    report = ClinicalVerifier().verify_response(MELANOMA_Q, named, {"lit": lit([CRC_PASSAGE])})
    assert not any(d.category == "POPULATION_MISMATCH" for d in report.defects)
    crc_q = "Safety of nivolumab in MSI-H metastatic colorectal cancer"
    report = ClinicalVerifier().verify_response(crc_q, CRC_CLAIM, {"lit": lit([CRC_PASSAGE])})
    assert not any(d.category == "POPULATION_MISMATCH" for d in report.defects)


# 3.4 absence claims ------------------------------------------------------------------------------------------
def test_absence_needs_one_plain_sentence_to_be_contradicted():
    v = ClinicalVerifier()
    claim = "The retrieved passages do not mention the biological rationale for the chosen duration."
    scattered = vmod._Doc("PMID:1", "the biological effect was large. the rationale was trial design. the chosen duration was 12 months.", None, "")
    status, reason, _doc, _pos = v._absence_verdict(claim, set(), [scattered], [scattered])
    assert status == "not_checked"
    hedged = vmod._Doc("PMID:2", "the biological rationale for the chosen duration remains unclear.", None, "")
    status, _r, _d, _p = v._absence_verdict(claim, set(), [hedged], [hedged])
    assert status == "not_checked"
    plain = vmod._Doc("PMID:3", "the chosen duration was based on a biological rationale from preclinical work. other text.", None, "")
    status, reason, _d, _p = v._absence_verdict(claim, set(), [plain], [plain])
    assert status == "contradicted"
    assert reason.startswith('the retrieved passage says: "the chosen duration was based on a biological rationale')


# 3.5 deterministic removal -------------------------------------------------------------------------------------
def _defect(category: str, sentence: str, description: str = "d") -> DefectItem:
    return DefectItem(category=category, severity=DefectSeverity.CRITICAL, description=description,
                      suggested_correction="", claim=sentence.rstrip("."), sentence=sentence)


def test_remove_flagged_sentences_adds_limitations():
    text = "Intro sentence here. Sotorasib cures every patient. Another fact.\n\n| T | HR |\n|---|---|\n| NCT1 | 0.5 |\n"
    pruned, removed = vmod.remove_flagged_sentences(
        text,
        [_defect("CLAIM_CONTRADICTED", "Sotorasib cures every patient."),
         DefectItem(category="POPULATION_MISMATCH", severity=DefectSeverity.MAJOR, suggested_correction="",
                    description="This figure comes from a colorectal cancer study, not melanoma (PMID:1).",
                    claim="NCT1 — HR 0.5", sentence="| NCT1 | 0.5 |")],
    )
    assert len(removed) == 2
    assert "Sotorasib cures" not in pruned.split("### Evidence limitations")[0]
    assert "| NCT1 | 0.5 |" not in pruned
    assert "Intro sentence here. Another fact." in pruned
    limits = pruned.split("### Evidence limitations")[1]
    assert "a retrieved source contradicts it" in limits and "colorectal cancer study, not melanoma" in limits
    # An existing heading is reused, not duplicated.
    again, _ = vmod.remove_flagged_sentences("A. Bad claim.\n\n## Evidence limitations\n- old\n", [_defect("CLAIM_CONTRADICTED", "Bad claim.")])
    assert again.count("Evidence limitations") == 1 and "- old" in again


class _Streaming:
    def __init__(self, text: str) -> None:
        self.text = text

    async def astream(self, _messages):
        yield SimpleNamespace(content=self.text, additional_kwargs={})


@pytest.mark.asyncio
async def test_orchestrator_removes_contradicted_claim_that_survives_revision(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    bad = "Sotorasib cures every patient."
    draft = f"KRAS signaling in pancreatic cancer is studied (PMID: 123456). {bad} p &lt;0.001 overall."
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _Streaming(draft))
    real = orch.verifier.verify_response
    seen: List[str] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerificationReport:
        report = real(*args, **kwargs)
        text = kwargs["response_markdown"]
        seen.append(text)
        if bad in text:
            report.defects.append(_defect("CLAIM_CONTRADICTED", bad))
            report.passed = False
            report.verification_score = 0.45
        return report

    monkeypatch.setattr(orch.verifier, "verify_response", fake_verify)
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-ws3", enable_web_search=False)
    assert bad not in res.response.split("Evidence limitations")[0]
    assert "Evidence limitations" in res.response and "a retrieved source contradicts it" in res.response
    assert all("&lt;" not in t for t in seen)  # entities decoded before verification
    titles = {t.get("id"): t.get("title", "") for t in res.thoughts}
    assert titles.get("claim_removal", "").startswith("Removed 1 statement")
    texts = " ".join(str(t.get("text") or "") for t in res.thoughts)
    assert "[CLAIM_CONTRADICTED]" not in texts and "Contradicted by a source:" in texts


# 3.6 honest verification step -------------------------------------------------------------------------------
def test_claim_tally_and_defect_labels():
    claims = [
        ClaimRecord(claim_id="c1", claim_type="clinical", claim_text="a", status="supported"),
        ClaimRecord(claim_id="c2", claim_type="clinical", claim_text="b", status="not_checked"),
    ]
    report = VerificationReport(passed=True, verification_score=1.0, claims=claims)
    assert vmod.claim_tally(report) == "2 claims checked: 1 supported, 0 contradicted, 1 not checked"
    line = vmod.defect_display(_defect("CLINICAL_CLAIM", "ORR was 45% &lt; expected."))
    assert line.startswith("Clinical claim not found in the evidence: ORR was 45% < expected")
    assert "[" not in line


@pytest.mark.asyncio
async def test_passed_answer_step_says_what_was_checked(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _Streaming("KRAS signaling in pancreatic cancer (PMID: 123456)."))
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-ws3b", enable_web_search=False)
    step = next(t for t in res.thoughts if t.get("id") in ("verification", "verification_result") and t.get("status") == "done")
    assert "Identifiers, numbers, and citations match" not in str(step.get("text"))
    assert step["title"].startswith(("Checked against the evidence", "Verified against the evidence"))


# 3.7 truncation and entities ----------------------------------------------------------------------------------
def test_truncate_words_and_unescape():
    long = "overall survival " * 30
    cut = vmod.truncate_words(long, 50)
    assert cut.endswith("…") and len(cut) <= 50 and not cut[:-1].endswith(("surv", "overall surv"))
    assert vmod.truncate_words("short text", 50) == "short text"
    assert vmod.unescape_entities("p &lt;0.001 &amp; HR &ge; 1") == "p <0.001 & HR ≥ 1"
    assert vmod.unescape_entities("&lt;script&gt;") == "&lt;script&gt;"  # never decoded into markup


def test_claim_records_are_cut_on_word_boundaries():
    claim = "In the phase 3 trial sotorasib " + "improved survival substantially " * 12 + "(PMID: 123456)."
    report = ClinicalVerifier().verify_response("sotorasib", claim, {"lit": lit([{"pmid": "123456", "passage_text": "x"}])})
    text = report.claims[0].claim_text
    assert len(text) <= 240 and text.endswith("…") and text[-2] != " "


# 3.8 confidence --------------------------------------------------------------------------------------------------
def test_term_overlap_support_alone_is_not_high():
    supported = {"verifier_status": "supported", "n_sources": 1, "best_grade": 0.5}
    assert conf.band(conf.score(supported)) == "medium"
    assert conf.band(conf.score({**supported, "n_sources": 4, "best_grade": 1.0})) == "medium"  # capped
    assert conf.band(conf.score({**supported, "exact_match": True})) == "high"
    assert conf.band(conf.score({**supported, "nli_entailment": 0.95})) == "high"


def test_exact_match_feature():
    doc = "pmid: 42583023. grade 3/4 events in 27 of 353 patients (7.6%)."
    assert conf.exact_match(CRC_CLAIM, "PMID:42583023", doc)
    assert not conf.exact_match(CRC_CLAIM.replace("7.6%", "9.1%"), "PMID:42583023", doc)
    assert not conf.exact_match("No identifier here, 27 of 353 (7.6%).", "PMID:42583023", doc)


def _state(draft: str = "", tool_results: Dict[str, Any] = None) -> AgentState:
    budget = RunBudget()

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    return AgentState(
        query="q", clean_query="q", ctx=None, registry=None, user_id="u1", thread_id="t1", model_name="fake",
        api_key=None, framework=AGENTIC, budget=budget, control=RunControl("u1", interactive=False),
        llm=AgentLLM(None, "fake", None, budget), tool_results=tool_results or {}, emit=_noop, send=_noop, draft=draft,
    )


def test_confidence_artifact_survives_stated_confidence_failure(monkeypatch):
    state = _state("KRAS drives pancreatic tumour growth in mouse models of disease.")
    state.verification = VerificationReport(
        passed=True, verification_score=1.0,
        claims=[ClaimRecord(claim_id="c1", claim_type="mechanistic", claim_text="KRAS drives pancreatic tumour growth",
                            status="supported", evidence=[ClaimEvidence(source_key="PMID:1", passage="kras drives growth")])],
    )
    monkeypatch.setattr(conf, "_elicit_allowed", lambda _s: True)

    async def boom(*_a: Any, **_k: Any) -> Dict[str, float]:
        raise AgentLLMUnavailable("connection refused")

    monkeypatch.setattr(conf, "stated_confidence", boom)
    asyncio.run(conf.ConfidencePhase().run(state))
    art = state.artifacts["claim_confidence"]
    assert art["claims"][0]["band"] == "medium" and art["calibration"]["stated_weight"] == 0.0


def test_gather_claims_skips_headings_via_verifier_claims():
    report = ClinicalVerifier().verify_response("prostate cancer niraparib", TABLE_ANSWER, {})
    state = _state(TABLE_ANSWER)
    state.verification = report
    texts = [c["text"] for c in conf.gather_claims(state)]
    assert texts and not any("###" in t or "Trial (NCT)" in t for t in texts)
    assert not any("Phase III trial outcomes" in s for s in nli.answer_sentences(TABLE_ANSWER))


def test_nli_llm_label_degrades_when_model_unavailable():
    state = _state()
    store = nli.NLIStore(nli.BACKEND_LLM)

    async def unavailable(*_a: Any, **_k: Any) -> Any:
        raise AgentLLMUnavailable("model call failed")

    state.llm.json = unavailable  # type: ignore[method-assign]
    calls = asyncio.run(nli.llm_label(state, store, [("claim text here", [("PMID:1", "passage")])]))
    assert calls <= 1 and not store.claims


# 3.9 after-draft counter-evidence re-check -------------------------------------------------------------------------
def test_recheck_picks_numeric_and_clinical_answer_claims_and_flags_conflicts():
    report = VerificationReport(
        passed=True, verification_score=1.0,
        claims=[
            ClaimRecord(claim_id="c1", claim_type="mechanistic", claim_text="KRAS signaling is relevant", status="supported"),
            ClaimRecord(claim_id="c2", claim_type="clinical", claim_text="Sotorasib improved survival in KRAS G12C lung cancer",
                        status="supported", evidence=[ClaimEvidence(source_key="PMID:1")]),
            ClaimRecord(claim_id="c3", claim_type="mechanistic", claim_text="Sotorasib response rate was 37% in KRAS G12C lung cancer",
                        status="supported"),
        ],
    )
    picked = ce.recheck_claims(report)
    assert [c["text"][:9] for c in picked] == ["Sotorasib", "Sotorasib"]
    state = _state()
    state.verification = report
    state.artifacts["evidence_conflicts"] = {"conflicts": [{
        "claim": "x", "passage": "Sotorasib did not improve survival in KRAS G12C lung cancer patients.", "source_key": "PMID:9",
        "label": "neutral", "likert": 5,
    }]}

    def judge(claim: str, passages: List[Dict[str, str]]) -> Dict[str, Any]:
        return {"status": "contradicted" if "improved" in claim else "unsupported", "reason": "nli"}

    state.bag["nli_judge_fn"] = judge
    phase = ce.CounterEvidenceRecheckPhase()
    assert phase.applies(state)
    asyncio.run(phase.run(state))
    art = state.artifacts["evidence_conflicts"]
    assert art["answer_rechecks"] and any(r["label"] == "contradicts" and r["phase"] == "after_draft" for r in art["conflicts"])


def test_recheck_heuristic_never_counts_as_contradiction():
    state = _state()
    state.artifacts["evidence_conflicts"] = {"conflicts": [{"passage": "Sotorasib showed resistance and no significant benefit in KRAS lung cancer.",
                                                            "source_key": "PMID:9"}]}
    rows = asyncio.run(ce.recheck_answer_claims(state, [{"id": "a1", "text": "Sotorasib benefit in KRAS lung cancer was 37%", "source_key": "PMID:1"}]))
    assert rows and all(r["likert"] < ce.FLAG_AT and r["classifier"] == "heuristic" for r in rows)


def test_evidence_limitations_section_is_not_audited():
    text = "Finding one is real.\n\n### Evidence limitations\n- Removed \"Sotorasib improved overall survival (PMID: 1)\" because a retrieved source contradicts it.\n\n## Next\nAfter text."
    claims = [c for _raw, c, _t in vmod.markdown_claim_units(text)]
    assert claims == ["Finding one is real.", "After text."]


def test_confidence_line_and_proposal_sections_are_not_audited_as_claims():
    from phronesis.phronesis.agents.verifier import markdown_claim_units

    text = (
        "KRAS G12D inhibition selects for RTK reactivation (PMID: 1; preclinical in vivo).\n\n"
        "Confidence: moderate (multiple preclinical in vivo and in vitro studies; no posted clinical trial results).\n"
        "**Confidence:** low (one phase I trial).\n\n"
        "### What would change this conclusion\n- Results of NCT01234567 showing a PFS benefit.\n\n"
        "### Suggested next experiments\n- A randomized phase II trial of the combination.\n"
    )
    claims = [c for _raw, c, _t in markdown_claim_units(text)]
    assert claims == ["KRAS G12D inhibition selects for RTK reactivation (PMID: 1; preclinical in vivo)."]


def test_proposal_sentences_are_not_audited_but_findings_are():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier as V

    for proposal in (
        "A practical repurposing pipeline for KRAS-mutant pancreatic cancer would (1) define the disease target using OpenTargets scores.",
        "Consequently, this step would need to be performed de novo (e.g., docking of an FDA-approved library to KRAS).",
        "Query ClinicalTrials.gov for completed trials of each candidate.",
    ):
        assert V._assertive_text(proposal) is None, proposal
    assert V._assertive_text("Sotorasib improved PFS versus docetaxel (HR 0.66, 95% CI 0.51-0.86; PMID: 36764316).")
    assert V._assertive_text("In CodeBreaK 200, sotorasib was approved after a phase III trial.")


def test_empty_reference_skips_the_plan_step():
    from types import SimpleNamespace

    import pytest

    from phronesis.agentic.plan_graph import RefError, resolve_args

    results = {"n2": SimpleNamespace(data={"drugs": []})}
    with pytest.raises(RefError):
        resolve_args("$n2.drugs[*].pref_name", results)


def test_rating_lines_faers_caveat_and_hypotheses_are_not_claims():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier as V, markdown_claim_units

    text = ("*Evidence level:* label (FDA approval).\n"
            "- FAERS counts are spontaneous reports that mention the drug in any role; they show neither incidence nor causation.\n"
            "Sotorasib is approved for KRAS G12C NSCLC (NDA214665).\n")
    assert [c for _r, c, _t in markdown_claim_units(text)] == ["Sotorasib is approved for KRAS G12C NSCLC (NDA214665)."]
    assert V._assertive_text("H1: FDA-approved MEK inhibitors will show predicted binding to mutant KRAS G12D.") is None


def test_nested_record_sections_get_their_own_document():
    from phronesis.phronesis.agents.verifier import _record_documents

    data = {"safety": [{"ingredient": "sotorasib", "disclaimer": "x " * 200,
                        "approval": {"status": "approved", "application_number": "NDA214665", "first_approval_date": "2021-05-28"}}]}
    docs = _record_documents(data)
    assert any(d.startswith("safety sotorasib approval:") and "nda214665" in d and "2021-05-28" in d for d in docs)


def test_biodiscovery_name_is_rewritten_in_prose():
    from phronesis.agents.authoritative_orchestrator import clean_internal_keys

    out = clean_internal_keys("Candidates are combined as illustrated by the BioDiscovery ranking. The BioDiscovery evidence-weighted ranking scores X.")
    assert "BioDiscovery" not in out and "the evidence-weighted ranking" in out


def test_claims_naming_a_source_are_checked_against_that_source():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier as V, _Doc, prefer_named_sources

    docs = [_Doc("tool:synapta", "kras associations exocrine pancreatic carcinoma mondo_0005192 overall score", None, ""),
            _Doc("tool:primekg", "repurposing exocrine pancreatic carcinoma target overlap network proximity", None, ""),
            _Doc("tool:openfda", "safety tegafur-uracil faers total reports 237 serious 232", None, "")]
    assert prefer_named_sources("PrimeKG repurposing ranks drugs", docs)[0].key == "tool:primekg"
    assert prefer_named_sources("FAERS shows 237 total reports", docs)[0].key == "tool:openfda"
    assert prefer_named_sources("KRAS is associated with PDAC", docs) == docs
    assert V._assertive_text("A practical pipeline for repurposing drugs begins with confirming the KRAS-disease link.") is None


def test_approval_claim_is_checked_against_its_own_drug_record():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier
    from phronesis.tools.registry import ToolResult

    def entry(drug, nda, date):
        return {"ingredient": drug, "disclaimer": "do not rely on openfda " * 30, "total_reports": 1000,
                "approval": {"status": "approved", "summary": f"FDA-approved ({nda}, first approved {date})",
                             "application_number": nda, "first_approval_date": date}}

    tr = {"openfda": ToolResult(success=True, tool_name="openfda_drug_safety",
                                data={"safety": [entry("adagrasib", "NDA216340", "2022-12-12"),
                                                 entry("sotorasib", "NDA214665", "2021-05-28")]})}
    seen = []

    def judge(claim, passages):
        seen.append(passages[0]["passage"])
        return {"status": "contradicted" if "adagrasib" in passages[0]["passage"] and "Sotorasib" in claim else "supported"}

    rep = ClinicalVerifier().verify_response(
        "sotorasib adagrasib FDA approval", "Sotorasib is FDA-approved (NDA 214665, first approved 2021-05-28).", tr,
        entailment_judge=judge)
    claim = next(c for c in rep.claims if "Sotorasib" in c.claim_text)
    assert claim.status == "supported", (claim.status, claim.reason)
    assert seen and "sotorasib" in seen[0] and "214665" in seen[0]


def test_judge_reads_the_window_with_the_claims_figures():
    from phronesis.phronesis.agents.verifier import judge_window

    doc = "header " * 400 + "hazard ratio 0.663 (0.509-0.864) median 5.62 months" + " tail" * 400
    assert "0.663" in judge_window(doc, "HR 0.663 (95% CI 0.509-0.864)")


def test_approval_ids_must_match_the_named_drug_and_rows_check_each_part():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier
    from phronesis.tools.registry import ToolResult

    def entry(drug, nda, date):
        return {"ingredient": drug, "approval": {"status": "approved", "application_number": nda, "first_approval_date": date}}

    tr = {"openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [
        entry("sotorasib", "NDA214665", "2021-05-28"), entry("adagrasib", "NDA216340", "2022-12-12")]})}
    v = ClinicalVerifier()

    def status(text):
        return [c.status for c in v.verify_response("approval", text, tr).claims]

    assert status("Sotorasib – FDA-approved (NDA 214665, first approval 2021-05-28).") == ["supported"]
    assert status("Sotorasib – FDA-approved (NDA 216340, first approval 2022-12-12).") == ["unsupported"]  # wrong drug's ids
    assert status("FDA approval — Sotorasib NDA 214665, 2021-05-28; Adagrasib NDA 216340, 2022-12-12") == ["supported"]
    assert status("FDA approval — Sotorasib NDA 216340, 2021-05-28; Adagrasib NDA 216340, 2022-12-12") == ["unsupported"]


def test_second_verification_question_cases():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier, _Doc, markdown_claim_units
    from phronesis.tools.registry import ToolResult

    def entry(drug, nda, date):
        return {"ingredient": drug, "label_warnings": "Myelodysplastic syndrome/acute myeloid leukemia (MDS/AML) occurred",
                "approval": {"status": "approved", "application_number": nda, "first_approval_date": date}}

    tr = {
        "openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [
            entry("olaparib", "NDA206162", "2014-12-19"), entry("niraparib", "NDA208447", "2017-03-27")]}),
        "synlethdb": ToolResult(success=True, tool_name="synlethdb_partners", data={"partners": [
            {"gene": "BRCA2", "partner": "PARP1", "evidence": "CRISPR/CRISPRi; high throughput", "pmids": ["27453043"]}]}),
    }
    v = ClinicalVerifier()

    def claims(text):
        rep = v.verify_response("BRCA2 synthetic lethal partners olaparib niraparib ovarian cancer", text, tr)
        return [(c.status, c.reason) for c in rep.claims], [d.category for d in rep.defects]

    # Approval dates are not "from a leukemia study" because the label warns about MDS/AML.
    out, defects = claims("Olaparib is a PARP inhibitor; its first FDA approval was 2014-12-19 (openFDA).")
    assert out[0][0] == "supported" and "POPULATION_MISMATCH" not in defects
    # Two drugs, two dates, two records.
    out, _ = claims("The FDA-approved PARP inhibitors olaparib (first approved 2014-12-19) and niraparib (first approved 2017-03-27) act on PARP1.")
    assert out[0][0] == "supported", out
    # A PMID carried inside the SynLethDB record.
    out, _ = claims("SynLethDB lists PARP1 as a synthetic-lethal partner of BRCA2 loss, supported by CRISPR screens (PMID: 27453043).")
    assert out and out[0][0] != "not_checked", out
    units = [c for _r, c, _t in markdown_claim_units("### What evidence would change this conclusion\n- A trial of X in BRCA2 loss.\n")]
    assert units == []


def test_disclosed_gaps_and_background_labels_are_not_audited():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier as V

    assert V._assertive_text("*Rucaparib and talazoparib are FDA-approved PARP inhibitors (established background), but "
                             "approval dates and application numbers were not present in the retrieved evidence.") is None
    assert V._assertive_text("Other partners (e.g., AKT2, BRAF, ATM) are listed in SynLethDB but lack FDA-approved drugs "
                             "that directly target them in the retrieved evidence.") is None
    assert V._assertive_text("In the SOLO-1 trial, olaparib improved PFS (HR = 0.30; 95% CI 0.23-0.41) (PMID: 42180878).")


def test_mixed_source_table_row_checks_each_cell_against_its_own_source():
    from phronesis.phronesis.agents.verifier import ClinicalVerifier
    from phronesis.tools.registry import ToolResult

    tr = {"openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [
              {"ingredient": "olaparib", "approval": {"status": "approved", "application_number": "NDA208558",
                                                      "first_approval_date": "2014-12-19"}}]}),
          "lit": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [
              {"pmid": "42180878", "passage_text": "In SOLO-1, maintenance olaparib improved progression-free survival "
               "versus placebo (hazard ratio 0.30; 95% CI 0.23-0.41) in BRCA-mutated ovarian cancer."}]})}

    def status(ci):
        row = (f"| Drug | Target | FDA approval (application #, date) | SOLO-1 PFS HR (95% CI) |\n|---|---|---|---|\n"
               f"| Olaparib | PARP1 | NDA208558, 2014-12-19 | 0.30 ({ci}) |\n")
        return [c.status for c in ClinicalVerifier().verify_response("olaparib SOLO-1", row, tr).claims]

    assert status("0.23-0.41") == ["supported"]
    assert status("0.20-0.45") == ["unsupported"]  # a wrong CI is still caught


def test_p_value_inequalities_and_operators_grounding():
    v = ClinicalVerifier()
    doc = ToolResult(success=True, tool_name="clinical_trials", data={
        "studies": [{"nct_id": "NCT04497844", "title": "niraparib in prostate cancer",
                     "outcome": "progression-free survival was achieved with p=0.0003"}]
    })
    tr = {"clinical_trials": doc}

    # Upper bound p < 0.001 is supported when document has p = 0.0003 (0.0003 <= 0.001)
    rep1 = v.verify_response("niraparib in prostate cancer",
                             "In NCT04497844, niraparib in prostate cancer showed p < 0.001.", tr)
    assert by_text(rep1, "p < 0.001").status == "supported"

    # Strict check: p < 0.0001 is NOT supported when document only had p = 0.0003 (0.0003 > 0.0001)
    rep2 = v.verify_response("niraparib in prostate cancer",
                             "In NCT04497844, niraparib in prostate cancer showed p < 0.0001.", tr)
    assert by_text(rep2, "p < 0.0001").status == "unsupported"

    # Non-significant inequality: p > 0.05 supported when doc has p = 0.12
    doc_ns = ToolResult(success=True, tool_name="clinical_trials", data={
        "studies": [{"nct_id": "NCT04497844", "title": "niraparib in prostate cancer",
                     "outcome": "overall survival was not statistically significant with p=0.12"}]
    })
    rep3 = v.verify_response("niraparib in prostate cancer",
                             "In NCT04497844, niraparib in prostate cancer showed p > 0.05 for OS.", {"clinical_trials": doc_ns})
    assert by_text(rep3, "p > 0.05").status == "supported"


def test_metric_rounding_and_proportions_grounding():
    v = ClinicalVerifier()
    doc = ToolResult(success=True, tool_name="clinical_trials", data={
        "studies": [{"nct_id": "NCT04497844", "title": "niraparib in prostate cancer",
                     "outcome": "radiographic progression-free survival hazard ratio 0.571 (0.421-0.774); objective response rate: 0.45"}]
    })
    tr = {"clinical_trials": doc}

    # Synthesis with standard 2-decimal rounded HR and CI bounds passes
    rep1 = v.verify_response("niraparib in prostate cancer",
                             "In NCT04497844, niraparib in prostate cancer improved rPFS with HR 0.57 (95% CI 0.42-0.77).", tr)
    assert by_text(rep1, "HR 0.57").status == "supported"

    # Proportion in doc (0.45) grounds percentage in claim (45%)
    rep2 = v.verify_response("niraparib in prostate cancer",
                             "In NCT04497844, niraparib in prostate cancer achieved a 45% response rate.", tr)
    assert by_text(rep2, "45%").status == "supported"


def test_judge_window_salient_terms_fallback():
    intro = "Background: Cancer research is broad. " * 30  # ~1170 chars
    results = "Results: Osimertinib showed remarkable clinical activity and overcame T790M resistance."
    doc_text = intro + results

    # Claim without decimal numbers or application dates
    claim = "Osimertinib overcame T790M resistance in patients."
    window = vmod.judge_window(doc_text, claim, size=1500)
    # The salient term probe finds osimertinib/T790M and centers the window so results are included
    assert "overcame T790M resistance" in window


def test_negated_safety_verbs_in_counter_evidence():
    text = "The treatment was well tolerated and did not show significant toxicity."
    cleaned = ce._NEGATED_SAFETY_RE.sub(" ", text)
    contra_cues = sum(w for pat, w in ce._CONTRA_CUES if pat.search(cleaned))
    assert contra_cues == 0  # not falsely classified as an efficacy contradiction


def test_confidence_exact_match_with_rounded_figures():
    doc_text = "Trial NCT04497844 reported radiographic PFS with hazard ratio 0.571 (0.421-0.774)."
    claim_text = "In NCT04497844, the hazard ratio was 0.57 (95% CI 0.42-0.77)."
    # Rounded figures qualify as exact match for empirical calibration
    assert conf.exact_match(claim_text, "NCT:NCT04497844", doc_text) is True


def test_mixed_source_table_row_attributes_all_sources():
    tr = {
        "openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [
            {"ingredient": "olaparib", "approval": {"status": "approved", "application_number": "NDA208558",
                                                    "first_approval_date": "2014-12-19"}}]}),
        "lit": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [
            {"pmid": "42180878", "passage_text": "In SOLO-1, maintenance olaparib improved progression-free survival "
             "versus placebo (hazard ratio 0.30; 95% CI 0.23-0.41) in BRCA-mutated ovarian cancer."}]}),
    }
    row = (f"| Drug | Target | FDA approval (application #, date) | SOLO-1 PFS HR (95% CI) |\n|---|---|---|---|\n"
           f"| Olaparib | PARP1 | NDA208558, 2014-12-19 | 0.30 (0.23-0.41) |\n")
    report = ClinicalVerifier().verify_response("olaparib SOLO-1", row, tr)
    claim = report.claims[0]
    assert claim.status == "supported"
    source_keys = {e.source_key for e in claim.evidence}
    # Both sources are recorded in the claim's evidence provenance
    assert "tool:openfda" in source_keys and "PMID:42180878" in source_keys
    # And both are recorded in claim_support
    support_keys = {s["evidence_key"] for s in report.claim_support}
    assert "tool:openfda" in support_keys and "PMID:42180878" in support_keys

