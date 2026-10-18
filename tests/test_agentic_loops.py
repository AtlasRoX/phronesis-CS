"""Agentic loops (WP2): gap-driven re-retrieval, multi-agent topologies and counter-evidence search."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List

import pytest

from phronesis.agentic import config as agent_config
from phronesis.agentic.counter_evidence import (
    COUNTER_EVIDENCE_PHASE,
    CounterEvidencePhase,
    heuristic_rating,
    negation_queries,
)
from phronesis.agentic.gaps import GAP_PHASE, hedged_sentences, rewrite_query
from phronesis.agentic.runtime import AgentPhase, register_phase, restore_phases, snapshot_phases
from phronesis.agentic.specialists import build_specialists, classify_tool, tool_allowed
from phronesis.agentic.topologies import DEBATE_PHASE, SUPERVISOR_PHASE, SWARM_PHASE
from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.query_context import QueryContext
from phronesis.tools import registry as registry_module
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import _fake_output, build_recording_registry, called

QUERY = "What diseases is KRAS associated with?"

# Trimmed passages from real abstracts.
SOTORASIB = {  # Skoulidis et al., NEJM 2021 (CodeBreaK 100)
    "pmid": "34096690",
    "passage_text": "Sotorasib therapy led to a durable clinical benefit without new safety signals in patients with "
    "previously treated KRAS p.G12C-mutated NSCLC.",
    "citation": "Skoulidis et al., 2021",
}
ADAGRASIB_RESISTANCE = {  # Awad et al., NEJM 2021
    "pmid": "34161704",
    "passage_text": "Clinical trials of the KRAS inhibitors adagrasib and sotorasib have shown promising activity in "
    "cancers harboring KRAS G12C mutations; putative mechanisms of acquired resistance to adagrasib were detected "
    "in 17 patients (45% of the cohort).",
    "citation": "Awad et al., 2021",
}
CETUXIMAB_KRAS = {  # Karapetis et al., NEJM 2008
    "pmid": "18946061",
    "passage_text": "Among patients with mutated K-ras tumors, there was no significant difference between those who "
    "were treated with cetuximab and those who received best supportive care alone with respect to overall survival.",
    "citation": "Karapetis et al., 2008",
}


@pytest.fixture
def phases():
    """Only the phases a test registers run (other packages' phases are switched off)."""
    state = snapshot_phases()
    from phronesis.extensions import load_extensions

    load_extensions()
    restore_phases({})

    def use(*items: AgentPhase) -> None:
        for p in items:
            register_phase(p)

    try:
        yield use
    finally:
        restore_phases(state)


def literature_tool(routes: Dict[str, Dict[str, Any]]):
    """A lithoscribe fake that returns a routed passage when a word of the query matches, else the default."""

    def fake(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        q = kwargs["query"].lower()
        for word, passage in routes.items():
            if word in q:
                return {"ranked_passages": [dict(passage)]}
        return _fake_output("lithoscribe_literature_synthesis", kwargs)

    return {"lithoscribe_literature_synthesis": (registry_module.LithoscribeSynthesisInput, fake)}


def disable_agent_model(monkeypatch, llm) -> None:
    """The agent (planning/critique) model is unavailable while the answer model still works."""
    monkeypatch.setenv("PHRONESIS_AGENT_MODEL", "agent-offline")

    def get_llm(*_a, **kw):
        if kw.get("model_name") == "agent-offline":
            raise RuntimeError("agent model offline")
        return llm

    monkeypatch.setattr(orch_module, "get_llm", get_llm)


def run(orch, framework=agent_config.AGENTIC, query=QUERY, **kw):
    return asyncio.run(collect_events(orch, query=query, thread_id=f"t-{framework}", framework=framework, **kw))


def lit_queries(calls) -> List[str]:
    return [kw["query"] for kw in called(calls, "lithoscribe_literature_synthesis")]


# F2 gaps ---------------------------------------------------------------------------------------------------------
def test_gap_loop_retrieves_resynthesizes_and_stops(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE)
    reply = json.dumps({"sub_questions": [{"id": "q0", "status": "partial"}], "queries": ["KRAS G12C sotorasib clinical response"]})
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"gaps": reply}, extra_tools=literature_tool({"sotorasib": SOTORASIB})
    )
    res, _ = run(orch)
    queries = lit_queries(calls)
    assert len(queries) == 2 and queries[1] == "KRAS G12C sotorasib clinical response"
    synth = llm.prompts("synthesis")
    assert len(synth) == 2  # first draft + one rewrite with the gap evidence
    assert "34096690" not in synth[0] and "34096690" in synth[1]
    assert "Gap re-retrieval" in synth[1]
    # The scripted rewrite returns the same text as the first draft: a rewrite that changes nothing ends the loop
    # (no second gap round over an unchanged draft).
    assert len(llm.prompts("gaps")) == 1
    assert llm.calls[[c[0] for c in llm.calls].index("gaps")][1].startswith("[phase:gaps]")
    run_art = res.execution_artifacts["agent_run"]
    assert run_art["draft_rounds"] == 1
    assert [p["phase"] for p in run_art["phases"]].count("gaps") == 1


