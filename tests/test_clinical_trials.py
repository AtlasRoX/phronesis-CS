"""
Offline tests for the ClinicalTrials.gov v2 feature (Lithoscribe): parsers, the httpx client (MockTransport),
the query planner, verifier grounding, evidence rendering and orchestrator routing.

Every payload comes from tests/fixtures/ctgov/, trimmed real API responses captured on 2026-10-13.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl

import httpx
import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.prompts import SYNTHESIS_SYSTEM_PROMPT, build_evidence_block, get_synthesis_system_prompt
from phronesis.extensions import hooks
from phronesis.extensions import trials as trials_ext
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult
from phronesis.trials import clinical_trials as ct
from phronesis.trials.render import describe_result, fallback_markdown, render_trials_evidence
from phronesis.trials.trial_grounding import audit_trial_figures, collect_trial_docs, extract_figures
from phronesis.trials.trial_query import (
    extract_condition,
    extract_drugs,
    extract_phases,
    extract_statuses,
    has_clinical_intent,
    plan_trial_query,
)
from phronesis.utils.http_limits import TTLCache
from tests.fakes import called, make_offline_orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "ctgov"
TOOL = trials_ext.TOOL_NAME
FLAURA, LAURA = "NCT02296125", "NCT03521154"


def load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def study(fixture: str, nct: str) -> Dict[str, Any]:
    return next(s for s in load(fixture)["studies"] if s["protocolSection"]["identificationModule"]["nctId"] == nct)


@pytest.fixture(autouse=True)
def trial_hooks():
    """Keep this feature's source and verifier hooks registered for each test, discarding test registrations."""
    with hooks.isolated_hooks():
        trials_ext.register_hooks()
        yield


# -----------------------------------------------------------------------------
# Mock transport
# -----------------------------------------------------------------------------

