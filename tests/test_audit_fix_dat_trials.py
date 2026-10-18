"""
Regression tests for the ClinicalTrials.gov audit fixes DAT-002 (analysis direction and hypothesis type),
DAT-011 (disclosed truncation of posted results) and DAT-010 (registry data version). Offline: fixtures from
tests/fixtures/ctgov/ (real API responses) and httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from phronesis.extensions import trials as trials_ext
from phronesis.trials import clinical_trials as ct
from phronesis.trials.render import render_trials_evidence

FIXTURES = Path(__file__).parent / "fixtures" / "ctgov"
FLAURA, LAURA = "NCT02296125", "NCT03521154"


def _study(nct: str) -> Dict[str, Any]:
    data = json.loads((FIXTURES / "results_flaura_laura.json").read_text(encoding="utf-8"))
    return next(s for s in data["studies"] if s["protocolSection"]["identificationModule"]["nctId"] == nct)


def _measures(nct: str) -> List[Dict[str, Any]]:
    return _study(nct)["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]


def _xml_for(nct: str, posted: List[ct.TrialOutcomeResult], total: Optional[int] = None) -> str:
    record = ct.parse_trial(_study(nct)).model_copy(update={"posted_results": posted, "posted_results_total": total})
    report = ct.ClinicalTrialsReport(query=ct.TrialSearchQuery(nct_ids=[nct], strategy="nct_ids"), total_count=1, trials=[record])
    return render_trials_evidence(report.model_dump())


# --- DAT-002 -------------------------------------------------------------------------------------------------

def test_laura_pfs_analysis_keeps_direction_and_hypothesis_fields():
    pfs = ct.parse_posted_results(_study(LAURA))[0]
    an = pfs.analyses[0]
    assert (an.param_type, an.param_value, an.ci_lower, an.ci_upper, an.p_value) == ("Hazard Ratio (HR)", "0.16", "0.10", "0.24", "<0.001")
    # Registry text "A hazard ratio \< 1 favours osimertinib" with its markdown escape removed.
    assert an.estimate_comment == "A hazard ratio < 1 favours osimertinib"
    assert an.non_inferiority_type == "SUPERIORITY"
    assert an.ci_num_sides == "TWO_SIDED"
    assert an.group_titles == ["Osimertinib", "Placebo"]


def test_flaura_china_cohort_analyses_are_other_type():
    pfs = next(m for m in _measures(FLAURA) if m["title"].startswith("Median Progression Free Survival"))
    parsed = ct.parse_measure(pfs)
    by_groups = {tuple(a.group_ids): a for a in parsed.analyses}
    assert by_groups[("OG000", "OG001")].non_inferiority_type == "SUPERIORITY"
    assert by_groups[("OG002", "OG003")].non_inferiority_type == "OTHER"


def test_analysis_render_states_direction_source_and_confirmatory_status():
    xml = _xml_for(LAURA, ct.parse_posted_results(_study(LAURA)))
    line = next(x for x in xml.splitlines() if x.startswith("<analysis") and 'value="0.16"' in x)
    assert 'groups="Osimertinib; Placebo"' in line and " vs " not in line
    assert 'group_order="registry order; does not encode the direction of the estimate"' in line
    assert 'ci_sides="TWO_SIDED"' in line
    assert 'confirmatory_test="true"' in line and 'hypothesis="superiority test"' in line
    assert "<estimate_comment>A hazard ratio &lt; 1 favours osimertinib</estimate_comment>" in line
    assert "direction=" not in line  # the registry states the direction, so no "not stated" note


def test_other_type_analysis_without_comment_is_marked_non_confirmatory_and_directionless():
    pfs = next(m for m in _measures(FLAURA) if m["title"].startswith("Median Progression Free Survival"))
    xml = _xml_for(FLAURA, [ct.parse_measure(pfs)])
    other = next(x for x in xml.splitlines() if x.startswith("<analysis") and "China" in x)
    assert 'confirmatory_test="false"' in other
    assert "not registered as a superiority, non-inferiority or equivalence test" in other
    assert 'direction="not stated in the registry: do not infer which arm the estimate favours from the group order"' in other


def test_group_description_and_p_value_comment_are_rendered():
    measure = copy.deepcopy(next(m for m in _measures(LAURA) if m["type"] == "PRIMARY"))
    measure["analyses"][0].pop("estimateComment")
    measure["analyses"][0]["groupDescription"] = "HR is placebo / osimertinib"
    measure["analyses"][0]["pValueComment"] = "Two-sided, stratified"
    an = ct.parse_measure(measure).analyses[0]
    assert (an.group_description, an.p_value_comment, an.estimate_comment) == ("HR is placebo / osimertinib", "Two-sided, stratified", None)
    xml = _xml_for(LAURA, [ct.parse_measure(measure)])
    assert "<group_description>HR is placebo / osimertinib</group_description>" in xml
    assert "<p_value_comment>Two-sided, stratified</p_value_comment>" in xml
    assert "direction=" not in xml  # group_description is a direction source


def test_prompt_rule_requires_direction_from_estimate_comment():
    rule = trials_ext.PROMPT_RULE
    assert "estimate_comment" in rule and "never from the order of its groups" in rule


# --- DAT-011 -------------------------------------------------------------------------------------------------

def test_plasma_measure_carries_pre_cap_count_and_renders_truncation():
    plasma = next(m for m in _measures(FLAURA) if m["title"].startswith("Plasma Concentrations"))
    parsed = ct.parse_measure(plasma)
    assert len(parsed.measurements) == ct.MAX_MEASUREMENTS
    assert parsed.measurements_total == 16 and parsed.measurements_total > ct.MAX_MEASUREMENTS
    assert parsed.analyses_total == 0
    xml = _xml_for(FLAURA, [parsed])
    head = next(x for x in xml.splitlines() if x.startswith("<posted_result") and "Plasma" in x)
    assert 'truncated="true"' in head and 'values_shown="12"' in head and 'values_total="16"' in head
    assert "only the first 12 of 16 posted values are shown" in xml


def test_uncapped_measure_has_no_truncation_marker():
    pfs = ct.parse_posted_results(_study(LAURA))[0]
    assert pfs.measurements_total == len(pfs.measurements) == 2
    assert "truncated=" not in _xml_for(LAURA, [pfs])


def test_analysis_groups_beyond_measurement_cap_keep_arm_titles():
    # Arms OG002/OG003 have their only cells after the first 12 (OG000/OG001 at 6 time points), so the kept
    # measurements carry no title for them; the analysis label must still use their arm titles.
    groups = [{"id": f"OG00{i}", "title": f"Arm {chr(65 + i)}"} for i in range(4)]
    measure: Dict[str, Any] = {
        "type": "PRIMARY", "title": "Response", "reportingStatus": "POSTED", "groups": groups,
        "analyses": [
            {"groupIds": ["OG000", "OG001"], "paramType": "Odds Ratio (OR)", "paramValue": "1.5"},
            {"groupIds": ["OG002", "OG003"], "paramType": "Odds Ratio (OR)", "paramValue": "2.5"},
            {"groupIds": ["OG000", "OG002"], "paramType": "Odds Ratio (OR)", "paramValue": "3.5"},
            {"groupIds": ["OG001", "OG003"], "paramType": "Odds Ratio (OR)", "paramValue": "4.5"},
        ],
    }
    measure["classes"] = [{"categories": [
        {"title": f"Month {k}", "measurements": [{"groupId": g, "value": str(k)} for g in ("OG000", "OG001")]} for k in range(6)
    ] + [{"title": "Month 9", "measurements": [{"groupId": g, "value": "9"} for g in ("OG002", "OG003")]}]}]
    parsed = ct.parse_measure(measure)
    assert {m.group_id for m in parsed.measurements} == {"OG000", "OG001"}
    assert parsed.analyses_total == 4 and len(parsed.analyses) == ct.MAX_ANALYSES
    xml = _xml_for(LAURA, [parsed])
    assert 'groups="Arm C; Arm D"' in xml and "OG002" not in xml
    assert 'analyses_shown="3"' in xml and 'analyses_total="4"' in xml


def test_posted_measures_beyond_cap_are_disclosed():
    study = copy.deepcopy(_study(LAURA))
    measures = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]
    extra = [dict(copy.deepcopy(measures[0]), title=f"Progression-free Survival copy {i}") for i in range(ct.MAX_MEASURES)]
    measures.extend(extra)
    assert ct.count_posted_results(study) == len(measures)
    posted = ct.parse_posted_results(study)
    assert len(posted) == ct.MAX_MEASURES
    xml = _xml_for(LAURA, posted, total=ct.count_posted_results(study))
    assert f'<posted_results_truncated shown="{ct.MAX_MEASURES}" total="{len(measures)}">' in xml


# --- DAT-010 -------------------------------------------------------------------------------------------------

def _routes(version_status: int = 200):
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/version"):
            if version_status != 200:
                return httpx.Response(version_status, json={})
            return httpx.Response(200, json={"apiVersion": "2.0.5", "dataTimestamp": "2026-10-15T09:00:05"})
        return httpx.Response(200, json={"studies": [], "totalCount": 0})

    return handler, calls


def test_data_version_recorded_in_metadata_and_xml():
    ct.clear_cache()
    handler, calls = _routes()
    client = ct.ClinicalTrialsGovClient(httpx.MockTransport(handler), cache=None, retry_backoff_s=0, probe_version=True)
    res = asyncio.run(ct.search_trials(client=client, term="osimertinib"))
    assert res.success
    assert res.metadata["source_release"] == "ClinicalTrials.gov data 2026-10-15T09:00:05 (API 2.0.5)"
    assert 'data_version="ClinicalTrials.gov data 2026-10-15T09:00:05 (API 2.0.5)"' in render_trials_evidence(res.data)
    assert "retrieved_at" not in res.metadata  # stamped centrally by the registry, not here
    # Cached: a second search does not probe again.
    n = calls.count("/api/v2/version")
    asyncio.run(ct.search_trials(client=client, term="osimertinib"))
    assert calls.count("/api/v2/version") == n == 1
    ct.clear_cache()


def test_failed_version_probe_never_fails_the_search():
    ct.clear_cache()
    handler, _ = _routes(version_status=500)
    client = ct.ClinicalTrialsGovClient(httpx.MockTransport(handler), cache=None, retry_backoff_s=0, probe_version=True)
    res = asyncio.run(ct.search_trials(client=client, term="osimertinib"))
    assert res.success and "source_release" not in res.metadata
    assert "data_version" not in render_trials_evidence(res.data)


def test_injected_transport_does_not_probe_by_default():
    ct.clear_cache()
    handler, calls = _routes()
    client = ct.ClinicalTrialsGovClient(httpx.MockTransport(handler), cache=None, retry_backoff_s=0)
    asyncio.run(ct.search_trials(client=client, term="osimertinib"))
    assert "/api/v2/version" not in calls