def test_gap_loop_without_new_evidence_does_not_rewrite(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE)
    reply = json.dumps({"sub_questions": [{"id": "q0", "status": "missing"}], "queries": ["KRAS pancreatic signaling"]})
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"gaps": reply})
    res, _ = run(orch)
    assert len(lit_queries(calls)) == 2  # the gap search ran, but returned the passage already in the evidence
    assert len(llm.prompts("synthesis")) == 1
    assert res.execution_artifacts["agent_run"]["draft_rounds"] == 0


def test_gap_loop_falls_back_to_hedged_sentences_without_agent_model(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE)
    draft = (
        "## Answer\n\nKRAS is associated with pancreatic carcinoma in the retrieved target-disease evidence. "
        "Evidence on KRAS mutation and response to cetuximab in colorectal cancer is unclear in the retrieved sources."
    )
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, extra_tools=literature_tool({"cetuximab": CETUXIMAB_KRAS}), default_synthesis=draft
    )
    disable_agent_model(monkeypatch, llm)
    run(orch)
    queries = lit_queries(calls)
    assert len(queries) == 2 and "cetuximab" in queries[1] and queries[1].startswith("KRAS")
    assert "unclear" not in queries[1].lower()
    assert "gaps" not in llm.phases()  # no agent model call
    synth = llm.prompts("synthesis")
    assert len(synth) == 2 and "18946061" in synth[1]


def test_hedge_detection_and_query_rewrite():
    draft = "KRAS drives pancreatic cancer. Whether KRAS G12D predicts survival could not be determined from the data."
    hedged = hedged_sentences(draft)
    assert hedged == ["Whether KRAS G12D predicts survival could not be determined from the data."]
    assert rewrite_query(hedged[0], ["KRAS G12D"]) == "KRAS G12D predicts survival"


# F4 topologies ---------------------------------------------------------------------------------------------------
def sequence(*replies: Dict[str, Any]):
    state = {"n": 0}

    def responder(_system: str, _user: str) -> str:
        i = min(state["n"], len(replies) - 1)
        state["n"] += 1
        return json.dumps(replies[i])

    return responder


def test_supervisor_delegates_to_specialist_that_calls_its_tool(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)
    responses = {
        "supervisor": sequence(
            {"is_done": False, "is_looping": False, "is_progressing": True, "next_specialist": "target",
             "instruction": "Get the KEGG pathways of BRAF", "reason": "pathway context is missing"},
            {"is_done": True, "is_looping": False, "is_progressing": True, "next_specialist": "", "instruction": "",
             "reason": "enough evidence"},
        ),
        "specialist": sequence(
            {"thought": "KEGG lookup", "action": "call", "tool": "synapta_kegg_pathways", "args": {"target_symbol": "BRAF"}},
            {"thought": "done", "action": "finish", "summary": "BRAF maps to the MAPK signaling pathway (hsa04010)."},
        ),
    }
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses=responses)
    res, events = run(orch, framework=agent_config.SUPERVISOR)
    assert {"target_symbol": "BRAF"} in [{"target_symbol": kw["target_symbol"]} for kw in called(calls, "synapta_kegg_pathways")]
    spec_system = [c[1] for c in llm.calls if c[0] == "specialist"][0]
    assert spec_system.startswith("[phase:specialist]") and "synapta_kegg_pathways" in spec_system
    assert "lithoscribe_literature_synthesis" not in spec_system  # only the specialist's own tools
    dialogue = res.execution_artifacts["agent_dialogue"]
    assert dialogue["topology"] == "supervisor"
    assert any(t["agent"] == "Target specialist" and t["tool"] == "synapta_kegg_pathways" for t in dialogue["turns"])
    assert any(t["agent"] == "Supervisor" and t["action"] == "delegate" for t in dialogue["turns"])
    agents = {e["data"].get("agent") for e in events if e.get("type") == "thought"}
    assert {"Supervisor", "Target specialist"} <= agents
    assert "<agent_evidence" in llm.prompts("synthesis")[0]