class Upstream:
    """Routes /api/v2/studies requests to fixtures and records the exact query params of each request."""

    def __init__(self, route: Callable[[Dict[str, str]], httpx.Response]):
        self.route = route
        self.requests: List[Dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/studies"
        assert request.headers["user-agent"] == ct.USER_AGENT
        params = dict(parse_qsl(request.url.query.decode()))
        self.requests.append(params)
        return self.route(params)

    def client(self) -> ct.ClinicalTrialsGovClient:
        return ct.ClinicalTrialsGovClient(httpx.MockTransport(self), cache=TTLCache(ttl_s=600), retry_backoff_s=0)


def fixture_response(name: str) -> httpx.Response:
    body = load(name)
    body.pop("_source", None)
    return httpx.Response(200, json=body)


def results_for(params: Dict[str, str]) -> httpx.Response:
    """Posted-results batch: only the requested studies, in the fixture's (not the request's) order."""
    wanted = set(params["filter.ids"].split(","))
    studies = [s for s in load("results_flaura_laura.json")["studies"]
               if s["protocolSection"]["identificationModule"]["nctId"] in wanted]
    return httpx.Response(200, json={"studies": studies})


def osimertinib_route(params: Dict[str, str]) -> httpx.Response:
    if params.get("fields") == ct.RESULT_FIELDS:
        return results_for(params)
    if params.get("aggFilters") == "results:with":
        return fixture_response("search_osimertinib_nsclc_with_results.json")
    return fixture_response("search_osimertinib_nsclc.json")


def nct_route(params: Dict[str, str]) -> httpx.Response:
    if params.get("fields") == ct.RESULT_FIELDS:
        return results_for(params)
    return fixture_response("search_ids_flaura_laura.json")


def run(coro):
    return asyncio.run(coro)


def flaura_laura_result() -> ToolResult:
    up = Upstream(nct_route)
    return run(ct.search_trials(client=up.client(), nct_ids=[FLAURA, LAURA]))


def osimertinib_result() -> ToolResult:
    up = Upstream(osimertinib_route)
    return run(ct.search_trials(client=up.client(), intervention="osimertinib", condition="non-small cell lung cancer"))


# -----------------------------------------------------------------------------
# 1. Parsers
# -----------------------------------------------------------------------------

def test_parse_trial_fields_from_search_record():
    t = ct.parse_trial(study("search_ids_flaura_laura.json", FLAURA))
    assert t.nct_id == FLAURA and t.acronym == "FLAURA"
    assert t.url == "https://clinicaltrials.gov/study/NCT02296125"
    assert t.phases == ["PHASE3"] and t.study_type == "INTERVENTIONAL"
    assert (t.enrollment_count, t.enrollment_type) == (674, "ACTUAL")
    assert (t.lead_sponsor, t.lead_sponsor_class) == ("AstraZeneca", "INDUSTRY")
    assert t.conditions == ["Locally Advanced or Metastatic EGFR Sensitising Mutation Positive Non Small Cell Lung Cancer"]
    assert {"type": "DRUG", "name": "AZD9291 80 mg/40 mg + placebo"} in t.interventions
    assert t.primary_outcomes[0] == {
        "measure": "Median Progression Free Survival (PFS) (Months)",
        "time_frame": "At baseline and every 6 weeks for the first 18 months and then every 12 weeks relative to randomisation until progression",
    }
    assert (t.start_date, t.results_first_posted, t.has_results) == ("2014-12-03", "2018-11-06", True)


def test_parse_trial_keeps_partial_dates_and_why_stopped():
    t = ct.parse_trial(study("search_osimertinib_nsclc.json", "NCT02954523"))
    assert t.start_date == "2016-10"  # YYYY-MM stays a string
    assert (t.overall_status, t.why_stopped) == ("TERMINATED", "Lack of accrual")


def test_parse_trial_drops_empty_interventions_and_missing_enrollment():
    sparse = ct.parse_trial(study("sparse_interventions.json", "NCT04695925"))
    assert sparse.interventions == []  # the API returned [{}, {}, {}]
    approved = ct.parse_trial(study("search_kras_g12c.json", "NCT05162443"))
    assert approved.overall_status == "APPROVED_FOR_MARKETING"
    assert approved.enrollment_count is None and approved.phases == []


def test_parse_posted_results_keeps_primary_and_efficacy_verbatim():
    results = ct.parse_posted_results(study("results_flaura_laura.json", FLAURA))
    assert len(results) == ct.MAX_MEASURES
    assert [r.type for r in results[:2]] == ["PRIMARY", "PRIMARY"]
    titles = [r.title for r in results]
    assert "Objective Response Rate (ORR)" in titles
    assert not any("Plasma" in t or "Depth of Response" in t for t in titles)  # PK filtered; depth fell past the cap

    orr = next(r for r in results if r.title == "Objective Response Rate (ORR)")
    arm = orr.measurements[0]
    assert (arm.group_id, arm.group_title, arm.n) == ("OG000", "Osimertinib 80 mg (Global Cohort)", "279")
    assert (arm.value, arm.lower, arm.upper) == ("76.7", "71.29", "81.53")
    assert orr.measurements[1].value == "69.0"  # never float-normalized to "69"

    pfs = results[0]
    assert pfs.param_type == "MEDIAN" and pfs.unit == "Months"
    hr = pfs.analyses[0]
    assert (hr.param_type, hr.param_value, hr.ci_lower, hr.ci_upper, hr.p_value, hr.method) == (
        "Hazard Ratio (HR)", "0.46", "0.37", "0.57", "<0.0001", "Log Rank")

    multi = results[1]
    assert len(multi.measurements) == 12
    assert "Progression free at 12 months (%)" in {m.class_title for m in multi.measurements}


def test_parse_posted_results_preserves_na_with_comment():
    pfs = ct.parse_posted_results(study("results_flaura_laura.json", LAURA))[0]
    osi = pfs.measurements[0]
    assert (osi.value, osi.upper) == ("39.13", "NA")
    assert osi.comment.startswith("NA - Not calculated as insufficient number of events")


def test_measurement_and_analysis_caps():
    measures = study("results_flaura_laura.json", FLAURA)["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]
    plasma = next(m for m in measures if m["title"].startswith("Plasma Concentrations"))
    assert sum(len(c["measurements"]) for cl in plasma["classes"] for c in cl["categories"]) > ct.MAX_MEASUREMENTS
    assert len(ct.parse_measure(plasma).measurements) == ct.MAX_MEASUREMENTS


def test_result_pmids_only_result_type():
    adaura = load("references_adaura.json")["studies"][0]
    assert ct.parse_result_pmids(adaura) == ["33596365"]  # the DERIVED references are excluded
    assert ct.parse_result_pmids(study("results_flaura_laura.json", FLAURA)) == []


def test_display_labels():
    assert ct.phase_label(["PHASE2", "PHASE3"]) == "Phase 2/3"
    assert ct.phase_label(["EARLY_PHASE1"]) == "Early Phase 1"
    assert ct.phase_label([]) == "N/A"
    assert ct.enrollment_label(674, "ACTUAL") == "674 actual"
    assert ct.enrollment_label(500, "ESTIMATED") == "500 est."
    assert ct.status_label("ACTIVE_NOT_RECRUITING") == "Active, not recruiting"


# -----------------------------------------------------------------------------
# 2. Client (httpx.MockTransport)
# -----------------------------------------------------------------------------

def test_client_relevance_and_companion_then_one_results_batch():
    up = Upstream(osimertinib_route)
    res = run(ct.search_trials(client=up.client(), intervention="osimertinib", condition="non-small cell lung cancer"))
    assert res.success, res.error
    search = [p for p in up.requests if p.get("fields") == ct.SEARCH_FIELDS]
    relevance = next(p for p in search if "aggFilters" not in p)
    companion = next(p for p in search if "aggFilters" in p)
    assert relevance == {
        "query.intr": "osimertinib",
        "query.cond": "non-small cell lung cancer",
        "fields": ct.SEARCH_FIELDS,
        "pageSize": "10",
        "countTotal": "true",
    }
    assert companion == {**relevance, "aggFilters": "results:with", "pageSize": "5"}
    batches = [p for p in up.requests if p.get("fields") == ct.RESULT_FIELDS]
    assert len(batches) == 1 and len(up.requests) == 3

    data = res.data
    ids = [t["nct_id"] for t in data["trials"]]
    assert ids == ["NCT04695925", "NCT02954523", "NCT07036016", "NCT07642024", LAURA, "NCT04035486", "NCT01802632"]
    assert set(batches[0]["filter.ids"].split(",")) == {"NCT02954523", LAURA, "NCT04035486", "NCT01802632"}
    assert data["total_count"] == 335 and data["query"]["strategy"] == "intervention+condition"
    assert data["results_fetched_for"] == [LAURA]
    laura = next(t for t in data["trials"] if t["nct_id"] == LAURA)
    assert laura["posted_results"][0]["measurements"][0]["upper"] == "NA"
    assert res.canonical_ids["nct_ids"] == ",".join(ids)
    assert res.metadata["api_calls"] == 3
    assert [c["id"] for c in data["citation_records"]] == ids
    assert any("Showing 7 of 335 registered trials" in n for n in data["limitations"])
    assert any("NCT02954523" in n and "not retrieved" in n for n in data["limitations"])


def test_companion_never_exceeds_max_trials():
    up = Upstream(osimertinib_route)
    res = run(ct.search_trials(client=up.client(), intervention="osimertinib", condition="non-small cell lung cancer", max_trials=3))
    assert res.success
    companion = next(p for p in up.requests if p.get("aggFilters") == "results:with")
    assert companion["pageSize"] == "1"
    trials = res.data["trials"]
    assert len(trials) <= 3
    assert trials[0]["nct_id"] == "NCT04695925"  # the relevance ranking still leads

    up1 = Upstream(osimertinib_route)
    res1 = run(ct.search_trials(client=up1.client(), intervention="osimertinib", max_trials=1))
    assert len(res1.data["trials"]) == 1 and not any("aggFilters" in p for p in up1.requests)


def test_study_without_valid_nct_is_skipped():
    body = load("search_osimertinib_nsclc.json")
    body.pop("_source", None)
    body["studies"][0]["protocolSection"]["identificationModule"].pop("nctId")
    up = Upstream(lambda params: httpx.Response(200, json=body))
    res = run(ct.search_trials(client=up.client(), intervention="osimertinib", include_results=False))
    assert res.success
    assert "NCT04695925" not in [t["nct_id"] for t in res.data["trials"]]
    assert len(res.data["trials"]) == len(body["studies"]) - 1


def test_client_nct_ids_maps_results_by_id_not_order():
    up = Upstream(nct_route)
    res = run(ct.search_trials(client=up.client(), nct_ids=["nct02296125", LAURA]))
    assert res.success
    assert up.requests[0]["filter.ids"] == f"{FLAURA},{LAURA}"
    assert "aggFilters" not in up.requests[0]
    # The fixture returns LAURA first; trials follow the requested order and results land on the right trial.
    trials = res.data["trials"]
    assert [t["nct_id"] for t in trials] == [FLAURA, LAURA]
    assert trials[0]["posted_results"][0]["measurements"][0]["value"] == "18.9"
    assert trials[1]["posted_results"][0]["measurements"][0]["value"] == "39.13"
    assert res.data["query"]["strategy"] == "nct_ids"


def test_client_fallback_strategy_when_first_matches_nothing():
    def route(params):
        if params.get("query.term") == "KRAS G12C" and "query.cond" not in params:
            return fixture_response("search_kras_g12c.json")
        return fixture_response("search_empty.json")

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), term="KRAS G12C", condition="NSCLC", include_results=False))
    assert res.success
    assert [p.get("query.term") for p in up.requests] == ["KRAS G12C", "KRAS G12C"]
    assert res.data["query"]["strategy"] == "term"
    assert res.data["total_count"] == 224
    assert any("term+condition" in n for n in res.data["limitations"])


