"""Regressions found by live runs against the local backend (Phronesis_Next_Test_Query.txt, 18 October 2026)."""

from __future__ import annotations

import pytest

from phronesis.agents.query_planner import detect_simulation_constraints

# The diagnostic query's own exclusion sentence asked a clarifying question ("Which model should I simulate?").
BAN_LIST = "No FDA sweep, simulation, omics, hypothesis generation or debate is needed."


@pytest.mark.parametrize(
    "text",
    [
        BAN_LIST,
        "Retrieve PMID 39369609 by exact identifier.\n" + BAN_LIST + "\nDo not rewrite the answer without new evidence.",
        "No simulation or single-cell analysis needed.",
        "No literature review, kinetic modelling or parameter scans are required.",
    ],
)
def test_a_listed_ban_forbids_simulation(text):
    c = detect_simulation_constraints(text)
    assert c.simulation_forbidden and not c.simulation_requested


@pytest.mark.parametrize(
    "text",
    [
        "Simulate the EGFR model. No KRAS literature needed.",
        "Run a time course of BIOMD0000000010 with no extra outputs.",
        "There is no evidence yet; simulate the MAPK model.",
    ],
)
def test_an_unrelated_no_does_not_ban_a_requested_simulation(text):
    c = detect_simulation_constraints(text)
    assert c.simulation_requested and not c.simulation_forbidden


# The diagnostic query also excludes debate and hypothesis generation; neither may be routed to or run.
def test_auto_routing_never_picks_debate_when_the_question_excludes_it():
    from phronesis.agentic import router
    from phronesis.agentic.config import DEBATE
    from phronesis.agents.query_context import QueryContext

    q = "Propose two testable hypotheses and debate whether drug A is superior to drug B. No debate is needed."
    ctx = QueryContext(raw_query=q, clean_query=q, excluded_modules={"debate"})
    assert router.rule_decision(q, ctx).framework != DEBATE
    ctx_free = QueryContext(raw_query=q, clean_query=q)
    signals = router.routing_signals(q, ctx_free)
    features = router.deterministic_features(signals)
    picked = router.decide(signals, features, excluded={"debate"})
    assert picked.framework != DEBATE and DEBATE not in picked.candidates


def test_an_excluded_debate_phase_does_not_run_even_under_the_debate_framework():
    from types import SimpleNamespace

    from phronesis.agentic.config import DEBATE
    from phronesis.agentic.topologies import DebatePhase

    state = SimpleNamespace(framework=DEBATE, bag={}, draft="An answer.", ctx=SimpleNamespace(scope="research", excluded_modules={"debate"}))
    assert not DebatePhase().applies(state)
    state.ctx.excluded_modules = set()
    assert DebatePhase().applies(state)


def test_named_identifiers_are_always_passed_to_the_literature_tool():
    import asyncio

    from phronesis.agentic.budget import RunBudget
    from phronesis.agentic.state import AgentState
    from phronesis.agents.query_context import QueryContext

    seen = {}

    class Reg:
        async def execute(self, name, **kwargs):
            seen.update(kwargs)
            from phronesis.tools.registry import ToolResult

            return ToolResult(success=True, tool_name=name, data={})

    q = "Retrieve PMID 39369609 by exact identifier."
    st = AgentState.__new__(AgentState)
    st.ctx, st.registry, st.budget, st.trace = QueryContext(raw_query=q, clean_query=q), Reg(), RunBudget(), []
    st.control = type("C", (), {"checkpoint": lambda self: None})()
    st.tool_results, st.evidence_labels = {}, {}
    asyncio.run(st.call_tool("lithoscribe_literature_synthesis", phase="plan", purpose="t", store=False, query="sotorasib"))
    assert seen["pinned_ids"] == ["39369609"]


def test_references_to_internal_evidence_sections_are_removed_from_the_answer():
    from phronesis.agents.authoritative_orchestrator import strip_evidence_markup

    text = "- The correction notice was retrieved (see `<literature_limitations>`).\n- Keep `x < y` and <b>bold</b>."
    out = strip_evidence_markup(text)
    assert out.startswith("- The correction notice was retrieved.") and "literature_limitations" not in out
    assert "`x < y`" in out and "<b>bold</b>" in out  # only snake_case internal section names are touched


def test_agentic_literature_documents_carry_the_title_and_record_url():
    from phronesis.agentic.evidence import render_result
    from phronesis.tools.registry import ToolResult

    res = ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [
        {"pmid": "39369609", "title": "Real-world comparative effectiveness of sotorasib versus docetaxel", "passage_text": "Objectives ..."}
    ]})
    out = render_result("plan.1.lit", res, {"phase": "plan", "purpose": "exact lookup"})
    assert 'title="Real-world comparative effectiveness of sotorasib versus docetaxel"' in out
    assert 'url="https://pubmed.ncbi.nlm.nih.gov/39369609/"' in out
