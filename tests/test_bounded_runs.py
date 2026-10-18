"""Bounded agentic runs: gap loops that terminate, a budget reserve that lets the answer finish, a truthful terminal
status, pipeline stage timing, liveness heartbeats and prompt cancellation (run-analysis regression case)."""

from __future__ import annotations

import asyncio
import itertools
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from langchain_core.messages import AIMessage

from phronesis.agentic import config as agent_config
from phronesis.agentic.budget import BudgetExhausted, RunBudget
from phronesis.agentic.control import RunCancelled, RunControl
from phronesis.agentic.gaps import GAP_PHASE, near_duplicate, useful_for_gap
from phronesis.agentic.llm import AgentLLM
from phronesis.agentic.runtime import AgentPhase, AgenticRuntime, PhaseOutcome, register_phase, restore_phases, snapshot_phases
from phronesis.agentic.state import AgentState, SubQuestion
from phronesis.agents.authoritative_orchestrator import strip_evidence_markup
from phronesis.agents.orchestration import draft as draft_stage
from phronesis.agents.orchestration.context import RunContext
from phronesis.agents.orchestration.finalize import apply_incomplete_status, turn_fully_supported
from phronesis.agents.query_context import QueryContext
from phronesis.tools import registry as registry_module
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import _fake_output, called

QUERY = "What diseases is KRAS associated with?"


@pytest.fixture
def phases():
    """Only the phases a test registers run."""
    saved = snapshot_phases()
    from phronesis.extensions import load_extensions

    load_extensions()
    restore_phases({})

    def use(*items: AgentPhase) -> None:
        for p in items:
            register_phase(p)

    try:
        yield use
    finally:
        restore_phases(saved)


def run(orch: Any, framework: str = agent_config.AGENTIC, **kw: Any) -> Any:
    return asyncio.run(collect_events(orch, query=QUERY, thread_id=f"t-{framework}", framework=framework, **kw))


def lit_queries(calls: Any) -> List[str]:
    return [kw["query"] for kw in called(calls, "lithoscribe_literature_synthesis")]


def fresh_literature(text: str) -> Dict[str, Any]:
    """A lithoscribe fake: every call returns a new passage about `text` (so it is always new evidence)."""
    counter = itertools.count(1)

    def fake(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        n = next(counter)
        if n == 1:  # the pipeline's own literature search
            return _fake_output("lithoscribe_literature_synthesis", kwargs)
        return {"ranked_passages": [{"pmid": str(40000000 + n), "passage_text": f"{text} (report {n}).", "citation": f"Ref {n}"}]}

    return {"lithoscribe_literature_synthesis": (registry_module.LithoscribeSynthesisInput, fake)}


def changing_synthesis() -> Any:
    """Synthesis replies that differ every call, so no rewrite is ever "unchanged"."""
    counter = itertools.count(1)
    return lambda _s, _u: (
        f"## Answer\n\nKRAS is associated with pancreatic carcinoma (draft {next(counter)}). "
        "Whether sotorasib has FDA approval in colorectal cancer could not be determined from the retrieved sources."
    )


# Gap loop ------------------------------------------------------------------------------------------------------
def test_near_duplicate_and_relevance_rules():
    assert near_duplicate("sotorasib FDA approval KRAS G12C", ["KRAS G12C sotorasib FDA approval"])
    assert near_duplicate("KRAS G12C sotorasib FDA approvals colorectal", ["KRAS G12C sotorasib FDA approval colorectal cancer"])
    assert not near_duplicate("adagrasib cetuximab colorectal response", ["KRAS G12C sotorasib FDA approval"])
    focus = ["KRAS"]
    q = "sotorasib FDA approval colorectal"
    assert useful_for_gap(q, "", "FDA approves sotorasib plus panitumumab for KRAS G12C colorectal cancer", focus)
    # New, but only about the focus gene: it does not fill the gap.
    assert not useful_for_gap(q, "", "KRAS signalling in pancreatic tumour organoids", focus)


def test_repeated_equivalent_gap_queries_terminate(monkeypatch, tmp_path, phases):
    """The observed loop: the gap checker keeps proposing a rephrasing of the same FDA query. The rephrasing is not
    searched again, so the loop ends instead of rewriting the whole answer every round."""
    phases(GAP_PHASE)
    gap_queries = iter([
        "KRAS G12C sotorasib FDA approval colorectal",
        "sotorasib FDA approval colorectal KRAS G12C",
        "FDA approval sotorasib KRAS G12C colorectal",
    ])
    reply = lambda _s, _u: (  # noqa: E731
        '{"sub_questions":[{"id":"q0","status":"partial"}],"queries":["' + next(gap_queries, "sotorasib FDA approval") + '"]}'
    )
    monkeypatch.setenv("PHRONESIS_AGENT_MAX_DRAFT_ROUNDS", "4")
    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"gaps": reply, "synthesis": changing_synthesis()},
        extra_tools=fresh_literature("Sotorasib FDA approval for KRAS G12C colorectal cancer with panitumumab"),
    )
    res, _ = run(orch)
    queries = lit_queries(calls)
    assert len(queries) == 2  # the pipeline's search + one gap search; the rephrasings were skipped
    assert len(llm.prompts("synthesis")) == 2  # first draft + one rewrite, not one per round
    report = res.execution_artifacts["agent_run"]
    assert report["draft_rounds"] == 1


