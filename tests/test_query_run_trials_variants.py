"""
Offline tests for the 2026-10-16 query-run repairs owned by the trials and variants features:

1.4  several conditions ("ovarian and prostate cancers") are each searched and merged;
2.1  clinical-setting terms go into query.term, a larger page plus phase 3 / results companions is re-ranked
     locally, and results-only extras no longer displace higher-ranked phase 3 trials;
1.5  up to three named variants are looked up concurrently, one card per variant;
2.6  CIViC evidence for the question's disease is ranked first and marked matches_context.

Registry payloads are synthetic study records (MockTransport); variant payloads reuse tests/fixtures/variant_intel.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl

import httpx
import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.tools.registry import ErrorType, ToolResult
from phronesis.trials import clinical_trials as ct
from phronesis.trials.trial_query import (
    extract_conditions,
    extract_setting_terms,
    extract_stages,
    plan_trial_query,
)
from phronesis.utils.http_limits import TTLCache
from phronesis.variants import clients
from phronesis.variants.checks import check_clinvar_classification, check_variant_frequencies
from phronesis.variants.parse import disease_matches_keyword
from phronesis.variants.render import compact_artifact, render_variant_evidence, variant_datas
from phronesis.variants.service import rank_civic_evidence
from phronesis.variants.source import VariantIntelSource, combine_variant_results, variant_plan
from tests.fakes import RecordingRegistry

KEYNOTE_716, CHECKMATE_76K = "NCT03553836", "NCT04099251"
VFIX = Path(__file__).parent / "fixtures" / "variant_intel"


def run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# Synthetic ClinicalTrials.gov records and transport
# -----------------------------------------------------------------------------

def study(
    nct: str,
    title: str,
    phases: Optional[List[str]] = None,
    enrollment: int = 40,
    has_results: bool = False,
    summary: str = "",
    conditions: Optional[List[str]] = None,
    study_type: str = "INTERVENTIONAL",
    allocation: str = "NA",
) -> Dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": title},
            "statusModule": {"overallStatus": "COMPLETED"},
            "designModule": {
                "studyType": study_type,
                "phases": phases or [],
                "enrollmentInfo": {"count": enrollment, "type": "ACTUAL"},
                "designInfo": {"allocation": allocation},
            },
            "conditionsModule": {"conditions": conditions or ["Melanoma"]},
            "descriptionModule": {"briefSummary": summary},
        },
        "hasResults": has_results,
    }


def page(studies: List[Dict[str, Any]], total: Optional[int] = None) -> httpx.Response:
    return httpx.Response(200, json={"studies": studies, "totalCount": total if total is not None else len(studies)})


class Upstream:
    def __init__(self, route: Callable[[Dict[str, str]], httpx.Response]):
        self.route = route
        self.requests: List[Dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.url.query.decode()))
        self.requests.append(params)
        return self.route(params)

    def client(self, fetch_size: Optional[int] = ct.FETCH_SIZE) -> ct.ClinicalTrialsGovClient:
        return ct.ClinicalTrialsGovClient(
            httpx.MockTransport(self), cache=TTLCache(ttl_s=600), retry_backoff_s=0, fetch_size=fetch_size
        )

    def searches(self) -> List[Dict[str, str]]:
        return [p for p in self.requests if p.get("fields") == ct.SEARCH_FIELDS]


def no_results(params: Dict[str, str]) -> httpx.Response:
    return httpx.Response(200, json={"studies": []})


# Fifty relevance hits that are mostly small phase 1/2 studies, as the registry returned for the melanoma question.
JUNK = [study(f"NCT0{7000000 + i}", f"Pilot study {i} of an anti-PD-1 combination in melanoma", ["PHASE1"], 20 + i)
        for i in range(48)]
KN716 = study(KEYNOTE_716, "Safety and Efficacy of Pembrolizumab Compared to Placebo in Resected High-risk Stage II Melanoma "
              "(MK-3475-716/KEYNOTE-716)", ["PHASE3"], 976, True, allocation="RANDOMIZED")
CM76K = study(CHECKMATE_76K, "Effectiveness Study of Nivolumab Compared to Placebo in Prevention of Recurrent Melanoma After "
              "Complete Resection of Stage IIB/C Melanoma", ["PHASE3"], 790, True,
              summary="nivolumab adjuvant immunotherapy compared to placebo after complete resection of Stage IIB/C melanoma",
              allocation="RANDOMIZED")
METASTATIC_P3 = study("NCT05000001", "Pembrolizumab in Advanced Unresectable Melanoma", ["PHASE3"], 600, False)
OBSERVATIONAL = study("NCT05000002", "Registry of adjuvant nivolumab in resected melanoma", [], 3000,
                      study_type="OBSERVATIONAL")


def melanoma_route(params: Dict[str, str]) -> httpx.Response:
    if params.get("fields") == ct.RESULT_FIELDS:
        return no_results(params)
    if params.get("filter.advanced") == "AREA[Phase]PHASE3":
        return page([METASTATIC_P3, CM76K, KN716], 23)
    if params.get("aggFilters") == "results:with":
        return page([study("NCT06000001", "Adjuvant pilot with results", ["PHASE1"], 12, True), KN716], 30)
    # KEYNOTE-716 sits past the first page of relevance results; CheckMate 76K is on it.
    return page([*JUNK[:20], CM76K, OBSERVATIONAL, *JUNK[20:]], 151)


MELANOMA_Q = ("What evidence supports adjuvant pembrolizumab or nivolumab in resected stage IIB/IIC cutaneous melanoma?")


# -----------------------------------------------------------------------------
# 1.4 Several conditions
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query, expected",
    [
        ("PARP inhibitors in HRD ovarian and prostate cancers", ["ovarian cancer", "prostate cancer"]),
        ("niraparib in breast, ovarian or pancreatic cancer", ["breast cancer", "ovarian cancer", "pancreatic cancer"]),
        ("ovarian cancer and prostate cancer trials of niraparib", ["ovarian cancer", "prostate cancer"]),
        ("uveal and cutaneous melanoma trials of nivolumab", ["uveal melanoma", "cutaneous melanoma"]),
        ("osimertinib trials in EGFR mutant lung cancer", ["lung cancer"]),
        ("adagrasib in NSCLC and CRC", ["NSCLC", "CRC"]),
        ("treatment and lung cancer", ["lung cancer"]),
    ],
)
def test_extract_conditions_expands_coordination(query, expected):
    assert extract_conditions(query) == expected


def test_planner_searches_every_coordinated_condition():
    plan = plan_trial_query("clinical trials of niraparib in HRD ovarian and prostate cancers", None)
    assert plan.condition == "ovarian cancer" and plan.conditions == ["ovarian cancer", "prostate cancer"]
    kwargs = plan.to_tool_kwargs()
    assert kwargs["condition"] == "ovarian cancer" and kwargs["conditions"] == ["prostate cancer"]
    assert plan.label == "niraparib + ovarian cancer, prostate cancer"
    inp = ct.ClinicalTrialsSearchInput(**kwargs)
    assert inp.search_conditions() == ["ovarian cancer", "prostate cancer"]


def test_planner_text_coordination_beats_a_single_resolved_disease():
    ctx = QueryContext(raw_query="q", clean_query="Trials of niraparib in HRD ovarian and prostate cancers")
    ctx.entities = [
        ResolvedEntity(type="drug", mention="niraparib", label="niraparib", source="local_lexicon"),
        ResolvedEntity(type="disease", mention="prostate cancers", label="prostate cancer", source="local_lexicon"),
    ]
    plan = plan_trial_query(ctx.clean_query, None, ctx)
    assert plan.conditions == ["ovarian cancer", "prostate cancer"]
    two = QueryContext(raw_query="q", clean_query="niraparib trials")
    two.entities = [
        ResolvedEntity(type="drug", mention="niraparib", label="niraparib", source="local_lexicon"),
        ResolvedEntity(type="disease", mention="a", label="ovarian carcinoma", source="local_lexicon"),
        ResolvedEntity(type="disease", mention="b", label="prostate carcinoma", source="local_lexicon"),
    ]
    assert plan_trial_query(two.clean_query, None, two).conditions == ["ovarian carcinoma", "prostate carcinoma"]


def test_single_condition_plans_are_unchanged():
    plan = plan_trial_query("osimertinib trials in EGFR mutant lung cancer", "EGFR")
    assert plan.conditions == [] and "conditions" not in plan.to_tool_kwargs()
    assert plan_trial_query("What clinical trials exist for KRAS G12C inhibitors?", "KRAS G12C").to_tool_kwargs() == {"term": "KRAS G12C"}


def test_multi_condition_search_runs_one_search_each_and_balances_the_list():
    ovarian = [study(f"NCT0{8000000 + i}", f"Niraparib ovarian study {i}", ["PHASE3"], 900 - i, conditions=["Ovarian Cancer"])
               for i in range(12)]
    prostate = [study(f"NCT0{8100000 + i}", f"Niraparib prostate study {i}", ["PHASE2"], 100 - i, conditions=["Prostate Cancer"])
                for i in range(12)]

    def route(params):
        if params.get("fields") == ct.RESULT_FIELDS:
            return no_results(params)
        if params.get("query.cond") == "ovarian cancer":
            return page(ovarian, 40)
        if params.get("query.cond") == "prostate cancer":
            return page(prostate, 25)
        return page([])

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), intervention="niraparib", condition="ovarian cancer",
                               conditions=["prostate cancer"]))
    assert res.success, res.error
    conds = {p.get("query.cond") for p in up.searches()}
    assert conds == {"ovarian cancer", "prostate cancer"}
    ids = [t["nct_id"] for t in res.data["trials"]]
    assert len(ids) == 10 and len(set(ids)) == 10
    assert sum(i.startswith("NCT081") for i in ids) == 5  # prostate keeps half the list despite lower phase
    assert res.data["total_count"] == 65
    q = res.data["query"]
    assert q["conditions"] == ["ovarian cancer", "prostate cancer"]
    assert "conditions: ovarian cancer; prostate cancer (one search each)" in ct.describe_strategy(ct.TrialSearchQuery(**q))


def test_multi_condition_partial_failure_is_a_limitation():
    def route(params):
        if params.get("query.cond") == "prostate cancer":
            return httpx.Response(400, text="bad query")
        if params.get("fields") == ct.RESULT_FIELDS:
            return no_results(params)
        return page([study("NCT08000001", "Ovarian study", ["PHASE3"], 500)])

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), intervention="niraparib", conditions=["ovarian cancer", "prostate cancer"]))
    assert res.success and [t["nct_id"] for t in res.data["trials"]] == ["NCT08000001"]
    assert any("search for prostate cancer failed" in n for n in res.data["limitations"])


# -----------------------------------------------------------------------------
# 2.1 Setting terms, larger page, local re-ranking
# -----------------------------------------------------------------------------

def test_setting_terms_and_stages_are_extracted():
    assert extract_setting_terms(MELANOMA_Q) == ["adjuvant", "resected", "stage IIB", "stage IIC"]
    assert extract_setting_terms("neoadjuvant and adjuvant therapy") == ["neoadjuvant", "adjuvant"]
    assert extract_setting_terms("neo-adjuvant chemo") == ["neoadjuvant"]
    assert extract_setting_terms("first-line metastatic castration-resistant prostate cancer") == [
        "first-line", "metastatic", "castration-resistant"]
    assert extract_setting_terms("randomized maintenance olaparib") == ["randomized", "maintenance"]
    assert extract_stages("stage IIB/C melanoma") == ["stage IIB", "stage IIC"]
    assert extract_stages("stage III or IV") == ["stage III", "stage IV"]
    assert extract_setting_terms("How does KRAS promote MAPK signaling?") == []


def test_setting_query_and_stage_matching():
    assert ct.setting_query(["adjuvant", "resected", "stage IIB", "randomized"]) == "adjuvant OR resected OR resection"
    assert ct.setting_query(["first-line"]) == '"first-line" OR "previously untreated"'
    assert ct.setting_query(["stage IIB"]) is None
    pat = ct.setting_pattern("stage IIB")
    assert pat.search("Stage IIB/C Melanoma") and pat.search("High-risk Stage II Melanoma")
    assert not pat.search("Stage III Melanoma")
    assert not ct.setting_pattern("adjuvant").search("Neoadjuvant nivolumab")


def test_melanoma_plan_carries_setting_terms():
    plan = plan_trial_query(MELANOMA_Q, "PDCD1")
    kwargs = plan.to_tool_kwargs()
    assert kwargs["intervention"] == "pembrolizumab OR nivolumab" and kwargs["condition"] == "cutaneous melanoma"
    assert kwargs["setting_terms"] == ["adjuvant", "resected", "stage IIB", "stage IIC"]


def test_ranked_search_surfaces_pivotal_phase3_trials():
    up = Upstream(melanoma_route)
    kwargs = plan_trial_query(MELANOMA_Q, "PDCD1").to_tool_kwargs()
    res = run(ct.search_trials(client=up.client(), **kwargs))
    assert res.success, res.error
    searches = up.searches()
    relevance = next(p for p in searches if "aggFilters" not in p and "filter.advanced" not in p)
    assert relevance["pageSize"] == "50"
    assert relevance["query.term"] == "adjuvant OR resected OR resection"
    assert relevance["query.cond"] == "cutaneous melanoma"
    phase3 = next(p for p in searches if p.get("filter.advanced") == "AREA[Phase]PHASE3")
    results = next(p for p in searches if p.get("aggFilters") == "results:with")
    # The companions carry the same condition and setting filter as the relevance search.
    for comp in (phase3, results):
        assert comp["query.term"] == relevance["query.term"] and comp["query.cond"] == relevance["query.cond"]

    ids = [t["nct_id"] for t in res.data["trials"]]
    assert len(ids) == 10
    assert ids[:2] == [KEYNOTE_716, CHECKMATE_76K] or ids[:2] == [CHECKMATE_76K, KEYNOTE_716]
    assert ids.index("NCT05000001") > 1  # phase 3 but not in the asked-for setting
    assert ids.index("NCT05000002") > ids.index("NCT05000001")  # observational below interventional phase 3
    assert res.data["query"]["setting_terms"] == ["adjuvant", "resected", "stage IIB", "stage IIC"]
    assert res.metadata["ranked"] is True
    assert any("re-ranked locally" in n for n in res.data["limitations"])


def test_results_only_extras_do_not_displace_phase3_trials():
    phase3 = [study(f"NCT0{9000000 + i}", f"Adjuvant phase 3 study {i}", ["PHASE3"], 800, False) for i in range(12)]

    def route(params):
        if params.get("fields") == ct.RESULT_FIELDS:
            return no_results(params)
        if params.get("aggFilters") == "results:with":
            return page([study("NCT09900001", "Adjuvant pilot with posted results", ["PHASE1"], 10, True)])
        if params.get("filter.advanced"):
            return page(phase3[:5])
        return page(phase3, 12)

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), intervention="pembrolizumab", condition="melanoma", setting_terms=["adjuvant"]))
    ids = [t["nct_id"] for t in res.data["trials"]]
    assert len(ids) == 10 and "NCT09900001" not in ids
    assert ids == [s["protocolSection"]["identificationModule"]["nctId"] for s in phase3[:10]]


def test_setting_search_without_matches_falls_back_without_the_terms():
    def route(params):
        if params.get("fields") == ct.RESULT_FIELDS:
            return no_results(params)
        if "query.term" in params:
            return page([], 0)
        return page([study("NCT09100001", "Pembrolizumab in melanoma", ["PHASE2"], 100)], 1)

    up = Upstream(route)
    res = run(ct.search_trials(client=up.client(), intervention="pembrolizumab", condition="melanoma",
                               setting_terms=["perioperative"]))
    assert [t["nct_id"] for t in res.data["trials"]] == ["NCT09100001"]
    assert res.data["query"]["strategy"] == "intervention+condition"
    assert any("No registered trials matched the setting terms (perioperative)" in n for n in res.data["limitations"])
    relevance = [p for p in up.searches() if "aggFilters" not in p and "filter.advanced" not in p]
    assert relevance[0]["query.term"] == "perioperative" and "query.term" not in relevance[1]


def test_requested_phase_disables_phase_ranking_and_the_phase3_companion():
    up = Upstream(melanoma_route)
    run(ct.search_trials(client=up.client(), intervention="nivolumab", condition="melanoma", phases=["PHASE2"]))
    assert not any(p.get("filter.advanced") == "AREA[Phase]PHASE3" for p in up.requests)
    assert all(p.get("filter.advanced") == "AREA[Phase]PHASE2" for p in up.searches())


def test_rank_score_orders_phase_design_setting_enrollment():
    rec = lambda s: ct.parse_trial(s)  # noqa: E731
    terms = ["adjuvant", "resected", "stage IIB"]
    p3 = ct.rank_score(rec(KN716), KN716, terms)
    p1 = ct.rank_score(rec(JUNK[0]), JUNK[0], terms)
    obs = ct.rank_score(rec(OBSERVATIONAL), OBSERVATIONAL, terms)
    assert p3 > obs > p1
    # With a requested phase, phase no longer separates trials.
    assert ct.rank_score(rec(METASTATIC_P3), METASTATIC_P3, [], phase_requested=True) < ct.rank_score(
        rec(KN716), KN716, [], phase_requested=True)


def test_production_client_is_ranked(monkeypatch):
    seen: Dict[str, Any] = {}
    up = Upstream(melanoma_route)
    real = ct.ClinicalTrialsGovClient

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(httpx.MockTransport(up), cache=None, retry_backoff_s=0, fetch_size=kwargs.get("fetch_size"))

    monkeypatch.setattr(ct, "ClinicalTrialsGovClient", Spy)
    res = run(ct.search_trials(intervention="nivolumab", condition="melanoma", setting_terms=["adjuvant"]))
    assert seen.get("fetch_size") == ct.FETCH_SIZE and res.metadata["ranked"] is True


def test_input_cleans_setting_terms_and_conditions():
    inp = ct.ClinicalTrialsSearchInput(conditions=[' "ovarian  cancer" ', "Ovarian cancer", ""], setting_terms=["(adjuvant)"])
    assert inp.conditions == ["ovarian cancer"] and inp.setting_terms == ["adjuvant"]
    with pytest.raises(ValueError):
        ct.ClinicalTrialsSearchInput(conditions=["a", "b", "c", "d"])


# -----------------------------------------------------------------------------
# 1.5 Several variants
# -----------------------------------------------------------------------------

def vfx(name: str) -> Any:
    return json.loads((VFIX / name).read_text(encoding="utf-8"))


def kras_result() -> ToolResult:
    return ToolResult(**vfx("tool_result_kras_g12d_pancreatic.json"))


def variant_result(gene: str, change: str, mutated: int = 49) -> ToolResult:
    """The captured KRAS G12D result relabelled as another variant, with its own cBioPortal count."""
    res = kras_result()
    data = copy.deepcopy(res.data)
    data["gene"], data["protein_change"] = gene, change
    data["cbioportal"][0]["mutated_samples"] = mutated
    data["cbioportal"][0]["prevalence_percent"] = round(100.0 * mutated / data["cbioportal"][0]["profiled_samples"], 1)
    return res.model_copy(update={"data": data})


def _ctx(query: str) -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query)


def test_variant_plan_keeps_up_to_three_variants():
    ctx = _ctx("EGFR T790M and C797S resistance to osimertinib in NSCLC")
    plan = variant_plan(ctx)
    assert plan["labels"] == ["EGFR T790M", "EGFR C797S"] and plan["label"] == "EGFR T790M"
    assert plan["not_looked_up"] == [] and plan["cancer_keyword"] == "nsclc"
    src = VariantIntelSource()
    assert src.plan_fact(ctx) == "Variants: EGFR p.T790M, EGFR p.C797S"
    assert src.running_title(ctx) == "Looking up EGFR T790M, EGFR C797S in ClinVar, CIViC, cBioPortal and gnomAD"
    four = variant_plan(_ctx("BRAF V600E and KRAS G12C and NRAS Q61K and TP53 R175H"))
    assert four["labels"] == ["BRAF V600E", "KRAS G12C", "NRAS Q61K"] and four["not_looked_up"] == ["TP53 R175H"]
    same = variant_plan(_ctx("KRAS G12D (rs121913529) prevalence in pancreatic cancer"))
    assert same["labels"] == ["KRAS G12D"]  # the rsID names the same variant


def test_entity_and_text_variants_are_merged():
    ctx = _ctx("EGFR T790M and C797S in NSCLC")
    ctx.entities = [ResolvedEntity(type="variant", mention="T790M", label="EGFR T790M", gene="EGFR", protein_change="T790M")]
    assert variant_plan(ctx)["labels"] == ["EGFR T790M", "EGFR C797S"]


def test_source_looks_up_each_variant_concurrently():
    ctx = _ctx("EGFR T790M and C797S resistance in NSCLC")
    results = {"T790M": variant_result("EGFR", "T790M", 30), "C797S": variant_result("EGFR", "C797S", 7)}
    reg = RecordingRegistry(lambda _tool, kwargs: results[kwargs["protein_change"]], delay=0.01)
    src = VariantIntelSource()
    res = run(src.run(ctx, reg, None))
    assert [c["protein_change"] for c in reg.kwargs] == ["T790M", "C797S"] and reg.max_active == 2
    assert all(c["cancer_keyword"] == "nsclc" for c in reg.kwargs)
    assert res.success and res.data["protein_change"] == "T790M"
    assert [d["protein_change"] for d in variant_datas(res.data)] == ["T790M", "C797S"]

    xml = src.evidence_xml(res)
    assert xml.count("<variant_intelligence ") == 2
    assert 'protein_change="T790M"' in xml and 'protein_change="C797S"' in xml

    title, detail = src.describe(res, ctx)
    assert title.startswith("Looked up 2 variants: EGFR T790M (4 of 4 databases), EGFR C797S")
    assert "EGFR C797S: " in detail
    key, art = src.artifact(res)
    assert key == "variant_intel" and art["protein_change"] == "T790M"
    assert [a["protein_change"] for a in art["additional_variants"]] == ["C797S"]
    assert "citation_records" not in art["additional_variants"][0]
    assert not [n for n in src.scope_notes(ctx, res) if "Only" in n]
    md = src.fallback_markdown(res)
    assert md.count("#### Variant & Biomarker Evidence") == 2
    assert src.citations(res)  # merged and de-duplicated
    assert len({(c.type, c.id) for c in src.citations(res)}) == len(src.citations(res))


def test_one_failed_variant_is_reported_not_fatal():
    failed = ToolResult(success=False, tool_name="synapta_variant_intelligence", error="No variant database returned data",
                        error_type=ErrorType.NOT_FOUND,
                        data={"gene": "EGFR", "protein_change": "C797S", "clinvar": [], "civic": None, "cbioportal": [],
                              "gnomad": None, "sources": [{"name": "clinvar", "status": "no_match", "detail": "none"}]})
    res = combine_variant_results([variant_result("EGFR", "T790M"), failed],
                                  [{"gene": "EGFR", "protein_change": "T790M", "label": "EGFR T790M"},
                                   {"gene": "EGFR", "protein_change": "C797S", "label": "EGFR C797S"}])
    assert res.success
    ctx = _ctx("EGFR T790M and C797S resistance in NSCLC")
    notes = VariantIntelSource().scope_notes(ctx, res)
    assert any(n.startswith("Synapta's variant lookup for EGFR C797S returned no data") for n in notes)
    both_failed = combine_variant_results([failed, failed], [{"gene": "EGFR", "label": "a"}, {"gene": "EGFR", "label": "b"}])
    assert not both_failed.success and both_failed.error_type == ErrorType.NOT_FOUND


def test_single_variant_result_still_notes_unsearched_variants():
    ctx = _ctx("EGFR L858R and T790M")
    notes = VariantIntelSource().scope_notes(ctx, kras_result())
    assert any("EGFR T790M" in n and n.startswith("Only EGFR L858R was looked up") for n in notes)


def test_checks_accept_figures_from_every_variant():
    combined = combine_variant_results(
        [variant_result("KRAS", "G12D", 49), variant_result("KRAS", "G12V", 12)],
        [{"gene": "KRAS", "protein_change": "G12D", "label": "KRAS G12D"},
         {"gene": "KRAS", "protein_change": "G12V", "label": "KRAS G12V"}],
    )

    def vctx(sentence: str) -> SimpleNamespace:
        return SimpleNamespace(tool_results={"variant_intel": combined}, evidence_documents=[], evidence_keys=[],
                               sentences=[sentence], hedge_pattern=re.compile(r"(?!x)x"))

    ok = "KRAS G12V was mutated in 12/179 profiled samples (6.7%) of the pancreatic cohort."
    assert check_variant_frequencies(vctx(ok)) == []
    bad = "KRAS G12V was mutated in 13/179 profiled samples (7.3%) of the pancreatic cohort."
    assert check_variant_frequencies(vctx(bad))
    assert check_clinvar_classification(vctx("ClinVar classifies KRAS G12V as benign.")) != []


# -----------------------------------------------------------------------------
# 2.6 CIViC evidence for the question's disease first
# -----------------------------------------------------------------------------

def test_disease_matching():
    for name in ("Pancreatic Cancer", "Pancreatic Ductal Carcinoma", "Malignant Exocrine Pancreas Neoplasm"):
        assert disease_matches_keyword(name, "pancreatic")
    assert not disease_matches_keyword("Lung Adenocarcinoma", "pancreatic")
    assert disease_matches_keyword("Lung Non-small Cell Carcinoma", "nsclc")
    assert disease_matches_keyword("Lung Adenocarcinoma", "nsclc")
    assert disease_matches_keyword("Cholangiocarcinoma", "cholangiocarcinoma")
    assert disease_matches_keyword("Glioblastoma", "glioblastoma multiforme")  # unknown keyword: token stems
    assert not disease_matches_keyword(None, "pancreatic") and not disease_matches_keyword("Pancreatic Cancer", None)


def test_rank_civic_evidence_puts_matching_disease_first():
    nodes = vfx("civic_evidence_mp79.json")["data"]["evidenceItems"]["nodes"]
    ranked = rank_civic_evidence(nodes, "pancreatic")
    names = [n["name"] for n, _ in ranked[:4]]
    assert names == ["EID1300", "EID1311", "EID1732", "EID937"]  # rating order within the matching group
    assert all(m for _, m in ranked[:4]) and not any(m for _, m in ranked[4:])
    assert [n["name"] for n, _ in ranked[4:6]] == ["EID305", "EID228"]
    assert all(m is None for _, m in rank_civic_evidence(nodes, None))


@pytest.fixture
def offline_variant_clients(monkeypatch):
    async def fake_sleep(seconds: float) -> None:
        return None

    class _NoLimit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(clients, "_sleep", fake_sleep)
    monkeypatch.setattr(clients, "_limiter", lambda name, interval: _NoLimit())
    clients.clear_caches()
    yield
    clients.clear_caches()


@pytest.mark.asyncio
async def test_civic_chain_ranks_the_question_disease_first(offline_variant_clients):
    from tests.test_variant_intel import Upstream as VariantUpstream, _run

    up = VariantUpstream()
    res = await _run(up)  # KRAS G12D, cancer_keyword='pancreatic'
    civic = res.data["civic"]
    assert civic["context_disease"] == "pancreatic" and civic["context_matched"] == 4
    ev = civic["evidence"]
    assert [e["eid"] for e in ev[:4]] == ["EID1300", "EID1311", "EID1732", "EID937"]
    assert [e["matches_context"] for e in ev[:5]] == [True, True, True, True, False]
    bodies = [b for b in up.bodies("civicdb.org") if "evidenceItems" in b["query"]]
    assert bodies and bodies[0]["variables"]["first"] == 50
    xml = render_variant_evidence(res.data)
    assert 'question_disease="pancreatic"' in xml and 'matches_question_disease="true"' in xml
    art = compact_artifact(res.data)
    assert art["civic"]["evidence"][0]["matches_context"] is True

    clients.clear_caches()
    res2 = await _run(VariantUpstream(), cancer_keyword=None)
    assert res2.data["civic"]["context_disease"] is None
    assert all(e["matches_context"] is None for e in res2.data["civic"]["evidence"])


def test_trial_drug_extraction_keeps_short_stem_inns():
    from phronesis.trials.trial_query import extract_drugs

    found = [d.lower() for d in extract_drugs("PARP inhibitors (e.g., olaparib, niraparib) and ixazomib, in contrast to the concept")]
    assert {"olaparib", "niraparib", "ixazomib"} <= set(found)
    assert "concept" not in found


def test_trials_built_around_the_searched_term_rank_first():
    from phronesis.trials.clinical_trials import TrialRecord, rank_score

    def rec(nct, title, phase):
        return TrialRecord(nct_id=nct, url=f"https://clinicaltrials.gov/study/{nct}", brief_title=title, phases=[phase],
                           study_type="INTERVENTIONAL", conditions=["Pancreatic Cancer"])

    targeted_p3 = rec("NCT07491445", "RASolute 303: daraxonrasib in KRAS G12-mutant PDAC", "PHASE3")
    generic_p3 = rec("NCT01964430", "Adjuvant nab-paclitaxel plus gemcitabine (APACT)", "PHASE3")
    targeted_p2 = rec("NCT00000001", "Sotorasib in KRAS G12C pancreatic cancer", "PHASE2")
    score = {r.nct_id: rank_score(r, {}, [], False, ["KRAS"]) for r in (targeted_p3, generic_p3, targeted_p2)}
    assert score["NCT07491445"] > score["NCT00000001"] > score["NCT01964430"]
    # Without a focus term the old phase ordering is unchanged.
    assert rank_score(generic_p3, {}, []) > rank_score(targeted_p2, {}, [])


def test_trials_input_accepts_natural_phase_status_and_list_wording():
    from phronesis.trials.clinical_trials import ClinicalTrialsSearchInput as I

    x = I(intervention=["osimertinib", "lazertinib"], condition=["NSCLC", "lung cancer"],
          phases=["Phase 3", "phase II/III", "N/A", "early phase 1"],
          statuses=["Active, not recruiting", "Recruiting", "not-a-status"])
    assert x.intervention == "osimertinib OR lazertinib" and x.condition == "NSCLC"
    assert x.phases == ["PHASE3", "PHASE2", "NA", "EARLY_PHASE1"]
    assert x.statuses == ["ACTIVE_NOT_RECRUITING", "RECRUITING"]  # unknown values are dropped, not fatal
    assert I(term="EGFR", statuses="Active, not recruiting").statuses == ["ACTIVE_NOT_RECRUITING"]
    assert I(term="EGFR", phases="PHASE2").phases == ["PHASE2"]