def test_client_zero_matches_is_success_with_limitation():
    up = Upstream(lambda params: fixture_response("search_empty.json"))
    res = run(ct.search_trials(client=up.client(), intervention="osimertinib", condition="NSCLC"))
    assert res.success and res.data["trials"] == [] and res.data["total_count"] == 0
    assert res.canonical_ids["nct_ids"] == ""
    assert any("No registered trials matched" in n for n in res.data["limitations"])
    assert len(up.requests) <= ct.MAX_HTTP_CALLS


@pytest.mark.parametrize(
    "status, fixture, error_type",
    [(400, "err_400.txt", ErrorType.VALIDATION_ERROR), (404, "err_404.txt", ErrorType.NOT_FOUND)],
)
def test_client_maps_text_plain_errors(status, fixture, error_type):
    body = text_fixture(fixture)
    up = Upstream(lambda params: httpx.Response(status, text=body, headers={"content-type": "text/plain; charset=UTF-8"}))
    res = run(ct.search_trials(client=up.client(), nct_ids=["NCT99999999"]))
    assert not res.success and res.error_type == error_type
    assert body in res.error
    assert len(up.requests) == 1  # client errors are not retried


def test_client_timeout_maps_to_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = ct.ClinicalTrialsGovClient(httpx.MockTransport(handler), cache=None, retry_backoff_s=0)
    res = run(ct.search_trials(client=client, term="KRAS G12C"))
    assert not res.success and res.error_type == ErrorType.TIMEOUT


