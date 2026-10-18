"""Query-run repair (2026-10-16 plan, WS-0.4 / WS-4 / WS-5 backend): orchestration modes run, or say they did not.

- supervisor: a deterministic fallback dispatches the ranked plan when the supervisor model gives no usable reply
- swarm: starts from the first step the question names, retries one bad reply, then follows the question's steps
  and makes sure every asked step (e.g. openFDA safety of the drugs found) ran its key tool
- task status: "partial" when the framework's core phase (debate, supervisor, swarm, plan_execute) did not run
- gaps: the answer's limitations section and analyses nobody asked for are never gaps
- prompts: the answer names data sources, not internal components
- runtime: phases are bounded by the time budget; a draft about to be rewritten is not NLI-scored
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agentic import config as agent_config
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.gaps import GAP_PHASE, hedged_sentences
from phronesis.agentic.hypothesis import HypothesisPhase, _disease_key, _same_disease
from phronesis.agentic.llm import AgentLLMUnavailable
from phronesis.agentic.runtime import AgentPhase, AgenticRuntime, PhaseOutcome, register_phase, restore_phases, snapshot_phases
from phronesis.agentic.specialists import not_requested_modules, team_specialists
from phronesis.agentic.state import core_phase_note
from phronesis.agentic.topologies import (
    DEBATE_PHASE,
    SUPERVISOR_PHASE,
    SWARM_PHASE,
    SwarmPhase,
    question_legs,
)
from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator
from phronesis.agents.discovery import BioDiscoveryEngine
from phronesis.agents.prompts import SYNTHESIS_SYSTEM_PROMPT
from phronesis.agents.query_context import QueryContext
from phronesis.drugintel.schemas import ChemblProfileInput, OpenFdaSafetyInput
from phronesis.tools import registry as registry_module
from phronesis.tools.registry import ToolResult
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import build_recording_registry, called
from tests.test_agentic_planning import make_state

QUERY = "What diseases is KRAS associated with?"


@pytest.fixture
def phases():
    """Only the phases a test registers run."""
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


def run(orch, framework, query=QUERY, **kw):
    return asyncio.run(collect_events(orch, query=query, thread_id=f"t-{framework}", framework=framework, **kw))


def not_json(_system: str, _user: str) -> str:
    return "I cannot answer in JSON right now."


# 4.1 Supervisor ------------------------------------------------------------------------------------------------
def test_supervisor_falls_back_to_ranked_plan_when_model_reply_is_unusable(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"supervisor": not_json, "specialist": not_json}
    )
    res, events = run(orch, agent_config.SUPERVISOR)
    # Two unusable replies (each with the JSON helper's own repair retry), then the fixed plan.
    assert len(llm.prompts("supervisor")) == 4
    turns = res.execution_artifacts["agent_dialogue"]["turns"]
    assert any(t["action"] == "fallback plan" for t in turns)
    delegated = [t["summary"] for t in turns if t["action"] == "delegate"]
    assert 1 <= len(delegated) <= 3 and all("fixed plan" in d for d in delegated)
    fixed = [t for t in turns if t["action"] == "call (fixed plan)"]
    assert fixed, "a specialist ran its fixed tool calls"
    assert any(kw.get("target_symbol") == "KRAS" for kw in called(calls, "synapta_kegg_pathways"))
    run_art = res.execution_artifacts["agent_run"]
    assert run_art["core_phase"]["phase"] == "supervisor" and run_art["core_phase"]["status"] == "fallback"
    assert res.task_status["task_status"] == "partial" and "fallback" in res.task_status["reason"]
    # No step keeps spinning once the run is done.
    assert all(th["status"] != "running" for th in res.thoughts)


def test_supervisor_model_failure_skips_specialist_model_calls(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)

    def boom(_system: str, _user: str) -> str:
        raise RuntimeError("APITimeoutError: request timed out")

    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"supervisor": boom})
    res, _ = run(orch, agent_config.SUPERVISOR)
    assert len(llm.prompts("supervisor")) == 1  # a failed call is not retried
    assert "specialist" not in llm.phases()  # the model is failing: specialists go straight to their fixed calls
    assert any(t["action"] == "call (fixed plan)" for t in res.execution_artifacts["agent_dialogue"]["turns"])


def test_supervisor_explicit_finish_is_not_a_fallback(monkeypatch, tmp_path, phases):
    phases(SUPERVISOR_PHASE)
    orch, _calls, _llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"supervisor": json.dumps({"is_done": True, "reason": "enough"})}
    )
    res, _ = run(orch, agent_config.SUPERVISOR)
    assert res.execution_artifacts["agent_run"]["core_phase"]["status"] == "ok"
    assert res.task_status["task_status"] == "completed"


def test_team_has_no_simulation_specialist_unless_simulation_is_asked():
    reg, _ = build_recording_registry()
    st = SimpleNamespace(registry=reg, tool_results={}, ctx=QueryContext(raw_query=QUERY, clean_query=QUERY))
    assert "simulation" not in team_specialists(st)
    st.ctx = QueryContext(raw_query="simulate MAPK", clean_query="simulate MAPK", is_simulation_intent=True)
    assert "simulation" in team_specialists(st)


# 4.2 Swarm -----------------------------------------------------------------------------------------------------
SL_QUESTION = (
    "For MYC in glioblastoma, find synthetic-lethal partners, then ChEMBL compounds against them, then "
    "clinical trials and safety warnings in openFDA."
)


def test_question_legs_follow_the_order_of_mention():
    assert question_legs(SL_QUESTION) == ["target", "chemistry", "clinical"]
    assert question_legs("openFDA label warnings for sotorasib, and its ChEMBL compounds") == ["clinical", "chemistry"]
    assert question_legs(QUERY) == []
    assert question_legs("slow sleep") == []  # "SL" only as its own upper-case word


def _swarm_tools():
    def chembl(kwargs):
        return {"target": {"pref_name": kwargs.get("target_symbol")}, "drugs": [],
                "target_approved_drugs": [{"pref_name": "OMOMYCIN"}], "target_bioactive_compounds": []}

    return {
        "chembl_drug_profile": (ChemblProfileInput, chembl),
        "openfda_drug_safety": (OpenFdaSafetyInput, lambda kw: {"ingredient": kw["ingredient"], "reactions": []}),
        "lithoscribe_clinical_trials_search": (registry_module.WebSurferSearchInput, lambda kw: {"results": []}),
    }


def test_swarm_starts_with_the_first_step_the_question_names():
    reg, _ = build_recording_registry(extra_tools=_swarm_tools())
    st = SimpleNamespace(registry=reg, tool_results={}, query=SL_QUESTION, clean_query=SL_QUESTION,
                         ctx=QueryContext(raw_query=SL_QUESTION, clean_query=SL_QUESTION))
    team = team_specialists(st)
    assert SwarmPhase._start(st, team).name == "target"
    q = "Which ChEMBL compounds hit KRAS, and what do trials show?"
    st.query = st.clean_query = q
    assert SwarmPhase._start(st, team).name == "chemistry"


def test_swarm_retries_one_bad_reply(monkeypatch, tmp_path, phases):
    phases(SWARM_PHASE)
    # The first ask gets two unparseable replies (the JSON helper repairs once); the retried ask finishes.
    replies = iter(["not json", "still not json", json.dumps({"action": "finish", "note": "done"})])
    orch, _calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"swarm": lambda _s, _u: next(replies, json.dumps({"action": "finish"}))}
    )
    res, _ = run(orch, agent_config.SWARM)
    assert len(llm.prompts("swarm")) == 3
    swarm = res.execution_artifacts["agent_run"]["core_phase"]
    assert swarm["status"] == "ok"
    turns = res.execution_artifacts["agent_dialogue"]["turns"]
    assert [t["action"] for t in turns].count("no usable reply") == 1 and turns[-1]["action"] == "finish"


def test_swarm_follows_the_question_steps_after_two_bad_replies(monkeypatch, tmp_path, phases):
    phases(SWARM_PHASE)
    q = "For KRAS, list ChEMBL compounds, then check their safety warnings in openFDA."
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"swarm": not_json}, extra_tools=_swarm_tools()
    )
    res, _ = run(orch, agent_config.SWARM, query=q)
    assert len(llm.prompts("swarm")) == 4  # two unusable asks (each repaired once), then the fixed chain
    assert any(kw.get("target_symbol") == "KRAS" for kw in called(calls, "chembl_drug_profile"))
    # The safety leg ran openFDA for the drug the chemistry leg found.
    assert [kw["ingredient"] for kw in called(calls, "openfda_drug_safety")] == ["OMOMYCIN"]
    core = res.execution_artifacts["agent_run"]["core_phase"]
    assert core["status"] == "fallback"
    assert res.task_status["task_status"] == "partial"


def test_swarm_runs_an_asked_step_the_model_skipped(monkeypatch, tmp_path, phases):
    phases(SWARM_PHASE)
    q = "For KRAS, list ChEMBL compounds, then check their safety warnings in openFDA."

    def swarm(system: str, _user: str) -> str:
        if "You are the Chemistry specialist" in system:
            return json.dumps({"action": "call", "target": "chembl_drug_profile", "args": {"target_symbol": "KRAS"}})
        return json.dumps({"action": "finish", "note": "done"})

    replies: List[str] = []

    def once_then_finish(system: str, user: str) -> str:
        replies.append(system)
        return swarm(system, user) if len(replies) == 1 else json.dumps({"action": "finish", "note": "done"})

    orch, calls, _llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"swarm": once_then_finish}, extra_tools=_swarm_tools()
    )
    res, _ = run(orch, agent_config.SWARM, query=q)
    assert len(called(calls, "chembl_drug_profile")) == 1  # not repeated by the safety net
    assert [kw["ingredient"] for kw in called(calls, "openfda_drug_safety")] == ["OMOMYCIN"]
    assert res.execution_artifacts["agent_run"]["core_phase"]["status"] == "ok"


# 4.3 Task status -----------------------------------------------------------------------------------------------
def test_task_outcome_is_partial_when_the_core_phase_did_not_run():
    ctx = QueryContext(raw_query=QUERY, clean_query=QUERY)
    ok = {"lithoscribe": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={})}
    note = core_phase_note({"phase": "debate", "label": "Debate", "status": "llm_unavailable", "reason": "timeout"})
    assert note == "Debate did not run (model call failed); the answer uses the standard pipeline."
    out = AuthoritativeOrchestrator._task_outcome(ctx, None, ok, None, True, note)
    assert out.task_status == "partial" and out.reason == note
    assert AuthoritativeOrchestrator._task_outcome(ctx, None, ok, None, True).task_status == "completed"
    assert core_phase_note({"phase": "debate", "label": "Debate", "status": "ok"}) == ""
    assert core_phase_note({"phase": "swarm", "label": "Swarm", "status": "not_run"}, research=False) == ""


def test_debate_without_critic_replies_reports_partial(monkeypatch, tmp_path, phases):
    phases(DEBATE_PHASE)

    def boom(_system: str, _user: str) -> str:
        raise RuntimeError("APITimeoutError: request timed out")

    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"debate": boom})
    res, _ = run(orch, agent_config.DEBATE)
    core = res.execution_artifacts["agent_run"]["core_phase"]
    assert core == {"phase": "debate", "label": "Debate", "status": "llm_unavailable", "reason": "the critics' model calls failed"}
    assert res.task_status["task_status"] == "partial"
    assert res.task_status["reason"].startswith("Debate did not run (model call failed)")
    step = next(th for th in res.thoughts if th["id"] == "agent_debate")
    assert step["status"] == "error" and step["title"] == "Debate did not run"


# 4.4 Plan & execute --------------------------------------------------------------------------------------------
def test_planner_failure_is_written_into_the_plan_artifact():
    from phronesis.agentic.plan_execute import PlanExecutePhase

    class FailingLLM:
        temperature = 0.0

        async def ainvoke(self, *_a, **_k):
            raise RuntimeError("APITimeoutError: request timed out")

    reg, _ = build_recording_registry()
    state, steps, _sent = make_state(reg, llm=FailingLLM())
    asyncio.run(PlanExecutePhase().run(state))
    art = state.artifacts["agent_plan"]
    assert art["planner"] == "none" and art["status"] == "planner_failed" and "model call failed" in art["error"]
    assert steps[-1]["id"] == "agent_plan" and steps[-1]["status"] == "error"
    assert steps[-1]["title"] == "The planner did not run"
    assert state.pop_phase_mark("plan_execute")["status"] == "llm_unavailable"


# 4.6 Gaps and limitations --------------------------------------------------------------------------------------
DRAFT = """## Answer
Pembrolizumab improved relapse-free survival in resected stage IIB/IIC melanoma (PMID: 35367007).
Whether the benefit extends to overall survival remains to be established in the retrieved trials.
No kinetic simulation was run, so pathway dynamics are unavailable for this answer.
### Evidence limitations
- Single-cell expression could not be assessed because no dataset was available.
- Long-term overall survival data were not retrieved for nivolumab.
### Suggested next experiments
Whether ctDNA predicts relapse could not be determined from the retrieved sources."""


def test_gaps_ignore_the_limitations_section_and_not_requested_modules():
    ctx = QueryContext(raw_query="adjuvant PD-1 in melanoma", clean_query="adjuvant PD-1 in melanoma")
    skip = not_requested_modules(ctx, {})
    assert skip == ["kinetic simulation", "single-cell analysis"]
    hedged = hedged_sentences(DRAFT, limit=5, skip_modules=skip)
    assert hedged == [
        "Whether the benefit extends to overall survival remains to be established in the retrieved trials.",
        "Whether ctDNA predicts relapse could not be determined from the retrieved sources.",
    ]
    sim = QueryContext(raw_query="simulate MAPK", clean_query="simulate MAPK", is_simulation_intent=True)
    assert not_requested_modules(sim, {}) == ["single-cell analysis"]


def test_gap_model_check_is_told_what_was_not_requested(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE)
    reply = json.dumps({"sub_questions": [], "queries": ["kinetic simulation of KRAS pathway dynamics"]})
    draft = ("## Answer\n\nKRAS is associated with pancreatic carcinoma.\n\n### Evidence limitations\n"
             "No kinetic simulation was run, so pathway dynamics are unavailable for this answer.")
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"gaps": reply}, default_synthesis=draft)
    run(orch, agent_config.AGENTIC)
    prompt = llm.prompts("gaps")[0]
    assert "Not requested (never a gap, no query): kinetic simulation, single-cell analysis" in prompt
    assert "No kinetic simulation was run" not in prompt  # the limitations section is not sent as a claim
    queries = [kw["query"] for kw in called(calls, "lithoscribe_literature_synthesis")]
    assert not any("kinetic" in q for q in queries)


def test_scope_notes_do_not_list_unrequested_simulation(monkeypatch, tmp_path, phases):
    phases()
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    run(orch, agent_config.AGENTIC)
    status = llm.prompts("synthesis")[0].split("<tool_status>", 1)[1].split("</tool_status>", 1)[0]
    assert "Vivarix" not in status and "simulation" not in status.lower()


def test_discovery_has_no_caveats_for_analyses_that_were_not_attempted():
    syn = ToolResult(success=True, tool_name="synapta", data={"associations": [{"disease_name": "PDAC", "overall_association_score": 0.8}]})
    rep = BioDiscoveryEngine.prioritize_hypotheses("q", "KRAS", {"synapta": syn})
    text = rep.candidates[0].rationale + (rep.iterative_update_summary or "")
    assert "Kinetics" not in text and "Single-cell" not in text and "Single-Cell" not in text
    failed = ToolResult(success=False, tool_name="vivarix_simulate_time_course", error="model not found")
    rep2 = BioDiscoveryEngine.prioritize_hypotheses("q", "KRAS", {"synapta": syn, "vivarix": failed})
    assert "Kinetics unsimulated" in rep2.candidates[0].rationale and "Kinetics:" in rep2.iterative_update_summary


# 4.7 Prompts ---------------------------------------------------------------------------------------------------
def test_prompt_no_longer_asks_for_component_narration():
    p = SYNTHESIS_SYSTEM_PROMPT
    assert "Attribute findings to the component" not in p
    assert "Lithoscribe retrieved PMID" not in p and "Vivarix simulation of" not in p
    assert "Refer to system components only by their Phronesis names" not in p
    assert "ClinicalTrials.gov" in p and "never internal components" in p
    assert "No kinetic simulation was run" not in p


# 4.8 Hypotheses ------------------------------------------------------------------------------------------------
def _hyp_state(question: str, passages: List[Dict[str, Any]], assocs: List[Dict[str, Any]], drugs=()):
    ctx = SimpleNamespace(primary_symbol="EGFR", drugs=[SimpleNamespace(mention=d, label=d) for d in drugs], diseases=[])
    results = {
        "synapta": ToolResult(success=True, tool_name="synapta", data={"associations": assocs}),
        "lithoscribe": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": passages}),
    }
    return SimpleNamespace(ctx=ctx, tool_results=results, query=question, clean_query=question)


def test_evidence_hypotheses_are_resistance_aware_and_deduplicated():
    assocs = [{"disease_name": n, "overall_association_score": s} for n, s in
              (("non-small cell lung carcinoma", 0.9), ("lung adenocarcinoma", 0.88), ("cancer", 0.8), ("glioblastoma", 0.6))]
    passages = [{"pmid": "26286987", "passage_text": "The EGFR C797S mutation mediates resistance to osimertinib in NSCLC."}]
    st = _hyp_state("How do T790M and C797S cause resistance to osimertinib in NSCLC?", passages, assocs, drugs=["osimertinib"])
    hyps = HypothesisPhase()._evidence_hypotheses(st)
    statements = [h.statement for h in hyps]
    assert statements[0] == "EGFR C797S confers resistance to osimertinib."
    assert "PMID:26286987" in hyps[0].mechanism and "not established" in hyps[0].mechanism
    assert not any("T790M" in s for s in statements)  # no passage names it: nothing is invented
    disease_rows = [h for h in hyps if h.readout and h.readout.endswith("phenotype")]
    assert [h.readout for h in disease_rows] == ["non-small cell lung carcinoma phenotype", "glioblastoma phenotype"]
    assert not any("disease activity" in s for s in statements)


def test_disease_near_duplicates():
    assert _same_disease(_disease_key("NSCLC"), _disease_key("lung adenocarcinoma"))
    assert not _same_disease(_disease_key("melanoma"), _disease_key("breast cancer"))


# 0.4 Runtime ---------------------------------------------------------------------------------------------------
class _Slow(AgentPhase):
    key = "slow_test"
    stage = "augment"
    order = 1

    async def run(self, state):
        await state.step("agent_slow", "Working slowly", status="running")
        # Outlasts the phase bound below (0.1 s budget + 0.1 s grace); wait_for cancels it, so no real wait.
        await asyncio.sleep(2)


class _Rewrite(AgentPhase):
    key = "rewrite_test"
    stage = "after_draft"
    order = 1

    async def run(self, state):
        return PhaseOutcome(resynthesize=True, feedback=["x"])


class _Precompute(AgentPhase):
    key = "nli_precompute"
    stage = "after_draft"
    order = 90
    ran = 0

    async def run(self, state):
        _Precompute.ran += 1


def test_phase_is_bounded_by_the_time_budget(monkeypatch, phases):
    from phronesis.agentic import runtime

    monkeypatch.setattr(runtime, "PHASE_GRACE_SECONDS", 0.1)
    phases(_Slow())
    reg, _ = build_recording_registry()
    budget = RunBudget(max_seconds=0.1)
    state, steps, _ = make_state(reg, budget=budget)
    state.framework = agent_config.AGENTIC
    asyncio.run(AgenticRuntime(state).run_stage("augment"))
    ran = state.bag["phases_ran"][-1]
    assert ran["phase"] == "slow_test" and ran["status"] == "budget"
    assert steps[-1]["id"] == "agent_slow" and steps[-1]["status"] == "error"  # closed in place, not spinning
    assert state.budget.exhausted()


def test_nli_precompute_skipped_while_a_rewrite_is_pending(phases):
    phases(_Rewrite(), _Precompute())
    reg, _ = build_recording_registry()
    state, _steps, _ = make_state(reg, budget=RunBudget(max_draft_rounds=2))
    _Precompute.ran = 0
    out = asyncio.run(AgenticRuntime(state).run_stage("after_draft"))
    assert out.resynthesize and _Precompute.ran == 0
    assert any(s["phase"] == "nli_precompute" for s in state.bag["skipped_phases"])
    state.draft_round = 1  # last round: the rewritten draft is not checked again, so score this one
    asyncio.run(AgenticRuntime(state).run_stage("after_draft"))
    assert _Precompute.ran == 1


def test_llm_unavailable_maps_to_marked_status(phases):
    class Fails(AgentPhase):
        key = "fails_test"
        stage = "augment"

        async def run(self, state):
            raise AgentLLMUnavailable("timed out")

    phases(Fails())
    reg, _ = build_recording_registry()
    state, _steps, _ = make_state(reg)
    asyncio.run(AgenticRuntime(state).run_stage("augment"))
    row = state.bag["phases_ran"][-1]
    assert row["status"] == "llm_unavailable" and "timed out" in row["reason"]


def test_open_orchestrator_steps_are_closed_at_end_of_run(monkeypatch, tmp_path, phases):
    class Leaves(AgentPhase):
        key = "leaves_open"
        stage = "finalize"

        async def run(self, state):
            await state.emit("untracked_step", "Untracked work", "", status="running", agent="Agent", extra=None)

    phases(Leaves())
    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, events = run(orch, agent_config.AGENTIC)
    step = next(th for th in res.thoughts if th["id"] == "untracked_step")
    assert step["status"] == "done"
    last = [e for e in events if e.get("type") == "thought" and e["data"]["id"] == "untracked_step"][-1]
    assert last["data"]["status"] == "done"


def test_tool_status_note_mentions_suppressed_simulation_without_component_name():
    from phronesis.agents.prompts import build_tool_status_block

    note = "The researcher asked not to run simulations, so none was run; do not discuss simulation and do not list it as a limitation."
    block = build_tool_status_block({}, [note])
    assert "Vivarix" not in block and "asked not to run simulations" in block
    assert re.search(r"do not list it as a limitation", block)


def test_plan_references_tolerate_guessed_collection_and_name_fields():
    from types import SimpleNamespace

    from phronesis.agentic.plan_graph import RefError, resolve_args

    res = SimpleNamespace(data={"target": {"pref_name": "GTPase KRas"},
                                "drugs": [{"chembl_id": "CHEMBL4535757", "pref_name": "SOTORASIB"},
                                          {"chembl_id": "CHEMBL4594350", "pref_name": "ADAGRASIB"}],
                                "unresolved_names": []})
    results = {"n1": res}
    assert resolve_args("$n1.drugs[*].pref_name", results) == ["SOTORASIB", "ADAGRASIB"]
    # The planner guessed "results" and "name": they map to the one list of records and its pref_name.
    assert resolve_args("$n1.results[0].name", results) == "SOTORASIB"
    assert resolve_args("$n1.results.*.id", results) == ["CHEMBL4535757", "CHEMBL4594350"]
    import pytest
    with pytest.raises(RefError):
        resolve_args("$n1.nonsense", results)


def test_tool_cards_list_output_paths():
    from types import SimpleNamespace

    from phronesis.agentic.tool_retrieval import output_fields
    from phronesis.drugintel.schemas import ChemblResolveOutput

    paths = output_fields(SimpleNamespace(output_schema=ChemblResolveOutput))
    assert "drugs[*].pref_name" in paths and "drugs[*].chembl_id" in paths


def test_structure_lookup_falls_back_to_uniprot_rest(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from phronesis.structintel import source

    class FakeClient:
        async def uniprot_accession(self, symbol):
            return {"KRAS": "P01116"}.get(symbol)

    monkeypatch.setattr(source, "get_client", lambda: FakeClient())
    ctx = SimpleNamespace(primary_gene=None, genes=[], raw_query="KRAS binding", primary_symbol="KRAS", query="", clean_query="")
    registry = SimpleNamespace(get_entry=lambda name: None)  # no biocontext_uniprot tool on this server
    acc, how = asyncio.run(source.resolve_accession(ctx, registry))
    assert acc == "P01116" and "UniProt" in how
