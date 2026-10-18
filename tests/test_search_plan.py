"""Search planning: concepts from the question, compiled EuropePMC tiers, and the planner model's plan validated."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from phronesis.agents import query_planner as qp
from phronesis.agents.search_plan import SearchPlan, classify_question, merge_llm_plan
from phronesis.lithoscribe.synthesis_engine import LiteratureSynthesisEngine


async def _plan(query: str) -> SearchPlan:
    ctx = await qp.build_query_context(
        query=query, clean_query=query, registry=None, enable_web_search=True, attachments=[], attachment_errors=[],
        user_id="u", thread_id="t", model_name="m", api_key=None, llm_factory=None,
    )
    return ctx.search_plan


@pytest.mark.parametrize(
    "query, qtype",
    [
        ("What are the diagnostic criteria for sepsis?", "diagnosis"),
        ("What is the global prevalence of type 2 diabetes?", "epidemiology"),
        ("How is Kawasaki disease managed?", "treatment"),
        ("What is the recommended metformin dose adjustment in chronic kidney disease?", "pharmacology"),
        ("How do I calculate sample size for a non-inferiority trial?", "statistics"),
        ("Are there ongoing phase 3 trials of semaglutide in Alzheimer's disease?", "trials"),
        ("Which drugs are FDA approved for obesity?", "regulatory"),
        ("How does pembrolizumab work?", "mechanism"),
    ],
)
def test_question_types(query, qtype):
    assert classify_question(query) == qtype


@pytest.mark.asyncio
async def test_concepts_drop_question_words_and_keep_topics():
    plan = await _plan("Are there ongoing phase 3 trials of semaglutide in Alzheimer's disease?")
    assert plan.concepts[0] == ["semaglutide"]
    assert ["Alzheimer's disease", "Alzheimer disease"] in plan.concepts  # possessive variant added
    assert not any(t in ("phase", "trials", "ongoing") for g in plan.concepts for t in g)

    variant = await _plan("What is the significance of KRAS G12C in NSCLC?")
    assert variant.concepts[:2] == [["KRAS"], ["G12C"]]  # the change is searched next to its gene

    methods = await _plan("How do I design a CRISPR knockout screen to find drug resistance genes?")
    assert methods.concepts[0] == ["CRISPR knockout screen"]  # the method, not the disease word, is the topic


@pytest.mark.asyncio
async def test_ranking_keywords_are_content_words_and_keep_concepts_past_the_limit():
    # Found by the evaluation harness: "of", "synthesize" and literal "mechanism*" ranked passages, and the
    # question's topic ("acquired resistance") was dropped by the three-group limit.
    plan = await _plan("Synthesize mechanisms of acquired resistance to osimertinib in EGFR-mutant NSCLC across clinical reports.")
    words = plan.keywords()
    assert {"osimertinib", "resistance"} <= set(words)
    assert not {"of", "synthesize", "across"} & set(words) and not any("*" in w for w in words)
    assert len(plan.concepts) == 3 and plan.ranking_terms  # extra concepts rank passages without narrowing the search


@pytest.mark.asyncio
async def test_web_query_is_the_question_not_padded_with_mechanism_words():
    plan = await _plan("What is the treatment of choice for MRSA bacteremia?")
    assert plan.web_query == "What is the treatment of choice for MRSA bacteremia?"
    assert "kinetics" not in plan.web_query


def test_europepmc_tiers():
    plan = SearchPlan(question_type="diagnosis", concepts=[["sepsis"], ["septic shock"]])
    queries = plan.europepmc_queries(year=2026)
    by_label = {q["label"]: q for q in queries}
    landmark, recent = by_label["landmark"], by_label["recent"]
    assert landmark["sort"] == "CITED desc" and landmark["tier"] == 1 and recent["sort"] is None
    assert 'TITLE:"sepsis"' in landmark["query"] and "PUB_YEAR:[2014 TO 2026]" in landmark["query"]
    assert "TITLE:diagnos*" in landmark["query"]  # diagnosis papers name it in the title; prefix stays unquoted
    assert 'TITLE_ABS:"sepsis"' in recent["query"] and "PUB_YEAR:[2023 TO 2026]" in recent["query"]
    assert {q["tier"] for q in queries} == {1, 2, 3}
    assert SearchPlan(concepts=[]).europepmc_queries() == []


def test_planner_plan_may_add_synonyms_but_not_answers():
    base = SearchPlan(question_type="treatment", concepts=[["MRSA"], ["bacteremia"]], web_query="q")
    raw = {
        "question_type": "treatment",
        "concepts": [
            ["MRSA", "methicillin-resistant Staphylococcus aureus"],
            ["vancomycin"],  # a candidate answer: shares no word with the question
            ["bacteremia", "bacteraemia", "AB"],  # two-letter abbreviations are too ambiguous to search
        ],
        "web_query": "MRSA bacteremia treatment",
    }
    merged = merge_llm_plan(base, raw, "What is the treatment of choice for MRSA bacteremia?")
    assert merged.source == "llm"
    assert merged.concepts == [["MRSA", "methicillin-resistant Staphylococcus aureus"], ["bacteremia", "bacteraemia"]]
    assert merged.web_query == "MRSA bacteremia treatment"
    # A concept the model left out is kept.
    dropped = merge_llm_plan(base, {"concepts": [["MRSA"]]}, "treatment of MRSA bacteremia")
    assert ["bacteremia"] in dropped.concepts
    assert merge_llm_plan(base, None, "x") is base and merge_llm_plan(base, {"concepts": "junk"}, "x") is base


class _Client:
    """EuropePMC stand-in: answers by query label so tier order can be checked."""

    def __init__(self, by_sort: Dict[Any, List[str]]):
        self.by_sort, self.calls = by_sort, []

    async def async_query_europepmc(self, query: str, limit: int = 5, sort: Any = None) -> Dict[str, Any]:
        self.calls.append((query, sort))
        ids = self.by_sort.get(sort, [])
        return {"articles": [{"id": i, "title": i, "pmid": i, "abstractText": f"{i} sepsis criteria."} for i in ids]}


@pytest.mark.asyncio
async def test_planned_search_interleaves_tiers_and_widens_only_when_needed():
    plan = SearchPlan(question_type="diagnosis", concepts=[["sepsis"]])
    client = _Client({"CITED desc": ["L1", "L2", "L3"], None: ["R1", "R2"]})
    engine = LiteratureSynthesisEngine(biocontext_client=client)
    found, err = await engine._planned_search(plan.europepmc_queries(year=2026), max_articles=8)
    assert list(found)[:4] == ["L1", "R1", "L2", "R2"] and err is None
    assert {sort for _, sort in client.calls} == {"CITED desc", None} and len(client.calls) == 2  # tier 1 sufficed

    sparse = _Client({})
    await LiteratureSynthesisEngine(biocontext_client=sparse)._planned_search(plan.europepmc_queries(year=2026), 8)
    assert len(sparse.calls) == len(plan.europepmc_queries(year=2026))  # nothing found: every tier was tried
