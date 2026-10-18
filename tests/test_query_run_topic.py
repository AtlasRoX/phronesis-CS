"""Query-run repair, topic relevance and web search (follow-up to plan item 2.7). Offline: EuropePMC and the web
search providers are stubs.

- A treatment question ("identify FDA-approved drugs that could be repurposed for treating KRAS-mutant pancreatic
  cancer, incorporating ... in-silico binding predictions ...") is searched for treatment, repurposing and docking
  papers on the subject in the disease, and diagnostic or technical papers (EUS-FNA assays, ddPCR cfDNA protocols)
  rank below them. Questions that are not about treatment keep their searches.
- A long brief is searched on the web by its concepts, with Unicode hyphens as ASCII.
- A web search no provider answered (DuckDuckGo's anti-bot challenge, HTTP 202) is a failed tool result
  ("Web search unavailable"), not "0 web results".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from phronesis.agents.search_plan import (
    SearchPlan,
    build_search_plan,
    is_therapeutic_question,
    merge_llm_plan,
    normalize_dashes,
    off_topic_title_terms,
    question_aspects,
)
from phronesis.lithoscribe.synthesis_engine import LiteratureSynthesisEngine
from phronesis.tools import web_search
from phronesis.tools.registry import ErrorType, ToolResult
from phronesis.tools.web_search import FreeWebSearchClient, clean_search_query

KRAS_Q = (
    "Design a step‑by‑step pipeline to identify FDA‑approved drugs that could be repurposed for treating "
    "KRAS‑mutant pancreatic cancer, incorporating clinical‑trial status, in‑silico binding predictions, "
    "and real‑world EHR safety data."
)
MELANOMA_Q = (
    "What is the efficacy of adjuvant pembrolizumab or nivolumab in resected stage II cutaneous melanoma, "
    "including hazard ratios from pivotal trials?"
)
PARP_Q = "What is the evidence for PARP inhibitors in ovarian cancer and prostate cancer with BRCA mutations, including pivotal trials?"
EGFR_Q = "What are the mechanisms of acquired resistance to osimertinib in EGFR-mutant non-small cell lung cancer?"


def _entity(type_: str, mention: str, query: str, label: Optional[str] = None, aliases: Tuple[str, ...] = ()) -> Any:
    start = query.index(mention)
    return SimpleNamespace(
        type=type_, mention=mention, label=label or mention, aliases=list(aliases), span=(start, start + len(mention)),
        ambiguous_with=[], protein_change=None, gene=None, variant_kind=None,
    )


def _ctx(entities: List[Any]) -> Any:
    return SimpleNamespace(entities=entities, primary_gene=None)


def _kras_plan() -> SearchPlan:
    ents = [_entity("gene", "KRAS", KRAS_Q),
            _entity("disease", "pancreatic cancer", KRAS_Q, label="exocrine pancreatic carcinoma")]
    return build_search_plan(_ctx(ents), KRAS_Q)


def _art(pmid: str, title: str, abstract: str) -> Dict[str, Any]:
    return {"id": pmid, "source": "MED", "pmid": pmid, "doi": "", "pmcid": "", "title": title, "abstractText": abstract,
            "pubYear": "2023", "journalTitle": "J", "authorString": "Author A", "pubType": "journal article"}


class _Client:
    """EuropePMC stand-in that answers each planned query by what it searches for."""

    def __init__(self, routes: List[Tuple[str, List[Dict[str, Any]]]], default: Optional[List[Dict[str, Any]]] = None):
        self.routes, self.default, self.calls = routes, default or [], []

    async def async_query_europepmc(self, query: str, limit: int = 5, sort: Any = None) -> Dict[str, Any]:
        self.calls.append({"query": query, "limit": limit, "sort": sort})
        for needle, arts in self.routes:
            if needle in query:
                return {"articles": [dict(a) for a in arts[:limit]]}
        return {"articles": [dict(a) for a in self.default[:limit]]}

    async def async_fetch_europepmc_fulltext(self, pmcid: str) -> str:  # pragma: no cover - no PMCIDs used
        return ""


# Off-topic records observed in the KRAS run, and the kinds of papers it missed.
EUS_FNA = _art("28450086", "KRAS mutation testing on EUS-guided fine-needle aspiration biopsies of the pancreas",
               "Diagnostic yield of KRAS mutation testing in pancreatic cancer specimens obtained by EUS-FNA.")
DDPCR = _art("28691390", "An improved digital PCR protocol to capture low-copy KRAS mutations in plasma cell-free DNA",
             "Detection of KRAS mutations in cfDNA of pancreatic cancer patients by droplet digital PCR.")
ASSAY = _art("24798941", "EUS-FNA biopsy coupled with a KRAS mutation assay for pancreatic masses",
             "A KRAS assay improves the diagnosis of pancreatic cancer on fine-needle aspiration.")
SOTORASIB = _art("36546651", "Sotorasib in KRAS p.G12C-Mutated Advanced Pancreatic Cancer",
                 "In this phase 1-2 trial, the KRAS G12C inhibitor sotorasib showed anticancer activity and an "
                 "acceptable safety profile in patients with pancreatic cancer; objective response 21%.")
REPURPOSING = _art("41361888", "Repurposing DPP-4 inhibitors as anticancer agents in KRAS-mutated pancreatic ductal adenocarcinoma",
                   "Drug repurposing of FDA-approved DPP-4 inhibitors suppressed KRAS-mutant pancreatic cancer growth.")
DOCKING = _art("42365569", "Benchmarking docking and ML re-scoring for KRAS G12D in pancreatic cancer",
               "Molecular docking and virtual screening of approved drugs against KRAS G12D in pancreatic cancer.")


# -----------------------------------------------------------------------------
# The search plan
# -----------------------------------------------------------------------------

def test_treatment_intent_is_detected_conservatively():
    assert is_therapeutic_question(KRAS_Q, "methods")  # "repurposed" says so whatever the type
    assert is_therapeutic_question("Which drugs could be repurposed for idiopathic pulmonary fibrosis?", "general")
    assert is_therapeutic_question(MELANOMA_Q, "trials", names_drug=True)
    # A method about the assay, a mechanism, a safety question and a diagnosis question are not.
    assert not is_therapeutic_question("How do I design a CRISPR knockout screen for drug resistance in melanoma?", "methods")
    assert not is_therapeutic_question(
        "Design a protocol to measure ctDNA response to sotorasib treatment in lung cancer", "methods", names_drug=True)
    assert not is_therapeutic_question(EGFR_Q, "mechanism", names_drug=True)
    assert not is_therapeutic_question("What are the side effects of metformin?", "safety", names_drug=True)
    assert not is_therapeutic_question("What is the diagnostic accuracy of KRAS testing on EUS-FNA?", "genetics")


def test_question_aspects_and_off_topic_terms():
    aspects = question_aspects(KRAS_Q)
    assert {"repurposing", "therapy", "in_silico", "safety", "trials", "real_world"} <= set(aspects)
    assert "docking" in aspects["in_silico"] and "repurpos*" in aspects["repurposing"]
    assert question_aspects(EGFR_Q) == {}
    off = off_topic_title_terms(KRAS_Q)
    assert {"diagnos*", "biops*", "assay*", "cfDNA", "PCR", "FNA"} <= set(off)
    # A term the question uses itself is never demoted.
    assert "diagnos*" not in off_topic_title_terms("Diagnosis and treatment of KRAS-mutant pancreatic cancer")
    assert "cfDNA" not in off_topic_title_terms("Can cfDNA guide treatment with KRAS inhibitors?")


def test_repurposing_pipeline_question_searches_for_treatment_not_protocols():
    plan = _kras_plan()
    assert plan.question_type == "methods" and plan.therapeutic and plan.facet_type == "treatment"
    assert plan.concepts[0] == ["KRAS"] and "pancreatic cancer" in plan.concepts[1]
    assert plan.subject_groups == 1 and plan.hard_groups == 2  # the subject and disease stay the anchors
    # Aspect phrases rank papers but are not concepts every paper must mention.
    assert not any("repurposed" in t for g in plan.concepts for t in g)
    assert "repurposed" in plan.ranking_terms and not plan.pivotal and not plan.intervention_terms
    queries = plan.europepmc_queries(year=2026)
    by_label = {q["label"]: q for q in queries}
    assert {"landmark", "recent", "therapy", "repurposing", "in_silico"} <= set(by_label)
    # The methods facet ("protocol*", "guide*" matched EUS-guided biopsies) is gone; the treatment facet is used.
    assert all("protocol*" not in q["query"] and "guide*" not in q["query"] for q in queries)
    assert 'TITLE_ABS:"treatment"' in by_label["landmark"]["query"]
    therapy = by_label["therapy"]
    assert therapy["tier"] == 1 and therapy["sort"] == "CITED desc" and 'TITLE:"KRAS"' in therapy["query"]
    assert "TITLE_ABS:inhibitor*" in therapy["query"] and "PUB_YEAR:[2021 TO 2026]" in therapy["query"]
    assert 'TITLE_ABS:"pancreatic cancer"' in therapy["query"]
    assert "TITLE_ABS:repurpos*" in by_label["repurposing"]["query"] and by_label["repurposing"]["sort"] is None
    assert 'TITLE_ABS:"docking"' in by_label["in_silico"]["query"]
    for q in queries:
        assert q["disease"] == plan.disease_terms
        assert "diagnos*" in q["topic"]["off_topic"] and "repurposing" in q["topic"]["aspects"]
    # Unicode hyphens are read as words: "step-by-step" is not a search term.
    assert "step" not in plan.keywords() and "status" in plan.keywords()


def test_long_brief_gets_a_concise_web_query():
    plan = _kras_plan()
    web = plan.web_query
    assert "‑" not in web and len(web.split()) <= 16
    assert web.startswith("KRAS pancreatic cancer") and "FDA-approved" in web and "drug repurposing" in web
    short = build_search_plan(_ctx([]), "Is metformin safe in pregnancy?")
    assert short.web_query == "Is metformin safe in pregnancy?"  # a short question is its own web query
    assert normalize_dashes("KRAS‑mutant – x") == "KRAS-mutant - x"


def test_questions_that_are_not_about_treatment_keep_their_searches():
    egfr_ents = [_entity("gene", "EGFR", EGFR_Q), _entity("drug", "osimertinib", EGFR_Q),
                 _entity("disease", "non-small cell lung cancer", EGFR_Q)]
    egfr = build_search_plan(_ctx(egfr_ents), EGFR_Q)
    assert egfr.question_type == "mechanism" and not egfr.therapeutic and egfr.topic() == {}
    labels = [q["label"] for q in egfr.europepmc_queries(year=2026)]
    assert not {"therapy", "repurposing", "in_silico"} & set(labels)
    assert all("topic" not in q for q in egfr.europepmc_queries(year=2026))
    crispr = build_search_plan(_ctx([]), "How do I design a CRISPR knockout screen for drug resistance in melanoma?")
    assert crispr.question_type == "methods" and not crispr.therapeutic and crispr.facet_type == "methods"
    assert any("protocol*" in q["query"] for q in crispr.europepmc_queries(year=2026))


def test_drug_questions_keep_their_pivotal_search_and_get_no_subject_searches():
    ents = [_entity("drug", "pembrolizumab", MELANOMA_Q), _entity("drug", "nivolumab", MELANOMA_Q),
            _entity("disease", "cutaneous melanoma", MELANOMA_Q)]
    plan = build_search_plan(_ctx(ents), MELANOMA_Q)
    assert plan.therapeutic and plan.subject_groups == 0  # the lead concept is the drugs, not a target
    labels = [q["label"] for q in plan.europepmc_queries(year=2026)]
    assert "pivotal" in labels and not {"therapy", "repurposing", "in_silico"} & set(labels)


def test_a_drug_class_named_after_a_target_is_the_intervention():
    ents = [_entity("gene", "PARP", PARP_Q), _entity("disease", "ovarian cancer", PARP_Q),
            _entity("disease", "prostate cancer", PARP_Q)]
    plan = build_search_plan(_ctx(ents), PARP_Q)
    assert plan.intervention_terms[0] == "PARP inhibitors" and plan.pivotal
    pivotal = next(q for q in plan.europepmc_queries(year=2026) if q["label"] == "pivotal")
    assert 'TITLE_ABS:"PARP inhibitor"' in pivotal["query"] and '"pivotal"' not in pivotal["query"]
    # Without a resolved target, a word about the trial itself ("pivotal") is never the intervention.
    bare = build_search_plan(_ctx([]), "What is the efficacy in ovarian cancer from pivotal trials?")
    assert "pivotal" not in [t.lower() for t in bare.intervention_terms]


def test_planner_model_plan_keeps_the_topic():
    plan = _kras_plan()
    merged = merge_llm_plan(
        plan, {"concepts": [["KRAS", "K-Ras"], ["pancreatic cancer", "PDAC"]], "web_query": "KRAS‑mutant PDAC drug repurposing"},
        KRAS_Q,
    )
    assert merged.therapeutic and merged.aspects == plan.aspects and merged.off_topic_terms == plan.off_topic_terms
    assert merged.subject_groups == 1 and merged.web_query == "KRAS-mutant PDAC drug repurposing"
    assert any(q["label"] == "repurposing" for q in merged.europepmc_queries(year=2026))


# -----------------------------------------------------------------------------
# Ranking in the engine
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_treatment_papers_outrank_diagnostic_and_assay_papers():
    plan = _kras_plan()
    client = _Client(
        [
            ("TITLE_ABS:inhibitor*", [SOTORASIB]),
            ("TITLE_ABS:repurpos*", [REPURPOSING]),
            ('TITLE_ABS:"docking"', [DOCKING]),
        ],
        default=[EUS_FNA, DDPCR, ASSAY],  # the landmark and recent searches
    )
    res = await LiteratureSynthesisEngine(biocontext_client=client).execute_literature_synthesis(  # type: ignore[arg-type]
        KRAS_Q, max_articles=4, check_integrity=False,
        search_queries=plan.europepmc_queries(year=2026), keywords=plan.keywords(),
    )
    assert res.success, res.error
    titles = [a["title"] for a in res.data["graded_articles"]]
    on_topic = [SOTORASIB["title"], REPURPOSING["title"], DOCKING["title"]]
    assert set(on_topic) <= set(titles)
    assert len([t for t in titles if t not in on_topic]) <= 1  # one diagnostic paper fills the last place
    ranking = res.data["retrieval"]["ranking"]
    assert ranking["off_topic_demoted"] == 3 and "repurposing" in ranking["aspects"]
    passages = res.data["ranked_passages"]
    good = [i for i, p in enumerate(passages) if p["title"] in on_topic]
    bad = [i for i, p in enumerate(passages) if p["title"] not in on_topic]
    assert good and (not bad or min(bad) > max(good))


@pytest.mark.asyncio
async def test_a_diagnostic_title_that_is_about_treatment_is_not_demoted():
    plan = _kras_plan()
    ctdna_response = _art("50000001", "Circulating tumor DNA detection predicts response to KRAS inhibitor therapy in pancreatic cancer",
                          "ctDNA detection during KRAS inhibitor treatment of pancreatic cancer predicted response.")
    client = _Client([], default=[EUS_FNA, ctdna_response])
    res = await LiteratureSynthesisEngine(biocontext_client=client).execute_literature_synthesis(  # type: ignore[arg-type]
        KRAS_Q, max_articles=2, check_integrity=False,
        search_queries=plan.europepmc_queries(year=2026), keywords=plan.keywords(),
    )
    assert res.success, res.error
    assert res.data["graded_articles"][0]["title"] == ctdna_response["title"]
    assert res.data["retrieval"]["ranking"]["off_topic_demoted"] == 1


# -----------------------------------------------------------------------------
# Web search
# -----------------------------------------------------------------------------

CHALLENGE = ('<html><div class="anomaly-modal__mask"></div><p>Unfortunately, bots use DuckDuckGo too.</p>'
             '<form id="challenge-form"></form></html>')
RESULT_HTML = ('<div class="result__body"><h2 class="result__title"><a href="https://example.org/a">Sotorasib in PDAC</a></h2>'
               '<a class="result__snippet">KRAS G12C inhibitor results.</a></div>')


def test_clean_search_query():
    q = clean_search_query("  KRAS‑mutant\npancreatic\tcancer  ")
    assert q == "KRAS-mutant pancreatic cancer"
    long = clean_search_query("word " * 200)
    assert len(long) <= web_search.MAX_QUERY_CHARS and not long.endswith(" ")


@pytest.mark.asyncio
async def test_duckduckgo_challenge_is_a_block_not_an_empty_result():
    blocked, attempt = await web_search._ddg_outcome(httpx.Response(202, text=CHALLENGE), web_search._parse_ddg_html, 3)
    assert blocked == [] and attempt["status"] == "blocked" and "202" in attempt["detail"]
    blocked, attempt = await web_search._ddg_outcome(httpx.Response(200, text=CHALLENGE), web_search._parse_ddg_html, 3)
    assert attempt["status"] == "blocked"
    found, attempt = await web_search._ddg_outcome(httpx.Response(200, text=RESULT_HTML), web_search._parse_ddg_html, 3)
    assert attempt["status"] == "ok" and found[0]["url"] == "https://example.org/a"
    none, attempt = await web_search._ddg_outcome(httpx.Response(200, text="<html></html>"), web_search._parse_ddg_html, 3)
    assert none == [] and attempt["status"] == "empty"
    _, attempt = await web_search._ddg_outcome(httpx.Response(503, text="down"), web_search._parse_ddg_html, 3)
    assert attempt["status"] == "error"


def _client(monkeypatch: pytest.MonkeyPatch, html: Any, lite: Any) -> Tuple[FreeWebSearchClient, List[str]]:
    for var in ("PHRONESIS_SEARXNG_URL", "TAVILY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = FreeWebSearchClient()
    seen: List[str] = []

    def provider(outcome: Any):
        async def run(query: str, max_results: int, timeout: float):
            seen.append(query)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return run

    monkeypatch.setattr(client, "_ddg_html", provider(html))
    monkeypatch.setattr(client, "_ddg_lite", provider(lite))
    return client, seen


@pytest.mark.asyncio
async def test_web_search_fails_loudly_when_every_provider_is_blocked(monkeypatch: pytest.MonkeyPatch):
    blocked = ([], web_search._attempt("", "blocked", "anti-bot challenge (HTTP 202)"))
    client, seen = _client(monkeypatch, blocked, httpx.ConnectTimeout("timed out"))
    res = await client.search_and_surf(KRAS_Q, max_results=3, surf_top=False)
    assert isinstance(res, ToolResult) and not res.success and res.error_type == ErrorType.UPSTREAM_API_ERROR
    assert res.error.startswith("Web search unavailable") and "DuckDuckGo HTML: blocked" in res.error
    assert "DuckDuckGo Lite: error" in res.error and "ConnectTimeout" in res.error
    assert res.data["results"] == [] and [a["provider"] for a in res.data["providers"]] == ["DuckDuckGo HTML", "DuckDuckGo Lite"]
    assert seen and all("‑" not in q and len(q) <= web_search.MAX_QUERY_CHARS for q in seen)


@pytest.mark.asyncio
async def test_web_search_falls_back_and_reports_providers(monkeypatch: pytest.MonkeyPatch):
    hit = [{"title": "Sotorasib in PDAC", "url": "https://example.org/a", "snippet": "x"}]
    blocked = ([], web_search._attempt("", "blocked", "anti-bot challenge (HTTP 202)"))
    client, _ = _client(monkeypatch, blocked, (hit, web_search._attempt("", "ok")))
    res = await client.search_and_surf("KRAS pancreatic cancer drug repurposing", max_results=3, surf_top=False)
    assert isinstance(res, dict) and res["results"] == hit
    assert [a["status"] for a in res["providers"]] == ["blocked", "ok"]
    # A provider that answered with nothing is a real empty result, not a failure.
    client, _ = _client(monkeypatch, ([], web_search._attempt("", "empty")), ([], web_search._attempt("", "empty")))
    empty = await client.search_and_surf("zzqx nonexistent term", max_results=3, surf_top=False)
    assert isinstance(empty, dict) and empty["results"] == []


@pytest.mark.asyncio
async def test_a_configured_searxng_instance_is_tried_first(monkeypatch: pytest.MonkeyPatch):
    client, seen = _client(monkeypatch, ([], web_search._attempt("", "empty")), ([], web_search._attempt("", "empty")))
    monkeypatch.setenv("PHRONESIS_SEARXNG_URL", "https://searx.example.org")
    hit = [{"title": "t", "url": "https://example.org/b", "snippet": "s"}]

    async def searx(query: str, max_results: int, timeout: float):
        return hit, web_search._attempt("", "ok")

    monkeypatch.setattr(client, "_searxng", searx)
    results, attempts = await client.search_with_status("KRAS pancreatic cancer", 3)
    assert results == hit and [a["provider"] for a in attempts] == ["SearXNG"] and not seen


def test_tavily_is_tried_first_when_configured(monkeypatch):
    import asyncio
    import json as _json

    import httpx

    from phronesis.tools import web_search as ws

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            sent["auth"] = request.headers.get("authorization")
            sent["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"results": [
                {"title": "Sotorasib in KRAS G12C PDAC", "url": "https://example.org/a", "content": "ORR 21%", "score": 0.9}]})
        raise AssertionError(f"unexpected request to {request.url.host}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda *a, **k: real_client(*a, transport=httpx.MockTransport(handler), **k))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("PHRONESIS_SEARXNG_URL", raising=False)
    results, attempts = asyncio.run(ws.FreeWebSearchClient().search_with_status("KRAS pancreatic cancer sotorasib", 3))
    assert results[0]["url"] == "https://example.org/a" and results[0]["snippet"] == "ORR 21%"
    assert attempts == [{"provider": "Tavily", "status": "ok"}]
    assert sent["auth"] == "Bearer tvly-test" and sent["body"]["max_results"] == 3


def test_rejected_tavily_key_falls_back_to_duckduckgo(monkeypatch):
    import asyncio

    import httpx

    from phronesis.tools import web_search as ws

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(401, json={"detail": "invalid key"})
        return httpx.Response(202, text="Unfortunately, bots use DuckDuckGo too.")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda *a, **k: real_client(*a, transport=httpx.MockTransport(handler), **k))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-bad")
    results, attempts = asyncio.run(ws.FreeWebSearchClient().search_with_status("KRAS", 3))
    assert results == [] and attempts[0]["provider"] == "Tavily" and "rejected" in attempts[0]["detail"]