def test_web_only_irrelevant_results_do_not_trigger_a_rewrite(monkeypatch, tmp_path, phases):
    phases(GAP_PHASE)
    reply = '{"sub_questions":[{"id":"q0","status":"missing"}],"queries":["KRAS G12C adagrasib cetuximab colorectal approval"]}'
    web = {
        "websurfer_search": (
            registry_module.WebSurferSearchInput,
            lambda kw: {"results": [{"url": f"https://example.org/{len(kw['query'])}", "title": "KRAS gene overview",
                                     "snippet": "KRAS is a gene on chromosome 12."}]},
        )
    }
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"gaps": reply}, extra_tools=web)
    res, _ = run(orch, enable_web_search=True)
    assert len(llm.prompts("synthesis")) == 1  # the new web result was off-topic: no rewrite
    assert res.execution_artifacts["agent_run"]["draft_rounds"] == 0


# Budget ---------------------------------------------------------------------------------------------------------
def test_optional_calls_stop_at_the_reserve_while_final_calls_still_fit():
    b = RunBudget(max_tokens=10_000, final_reserve_tokens=3_000)
    b.charge_llm("gaps", prompt_chars=4 * 6_000)
    with pytest.raises(BudgetExhausted):
        b.charge_llm("debate", prompt_chars=4 * 1_500)
    assert b.optional_exhausted() and not b.exhausted()
    assert "kept for the final answer" in b.stop_reason()
    with pytest.raises(BudgetExhausted):  # answer rewrites are optional work: they cannot drain the reserve (F3)
        b.charge_llm("synthesis", prompt_chars=4 * 2_000)
    b.charge_llm("first_draft", prompt_chars=4 * 2_000)  # the first draft may use the reserve
    b.charge_llm("revision", prompt_chars=4 * 1_500)  # and so may the verifier's revision
    assert b.tokens == 9_500 and not b.exhausted()


def test_from_env_sets_a_finalization_reserve(monkeypatch):
    monkeypatch.delenv("PHRONESIS_AGENT_FINAL_RESERVE_TOKENS", raising=False)
    b = RunBudget.from_env(None)
    assert b.final_reserve_tokens == 30_000 and b.final_reserve_calls == 2
    monkeypatch.setenv("PHRONESIS_AGENT_FINAL_RESERVE_TOKENS", "5000")
    assert RunBudget.from_env(None).final_reserve_tokens == 5_000


class SlowLLM:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def ainvoke(self, *_a: Any, **_k: Any) -> AIMessage:
        await asyncio.sleep(self.delay)
        return AIMessage(content='{"ok": true}')