def test_client_retries_once_on_503_then_fails():
    up = Upstream(lambda params: httpx.Response(503, text="Service Unavailable"))
    res = run(ct.search_trials(client=up.client(), term="KRAS G12C", include_results=False))
    assert not res.success and res.error_type == ErrorType.UPSTREAM_API_ERROR
    assert len(up.requests) == 2


def test_client_retry_recovers_after_one_503():
    responses = [httpx.Response(503, text="Service Unavailable")]

    def route(params):
        return responses.pop() if responses else fixture_response("search_kras_g12c.json")

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), term="KRAS G12C", include_results=False))
    assert res.success and len(up.requests) == 2


def test_client_caches_identical_requests():
    up = Upstream(nct_route)
    client = up.client()
    run(ct.search_trials(client=client, nct_ids=[FLAURA]))
    n = len(up.requests)
    again = run(ct.search_trials(client=client, nct_ids=[FLAURA]))
    assert again.success and len(up.requests) == n and again.metadata["cache_hits"] == 2


def test_input_schema_validation():
    with pytest.raises(ValueError):
        ct.ClinicalTrialsSearchInput()
    with pytest.raises(ValueError):
        ct.ClinicalTrialsSearchInput(nct_ids=["NCT123"])
    with pytest.raises(ValueError):
        ct.ClinicalTrialsSearchInput(term="KRAS", statuses=["FOO"])
    with pytest.raises(ValueError):
        ct.ClinicalTrialsSearchInput(term="x" * 201)
    ok = ct.ClinicalTrialsSearchInput(term="  KRAS  G12C ", statuses=["recruiting"], phases=["PHASE3"])
    assert (ok.term, ok.statuses, ok.max_trials, ok.max_results_trials) == ("KRAS G12C", ["RECRUITING"], 10, 5)


def test_phase_and_status_filters_are_sent():
    up = Upstream(lambda params: fixture_response("search_kras_g12c.json"))
    run(ct.search_trials(client=up.client(), term="KRAS G12C", phases=["PHASE2", "PHASE3"], statuses=["RECRUITING"], include_results=False))
    assert up.requests[0]["filter.advanced"] == "AREA[Phase](PHASE2 OR PHASE3)"
    assert up.requests[0]["filter.overallStatus"] == "RECRUITING"


# -----------------------------------------------------------------------------
# 3. Query planner
# -----------------------------------------------------------------------------

def test_planner_uses_target_never_the_sentence():
    q = "What clinical trials exist for KRAS G12C inhibitors?"
    plan = plan_trial_query(q, "KRAS G12C")
    assert plan is not None
    assert plan.to_tool_kwargs() == {"term": "KRAS G12C"}
    assert all(q not in json.dumps(s) for s in plan.strategies)


def test_planner_drug_condition_with_target_fallbacks():
    plan = plan_trial_query("osimertinib trials in EGFR mutant lung cancer", "EGFR")
    assert plan.intervention == "osimertinib" and plan.condition == "lung cancer" and plan.term == "EGFR"
    assert [s["name"] for s in plan.strategies] == ["intervention+condition", "term+condition", "term"]
    assert plan.label == "osimertinib + lung cancer"


def test_planner_nct_ids_and_non_clinical():
    assert plan_trial_query("Status of NCT02296125", None).to_tool_kwargs() == {"nct_ids": [FLAURA]}
    assert plan_trial_query("Simulate BIOMD0000000010", None) is None
    assert plan_trial_query("Explain the mechanism of KRAS G12D signaling in pancreatic cancer", "KRAS G12D") is None
    assert plan_trial_query("What phase 3 trials are recruiting?", None) is None  # intent but nothing searchable