def test_supervisor_replans_after_stalls_and_stops(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)
    stalled = {"is_done": False, "is_looping": True, "is_progressing": False, "next_specialist": "literature",
               "instruction": "search again", "reason": "?"}
    responses = {"supervisor": sequence(stalled), "specialist": sequence({"action": "finish", "summary": "nothing new"})}
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses=responses)
    res, _ = run(orch, framework=agent_config.SUPERVISOR)
    turns = res.execution_artifacts["agent_dialogue"]["turns"]
    assert [t["action"] for t in turns].count("replan") >= 1
    assert turns[-1]["action"] == "stop" or len([t for t in turns if t["action"] == "delegate"]) <= 6
    assert len(llm.prompts("supervisor")) <= 12


def test_swarm_handoff_and_ping_pong_prevention(monkeypatch, tmp_path, phases):
    phases(SWARM_PHASE)
    seen: List[str] = []

    def swarm(system: str, _user: str) -> str:
        label = re.search(r"You are the (\w+) specialist", system).group(1).lower()
        seen.append(label)
        other = "target" if label == "literature" else "literature"
        if len(seen) == 1:
            return json.dumps({"action": "handoff", "target": other, "note": "look this up"})
        if len(seen) == 2:
            if label == "target":
                return json.dumps({"action": "call", "target": "synapta_kegg_pathways", "args": {"target_symbol": "BRAF"}})
            return json.dumps({"action": "call", "target": "lithoscribe_literature_synthesis", "args": {"query": "BRAF V600E melanoma"}})
        if len(seen) == 3:
            return json.dumps({"action": "handoff", "target": seen[0], "note": "back to you"})
        return json.dumps({"action": "finish", "note": "done"})

    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"swarm": swarm})
    res, _ = run(orch, framework=agent_config.SWARM)
    assert len(seen) == 4
    assert seen[1] != seen[0] and seen[2] == seen[1] and seen[3] == seen[1]  # the hand-back was refused
    turns = res.execution_artifacts["agent_dialogue"]["turns"]
    actions = [t["action"] for t in turns]
    assert actions.count("handoff") == 1 and actions.count("handoff refused") == 1
    assert any("no ping-pong" in t["summary"] for t in turns if t["action"] == "handoff refused")
    if seen[1] == "target":
        assert any(kw["target_symbol"] == "BRAF" for kw in called(calls, "synapta_kegg_pathways"))
    else:
        assert "BRAF V600E melanoma" in lit_queries(calls)


def critique(claim: str, key: str, severity: str = "medium") -> Dict[str, str]:
    return {"claim": claim, "issue": "state that this is target-disease association evidence, not a clinical outcome",
            "evidence_key": key, "severity": severity}


def test_debate_feedback_triggers_resynthesis(monkeypatch, tmp_path, phases):
    phases(DEBATE_PHASE)
    claim = "KRAS is associated with pancreatic carcinoma"

    def debate(system: str, user: str) -> str:
        if "methodologist" in system:
            return json.dumps({"critiques": [critique(claim, "synapta"), {"claim": "x", "issue": "no key"}], "verdict": "revise"})
        if "clinical skeptic" in system:
            return json.dumps({"critiques": [critique(claim, "PMID: 123456", "high")], "verdict": "revise"})
        return json.dumps({"critiques": [], "verdict": "accept"})

    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"debate": debate})
    res, _ = run(orch, framework=agent_config.DEBATE)
    prompts = [c for c in llm.calls if c[0] == "debate"]
    assert len(prompts) == 6  # three critics, two rounds
    assert all(c[1].startswith("[phase:debate]") for c in prompts)
    assert sum("Using the opinion of other agents as additional advice" in c[2] for c in prompts) == 3
    synth = llm.prompts("synthesis")
    assert len(synth) == 2
    assert "raised by clinical skeptic, methodologist" in synth[1] or "raised by methodologist, clinical skeptic" in synth[1]
    assert "PMID:123456" in synth[1] and "no key" not in synth[1]
    dialogue = res.execution_artifacts["agent_dialogue"]
    assert dialogue["topology"] == "debate"
    assert {"Critic: methodologist", "Critic: clinical skeptic", "Critic: mechanistic biologist"} <= {t["agent"] for t in dialogue["turns"]}


