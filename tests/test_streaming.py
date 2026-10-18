"""
Offline tests for live streaming: the model's own reasoning and answer tokens must reach the
event callback as they arrive, and activity steps must describe what the tools actually returned.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.utils.llm import llm_factory
from phronesis.utils.llm.llm_factory import _ThinkTagSplitter, astream_with_reasoning
from tests.test_audit_regressions import offline_orchestrator  # noqa: F401


def _split_all(chunks: List[str]) -> Dict[str, str]:
    s = _ThinkTagSplitter()
    out = {"reasoning": "", "text": ""}
    for c in chunks:
        for kind, part in s.feed(c):
            out[kind] += part
    for kind, part in s.flush():
        out[kind] += part
    return out


def test_think_tags_split_even_across_chunk_boundaries():
    out = _split_all(["<thi", "nk>KRAS is a GTP", "ase.</th", "ink>Answer: ", "yes"])
    assert out == {"reasoning": "KRAS is a GTPase.", "text": "Answer: yes"}


def test_text_without_think_tags_passes_through():
    assert _split_all(["a < b", " and c"]) == {"reasoning": "", "text": "a < b and c"}


class _FakeStream:
    def __init__(self, deltas):
        self._deltas = iter(deltas)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            reasoning, content = next(self._deltas)
        except StopIteration:
            raise StopAsyncIteration
        delta = SimpleNamespace(content=content, model_extra={"reasoning_content": reasoning} if reasoning else {})
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


@pytest.mark.asyncio
async def test_chat_openai_stream_surfaces_reasoning_content():
    """LangChain drops `reasoning_content`; the raw stream must keep it."""
    llm = ChatOpenAI(model="nvidia/nemotron-3-super-120b-a12b", api_key="k", base_url="http://unused", temperature=0.0)
    captured: Dict[str, Any] = {}

    class FakeCompletions:
        async def create(self, **params):
            captured.update(params)
            return _FakeStream([("Need to check ", None), ("PMIDs.", None), (None, "KRAS "), (None, "drives MAPK.")])

    fake_client = SimpleNamespace(
        with_options=lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    )
    object.__setattr__(llm, "root_async_client", fake_client)

    seen: List[tuple] = []

    async def on_r(d):
        seen.append(("r", d))

    async def on_t(d):
        seen.append(("t", d))

    text, reasoning = await astream_with_reasoning(llm, [HumanMessage(content="q")], on_reasoning=on_r, on_text=on_t)
    assert reasoning == "Need to check PMIDs."
    assert text == "KRAS drives MAPK."
    assert seen[0] == ("r", "Need to check ") and seen[-1] == ("t", "drives MAPK.")
    assert captured["stream"] is True and captured["messages"] == [{"role": "user", "content": "q"}]


@pytest.mark.asyncio
async def test_orchestrator_streams_reasoning_answer_and_real_steps(offline_orchestrator, monkeypatch):  # noqa: F811
    orch, _calls = offline_orchestrator
    monkeypatch.setenv("PHRONESIS_SHOW_REASONING", "1")

    class FakeStreamingLLM:
        async def astream(self, _messages):
            for piece in ["<think>Only one passage, PMID 123456.</think>", "Lithoscribe retrieved ", "PMID: 123456 on KRAS."]:
                yield SimpleNamespace(content=piece, additional_kwargs={})

    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: FakeStreamingLLM())
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-stream", enable_web_search=False, event_callback=cb)

    reasoning = "".join(e["delta"] for e in events if e["type"] == "reasoning_delta")
    answer = "".join(e["delta"] for e in events if e["type"] == "content_delta")
    assert reasoning == "Only one passage, PMID 123456."
    assert answer == "Lithoscribe retrieved PMID: 123456 on KRAS."
    assert res.reasoning == reasoning

    # Each tool step is announced as running, then updated in place with what it actually returned.
    lit = [e["data"] for e in events if e["type"] == "thought" and e["data"]["id"] == "lithoscribe"]
    assert [s["status"] for s in lit] == ["running", "done"]
    assert lit[-1]["title"] == "Found 1 relevant literature passage"
    assert len([t for t in res.thoughts if t["id"] == "lithoscribe"]) == 1
    assert all(t["status"] != "running" for t in res.thoughts)

    # The plan step reflects parsed intent, not boilerplate.
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Target: KRAS" in plan["text"] and "skipped at your request" in plan["text"]


@pytest.mark.asyncio
async def test_casual_greeting_shows_no_fabricated_activity(offline_orchestrator):  # noqa: F811
    orch, _ = offline_orchestrator
    res = await orch.execute_inquiry("hello", thread_id="t-hi", enable_web_search=False)
    assert res.thoughts == [] and res.reasoning == ""


def test_stream_read_timeout_is_not_the_short_request_timeout():
    assert llm_factory.STREAM_READ_TIMEOUT >= 60