def test_planner_phases_statuses_and_drug_codes():
    assert extract_drugs("Compare AZD9291 with BMS-986507 and sotorasib") == ["AZD9291", "BMS-986507", "sotorasib"]
    assert extract_drugs("CD274 expression in GSE123456 with peptide concept") == []
    assert extract_phases("phase 3 or phase II/III studies") == ["PHASE3", "PHASE2"]
    assert extract_statuses("recruiting phase 3 trials") == ["RECRUITING"]
    assert extract_statuses("active, not recruiting studies") == ["ACTIVE_NOT_RECRUITING"]
    plan = plan_trial_query("phase 3 recruiting trials of adagrasib", None)
    assert plan.to_tool_kwargs() == {"intervention": "adagrasib", "phases": ["PHASE3"], "statuses": ["RECRUITING"]}


def test_condition_extraction():
    assert extract_condition("trials for osimertinib in non-small cell lung cancer") == "non-small cell lung cancer"
    assert extract_condition("sotorasib in KRAS G12C-mutant solid tumors") == "solid tumors"
    assert extract_condition("efficacy of adagrasib in NSCLC") == "NSCLC"
    assert has_clinical_intent("What was the objective response rate?")
    assert not has_clinical_intent("How does KRAS promote MAPK signaling?")
    assert not has_clinical_intent("What is the mutational landscape of TP53 in AML?")
    assert has_clinical_intent("Describe the clinical landscape for KRAS G12C")


def test_planner_prefers_query_context_entities():
    from phronesis.agents.query_context import QueryContext, ResolvedEntity

    ctx = QueryContext(raw_query="q", clean_query="Is Tagrisso effective in lung adenocarcinoma?")
    ctx.entities = [
        ResolvedEntity(type="drug", mention="Tagrisso", label="osimertinib", source="local_lexicon"),
        ResolvedEntity(type="disease", mention="lung adenocarcinoma", label="lung adenocarcinoma", source="local_lexicon"),
    ]
    plan = plan_trial_query(ctx.clean_query, None, ctx)
    assert plan.intervention == "osimertinib" and plan.condition == "lung adenocarcinoma"
    assert plan.entity_source == "query_context"


# -----------------------------------------------------------------------------
# 4. Verifier
# -----------------------------------------------------------------------------

def verify(answer: str, tool_results: Dict[str, ToolResult], query: str = "osimertinib FLAURA LAURA trial results"):
    return ClinicalVerifier().verify_response(query=query, response_markdown=answer, tool_results=tool_results)


def figure_defects(report):
    return [d for d in report.defects if d.category == "CLINICAL_CLAIM" and "posted record" in d.description]


def test_hedged_fabricated_nct_is_flagged():
    report = verify("NCT09999999 has not posted results yet.", {"clinical_trials": flaura_laura_result()})
    assert any(d.category == "IDENTIFIER" and d.claim == "NCT09999999" for d in report.defects)


def test_correct_orr_sentence_passes():
    answer = (
        "In FLAURA (NCT02296125), the objective response rate was 76.7% with Osimertinib 80 mg (Global Cohort) "
        "versus 69.0% with SoC EGFR-TKI (Global Cohort)."
    )
    report = verify(answer, {"clinical_trials": flaura_laura_result()})
    assert report.passed, report.defects
    assert "NCT:NCT02296125" in report.verified_identifiers
    assert any(c["evidence_key"] == "NCT:NCT02296125" for c in report.claim_support)


def test_wrong_orr_is_flagged():
    answer = "In FLAURA (NCT02296125), the objective response rate with Osimertinib 80 mg (Global Cohort) was 80.1%."
    report = verify(answer, {"clinical_trials": flaura_laura_result()})
    assert not report.passed
    assert [d.description for d in figure_defects(report)] == ["figure 80.1 not found in the posted record of NCT02296125"]


def test_cross_trial_figure_is_flagged():
    tr = {"clinical_trials": flaura_laura_result()}
    assert "76.7" not in collect_trial_docs(tr)[LAURA]
    report = verify("In LAURA (NCT03521154), the objective response rate was 76.7% with osimertinib.", tr)
    assert [d.description for d in figure_defects(report)] == ["figure 76.7 not found in the posted record of NCT03521154"]


