"""Retrieval obligations (source audit F5, F10, and the live diagnostic run of 18 October 2026).

- Explicit identifiers in a question are fetched by exact identifier before topical search, through the registered
  literature tool down to the Europe PMC request; a failed lookup is "not retrieved", never "does not exist".
- Gap-query deduplication keeps distinct clinical requirements (disease, regimen, year) apart.
- Explicit exclusions ("No FDA sweep, simulation, omics, hypothesis generation or debate is needed") are honoured.

Identifiers 90000001-90000003 are synthetic: these fakes stand in for HTTP responses and are not scientific records.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from phronesis.lithoscribe.identifiers import extract_literature_ids, normalize_literature_id, strip_literature_ids
from phronesis.lithoscribe.synthesis_engine import LiteratureSynthesisEngine
from phronesis.tools import registry as registry_module
from phronesis.utils.biocontext import BioContextMCPClient

SYN_PMID = "90000001"
SYN_ERRATUM = "90000002"


def _record(pmid: str, title: str, abstract: str, **extra: Any) -> Dict[str, Any]:
    art = {
        "id": pmid, "source": "MED", "pmid": pmid, "doi": "", "pmcid": "", "title": title, "abstractText": abstract,
        "pubYear": "2024", "journalTitle": "Synthetic Journal", "authorString": "Author A", "pubType": "journal article",
        "pubTypeList": {"pubType": ["Journal Article"]}, "isOpenAccess": False,
    }
    art.update(extra)
    return art


SYN_NSCLC = _record(
    SYN_PMID, "Synthetic comparative study of drug A versus drug B in advanced non-small cell lung cancer",
    "In advanced NSCLC, drug A (N=10) and drug B (N=8) gave median OS of 9.1 and 6.4 months.",
    commentCorrectionList={"commentCorrection": [
        {"source": "MED", "id": SYN_ERRATUM, "reference": "Synth J. 2025;1:1.", "type": "Erratum in", "orderIn": 1}
    ]},
)
SYN_ERRATUM_REC = _record(SYN_ERRATUM, "Corrigendum to synthetic comparative study", "", pubType="Published Erratum",
                          pubTypeList={"pubType": ["Published Erratum"]})


class FakeEuropePMC:
    """Europe PMC stand-in at the client boundary: answers exact-identifier queries from a table."""

    def __init__(self, records: Dict[str, Dict[str, Any]], fail: Optional[str] = None):
        self.records, self.fail, self.calls = records, fail, []

    async def async_query_europepmc(self, query: str, limit: int = 5, sort: Any = None) -> Dict[str, Any]:
        self.calls.append(query)
        if self.fail == "timeout":
            raise asyncio.TimeoutError("read timeout")
        for pmid, rec in self.records.items():
            if query == f"EXT_ID:{pmid} AND SRC:MED":
                return {"articles": [dict(rec)], "hit_count": 1}
        return {"articles": [], "hit_count": 0}

    async def async_fetch_europepmc_fulltext(self, pmcid: str) -> str:  # pragma: no cover - no PMCIDs used
        return ""


# Identifier extraction --------------------------------------------------------------------------------------------
def test_explicit_identifiers_are_extracted_and_normalized():
    q = ("Retrieve PMID 39369609 by exact identifier; also PMID:23313110, PMC1234567 and "
         "doi 10.1016/j.lungcan.2024.107960. Report 2024 data at 960 mg.")
    assert extract_literature_ids(q) == ["39369609", "23313110", "PMC1234567", "10.1016/j.lungcan.2024.107960"]
    assert extract_literature_ids("https://pubmed.ncbi.nlm.nih.gov/41660470/") == ["41660470"]
    assert extract_literature_ids("Median OS was 10.2 months in 2024 (n=164).") == []
    assert normalize_literature_id("PMID:39369609") == ("39369609", "pmid")
    assert normalize_literature_id("pmc77") == (None, "invalid")
    assert normalize_literature_id("not-an-id") == (None, "invalid")
    assert "39369609" not in strip_literature_ids("Retrieve PMID 39369609 now")


# The registered tool, through to the client request -------------------------------------------------------------
def _registered(monkeypatch: Any, fake: FakeEuropePMC) -> Any:
    monkeypatch.setattr(BioContextMCPClient, "async_query_europepmc", lambda self, *a, **k: fake.async_query_europepmc(*a, **k))
    monkeypatch.setattr(BioContextMCPClient, "async_fetch_europepmc_fulltext", lambda self, *a, **k: fake.async_fetch_europepmc_fulltext(*a, **k))
    reg = registry_module.BiomedicalToolRegistry()
    registry_module.register_default_biomedical_tools(reg)
    return reg


def test_schema_exposes_pinned_ids_and_the_planner_sees_them():
    from phronesis.agentic.tool_retrieval import tool_card

    inp = registry_module.LithoscribeSynthesisInput.model_validate({"query": "x", "pinned_ids": ["PMID:39369609"]})
    assert inp.pinned_ids == ["PMID:39369609"]
    reg = registry_module.BiomedicalToolRegistry()
    registry_module.register_default_biomedical_tools(reg)
    card = tool_card(reg.get_entry("lithoscribe_literature_synthesis"))
    assert "pinned_ids" in card["args"] and "exact" in card["args"]["pinned_ids"].get("description", "").lower()


def test_registered_tool_fetches_a_pinned_pmid_by_exact_identifier(monkeypatch):
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC, SYN_ERRATUM: SYN_ERRATUM_REC})
    reg = _registered(monkeypatch, fake)
    res = asyncio.run(reg.execute("lithoscribe_literature_synthesis", query="anything", pinned_ids=[f"PMID:{SYN_PMID}"],
                                  check_integrity=False, max_articles=1))
    assert fake.calls[0] == f"EXT_ID:{SYN_PMID} AND SRC:MED"
    assert res.success, res.error
    assert any(p["pmid"] == SYN_PMID for p in res.data["ranked_passages"])


def test_identifier_in_the_question_is_an_obligation_even_without_pinned_ids(monkeypatch):
    """The live run's plan called the tool with query="PMID:39369609": Europe PMC has no PMID field, so the free-text
    search found nothing. The identifier in the query text is now looked up exactly, and no free-text PMID query runs."""
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC})
    reg = _registered(monkeypatch, fake)
    res = asyncio.run(reg.execute("lithoscribe_literature_synthesis", query=f"PMID:{SYN_PMID}", check_integrity=False,
                                  max_articles=1))
    assert res.success, res.error
    assert fake.calls[0] == f"EXT_ID:{SYN_PMID} AND SRC:MED"
    assert not any(c.startswith("PMID") for c in fake.calls)


def test_a_pinned_record_is_kept_when_its_disease_contradicts_the_question(monkeypatch):
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC})
    engine = LiteratureSynthesisEngine(biocontext_client=fake)  # type: ignore[arg-type]
    res = asyncio.run(engine.execute_literature_synthesis(
        f"Does PMID {SYN_PMID} report outcomes in colorectal cancer?", check_integrity=False,
        disease_terms=["colorectal cancer"], max_articles=3,
    ))
    assert res.success, res.error
    assert any(p["pmid"] == SYN_PMID for p in res.data["ranked_passages"])


def test_classic_stage_shape_keeps_the_named_paper_despite_unrelated_search_anchors():
    """The live baseline's evidence stage: a search plan whose lead concept was "Exact title disease" excluded every
    record as off-subject. The paper the question names is fetched exactly and is exempt from subject anchors."""
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC})
    engine = LiteratureSynthesisEngine(biocontext_client=fake)  # type: ignore[arg-type]
    question = (f"Retrieve PMID {SYN_PMID} by exact identifier. Report: 1. Exact title, disease population and study "
                "design. No FDA sweep, simulation, omics, hypothesis generation or debate is needed.")
    res = asyncio.run(engine.execute_literature_synthesis(
        question, check_integrity=False, search_queries=[{"query": '"exact title" AND disease', "tier": 1, "label": "lead"}],
    ))
    assert res.success, res.error
    assert fake.calls[0] == f"EXT_ID:{SYN_PMID} AND SRC:MED"
    assert [p["pmid"] for p in res.data["ranked_passages"]][:1] == [SYN_PMID]
    assert res.data["retrieval"]["pinned"][0]["status"] == "found"


def test_invalid_identifier_is_reported_not_silently_dropped(monkeypatch):
    fake = FakeEuropePMC({})
    reg = _registered(monkeypatch, fake)
    res = asyncio.run(reg.execute("lithoscribe_literature_synthesis", query="exact lookup", pinned_ids=["not-an-id"],
                                  check_integrity=False))
    pinned = res.data["retrieval"]["pinned"]
    assert pinned[0]["status"] == "invalid_identifier"
    assert "does not exist" not in (res.error or "")


def test_upstream_timeout_uses_one_bounded_alternative_then_says_not_retrieved(monkeypatch):
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC}, fail="timeout")
    alt_calls: List[str] = []

    async def failing_alternative(pmid: str) -> Optional[Dict[str, Any]]:
        alt_calls.append(pmid)
        raise asyncio.TimeoutError("efetch timeout")

    engine = LiteratureSynthesisEngine(biocontext_client=fake, pubmed_fetcher=failing_alternative)  # type: ignore[arg-type]
    res = asyncio.run(engine.execute_literature_synthesis(f"PMID {SYN_PMID}", check_integrity=False))
    assert alt_calls == [SYN_PMID]  # exactly one alternative strategy
    assert not res.success
    entry = res.data["retrieval"]["pinned"][0]
    assert entry["status"] == "not_retrieved" and entry["upstream"] == "timeout"
    assert entry["alternative"] == {"source": "ncbi_efetch", "status": "timeout", "error": "TimeoutError"}
    assert "not retrieved" in res.error and "not proof" in res.error
    assert "No peer-reviewed publications found" not in res.error


def test_upstream_timeout_recovers_through_the_alternative(monkeypatch):
    fake = FakeEuropePMC({}, fail="timeout")

    async def alternative(pmid: str) -> Optional[Dict[str, Any]]:
        return dict(SYN_NSCLC)

    engine = LiteratureSynthesisEngine(biocontext_client=fake, pubmed_fetcher=alternative)  # type: ignore[arg-type]
    res = asyncio.run(engine.execute_literature_synthesis(f"PMID {SYN_PMID}", check_integrity=False))
    assert res.success, res.error
    entry = res.data["retrieval"]["pinned"][0]
    assert entry["status"] == "found" and entry["via"] == "ncbi_efetch"


def test_correction_linkage_is_retrieved_and_reported(monkeypatch):
    fake = FakeEuropePMC({SYN_PMID: SYN_NSCLC, SYN_ERRATUM: SYN_ERRATUM_REC})
    engine = LiteratureSynthesisEngine(biocontext_client=fake)  # type: ignore[arg-type]
    res = asyncio.run(engine.execute_literature_synthesis(f"PMID {SYN_PMID}", check_integrity=False))
    assert res.success, res.error
    corr = res.data["retrieval"]["pinned"][0]["corrections"]
    assert corr == [{
        "id": SYN_ERRATUM, "type": "Erratum in", "reference": "Synth J. 2025;1:1.", "retrieved": True,
        "title": "Corrigendum to synthetic comparative study", "content_available": False, "applied": False,
    }]
    assert any("corrected content was not applied" in x for x in res.data["limitations"])


# Gap deduplication ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "first,second",
    [
        ("KRAS G12C sotorasib FDA approval NSCLC", "KRAS G12C sotorasib FDA approval colorectal cancer"),
        ("KRAS G12C sotorasib FDA approval colorectal cancer monotherapy",
         "KRAS G12C sotorasib FDA approval colorectal cancer combination panitumumab"),
        ("KRAS G12C sotorasib FDA approval colorectal cancer 2024", "KRAS G12C sotorasib FDA approval colorectal cancer 2025"),
        ("sotorasib FDA approval NSCLC first-line", "sotorasib FDA approval NSCLC second-line"),
        ("sotorasib EMA approval NSCLC", "sotorasib FDA approval NSCLC"),
        ("adagrasib overall survival NSCLC", "adagrasib progression-free survival NSCLC"),
    ],
)
def test_distinct_clinical_requirements_are_never_duplicates(first, second):
    from phronesis.agentic.gaps import near_duplicate, requirement_keys

    assert requirement_keys(first) != requirement_keys(second)
    assert not near_duplicate(second, [first])


def test_equivalent_rephrasings_are_still_duplicates():
    from phronesis.agentic.gaps import near_duplicate

    assert near_duplicate("sotorasib FDA approval KRAS G12C", ["KRAS G12C sotorasib FDA approval"])
    assert near_duplicate("FDA approval sotorasib KRAS G12C colorectal", ["KRAS G12C sotorasib FDA approval CRC"])


def test_off_topic_results_are_not_stored_by_fetch_new_literature():
    from phronesis.agentic.specialists import fetch_new_literature

    stored: Dict[str, Any] = {}

    class State:
        tool_results = stored
        evidence_labels: Dict[str, Any] = {}

        async def call_tool(self, *_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [
                {"pmid": "1", "passage_text": "KRAS biology in organoids."},
                {"pmid": "2", "passage_text": "Sotorasib FDA approval in colorectal cancer."},
            ]})

        def next_key(self, *_a: Any) -> str:
            return "k1"

        def add_result(self, key: str, res: Any, **_k: Any) -> str:
            stored[key] = res
            return key

    stats: Dict[str, Any] = {}
    key, kept = asyncio.run(fetch_new_literature(
        State(), phase="gaps", query="sotorasib approval colorectal", purpose="t", seen=set(),
        keep=lambda p: "colorectal" in p["passage_text"], stats=stats,
    ))
    assert [p["pmid"] for p in kept] == ["2"]
    assert [p["pmid"] for p in stored[key].data["ranked_passages"]] == ["2"]
    assert stats == {"status": "ok", "returned": 2, "new": 2, "kept": 1}


def test_gap_outcomes_are_classified():
    from phronesis.agentic.gaps import gap_outcome

    assert gap_outcome([{"status": "error"}]) == "error"
    assert gap_outcome([{"status": "ok", "new": 2, "kept": 0}]) == "off_topic"
    assert gap_outcome([{"status": "ok", "new": 0, "kept": 0, "returned": 3}]) == "already_seen"
    assert gap_outcome([{"status": "empty"}]) == "empty"
    assert gap_outcome([{"status": "ok", "new": 1, "kept": 1, "incomplete": True}]) == "filled"
    assert gap_outcome([{"status": "ok", "new": 0, "kept": 0, "returned": 0, "incomplete": True}]) == "incomplete"
    assert gap_outcome([{"status": "error"}, {"status": "ok", "new": 1, "kept": 1}]) == "filled"


# Explicit exclusions --------------------------------------------------------------------------------------------
QUERY_EXCLUSIONS = "No FDA sweep, simulation, omics, hypothesis generation or debate is needed."


def test_exclusion_list_is_detected():
    from phronesis.agents.query_planner import detect_excluded_modules

    assert detect_excluded_modules(QUERY_EXCLUSIONS) >= {"fda_sweep", "simulation", "omics", "hypothesis", "debate"}
    assert detect_excluded_modules("Do not generate hypotheses. Skip the debate.") >= {"hypothesis", "debate"}
    assert detect_excluded_modules("without any omics analysis") >= {"omics"}


@pytest.mark.parametrize(
    "text",
    [
        "There is no evidence that this hypothesis holds; propose two testable hypotheses.",
        "Propose two hypotheses and debate their merits.",
        "Is there no FDA approval for adagrasib in CRC?",
        "No patients received omics profiling in the trial, according to the paper.",
    ],
)
def test_exclusion_false_positive_controls(text):
    from phronesis.agents.query_planner import detect_excluded_modules

    assert not ({"hypothesis", "debate", "omics", "fda_sweep", "regulatory"} & detect_excluded_modules(text))


def _ctx(query: str) -> Any:
    from phronesis.agents.query_context import QueryContext
    from phronesis.agents.query_planner import apply_simulation_request

    ctx = QueryContext(raw_query=query, clean_query=query)
    apply_simulation_request(ctx, query)
    return ctx


def test_excluded_modules_do_not_run():
    from phronesis.agentic.hypothesis import HypothesisPhase
    from phronesis.agentic.plan_graph import policy_violation
    from phronesis.agentic.router import EscalationPhase

    q = "Retrieve PMID 39369609 by exact identifier. " + QUERY_EXCLUSIONS
    ctx = _ctx(q)
    assert ctx.excluded_modules >= {"hypothesis", "debate", "omics", "fda_sweep"}
    state = SimpleNamespace(ctx=ctx, framework="agentic-plan-execute", draft="draft", draft_round=0, bag={"routing": {"requested": "auto"}},
                            clean_query=q, query=q, tool_results={}, sub_questions=[], budget=SimpleNamespace(llm_calls=0, max_llm_calls=40, exhausted=lambda: False),
                            llm=SimpleNamespace(available=True), options={})
    assert not HypothesisPhase().applies(state)
    assert not EscalationPhase().applies(state)
    reg = registry_module.BiomedicalToolRegistry()
    registry_module.register_default_biomedical_tools(reg)
    from phronesis.extensions import load_extensions

    load_extensions()
    from phronesis.extensions import register_extension_tools

    register_extension_tools(reg)
    omics = [e for e in reg.list_tools() if e.name.startswith(("gtex", "gwas", "depmap"))]
    assert len(omics) == 3, "expected the GWAS, GTEx and DepMap tools to be registered"
    assert all("excluded" in (policy_violation(e, ctx) or "") for e in omics)
    # Literature, chemistry and pathway tools are not omics analyses.
    for name in ("lithoscribe_literature_synthesis", "pubchem_compound_properties", "reactome_pathways"):
        assert policy_violation(reg.get_entry(name), ctx) is None or "excluded" not in policy_violation(reg.get_entry(name), ctx)
    assert ctx.literature_ids == ["39369609"]


def test_openfda_regulatory_sweep_is_skipped_when_excluded():
    from phronesis.drugintel.sources import OpenFdaSource

    ctx = _ctx("What are the FDA-approved indications for KRAS G12C inhibitors? No FDA sweep is needed.")
    decision = OpenFdaSource().should_run(ctx)
    assert not decision.run and "excluded" in decision.reason
