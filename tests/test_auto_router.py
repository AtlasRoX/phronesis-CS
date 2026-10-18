"""
Auto routing (phronesis/agentic/router.py, router_store.py): the routing matrix and benchmark accuracy, semantic
features and self-consistency, guardrails and adjudication, outcome learning, escalation, and Auto end to end
through the orchestrator. No network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from langchain_core.messages import AIMessage

from phronesis.agentic import config as agent_config
from phronesis.agentic import router, router_store
from phronesis.agents import query_planner as qp
from tests.agentic_fakes import collect_events, make_agentic_orchestrator

AGENTIC, SUPERVISOR, SWARM, DEBATE = agent_config.AGENTIC, agent_config.SUPERVISOR, agent_config.SWARM, agent_config.DEBATE
ROOT = Path(__file__).resolve().parents[1]

# One question per clinical question type, with the route and the explanation rule the router should give.
ROUTES = [
    (
        "For KRAS G12C-mutant non-small cell lung cancer, compare sotorasib and adagrasib: summarize their pivotal "
        "Phase 3 trial endpoints with hazard ratios (CodeBreaK 200 and KRYSTAL-12), their FDA approval dates and NDA "
        "numbers, their binding affinities (IC50/Kd) against KRAS G12C, their safety signals in FAERS, and how "
        "druggable KRAS is.",
        AGENTIC, "default_backbone",
    ),
    ("Is sotorasib better than adagrasib for KRAS G12C NSCLC?", DEBATE, "contested_claim"),
    ("What is the recommended dose of osimertinib for a 72-year-old patient with renal impairment?", AGENTIC, "default_backbone"),
    ("What are the adverse events of pembrolizumab?", AGENTIC, "default_backbone"),
    ("Interpret the variant BRAF V600E in melanoma.", AGENTIC, "default_backbone"),
    ("Evaluate PCSK9 as a drug target for cardiovascular disease.", SUPERVISOR, "open_multi_domain"),
    ("Propose novel drug repurposing candidates for glioblastoma.", DEBATE, "hypothesis_generation"),
    ("Simulate the MAPK cascade model BIOMD0000000010 for 100 seconds.", AGENTIC, "computational"),
    ("Which cell clusters express CD19 in my single-cell dataset?", AGENTIC, "computational"),
    ("Explore the connections between TP53 and ferroptosis and drugs that target that link.", SWARM, "exploratory_chain"),
]


async def _ctx(query: str) -> Any:
    return await qp.build_query_context(
        query=query, clean_query=query, registry=None, enable_web_search=False, attachments=[], attachment_errors=[],
        user_id="u", thread_id="t", model_name="m", api_key=None,
    )


@pytest.mark.parametrize("question, framework, rule", ROUTES, ids=[r[0][:40] for r in ROUTES])
def test_routing_matrix(question, framework, rule):
    decision = router.rule_decision(question, asyncio.run(_ctx(question)))
    assert (decision.framework, decision.rule) == (framework, rule), decision.features
    assert decision.reason and 0 < decision.confidence <= 1
    assert abs(sum(decision.probabilities.values()) - 1) < 1e-3
    art = decision.to_artifact()
    assert art["requested"] == agent_config.AUTO and art["chosen"] == framework and art["bucket"]
    json.dumps(art)


def _eval_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import eval_router  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return eval_router


@pytest.mark.parametrize("name, strict, acceptable", [("benchmark", 0.9, 0.95), ("holdout", 0.8, 0.9)])
def test_deterministic_accuracy_on_the_routing_benchmarks(monkeypatch, name, strict, acceptable):
    """Regression guard: the offline router keeps its measured accuracy (scripts/eval_router.py prints the misses)."""
    monkeypatch.setenv("PHRONESIS_ROUTER_LLM", "off")
    ev = _eval_module()
    bench = ev.load_benchmark(name)
    report = ev.score(bench, asyncio.run(ev.route_all(bench["cases"], False, "m")))
    assert report["strict_accuracy"] >= strict, report["misses"]
    assert report["acceptable_accuracy"] >= acceptable, report["misses"]


def test_safety_critical_question_is_flagged_for_escalation():
    q = "What is the recommended dose of osimertinib for a 72-year-old patient with renal impairment?"
    assert router.rule_decision(q, asyncio.run(_ctx(q))).features["high_stakes"] == 1.0


def test_non_research_message_routes_without_a_model_call():
    def factory(**_kw: Any) -> Any:
        raise AssertionError("no model call for a non-research message")

    ctx = SimpleNamespace(scope="conversational", intents=set(), entities=[], search_plan=None)
    decision = asyncio.run(router.choose_framework("thanks!", ctx, llm_factory=factory))
    assert (decision.framework, decision.rule, decision.tie_break) == (AGENTIC, "not_research", "none")


# -- semantic features -------------------------------------------------------------------------------------------------

class _PhaseLLM:
    """Replies by phase marker: [phase:router] features (a list cycles per call), [phase:router_adjudicate]."""

    def __init__(self, features: Any = None, adjudicate: str = "{}") -> None:
        self.features = features if isinstance(features, list) else [features]
        self.adjudicate = adjudicate
        self.calls: List[str] = []

    async def ainvoke(self, messages: Any) -> AIMessage:
        system = str(messages[0].content)
        if system.startswith("[phase:router_adjudicate]"):
            self.calls.append("adjudicate")
            return AIMessage(content=self.adjudicate)
        self.calls.append("features")
        reply = self.features[(len([c for c in self.calls if c == "features"]) - 1) % len(self.features)]
        return AIMessage(content=json.dumps(reply) if isinstance(reply, dict) else str(reply))


def _route(question: str, llm: _PhaseLLM) -> Any:
    ctx = asyncio.run(_ctx(question))
    return asyncio.run(router.choose_framework(question, ctx, llm_factory=lambda **_k: llm, model_name="m"))


CONTESTED_FEATURES = {
    "question_kind": "therapy_comparison", "sub_questions": 1, "decomposable": False, "sequential": False,
    "domains": ["clinical"], "needs_computation": False, "hypothesis_generation": False, "contested": True,
    "high_stakes": True, "asks_recommendation": True, "comparison": True, "exploratory": False, "open_ended": False,
}


def test_semantic_features_catch_what_the_keywords_miss():
    q = "Is immunotherapy preferable to chemotherapy for elderly patients with MSI-high colorectal cancer?"
    assert router.rule_decision(q, asyncio.run(_ctx(q))).framework == AGENTIC  # "preferable to" is not a keyword
    decision = _route(q, _PhaseLLM(CONTESTED_FEATURES))
    assert decision.framework == DEBATE and decision.rule == "contested_claim"
    assert decision.semantic == {"used": True, "samples": 1, "valid_samples": 1, "question_kind": "therapy_comparison"}
    assert decision.bucket.startswith("therapy_comparison|")


def test_unusable_semantic_replies_fall_back_to_the_deterministic_signals():
    q = "What are the adverse events of pembrolizumab?"
    decision = _route(q, _PhaseLLM("not json"))
    assert decision.framework == AGENTIC and decision.semantic["used"] is False


def test_self_consistency_averages_samples(monkeypatch):
    monkeypatch.setenv("PHRONESIS_ROUTER_SAMPLES", "3")
    no = {**CONTESTED_FEATURES, "contested": False}
    llm = _PhaseLLM([CONTESTED_FEATURES, CONTESTED_FEATURES, no])
    decision = _route("Is immunotherapy preferable to chemotherapy for elderly patients?", llm)
    assert llm.calls.count("features") == 3 and decision.semantic["valid_samples"] == 3
    # 2 of 3 samples say contested and no keyword does: a model-only flag, discounted
    assert decision.features["contested"] == pytest.approx(router.SEMANTIC_ONLY_WEIGHT * 2 / 3, abs=0.01)


def test_the_model_can_raise_but_never_clear_safety_flags():
    calm = {**CONTESTED_FEATURES, "high_stakes": False, "contested": False, "comparison": False, "asks_recommendation": False}
    decision = _route("What dose of vancomycin is needed in dialysis patients?", _PhaseLLM(calm))
    assert decision.features["high_stakes"] == 1.0


def test_safety_critical_exploration_is_never_routed_to_the_swarm():
    s = router.RoutingSignals()
    features = {f: 0.0 for f in router.FEATURES}
    features.update(exploratory=1.0, high_stakes=0.0)
    assert router.decide(s, features).framework == SWARM
    features["high_stakes"] = 1.0
    features["exploratory"] = 1.0
    # Make the swarm the top score despite the stakes penalty, so only the guardrail can stop it.
    decision = router.decide(s, features, learned={SWARM: {"delta": 3.0, "runs": 9, "quality": 1.0}})
    assert decision.framework != SWARM and decision.guardrail


def test_close_routes_are_adjudicated_by_the_model(monkeypatch):
    real = router.decide

    def close(*args, **kwargs):
        d = real(*args, **kwargs)
        d.candidates = [AGENTIC, SUPERVISOR]
        return d

    monkeypatch.setattr(router, "decide", close)
    llm = _PhaseLLM("not json", adjudicate='{"route": "supervisor", "reason": "Open-ended across several domains."}')
    decision = _route("What are the adverse events of pembrolizumab?", llm)
    assert (decision.framework, decision.rule, decision.tie_break) == (SUPERVISOR, "llm_tie_break", "llm")
    assert decision.reason == "Open-ended across several domains."
    bad = _route("What are the adverse events of pembrolizumab?", _PhaseLLM("not json", adjudicate='{"route": "swarm"}'))
    assert (bad.framework, bad.tie_break) == (AGENTIC, "llm_failed")


# -- outcome learning --------------------------------------------------------------------------------------------------

def test_outcome_quality_weighs_grounding_confidence_completion_and_core_phase():
    # AGT-004: self-scored signals (verification, confidence) are excluded; operational quality only
    assert router_store.outcome_quality(1.0, 1.0, "completed", "ok") == 1.0
    assert router_store.outcome_quality(0.0, 0.0, "failed", "error") == 0.0
    assert router_store.outcome_quality(0.8, None, "partial", "fallback") == 0.5


def test_history_nudges_scores_only_with_enough_runs(tmp_path):
    store = router_store.RouterStore(str(tmp_path))
    bucket = "treatment|contested"
    for _ in range(2):
        store.record(bucket, DEBATE, True, 0.95, 0.3, {})
    assert router_store.adjustments(store.stats(bucket)) == {}  # fewer than MIN_RUNS
    for _ in range(4):
        store.record(bucket, DEBATE, True, 0.95, 0.3, {})
        store.record(bucket, AGENTIC, False, 0.40, 0.1, {})
    adj = router_store.adjustments(store.stats(bucket))
    assert adj[DEBATE]["delta"] > 0 > adj[AGENTIC]["delta"]
    assert all(abs(a["delta"]) <= router_store.MAX_ADJUSTMENT for a in adj.values())
    s = router.RoutingSignals()
    plain = {f: 0.0 for f in router.FEATURES}
    assert router.decide(s, plain).framework == AGENTIC
    nudged = router.decide(s, plain, learned=adj)
    assert nudged.probabilities[DEBATE] > router.decide(s, plain).probabilities[DEBATE]
    assert ("history", adj[DEBATE]["delta"]) not in nudged.factors or nudged.framework == DEBATE


def test_store_keeps_features_not_question_text(tmp_path):
    store = router_store.RouterStore(str(tmp_path))
    store.record("general|plain", AGENTIC, True, 0.7, 0.2, {"open_ended": 0.0})
    store.record("general|plain", "authoritative-react-compiler", False, 0.7, 0.2, {})  # classic is not learned
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT bucket, framework, features_json FROM outcomes").fetchall()
    assert rows == [("general|plain", AGENTIC, '{"open_ended": 0.0}')]


# -- escalation --------------------------------------------------------------------------------------------------------

def _state(routing: Dict[str, Any], conflicts: list, subs: list = (), core: Any = None) -> Any:
    return SimpleNamespace(
        bag={"routing": {"requested": agent_config.AUTO, **routing}},
        artifacts={"evidence_conflicts": {"conflicts": conflicts}},
        sub_questions=[SimpleNamespace(status=s) for s in subs],
        core_phase_status=lambda: core,
    )


def test_escalation_triggers():
    conflicts = [
        {"label": "contradicts", "classifier": "nli"},
        {"label": "contradicts", "classifier": "heuristic"},  # cue words alone never escalate
        {"label": "neutral", "classifier": "llm"},
    ]
    assert router.escalation_triggers(_state({"confidence": 0.9}, conflicts)) == [
        "1 contradicting passage found by the counter-evidence search"
    ]
    assert router.escalation_triggers(_state({"features": {"high_stakes": 1.0}, "confidence": 0.9}, [])) == [
        "a safety-critical question (patient-specific, dosing or interactions)"
    ]
    assert router.escalation_triggers(_state({"confidence": 0.4}, [])) == ["an uncertain route (top choice 40%)"]
    assert router.escalation_triggers(_state({"confidence": 0.9}, [], subs=["answered", "open", "partial"])) == [
        "2 of 3 sub-questions still lack evidence"
    ]
    fallback = {"status": "fallback", "label": "Supervisor"}
    assert router.escalation_triggers(_state({"confidence": 0.9}, [], core=fallback)) == [
        "the Supervisor step fell back to a fixed plan"
    ]
    assert router.escalation_triggers(_state({"confidence": 0.9}, [], subs=["answered", "answered"])) == []


# -- end to end --------------------------------------------------------------------------------------------------------

def _thought(events, step_id):
    return [e["data"] for e in events if e.get("type") == "thought" and (e.get("data") or {}).get("id") == step_id]


def test_auto_end_to_end_routes_explains_records_and_learns(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_ROUTER_LEARNING", "on")
    monkeypatch.setenv("PHRONESIS_ROUTER_DIR", str(tmp_path / "router"))
    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    question = "What diseases is KRAS associated with, and which pathways is it part of?"
    res, events = asyncio.run(collect_events(orch, query=question, thread_id="auto-e2e", framework=agent_config.AUTO))
    assert events[0]["type"] == "run_control"
    assert res.framework_used == AGENTIC
    routing = res.execution_artifacts["framework_routing"]
    assert routing["requested"] == agent_config.AUTO and routing["chosen"] == AGENTIC
    assert routing["confidence"] >= router.UNCERTAIN_BELOW and routing["escalation"] is None
    assert set(routing["probabilities"]) == set(router.ARCHITECTURES)
    route_steps = _thought(events, "route")
    assert route_steps and route_steps[-1]["title"] == "Chose Plan & execute" and route_steps[-1]["text"]
    json.dumps(res.to_dict(), default=str)
    stats = router_store.get_store().stats(routing["bucket"])
    assert stats["bucket"][AGENTIC].runs == 1


def test_user_chosen_runs_are_learned_too(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_ROUTER_LEARNING", "on")
    monkeypatch.setenv("PHRONESIS_ROUTER_DIR", str(tmp_path / "router"))
    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, _ = asyncio.run(collect_events(orch, query="What are the adverse events of pembrolizumab?", thread_id="u1", framework=SUPERVISOR))
    assert "framework_routing" not in res.execution_artifacts
    import sqlite3

    with sqlite3.connect(router_store.get_store().db_path) as conn:
        assert conn.execute("SELECT framework, routed FROM outcomes").fetchall() == [(SUPERVISOR, 0)]


def test_auto_escalates_a_safety_critical_question_to_the_critic_debate(monkeypatch, tmp_path):
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    question = "What is the recommended dose of osimertinib for a 72-year-old patient with renal impairment?"
    res, events = asyncio.run(collect_events(orch, query=question, thread_id="auto-esc", framework=agent_config.AUTO))
    assert res.framework_used == AGENTIC  # the run keeps its architecture; only this draft is debated
    escalation = res.execution_artifacts["framework_routing"]["escalation"]
    assert escalation["status"] == "escalated" and escalation["to"] == DEBATE
    assert any("safety-critical" in t for t in escalation["triggers"])
    assert "debate" in llm.phases()
    assert _thought(events, "escalate")


def test_plan_execute_without_auto_never_runs_the_debate(monkeypatch, tmp_path):
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    question = "What is the recommended dose of osimertinib for a 72-year-old patient with renal impairment?"
    res, _events = asyncio.run(collect_events(orch, query=question, thread_id="pe-only", framework=AGENTIC))
    assert "framework_routing" not in res.execution_artifacts
    assert "debate" not in llm.phases()


def test_api_lists_auto_and_accepts_it(monkeypatch):
    from app.backend import main

    monkeypatch.delenv("PHRONESIS_DEFAULT_FRAMEWORK", raising=False)
    listing = asyncio.run(main.list_frameworks())
    assert listing["default"] == agent_config.AUTO
    assert listing["frameworks"][0]["id"] == agent_config.AUTO and listing["frameworks"][0]["agentic"] is True
    req = main.ChatRequest(message="What is KRAS?", framework=agent_config.AUTO)
    assert main._request_framework(req) == agent_config.AUTO
    assert main._request_framework(main.ChatRequest(message="What is KRAS?")) == agent_config.AUTO