def test_correct_landscape_and_results_rows_pass():
    answer = (
        "Osimertinib trial landscape from LAURA and FLAURA:\n\n"
        "| NCT ID | Phase | Status | Enrollment | Lead sponsor |\n|---|---|---|---|---|\n"
        "| NCT02296125 | Phase 3 | Completed | 674 | AstraZeneca |\n"
        "| NCT03521154 | Phase 3 | Active, not recruiting | 216 | AstraZeneca |\n\n"
        "| NCT ID | Endpoint | Arm | Value |\n|---|---|---|---|\n"
        "| NCT02296125 | Median PFS | Osimertinib 80 mg (Global Cohort) | 18.9 months (15.2 to 21.4); HR 0.46, p<0.0001 |\n"
    )
    report = verify(answer, {"clinical_trials": flaura_laura_result()})
    assert report.passed, report.defects


def test_wrong_enrollment_in_landscape_row_is_flagged():
    answer = "| NCT ID | Phase | Status | Enrollment | Lead sponsor |\n|---|---|---|---|---|\n| NCT02296125 | Phase 3 | Completed | 700 | AstraZeneca |"
    report = verify(answer, {"clinical_trials": flaura_laura_result()})
    assert [d.description for d in figure_defects(report)] == ["figure 700 not found in the posted record of NCT02296125"]


def test_figure_extraction_skips_phases_dates_and_ci_level():
    row = "| NCT02296125 | Phase 2/3 | 2017-06-19 | 1,674 | HR 0.46 (95% CI 0.37-0.57) |"
    assert extract_figures(row, table_row=True) == ["0.46", "0.37", "0.57", "1674"]
    assert extract_figures("NCT02296125 started in 2014 and gefitinib or erlotinib was given") == []


def flaura_os_literature() -> ToolResult:
    """A literature result carrying the real PubMed RESULTS passage of the FLAURA overall-survival paper."""
    pub = load("pubmed_flaura_os_31751012.json")
    return ToolResult(
        success=True,
        tool_name="lithoscribe_literature_synthesis",
        data={"ranked_passages": [{"pmid": pub["pmid"], "title": pub["title"], "passage_text": pub["passage_text"]}]},
        canonical_ids={"pmids": pub["pmid"]},
    )


def test_figure_from_a_cited_publication_passes():
    tr = {"clinical_trials": flaura_laura_result(), "literature": flaura_os_literature()}
    assert "38.6" not in collect_trial_docs(tr)[FLAURA]  # overall survival is not in the posted subset
    cited = "In FLAURA (NCT02296125), median overall survival was 38.6 months with osimertinib (PMID: 31751012)."
    assert not figure_defects(verify(cited, tr))
    # The same figure without the publication citation is still attributed to the registry record and flagged.
    uncited = "In FLAURA (NCT02296125), median overall survival was 38.6 months with osimertinib."
    assert [d.description for d in figure_defects(verify(uncited, tr))] == [
        "figure 38.6 not found in the posted record of NCT02296125"
    ]


def test_figure_from_nct_mentioning_literature_passes():
    lit = ToolResult(
        success=True,
        tool_name="lithoscribe_literature_synthesis",
        data={"ranked_passages": [{
            "pmid": "36870338",
            "title": "CodeBreak 200: Sotorasib versus docetaxel for KRAS G12C NSCLC",
            "passage_text": "CodeBreak 200 (NCT04303780) demonstrated median PFS was 5.62 months vs 4.47 months (HR 0.663, p=0.0017)."
        }]},
        canonical_ids={"pmids": "36870338"},
    )
    # Mock a trial record for NCT04303780 without posted results
    tr = {
        "clinical_trials": ToolResult(
            success=True,
            tool_name="lithoscribe_clinical_trials_search",
            data={"trials": [{"nct_id": "NCT04303780", "brief_title": "CodeBreak 200", "has_results": False}]},
        ),
        "literature": lit,
    }
    sent = "In CodeBreak 200 (NCT04303780), median PFS was 5.62 months compared to 4.47 months with docetaxel (HR 0.663, p=0.0017)."
    assert not figure_defects(verify(sent, tr))


def test_figure_from_cited_url_passes():
    url = "https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(23)00285-2"
    lit = ToolResult(
        success=True,
        tool_name="websurfer",
        data={"results": [{"url": url, "title": "Lancet CodeBreak 200", "snippet": "median PFS was 5.62 months vs 4.47 months"}]},
    )
    tr = {
        "clinical_trials": ToolResult(
            success=True,
            tool_name="lithoscribe_clinical_trials_search",
            data={"trials": [{"nct_id": "NCT04303780", "brief_title": "CodeBreak 200", "has_results": False}]},
        ),
        "websurfer": lit,
    }
    sent = f"CodeBreak 200 (NCT04303780) reached 5.62 months vs 4.47 months ([The Lancet]({url}))."
    assert not figure_defects(verify(sent, tr))


def test_identifiers_in_rows_are_not_figures():
    row = "| NCT02296125 | EGFR exon 19/21 | PMID: 31751012 | doi 10.1056/NEJMoa1913662 | MONDO:0005233 | 674 |"
    assert extract_figures(row, table_row=True) == ["674"]