def test_concurrent_calls_cannot_overshoot_the_budget(monkeypatch):
    """Parallel critics each reserve prompt + output before they start: only as many as fit run."""
    monkeypatch.setenv("PHRONESIS_AGENT_OUTPUT_RESERVE_TOKENS", "2000")
    budget = RunBudget(max_tokens=10_000, max_llm_calls=40)
    llm = AgentLLM(lambda **_k: SlowLLM(), "m", None, budget)

    async def go() -> List[Any]:
        prompt = "x" * 4_000  # 1,000 tokens; with its 2,000-token output reservation, 3,000 per call
        return await asyncio.gather(*(llm.complete("[phase:debate]", prompt, phase="debate") for _ in range(5)),
                                    return_exceptions=True)

    results = asyncio.run(go())
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, BudgetExhausted)]
    assert len(ok) == 3 and len(refused) == 2
    assert budget.tokens <= budget.max_tokens  # reservations were replaced by the short replies
    assert budget.tokens < 3 * 3_000


# Runtime / terminal status -----------------------------------------------------------------------------------
def _state(budget: Optional[RunBudget] = None, framework: str = agent_config.DEBATE) -> AgentState:
    budget = budget or RunBudget()

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    ctx = QueryContext(raw_query=QUERY, clean_query=QUERY)
    return AgentState(
        query=QUERY, clean_query=QUERY, ctx=ctx, registry=None, user_id="u", thread_id="t", model_name="m",
        api_key=None, framework=framework, budget=budget, control=RunControl("u", interactive=False),
        llm=AgentLLM(None, "m", None, budget), tool_results={}, emit=noop, send=noop,
    )


def test_finalize_phases_without_model_calls_still_run_after_exhaustion(phases):
    ran: List[str] = []

    class Deterministic(AgentPhase):
        key = "det_finalize"
        stage = "finalize"

        async def run(self, state: Any) -> Optional[PhaseOutcome]:
            ran.append(self.key)
            return None

    class NeedsModel(Deterministic):
        key = "llm_finalize"
        needs_llm = True

    phases(Deterministic(), NeedsModel())
    state = _state()
    state.llm = SimpleNamespace(available=True, unavailable_reason="")  # type: ignore[assignment]
    state.budget.exhausted_reason = "token budget of 250000 used"
    asyncio.run(AgenticRuntime(state).run_stage("finalize"))
    assert ran == ["det_finalize"]
    assert any(p["phase"] == "llm_finalize" for p in state.bag["skipped_phases"])


def test_budget_stop_makes_a_completed_time_course_partial_with_reasons():
    state = _state()
    state.budget.exhausted_reason = "token budget of 250000 used (a 3262-token prompt does not fit in the 2215 left)"
    state.sub_questions = [
        SubQuestion(id="q1", text="Mechanisms in NSCLC vs CRC", status="answered"),
        SubQuestion(id="q2", text="Exact FDA-approved indications", status="partial"),
        SubQuestion(id="q3", text="Two testable hypotheses", status="open"),
    ]
    state.bag["phases_ran"] = [{"phase": "plan_execute", "stage": "augment", "status": "llm_unavailable", "seconds": 1.0}]
    status = apply_incomplete_status(
        {"task_status": "completed", "requested_operation": "time_course", "executed_operation": "time_course",
         "reason": "Time course computed."},
        state,
    )
    assert status["task_status"] == "partial"
    assert status["unresolved"] == ["Exact FDA-approved indications", "Two testable hypotheses"]
    assert any("Agent budget exhausted" in r for r in status["incomplete_reasons"])
    assert any("planner model call failed" in r for r in status["incomplete_reasons"])
    assert status["reason"].endswith("Time course computed.")


def test_finished_run_keeps_its_status_and_empty_lists():
    state = _state(framework=agent_config.AGENTIC)
    state.sub_questions = [SubQuestion(id="q1", text="A", status="answered")]
    status = apply_incomplete_status({"task_status": "completed", "reason": "Evidence gathered and synthesized."}, state)
    assert status["task_status"] == "completed" and status["incomplete_reasons"] == [] and status["unresolved"] == []


