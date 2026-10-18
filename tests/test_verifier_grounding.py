"""Verifier grounding regressions from Phronesis_Source_Audit_2026-10-18.md (findings F6-F9) and operational claims.

F6: FDA eligibility comes from the record's own URL host, never from a URL quoted in its text.
F7: population checks cover typed clinical tool records (CIViC/variant evidence), not only articles.
F8: numbers are grounded as relationships (subgroup/arm/denominator); an explicit relational judge verdict is honoured.
F9: the PMID 39369609 positive control uses the real, provenance-tracked abstract (tests/fixtures/primary/).
Operational sentences (a lookup failed, a record was not retrieved) are grounded in the run's tool outcomes, never in
a scientific citation.

Offline: no network requests are made; the reserved example hosts below are only input strings.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from phronesis.phronesis.agents.verifier import ClaimRecord, ClinicalVerifier, VerificationReport, _Doc, is_regulatory_doc
from phronesis.tools.registry import ToolResult
from phronesis.utils import source_authority as sa

Q = "KRAS G12C inhibitors in NSCLC and colorectal cancer: FDA-approved indications and clinical evidence"
APPROVAL = "Sotorasib received FDA accelerated approval for KRAS G12C-mutated NSCLC."
FIXTURE = Path(__file__).parent / "fixtures" / "primary" / "pmid_39369609.json"


def tool(name: str, data: Dict[str, Any]) -> ToolResult:
    return ToolResult(success=True, tool_name=name, data=data)


def lit(passages: List[Dict[str, Any]]) -> ToolResult:
    return tool("lithoscribe_literature_synthesis", {"ranked_passages": passages})


def claim_for(report: VerificationReport, needle: str) -> ClaimRecord:
    return next(c for c in report.claims if needle in c.claim_text)


def categories(report: VerificationReport) -> List[str]:
    return [d.category for d in report.defects]


# F6: regulatory origin from the record's own host -------------------------------------------------------------
SNIPPET = "Sotorasib accelerated approval for KRAS G12C-mutated NSCLC."


def web(results: List[Dict[str, Any]], top_page: Dict[str, Any] | None = None) -> ToolResult:
    return tool("websurfer_search", {"query": "sotorasib approval", "results": results, "top_page": top_page})


@pytest.mark.parametrize(
    "url",
    [
        "https://www.fda.gov.example.test/fake",  # lookalike: the host is example.test
        "https://fda.gov@evil.example.test/drugs",  # userinfo names FDA; the host is evil.example.test
        "https://fda.gov.evil.example.test",
        "ftp://www.fda.gov/drugs",  # not a web page
        "https://203.0.113.7/fda.gov",  # an address literal, not a host
        "",
    ],
)
def test_lookalike_userinfo_and_missing_urls_are_not_fda_records(url):
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([{"title": "FDA", "url": url, "snippet": SNIPPET}])})
    c = claim_for(report, "accelerated approval")
    assert c.status == "unsupported" and "no FDA label or approval record" in c.reason


def test_a_third_party_page_quoting_an_fda_link_is_not_an_fda_record():
    page = {"title": "News", "url": "https://example.test/article",
            "snippet": SNIPPET + " Source: https://www.fda.gov/drugs/resources-information-approved-drugs/sotorasib"}
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([page])})
    assert claim_for(report, "accelerated approval").status == "unsupported"


@pytest.mark.parametrize(
    "url",
    ["https://www.fda.gov/drugs/resources-information-approved-drugs/sotorasib",
     "https://accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=214665",
     "https://WWW.FDA.GOV./drugs/notice"],
)
def test_genuine_fda_hosts_are_eligible(url):
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([{"title": "FDA", "url": url, "snippet": SNIPPET}])})
    assert claim_for(report, "accelerated approval").status == "supported"


def test_origin_alone_does_not_establish_the_claim():
    unrelated = {"title": "FDA", "url": "https://www.fda.gov/drugs/notice", "snippet": "Guidance on bioequivalence studies for generic tablets."}
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([unrelated])})
    assert claim_for(report, "accelerated approval").status == "unsupported"


def test_a_redirect_is_judged_by_its_final_host():
    redirected_away = {"title": "FDA", "url": "https://www.fda.gov/drugs/sotorasib", "final_url": "https://example.test/landing",
                       "content": SNIPPET}
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([], top_page=redirected_away)})
    assert claim_for(report, "accelerated approval").status == "unsupported"
    redirected_in = dict(redirected_away, url="https://example.test/go", final_url="https://www.fda.gov/drugs/sotorasib")
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"websurfer": web([], top_page=redirected_in)})
    assert claim_for(report, "accelerated approval").status == "supported"
    assert sa.record_url({"url": "https://a.example.test", "redirects": ["https://b.example.test", "https://www.fda.gov/x"]}) == "https://www.fda.gov/x"


def test_one_authority_policy_is_shared():
    from phronesis.agents import prompts
    from phronesis.agents.citations import source_authority as cite_authority

    assert prompts.source_authority is sa.source_authority and cite_authority is sa.source_authority
    assert sa.source_authority("https://www.fda.gov.example.test/fake") == "web"
    assert sa.source_authority("https://fda.gov@evil.example.test") == "web"
    assert sa.source_authority("https://www.prnewswire.com/news/x") == "manufacturer"
    doc = _Doc("tool:websurfer", "sotorasib approval https://www.fda.gov/x", None, "", "https://example.test/a")
    assert not is_regulatory_doc(doc, APPROVAL)


# F7: population checks for typed clinical tool records ----------------------------------------------------------
NSCLC_TEXT = ("In advanced non-small cell lung cancer (NSCLC), KRAS mutations were associated with resistance to "
              "gefitinib and erlotinib; the objective response rate to erlotinib was 5.0% in KRAS-mutant tumours.")
CRC_CLAIM = "In colorectal cancer, the objective response rate to erlotinib was 5.0% in KRAS-mutant tumours."


def variant_tool() -> ToolResult:
    return tool("variant_intel", {"civic": {"evidence": [
        {"disease": "Lung Non-small Cell Carcinoma", "therapies": "erlotinib", "evidence_level": "B",
         "description": NSCLC_TEXT, "variant": "KRAS mutation"},
    ]}})


def _relational_judge(claim: str, passages: List[Dict[str, str]]) -> Dict[str, str]:
    return {"status": "supported", "reason": "the passage reports the same response rate"}


@pytest.mark.parametrize("judge", [None, _relational_judge])
def test_a_variant_record_about_nsclc_cannot_support_a_crc_claim(judge):
    report = ClinicalVerifier().verify_response(Q, CRC_CLAIM, {"variants": variant_tool()}, entailment_judge=judge)
    c = claim_for(report, "5.0%")
    assert c.status == "unsupported" and "population mismatch" in c.reason
    assert "POPULATION_MISMATCH" in categories(report)


@pytest.mark.parametrize("judge", [None, _relational_judge])
def test_the_same_data_through_the_article_path_fails_the_same_way(judge):
    passage = {"pmid": "00000002", "passage_id": "a", "title": "Synthetic NSCLC cohort", "passage_text": NSCLC_TEXT}
    report = ClinicalVerifier().verify_response(Q, CRC_CLAIM + " (PMID: 00000002)", {"lit": lit([passage])}, entailment_judge=judge)
    assert claim_for(report, "5.0%").status == "unsupported" and "POPULATION_MISMATCH" in categories(report)


def test_a_variant_record_in_the_claims_disease_still_supports_it():
    right = CRC_CLAIM.replace("In colorectal cancer", "In NSCLC")
    report = ClinicalVerifier().verify_response(Q, right, {"variants": variant_tool()})
    assert claim_for(report, "5.0%").status == "supported" and "POPULATION_MISMATCH" not in categories(report)


def test_a_label_listing_unrelated_warnings_is_not_a_population_mismatch():
    label = tool("openfda_drug_safety", {"safety": [{
        "ingredient": "sotorasib",
        "label": {"indications_and_usage": "LUMAKRAS is indicated for KRAS G12C-mutated locally advanced or metastatic NSCLC.",
                  "warnings": "Hepatotoxicity; interstitial lung disease. Secondary acute myeloid leukemia (AML) was not observed."},
        "approval": {"status": "approved", "application_number": "NDA214665", "first_approval_date": "2021-05-28"},
    }]})
    report = ClinicalVerifier().verify_response(Q, APPROVAL, {"openfda": label})
    c = claim_for(report, "accelerated approval")
    assert "population mismatch" not in (c.reason or "") and "POPULATION_MISMATCH" not in categories(report)


# F8: numerical relationships, not a bag of numbers --------------------------------------------------------------
COHORT = {
    "pmid": "00000003", "passage_id": "r", "title": "Synthetic KRAS-mutant cohort treated with sotorasib",
    "passage_text": "Of 45 patients with KRAS-mutant NSCLC, 16 had KRAS G12C. In the KRAS G12C subgroup (n=16), "
                    "median progression-free survival with sotorasib was 19.9 months.",
}
WRONG_N = "In the KRAS G12C subgroup (n=45), median progression-free survival with sotorasib was 19.9 months (PMID: 00000003)."
RIGHT_N = WRONG_N.replace("(n=45)", "(n=16)")


def _denominator_judge(claim: str, passages: List[Dict[str, str]]) -> Dict[str, str]:
    if "n=45" in claim:
        return {"status": "unsupported",
                "reason": "denominator mismatch: n=45 is the whole KRAS-mutant cohort; the G12C subgroup is n=16"}
    return {"status": "supported", "reason": "matches"}


def _inconclusive_judge(claim: str, passages: List[Dict[str, str]]) -> Dict[str, str]:
    return {"status": "unsupported", "reason": "the excerpt does not clearly state this"}


# The subgroup's own size is not stated in this text, so only a reader of the relationship (the judge) can tell that
# n=45 is the whole cohort: the numbers all occur, and the deterministic binding check has nothing to compare.
COHORT_UNSTATED = dict(COHORT, passage_text="The KRAS-mutant NSCLC cohort comprised 45 patients (n=45) treated with "
                                            "sotorasib; median progression-free survival in the KRAS G12C subgroup was "
                                            "19.9 months.")


def test_an_explicit_denominator_error_from_the_judge_is_not_overridden_by_matching_numbers():
    report = ClinicalVerifier().verify_response(Q, WRONG_N, {"lit": lit([COHORT_UNSTATED])})
    assert claim_for(report, "19.9 months").status == "supported"  # numbers all present: deterministic check passes
    report = ClinicalVerifier().verify_response(Q, WRONG_N, {"lit": lit([COHORT_UNSTATED])}, entailment_judge=_denominator_judge)
    c = claim_for(report, "19.9 months")
    assert c.status == "unsupported" and "denominator" in c.reason
    assert report.defects and "NUMERIC_RELATION_MISMATCH" in categories(report)
    assert c.cited_source_supported is not True


def test_an_inconclusive_judge_does_not_overturn_matching_numbers():
    report = ClinicalVerifier().verify_response(Q, WRONG_N, {"lit": lit([COHORT_UNSTATED])}, entailment_judge=_inconclusive_judge)
    assert claim_for(report, "19.9 months").status == "supported"
    assert "NUMERIC_RELATION_MISMATCH" not in categories(report)


def test_a_parent_cohort_size_given_for_a_subgroup_fails_without_a_judge():
    report = ClinicalVerifier().verify_response(Q, WRONG_N, {"lit": lit([COHORT])})
    c = claim_for(report, "19.9 months")
    assert c.status == "unsupported" and "16" in c.reason
    assert "NUMERIC_RELATION_MISMATCH" in categories(report)


@pytest.mark.parametrize("judge", [None, _denominator_judge, _inconclusive_judge])
def test_the_correct_subgroup_size_still_passes(judge):
    report = ClinicalVerifier().verify_response(Q, RIGHT_N, {"lit": lit([COHORT])}, entailment_judge=judge)
    c = claim_for(report, "19.9 months")
    assert c.status == "supported", c.reason
    assert "NUMERIC_RELATION_MISMATCH" not in categories(report)


# F9: the real PMID 39369609 abstract as a provenance-tracked positive control --------------------------------------
def _fixture() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_primary_fixture_is_intact_and_honest_about_the_correction():
    fx = _fixture()
    assert fx["pmid"] == "39369609" and fx["doi"] == "10.1016/j.lungcan.2024.107960"
    for line in ("2L", "2L+"):
        ex = fx["excerpts"][line]
        assert hashlib.sha256(ex["text"].encode("utf-8")).hexdigest() == ex["sha256"]
        text = ex["text"].replace(" ", " ")
        res = fx["results"][line]
        for arm in ("sotorasib", "docetaxel"):
            assert f"(N={res[arm]['n']})" in text
        assert f"{res['mortality_hr']} ({res['hr_ci'][0]}-{res['hr_ci'][1]})" in text
    assert fx["correction"]["exists"] and not fx["correction"]["content_retrieved"] and not fx["correction"]["applied"]


def _primary_passage() -> Dict[str, Any]:
    fx = _fixture()
    return {"pmid": fx["pmid"], "passage_id": "abstract", "section": "abstract", "title": fx["title"],
            "passage_text": fx["excerpts"]["2L"]["text"] + " " + fx["excerpts"]["2L+"]["text"]}


def test_correct_claims_from_the_real_abstract_are_supported():
    passages = {"lit": lit([_primary_passage()])}
    claims = [
        "In the 2L setting, median overall survival was 10.2 months (95% CI 7.6-16.3) with sotorasib (N=102) versus "
        "6.0 months (95% CI 4.2-11.0) with docetaxel (N=58), mortality HR 0.62 (95% CI 0.41-0.93) (PMID: 39369609).",
        "In the 2L+ setting, median overall survival was 10.2 months (95% CI 8.0-14.6) with sotorasib (N=164) versus "
        "7.2 months (95% CI 5.1-10.6) with docetaxel (N=116), mortality HR 0.65 (95% CI 0.49-0.87) (PMID: 39369609).",
    ]
    for answer in claims:
        report = ClinicalVerifier().verify_response(Q, answer, passages)
        c = claim_for(report, "median overall survival")
        assert c.status == "supported", (answer, c.reason)


def test_swapping_the_2l_plus_sotorasib_cohort_for_the_comparator_arm_fails():
    swapped = ("In the 2L+ setting, median overall survival was 7.2 months with docetaxel (N=164) "
               "(PMID: 39369609).")
    report = ClinicalVerifier().verify_response(Q, swapped, {"lit": lit([_primary_passage()])})
    c = claim_for(report, "median overall survival")
    assert c.status == "unsupported", c.reason
    assert "NUMERIC_RELATION_MISMATCH" in categories(report)


# Operational status sentences ------------------------------------------------------------------------------------
def test_a_failed_lookup_is_grounded_in_the_tool_outcome_not_in_a_paper():
    failed = ToolResult(success=False, tool_name="lithoscribe_literature_synthesis",
                        error="No peer-reviewed publications found in this search for: 'PMID:39369609'")
    answer = "The exact-identifier search for PMID 39369609 failed in this run."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": failed, "lit2": lit([_primary_passage()])})
    c = claim_for(report, "search for PMID 39369609 failed")
    assert c.claim_type == "operational"
    assert all(not (e.source_key or "").startswith(("PMID:", "DOI:")) for e in c.evidence)
    assert c.status == "supported" and c.evidence and c.evidence[0].source_key.startswith("trace:")


def test_an_operational_sentence_without_a_matching_tool_event_is_not_checked():
    answer = "The full correction document was not retrieved, so corrected content could not be applied."
    report = ClinicalVerifier().verify_response(Q, answer, {"lit": lit([_primary_passage()])})
    c = claim_for(report, "correction document was not retrieved")
    assert c.claim_type == "operational" and c.status == "not_checked" and "operational status" in c.reason
    assert not c.evidence


def test_surf_url_records_the_final_url_and_redirect_hops(monkeypatch):
    import asyncio

    import httpx

    from phronesis.tools import web_search as ws

    async def public(_url):
        return None

    monkeypatch.setattr(ws, "_check_public_url", public)
    monkeypatch.setattr(ws, "_check_connected_peer", lambda resp, url: None)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.test":
            return httpx.Response(301, headers={"location": "https://www.fda.gov/drugs/sotorasib"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><title>FDA</title><body><p>" + SNIPPET + "</p></body></html>")

    client = ws.FreeWebSearchClient(transport=httpx.MockTransport(handle))
    page = asyncio.run(client.surf_url("https://example.test/go"))
    assert page["requested_url"] == "https://example.test/go"
    assert page["final_url"] == "https://www.fda.gov/drugs/sotorasib" and page["redirects"] == [page["final_url"]]
    assert sa.source_authority(sa.record_url(page)) == "regulatory"


# Live regression (18 Oct 2026, Phronesis_Next_Test_Query.txt): the answer was right but scored 0.15 because the
# retrieved erratum's PMID was "not found", the correction-status sentences were judged against the paper, and the
# "Source: abstract of PMID ...; URL: ..." lines were audited as claims. The answer text is the live run's own.
LIVE_ANSWER = Path(__file__).parent / "fixtures" / "primary" / "live_answer_pmid_39369609.md"


def _live_literature_result() -> ToolResult:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    passages = [
        {"pmid": fx["pmid"], "title": fx["title"], "passage_id": f"abs-{k}", "section": "abstract", "passage_text": e["text"]}
        for k, e in fx["excerpts"].items()
    ]
    passages.append({
        "pmid": fx["pmid"], "title": fx["title"], "passage_id": "abs-design", "section": "abstract",
        "passage_text": "Observational comparative effectiveness study of pretreated KRAS G12C-mutated advanced NSCLC patients "
                        "in a US-based electronic health record-derived de-identified database, using overlap weighting "
                        "propensity score methods.",
    })
    retrieval = {"pinned": [{
        "id": fx["pmid"], "normalized": fx["pmid"], "kind": "pmid", "status": "found", "via": "europepmc",
        "corrections": [{"id": fx["correction"]["pmid"], "type": fx["correction"]["type"], "retrieved": True,
                         "content_available": False, "applied": False}],
    }]}
    return tool("lithoscribe_literature_synthesis", {"ranked_passages": passages, "retrieval": retrieval})


def test_live_answer_correction_and_source_lines_are_not_false_findings():
    answer = LIVE_ANSWER.read_text(encoding="utf-8")
    report = ClinicalVerifier().verify_response("Retrieve PMID 39369609", answer, {"lit": _live_literature_result()})
    descriptions = " ".join(d.description for d in report.defects)
    assert "39984314" not in descriptions  # the retrieved erratum is a retrieved record
    assert not any(c.claim_text.lstrip("*_ ").lower().startswith("source") for c in report.claims)
    corrections = [c for c in report.claims if "correction" in c.claim_text.lower()]
    assert corrections and all(c.claim_type == "operational" and c.status == "supported" for c in corrections)
    numeric = [c for c in report.claims if "102" in c.claim_text or "164" in c.claim_text]
    assert numeric and all(c.status == "supported" for c in numeric)
    assert not [c for c in report.claims if c.status in ("unsupported", "contradicted")]


LIVE_ANSWER_2 = Path(__file__).parent / "fixtures" / "primary" / "live_answer2_pmid_39369609.md"


def test_second_live_answer_erratum_linkage_and_figure_provenance_are_not_false_findings():
    # The second matched run: "A correction (erratum) exists for PMID X (linked erratum PMID Y)" is record linkage the
    # exact lookup reports, and "*All figures are taken verbatim from the abstract ...*" only locates the numbers.
    report = ClinicalVerifier().verify_response("Retrieve PMID 39369609", LIVE_ANSWER_2.read_text(encoding="utf-8"),
                                                {"lit": _live_literature_result()})
    erratum = claim_for(report, "erratum) exists")
    assert erratum.claim_type == "operational" and erratum.status == "supported"
    assert not any("taken verbatim" in c.claim_text for c in report.claims)
    numeric = [c for c in report.claims if "102" in c.claim_text or "164" in c.claim_text]
    assert all(c.status == "supported" for c in numeric)


def test_third_live_answer_has_no_false_findings():
    # The final matched run: its only remaining findings were "**Article:** <URL>" and "Erratum notice: PMID ..."
    # location lines. Every evidence claim in it is supported by the real abstract.
    answer = (Path(__file__).parent / "fixtures" / "primary" / "live_answer3_pmid_39369609.md").read_text(encoding="utf-8")
    report = ClinicalVerifier().verify_response("Retrieve PMID 39369609", answer, {"lit": _live_literature_result()})
    assert not [c for c in report.claims if c.status in ("unsupported", "contradicted")], [
        (c.status, c.claim_text[:80]) for c in report.claims if c.status != "supported"]
    assert not any(c.claim_text.lstrip("*_- ").startswith(("Article", "Erratum notice")) for c in report.claims)