def test_audit_is_inert_without_trial_results():
    class V:
        tool_results: Dict[str, Any] = {}
        sentences = ["NCT02296125 reported 76.7%."]
        table_rows: List[str] = []

    assert audit_trial_figures(V()) == []


# -----------------------------------------------------------------------------
# 5. Rendering
# -----------------------------------------------------------------------------

def test_evidence_block_renders_trials_and_escapes():
    tr = {"clinical_trials": flaura_laura_result()}
    block = build_evidence_block(tr, "ranking")
    assert '<clinical_trials source="ClinicalTrials.gov API v2"' in block
    assert '<trial nct_id="NCT02296125" acronym="FLAURA" phase="Phase 3" status="COMPLETED" enrollment="674"' in block
    assert '<posted_result type="PRIMARY" title="Median Progression Free Survival (PFS) (Months)"' in block
    assert '<arm group="Osimertinib 80 mg (Global Cohort)" n="279" value="18.9" ci_lower="15.2" ci_upper="21.4"/>' in block
    assert 'p_value="&lt;0.0001"' in block and "<0.0001" not in block
    assert block.index("<clinical_trials") < block.index("<biodiscovery_ranking>")


def test_render_escapes_ampersand_and_marks_missing_results():
    st = load("search_keynote189.json")["studies"][0]
    record = ct.parse_trial(st)
    report = ct.ClinicalTrialsReport(
        query=ct.TrialSearchQuery(nct_ids=[record.nct_id], strategy="nct_ids", api_params={"filter.ids": record.nct_id}),
        total_count=1,
        trials=[record.model_copy(update={"has_results": False})],
    )
    xml = render_trials_evidence(report.model_dump())
    assert 'sponsor="Merck Sharp &amp; Dohme LLC"' in xml
    assert "<posted_results>none posted on ClinicalTrials.gov</posted_results>" in xml
    xml_posted = render_trials_evidence(ct.ClinicalTrialsReport(query=report.query, total_count=1, trials=[record]).model_dump())
    assert "posted on ClinicalTrials.gov but not retrieved" in xml_posted


def test_full_range_bounds_are_not_rendered_as_ci():
    st = load("results_full_range_nct02954523.json")["studies"][0]
    record = ct.parse_trial(st).model_copy(update={"posted_results": ct.parse_posted_results(st)})
    pfs = record.posted_results[0]
    assert pfs.dispersion_type == "Full Range" and pfs.measurements[0].lower is not None
    report = ct.ClinicalTrialsReport(
        query=ct.TrialSearchQuery(nct_ids=[record.nct_id], strategy="nct_ids"), total_count=1, trials=[record]
    )
    xml = render_trials_evidence(report.model_dump())
    assert 'dispersion="Full Range"' in xml
    assert f'range_lower="{pfs.measurements[0].lower}"' in xml and "ci_lower" not in xml


def test_render_empty_search():
    up = Upstream(lambda params: fixture_response("search_empty.json"))
    res = run(ct.search_trials(client=up.client(), term="KRAS G12C"))
    xml = render_trials_evidence(res.data)
    assert xml.endswith('total_registered="0">No registered trials matched.</clinical_trials>')
    title, _ = describe_result(res.data, "KRAS G12C")
    assert title == "No registered trials matched KRAS G12C"


def test_describe_result_and_fallback_markdown():
    res = osimertinib_result()
    title, detail = describe_result(res.data, "osimertinib + non-small cell lung cancer")
    assert title == "Found 7 registered trials (of 335 on ClinicalTrials.gov)"
    assert detail.startswith("4 with posted results · NCT04695925 TOP · NCT02954523 · NCT07036016 · ")
    assert "AstraZeneca" not in title
    md = fallback_markdown(res.data)
    assert md.startswith("#### Registered Clinical Trials (📚 Lithoscribe)\n| NCT ID | Phase | Status | Enrollment | Lead sponsor |")
    assert "| NCT04695925 | Phase 3 | Active, not recruiting | 294 actual |" in md
    assert "| NCT03521154 | Progression-free Survival (PFS) by Blinded Independent Central Review (BICR) | Osimertinib | 39.13 Months (31.51 to NA) |" in md
    # The deterministic table passes the figure audit it will be checked against.
    report = verify(md, {"clinical_trials": res})
    assert not figure_defects(report)


def test_source_hooks_on_real_result():
    src = trials_ext.ClinicalTrialsSource()
    res = osimertinib_result()
    assert src.allowed_identifiers(res)["nct"] >= {"NCT04695925", LAURA}
    cites = src.citations(res)
    assert cites[0].type == "nct" and cites[0].url == "https://clinicaltrials.gov/study/NCT04695925"
    key, artifact = src.artifact(res)
    assert key == "clinical_trials" and "citation_records" not in artifact
    assert len(json.dumps(artifact)) < 30_000
    assert artifact["trials"][0]["nct_id"] == "NCT04695925" and "why_stopped" not in artifact["trials"][0]