def test_debate_majority_accept_keeps_draft(monkeypatch, tmp_path, phases):
    phases(DEBATE_PHASE)

    def debate(system: str, _user: str) -> str:
        if "methodologist" in system:
            return json.dumps({"critiques": [critique("KRAS is associated with pancreatic carcinoma", "synapta")], "verdict": "revise"})
        return json.dumps({"critiques": [], "verdict": "accept"})

    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"debate": debate})
    res, _ = run(orch, framework=agent_config.DEBATE)
    assert len(llm.prompts("synthesis")) == 1
    assert res.execution_artifacts["agent_dialogue"]["turns"][-1]["action"] == "accept"


def test_specialists_follow_registry_and_policy():
    reg, _ = build_recording_registry(
        extra_tools={
            "pubchem_compound_properties": (registry_module.WebSurferSearchInput, lambda kw: {"results": []}),
            "gtex_median_expression": (registry_module.WebSurferSearchInput, lambda kw: {"results": []}),
        }
    )

    class S:
        registry = reg

    s = S()
    s.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY)
    groups = build_specialists(s)
    assert set(groups) == {"literature", "target", "simulation", "omics", "chemistry"}
    assert "pubchem_compound_properties" in groups["chemistry"].tool_names()  # new tools join automatically
    assert "gtex_median_expression" in groups["omics"].tool_names()
    assert groups["simulation"].tool_names() == [
        "vivarix_simulate_time_course", "vivarix_compute_steady_state", "vivarix_parameter_scan"
    ]

    s.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, suppress_simulation=True)
    assert "simulation" not in build_specialists(s)

    s.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, source_allowlist={"lithoscribe"}, enable_web_search=True)
    papers = build_specialists(s)
    assert set(papers) == {"literature"} and papers["literature"].tool_names() == ["lithoscribe_literature_synthesis"]

    s.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, enable_web_search=False)
    assert "websurfer_search" not in build_specialists(s)["literature"].tool_names()
    assert classify_tool(reg.get_entry("lumicell_analyze_single_cell")) == "omics"
    assert not tool_allowed(QueryContext(raw_query=QUERY, clean_query=QUERY, suppress_simulation=True),
                            reg.get_entry("vivarix_parameter_scan"))[0]


def test_supervisor_team_has_no_simulation_specialist_when_suppressed(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)
    responses = {"supervisor": json.dumps({"is_done": True, "reason": "nothing to add"})}
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses=responses)
    res, _ = run(orch, framework=agent_config.SUPERVISOR, query=f"{QUERY} Do not run any simulation.")
    prompt = llm.prompts("supervisor")[0]
    assert "Target specialist" in prompt and "Simulation specialist" not in prompt and "vivarix" not in prompt
    assert not any(name.startswith("vivarix") for name, _ in calls)


# F7 counter-evidence ---------------------------------------------------------------------------------------------
def test_counter_evidence_heuristic_flags_possible_conflict(monkeypatch, tmp_path, phases):
    phases(COUNTER_EVIDENCE_PHASE)
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, extra_tools=literature_tool({"resistance": ADAGRASIB_RESISTANCE})
    )
    disable_agent_model(monkeypatch, llm)
    res, _ = run(orch)
    assert lit_queries(calls)[1:] == ["KRAS resistance", "KRAS no significant effect", "KRAS failed trial"]
    art = res.execution_artifacts["evidence_conflicts"]
    assert art["classifier"] == "heuristic" and art["claims_checked"] >= 1
    row = next(r for r in art["conflicts"] if r["pmid"] == "34161704")
    assert row["claim"] == "KRAS is associated with pancreatic carcinoma"
    assert row["classifier"] == "heuristic" and row["likert"] == 7 and row["label"] == "neutral"  # possible, not certain
    synth = llm.prompts("synthesis")[0]
    assert "<evidence_conflicts" in synth and 'flag="possible"' in synth and "34161704" in synth
    assert "Discuss these conflicts explicitly" in synth and "not as established contradictions" in synth
    assert "<untrusted_data" in synth.split("<evidence_conflicts", 1)[1]