def test_agentic_run_reports_pipeline_stage_timing_and_task_status_lists(monkeypatch, tmp_path, phases):
    class Critic(AgentPhase):
        key = "critic"
        stage = "after_draft"

        async def run(self, state: Any) -> Optional[PhaseOutcome]:
            return PhaseOutcome(resynthesize=True, feedback=["Mention the pathway."]) if state.draft_round == 0 else None

    phases(Critic())
    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={"synthesis": changing_synthesis()})
    res, _ = run(orch)
    pipeline = [p for p in res.execution_artifacts["agent_run"]["phases"] if p.get("stage") == "pipeline"]
    names = [p["phase"] for p in pipeline]
    for name in ("route", "evidence", "first_draft", "rewrite_1", "verification", "citations", "memory"):
        assert name in names
    assert all(p["seconds"] >= 0 and "at" in p for p in pipeline)
    assert isinstance(res.task_status["incomplete_reasons"], list) and isinstance(res.task_status["unresolved"], list)


# Memory -------------------------------------------------------------------------------------------------------
def test_unsupported_claims_keep_a_turn_out_of_verified_memory():
    passed_with_gap = SimpleNamespace(passed=True, claims=[{"status": "supported"}, {"status": "unsupported"}])
    assert not turn_fully_supported(passed_with_gap)
    assert turn_fully_supported(SimpleNamespace(passed=True, claims=[{"status": "supported"}]))
    assert not turn_fully_supported(SimpleNamespace(passed=False, claims=[]))


# Sanitizer ----------------------------------------------------------------------------------------------------
def test_evidence_markup_is_stripped_from_the_answer():
    text = (
        'Sotorasib is approved <untrusted_data source="web:fda">for NSCLC</untrusted_data> per <agent_evidence note="x"> '
        'the label and &lt;tool_output key="" tool="lithoscribe"&gt; data; `<document pmid="" doi="">` key="" here. '
        "A <b>bold</b> word, x < 5 and y > 3 stay."
    )
    out = strip_evidence_markup(text)
    for leaked in ("untrusted_data", "agent_evidence", "tool_output", "<document", 'key=""'):
        assert leaked not in out
    assert "for NSCLC" in out and "<b>bold</b>" in out and "x < 5 and y > 3" in out


# Liveness and cancellation --------------------------------------------------------------------------------------
def _run_context(events: List[Dict[str, Any]], control: Optional[RunControl] = None) -> RunContext:
    async def cb(ev: Dict[str, Any]) -> None:
        events.append(ev)

    return RunContext(
        orch=SimpleNamespace(_sanitize_agent_names=lambda s: s), rec=None, start_time=time.perf_counter(), pending={},
        query=QUERY, thread_id="t", model_name="m", enable_web_search=False, api_key=None, event_callback=cb,
        user_id="u", dataset_ids=None, persist_memory=False, recall_memory=False, control=control,
    )


def test_a_slow_model_call_emits_heartbeats_on_its_step(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0.05")
    events: List[Dict[str, Any]] = []
    rc = _run_context(events)

    async def go() -> Any:
        await rc.emit("synthesis", "Reasoning over the evidence", "3 sources", status="running")
        return await rc.watched("synthesis", asyncio.sleep(0.32, result="done"))

    assert asyncio.run(go()) == "done"
    beats = [e["data"] for e in events if e["type"] == "thought" and e["data"]["extra"].get("heartbeat")]
    assert len(beats) >= 3
    assert all(b["title"] == "Reasoning over the evidence" and b["status"] == "running" for b in beats)
    assert beats[-1]["extra"]["waiting_s"] > beats[0]["extra"]["waiting_s"]
    assert beats[-1]["extra"]["last_progress_s"] <= beats[0]["extra"]["waiting_s"] + 0.05


def test_cancel_stops_a_long_model_call_promptly(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0")
    control = RunControl("u", interactive=False)
    rc = _run_context([], control)

    async def go() -> None:
        asyncio.get_running_loop().call_later(0.1, control.cancel)
        await rc.watched("synthesis", asyncio.sleep(30))

    t0 = time.perf_counter()
    with pytest.raises(RunCancelled):
        asyncio.run(go())
    assert time.perf_counter() - t0 < 3.0


# Dates ----------------------------------------------------------------------------------------------------------
def test_retrieval_date_is_utc(monkeypatch):
    seen: Dict[str, Any] = {}

    class FakeDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            seen["tz"] = tz
            return datetime(2026, 10, 18, 23, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(draft_stage, "datetime", FakeDatetime)
    assert draft_stage.retrieval_date() == "2026-10-18"
    assert seen["tz"] is timezone.utc