def test_prompt_hooks_register_with_the_tool():
    hooks.unregister("prompt_component", "trials")
    hooks.unregister("prompt_rule", "trials")
    before = get_synthesis_system_prompt()
    assert trials_ext.PROMPT_COMPONENT not in before and "Trial landscape" not in before
    assert SYNTHESIS_SYSTEM_PROMPT.startswith(before.split("- BioDiscovery:")[0])
    reg = BiomedicalToolRegistry()
    trials_ext.register_tools(reg)
    entry = reg.get_entry(TOOL)
    assert entry is not None and entry.input_schema is ct.ClinicalTrialsSearchInput and entry.timeout_seconds == 25.0
    prompt = get_synthesis_system_prompt()
    assert trials_ext.PROMPT_COMPONENT + "\n" in prompt
    assert "Trial landscape" in prompt and "<posted_result>" in prompt
    for word in ("openai", "nvidia", "langgraph"):
        assert word not in prompt.lower()


# -----------------------------------------------------------------------------
# 6. Orchestrator routing (offline registry with a fake tool returning the real fixture result)
# -----------------------------------------------------------------------------

@pytest.fixture
def osimertinib_tool():
    """Built in a sync fixture: asyncio.run cannot be used inside the async tests' running loop."""
    return trial_tool(osimertinib_result())


def trial_tool(result: ToolResult):
    def fake(kwargs: Dict[str, Any]) -> ToolResult:
        return copy.deepcopy(result)

    return {TOOL: (ct.ClinicalTrialsSearchInput, fake)}


async def collect(orch, query: str, **kw):
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(query, thread_id="t-trials", enable_web_search=False, event_callback=cb, **kw)
    steps = [e["data"] for e in events if e["type"] == "thought" and e["data"].get("id") == "clinical_trials"]
    return res, steps


@pytest.mark.asyncio
async def test_clinical_question_calls_the_tool(monkeypatch, tmp_path, osimertinib_tool):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=osimertinib_tool)
    res, steps = await collect(orch, "What clinical trials of osimertinib exist in non-small cell lung cancer?")
    tool_calls = called(calls, TOOL)
    assert len(tool_calls) == 1
    assert tool_calls[0]["intervention"] == "osimertinib"
    assert tool_calls[0]["condition"] == "non-small cell lung cancer"

    assert [s["status"] for s in steps] == ["running", "done"]
    assert steps[0]["title"].startswith("Searching ClinicalTrials.gov for osimertinib")
    assert steps[-1]["title"] == "Found 7 registered trials (of 335 on ClinicalTrials.gov)"
    assert {"agent": "Lithoscribe", "role": "assistant", "action": "ClinicalTrials.gov v2 Registry Search"} in res.steps

    art = res.execution_artifacts["clinical_trials"]
    assert art["total_count"] == 335 and len(art["trials"]) == 7
    # With the model unavailable, the deterministic answer carries the registry table.
    assert "#### Registered Clinical Trials (📚 Lithoscribe)" in res.response
    assert not [d for d in res.verification_report["defects"] if "posted record" in d.get("description", "")]


@pytest.mark.asyncio
async def test_mechanistic_question_does_not_call_the_tool(monkeypatch, tmp_path, osimertinib_tool):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=osimertinib_tool)
    res, steps = await collect(orch, "Explain the mechanism of KRAS G12D signaling in pancreatic cancer. Papers only.")
    assert called(calls, TOOL) == [] and steps == []
    assert "clinical_trials" not in res.execution_artifacts


@pytest.mark.asyncio
async def test_unregistered_tool_yields_no_step(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res, steps = await collect(orch, "What clinical trials of osimertinib exist in non-small cell lung cancer?")
    assert steps == [] and called(calls, TOOL) == []
    assert "clinical_trials" not in res.execution_artifacts


@pytest.mark.asyncio
async def test_clinical_intent_without_entities_adds_scope_note(monkeypatch, tmp_path, osimertinib_tool):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=osimertinib_tool)
    seen: Dict[str, Any] = {}
    real = orch_module.build_tool_status_block

    def capture(tool_results, scope_notes):
        seen["notes"] = list(scope_notes)
        return real(tool_results, scope_notes)

    monkeypatch.setattr(orch_module, "build_tool_status_block", capture)
    _, steps = await collect(orch, "Which phase 3 trials are recruiting right now?")
    assert steps == [] and called(calls, TOOL) == []
    assert trials_ext.NOT_SEARCHED_NOTE in seen["notes"]