def test_counter_evidence_uses_injected_nli_judge(monkeypatch, tmp_path, phases):
    judged: List[Any] = []

    def judge(claim: str, passages: List[Dict[str, str]]) -> Dict[str, str]:
        judged.append((claim, passages))
        return {"status": "contradicted", "reason": "acquired resistance limits the benefit"}

    async def judge_async(claim: str, passages: List[Dict[str, str]]):
        judged.append(("async", passages[0]["source_key"]))
        return None  # e.g. the model-call cap is used up: fall back to the sync judge

    class InstallJudge(AgentPhase):
        key = "nli_stub"
        stage = "augment"
        order = 5

        async def run(self, state):
            state.bag["nli_judge_fn"] = judge
            state.bag["nli_judge_async"] = judge_async

    phases(InstallJudge(), COUNTER_EVIDENCE_PHASE)
    orch, _calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, extra_tools=literature_tool({"resistance": ADAGRASIB_RESISTANCE})
    )
    res, _ = run(orch)
    assert judged[0] == ("async", "PMID:34161704")  # the async judge is asked first
    assert judged[1][1][0]["source_key"] == "PMID:34161704"
    art = res.execution_artifacts["evidence_conflicts"]
    assert art["classifier"] == "nli"
    assert art["conflicts"][0]["label"] == "contradicts" and art["conflicts"][0]["likert"] == 9
    assert "counter_evidence" not in llm.phases()
    assert 'label="contradicts"' in llm.prompts("synthesis")[0]


def test_counter_evidence_model_likert_rating(monkeypatch, tmp_path, phases):
    phases(COUNTER_EVIDENCE_PHASE)
    reply = json.dumps({"ratings": [{"pair": 1, "likert": 6, "reason": "different setting"}]})
    orch, _calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"counter_evidence": reply},
        extra_tools=literature_tool({"resistance": ADAGRASIB_RESISTANCE}),
    )
    res, _ = run(orch)
    art = res.execution_artifacts["evidence_conflicts"]
    assert art["classifier"] == "llm" and art["conflicts"][0]["likert"] == 6
    assert llm.calls[llm.phases().index("counter_evidence")][1].startswith("[phase:counter_evidence]")
    assert "<evidence_conflicts" not in llm.prompts("synthesis")[0]  # nothing at or above the flag threshold


def test_counter_evidence_switches_off(monkeypatch, tmp_path, phases):
    phases(COUNTER_EVIDENCE_PHASE)
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, _ = run(orch, agent_options={"enable_counter_evidence": False})
    assert len(lit_queries(calls)) == 1 and "evidence_conflicts" not in res.execution_artifacts

    class St:
        options: Dict[str, Any] = {}
        registry = build_recording_registry()[0]

    st = St()
    st.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, source_allowlist={"clinical_trials"})
    assert not CounterEvidencePhase().applies(st)
    st.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, scope="conversational")
    assert not CounterEvidencePhase().applies(st)
    st.ctx = QueryContext(raw_query=QUERY, clean_query=QUERY, source_allowlist={"lithoscribe"})
    assert CounterEvidencePhase().applies(st)


def test_negation_queries_and_heuristic():
    ents = {"genes": ["KRAS"], "drugs": ["sotorasib"], "diseases": []}
    assert negation_queries(ents) == ["KRAS resistance", "sotorasib adverse events", "KRAS no significant effect"]
    assert negation_queries({"genes": [], "drugs": [], "diseases": ["asthma"]}) == []
    likert, _ = heuristic_rating("KRAS mutation predicts cetuximab benefit", "KRAS mutant tumours showed no significant benefit from cetuximab.")
    # AGT-008: cue words alone stop just below the contradiction flag (FLAG_AT = 8): a possible conflict, not a verdict.
    assert likert == 7
    assert heuristic_rating("KRAS drives pancreatic cancer", "EGFR signalling in lung tissue.")[0] == 5


# Classic pipeline ------------------------------------------------------------------------------------------------
def test_classic_framework_runs_none_of_the_loops(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE, COUNTER_EVIDENCE_PHASE, SUPERVISOR_PHASE, SWARM_PHASE, DEBATE_PHASE)
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, extra_tools=literature_tool({"resistance": ADAGRASIB_RESISTANCE})
    )
    res, _ = asyncio.run(collect_events(orch, query=QUERY, thread_id="classic"))
    assert res.framework_used == agent_config.CLASSIC
    assert len(lit_queries(calls)) == 1
    assert set(llm.phases()) <= {"synthesis", "correction"}
    for key in ("agent_run", "agent_dialogue", "evidence_conflicts"):
        assert key not in res.execution_artifacts
