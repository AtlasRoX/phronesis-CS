"""Per-call budget admission (source audit 2026-10-18, F1-F4): every agentic model call is admitted with its own
input estimate and output allocation, the provider receives exactly that allocation, usage is reconciled, the
finalization reserve survives parallel optional work, and long calls obey the run's absolute deadline."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from phronesis.agentic import config as agent_config
from phronesis.agentic.budget import BudgetExhausted, RunBudget, RunDeadlineExceeded
from phronesis.agentic.control import RunCancelled, RunControl
from phronesis.agentic.llm import AgentLLM
from phronesis.agents.orchestration.agent_rounds import generation_allocation
from phronesis.agents.orchestration.context import RunContext
from tests.agentic_fakes import collect_events, make_agentic_orchestrator

QUERY = "What diseases is KRAS associated with?"


class CapUsingModel(BaseChatModel):
    """A chat model that reports exact usage: 1,000 input tokens and an output as long as the max_tokens it was
    sent (a provider may legally use its whole cap), or 8,000 tokens when no cap was sent."""

    caps: List[Optional[int]] = []
    delay: float = 0.02

    @property
    def _llm_type(self) -> str:
        return "cap-using-fake"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        cap = kwargs.get("max_tokens")
        self.caps.append(cap)
        await asyncio.sleep(self.delay)
        out = int(cap) if cap else 8_000
        msg = AIMessage(content="{}", usage_metadata={"input_tokens": 1_000, "output_tokens": out, "total_tokens": 1_000 + out})
        return ChatResult(generations=[ChatGeneration(message=msg)])


PROMPT = "x" * 4_000  # exactly 1,000 estimated input tokens, matching the reported usage


def _parallel(budget: RunBudget, n: int, phase: str = "debate") -> List[Any]:
    model = CapUsingModel(caps=[])
    llm = AgentLLM(lambda **_k: model, "m", None, budget)

    async def go() -> List[Any]:
        return await asyncio.gather(*(llm.complete("[phase:debate]", PROMPT, phase=phase) for _ in range(n)),
                                    return_exceptions=True)

    out = asyncio.run(go())
    return [out, model.caps]


# F2 --------------------------------------------------------------------------------------------------------------
def test_regression_a_concurrent_calls_cannot_exceed_the_hard_budget(monkeypatch):
    """Audit F2, case A: 10,000 tokens, three concurrent calls, 1,000 exact input tokens and 2,048 reserved output
    each. The old code sent caps from the global remainder (6,952 / 3,904 / 856) and reconciled to 11,760."""
    monkeypatch.setenv("PHRONESIS_AGENT_OUTPUT_RESERVE_TOKENS", "2048")
    budget = RunBudget(max_tokens=10_000, max_llm_calls=40)
    results, caps = _parallel(budget, 3)
    assert all(c is not None and c <= 2_048 for c in caps), caps  # each call's own allocation is what was sent
    assert budget.tokens <= budget.max_tokens, budget.tokens
    assert not any(isinstance(r, Exception) and not isinstance(r, BudgetExhausted) for r in results)


def test_regression_b_optional_calls_leave_the_final_reserve_intact(monkeypatch):
    """Audit F2, case B: 3,000 tokens kept for final work; two optional calls used to reach 9,904."""
    monkeypatch.setenv("PHRONESIS_AGENT_OUTPUT_RESERVE_TOKENS", "2048")
    budget = RunBudget(max_tokens=10_000, max_llm_calls=40, final_reserve_tokens=3_000)
    _parallel(budget, 2)
    assert budget.tokens <= budget.max_tokens - budget.final_reserve_tokens, budget.tokens
    alloc = budget.admit("revision", prompt_chars=4 * 1_000, max_output=1_500)  # final work still fits
    assert alloc.output_cap == 1_500


def test_failed_and_cancelled_calls_release_their_output_reservation():
    budget = RunBudget(max_tokens=10_000)
    a = budget.admit("gaps", prompt_chars=4 * 1_000, max_output=2_000)
    assert budget.tokens == 3_000 and budget.reserved_open == 2_000
    budget.settle(a, failed=True)  # usage unknown: the prompt stays charged, the unused output is released
    assert budget.tokens == 1_000 and budget.reserved_open == 0 and budget.unreconciled_calls == 1
    b = budget.admit("gaps", prompt_chars=4 * 1_000, max_output=2_000)
    budget.settle(b, input_tokens=900, output_tokens=150)
    assert budget.tokens == 1_000 + 1_050 and budget.tokens_provider == 1_050
    budget.settle(b, input_tokens=900, output_tokens=150)  # settling twice changes nothing
    assert budget.tokens == 2_050


def test_an_optional_allocation_shrinks_to_what_fits_before_the_reserve():
    budget = RunBudget(max_tokens=10_000, final_reserve_tokens=3_000, tokens=5_000)
    a = budget.admit("gaps", prompt_chars=4 * 1_000, max_output=4_000)
    assert a.output_cap == 1_000 and budget.tokens == 7_000
    with pytest.raises(BudgetExhausted):
        budget.admit("gaps", prompt_chars=4 * 100, max_output=500)
    assert budget.optional_exhausted() and not budget.exhausted()


# F3 --------------------------------------------------------------------------------------------------------------
def test_regression_f3_rewrite_allocation_never_exceeds_what_is_left():
    """Audit F3: 1,100 tokens left and a 1,000-token prompt used to ask the model for 1,024 output tokens."""
    budget = RunBudget(max_tokens=10_000, tokens=8_900)
    alloc, reason = generation_allocation(budget, "revision", prompt_chars=4 * 1_000, draft_chars=4 * 50)
    assert alloc is None or alloc.output_cap <= 100
    assert budget.tokens <= budget.max_tokens


def test_a_whole_answer_rewrite_is_skipped_when_it_would_be_truncated():
    budget = RunBudget(max_tokens=10_000, tokens=6_000)
    # A 2,000-token draft cannot be rewritten in the ~3,000 tokens left after a 1,000-token prompt? It can; but not
    # with only 500 left.
    budget2 = RunBudget(max_tokens=10_000, tokens=8_500)
    alloc, reason = generation_allocation(budget2, "revision", prompt_chars=4 * 1_000, draft_chars=4 * 2_000)
    assert alloc is None and "would be cut off" in reason
    assert budget2.tokens == 8_500 and budget2.llm_calls == 0  # nothing charged
    ok, _ = generation_allocation(budget, "revision", prompt_chars=4 * 1_000, draft_chars=4 * 2_000)
    assert ok is not None and ok.output_cap >= 2_000


def test_rewrites_cannot_drain_the_final_reserve():
    budget = RunBudget(max_tokens=20_000, final_reserve_tokens=6_000, tokens=12_000)
    alloc, reason = generation_allocation(budget, "synthesis", prompt_chars=4 * 1_000, draft_chars=4 * 2_000)
    assert alloc is None  # only 1,000 optional tokens are left before the reserve
    assert budget.tokens == 12_000
    final, _ = generation_allocation(budget, "revision", prompt_chars=4 * 1_000, draft_chars=4 * 2_000)
    assert final is not None  # the reserve is still there for the verifier's revision


# F1 --------------------------------------------------------------------------------------------------------------
def test_regression_f1_first_draft_is_charged_to_the_run_ledger(monkeypatch, tmp_path):
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, _ = asyncio.run(collect_events(orch, query=QUERY, thread_id="t-f1", framework=agent_config.AGENTIC))
    by_phase = res.execution_artifacts["agent_run"]["budget"]["by_phase"]
    assert by_phase.get("first_draft", {}).get("llm_calls") == 1
    assert len(llm.prompts("synthesis")) >= 1


def test_regression_f1_no_uncharged_first_draft_after_hard_exhaustion(monkeypatch, tmp_path):
    """With the model-call budget already used up, the first draft must not call the provider: the answer is the
    deterministic evidence summary, marked as a budget stop."""
    monkeypatch.setenv("PHRONESIS_AGENT_MAX_LLM_CALLS", "1")
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses={
        "decompose": '{"complex": true, "sub_questions": [{"id": "q1", "text": "What diseases is KRAS associated with?"},'
                     ' {"id": "q2", "text": "Which KRAS variants matter?"}]}',
    })
    res, events = asyncio.run(collect_events(orch, query=QUERY, thread_id="t-f1b", framework=agent_config.AGENTIC))
    budget = res.execution_artifacts["agent_run"]["budget"]
    assert budget["used"]["llm_calls"] == budget["limits"]["llm_calls"] == 1
    assert "first_draft" not in budget["by_phase"]
    assert llm.prompts("synthesis") == []  # no uncharged synthesis call
    titles = [e["data"]["title"] for e in events if e.get("type") == "thought" and e["data"].get("id") == "synthesis"]
    assert any("budget used up" in t.lower() for t in titles)
    assert res.response.strip()  # the deterministic evidence summary


def test_first_draft_streams_with_its_admitted_output_cap(monkeypatch, tmp_path):
    seen: List[Dict[str, Any]] = []
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    from phronesis.agents import authoritative_orchestrator as orch_module

    def get_llm(*_a: Any, **k: Any) -> Any:
        seen.append(k)
        return llm

    monkeypatch.setattr(orch_module, "get_llm", get_llm)
    asyncio.run(collect_events(orch, query=QUERY, thread_id="t-f1c", framework=agent_config.AGENTIC))
    streamed = [k for k in seen if k.get("streaming")]
    assert streamed and all(isinstance(k.get("max_tokens"), int) and k["max_tokens"] > 0 for k in streamed)


# F4 --------------------------------------------------------------------------------------------------------------
def _run_context(budget: Optional[RunBudget] = None, control: Optional[RunControl] = None) -> RunContext:
    async def cb(_ev: Dict[str, Any]) -> None:
        return None

    rc = RunContext(
        orch=SimpleNamespace(_sanitize_agent_names=lambda s: s), rec=None, start_time=time.perf_counter(), pending={},
        query=QUERY, thread_id="t", model_name="m", enable_web_search=False, api_key=None, event_callback=cb,
        user_id="u", dataset_ids=None, persist_memory=False, recall_memory=False, control=control,
    )
    if budget is not None:
        rc.agent_state = SimpleNamespace(budget=budget, control=control)
    return rc


def test_regression_f4_watched_enforces_the_run_deadline(monkeypatch):
    """Audit F4: a 0.01 s run budget around a 0.05 s call used to return "finished"."""
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0")
    budget = RunBudget(max_seconds=0.01)
    rc = _run_context(budget, RunControl("u", interactive=False))
    with pytest.raises(RunDeadlineExceeded):
        asyncio.run(rc.watched("synthesis", asyncio.sleep(0.05, result="finished")))
    assert budget.exhausted() and "time budget" in budget.exhausted_reason


def test_deadline_cancels_a_long_call_promptly_without_a_control(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0")
    budget = RunBudget(max_seconds=0.2)
    rc = _run_context(budget)
    t0 = time.perf_counter()
    with pytest.raises(RunDeadlineExceeded):
        asyncio.run(rc.watched("synthesis", asyncio.sleep(30)))
    assert time.perf_counter() - t0 < 2.0


async def _stubborn(seconds: float) -> str:
    """A provider call that ignores cancellation for `seconds` before it stops."""
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        t_end = time.perf_counter() + seconds
        while time.perf_counter() < t_end:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        raise
    return "never"


def test_a_call_that_delays_its_cancellation_does_not_hold_the_run(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0")
    monkeypatch.setenv("PHRONESIS_CANCEL_GRACE_S", "0.2")
    control = RunControl("u", interactive=False)
    rc = _run_context(RunBudget(), control)

    seen: Dict[str, Any] = {}

    async def go() -> None:
        asyncio.get_running_loop().call_later(0.05, control.cancel)
        t0 = time.perf_counter()
        try:
            await rc.watched("synthesis", _stubborn(1.5))
        except RunCancelled:
            seen["seconds"] = time.perf_counter() - t0  # the run moves on here; the abandoned call ends on its own
            raise

    with pytest.raises(RunCancelled):
        asyncio.run(go())
    assert seen["seconds"] < 1.0  # waited the 0.2 s grace period, not the provider's 1.5 s


def test_a_lost_client_cancels_the_watched_call(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PROGRESS_HEARTBEAT_S", "0")
    rc = _run_context(RunBudget())
    inner: Dict[str, Any] = {}

    async def call() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            inner["cancelled"] = True
            raise

    async def go() -> None:
        outer = asyncio.ensure_future(rc.watched("synthesis", call()))
        await asyncio.sleep(0.05)
        outer.cancel()  # what the SSE endpoint does when the browser disconnects
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)

    asyncio.run(go())
    assert inner.get("cancelled")


def test_no_optional_work_starts_after_the_deadline():
    budget = RunBudget(max_seconds=0.01)
    time.sleep(0.02)
    assert budget.exhausted() and budget.optional_exhausted()
    with pytest.raises(BudgetExhausted):
        budget.admit("gaps", prompt_chars=40, max_output=100)
    with pytest.raises(BudgetExhausted):
        budget.admit("revision", prompt_chars=40, max_output=100)


def test_stream_total_timeout_follows_the_run_deadline():
    from phronesis.utils.llm.llm_factory import STREAM_TOTAL_TIMEOUT, stream_timeout

    assert stream_timeout(None) == STREAM_TOTAL_TIMEOUT
    assert stream_timeout(12.5) == 12.5
    assert stream_timeout(10_000.0) == STREAM_TOTAL_TIMEOUT
