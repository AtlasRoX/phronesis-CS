"""Workstream C (memory and backend) regression tests: docs/plans/audit-repair-plan.md, report F09."""

from __future__ import annotations

import asyncio
import gc
import json
import os
from typing import Any, Dict, List

import pytest

from phronesis.utils.memory.clinical_memory import (
    ClinicalMemoryEngine,
    assertions_conflict,
    classify_instructions,
    summarize_tool_data,
)


@pytest.fixture
def mem(tmp_path) -> ClinicalMemoryEngine:
    return ClinicalMemoryEngine(storage_dir=str(tmp_path / "mem"))


def _turn(mem: ClinicalMemoryEngine, thread: str, query: str, tools: Dict[str, Any] | None = None, **kw: Any) -> Dict[str, Any]:
    return mem.consolidate_turn_sync(thread, query, "resp", tools or {}, **kw)


# --- isolation and scoping ---------------------------------------------------------------------------------

def test_user_a_cannot_read_user_b(mem):
    _turn(mem, "t", "BRAF question", {"synapta": "BRAF V600E mutation findings"}, user_id="bob", is_turn_verified=True,
          active_subject={"gene": "BRAF"})
    assert not mem.search_memory("BRAF V600E", user_id="alice")
    assert not mem.get_conversation_history("t", user_id="alice")
    pkg = mem.get_context_package("t", user_id="alice")
    assert not (pkg.verified_evidence or pkg.query_history or pkg.active_subject)
    assert mem.get_active_subject("alice", "t") is None
    assert mem.search_memory("BRAF V600E", user_id="bob")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_user_id_rejected(mem, bad):
    with pytest.raises(ValueError):
        mem.search_memory("KRAS", user_id=bad)
    with pytest.raises(ValueError):
        mem.get_context_package("t", user_id=bad)
    with pytest.raises(ValueError):
        mem.clear_thread_memory("t", user_id=bad)
    with pytest.raises(ValueError):
        mem.clear_all_memory(bad)
    with pytest.raises(ValueError):
        mem.get_memory_stats(user_id=bad)
    with pytest.raises(ValueError):
        mem.consolidate_turn_sync("t", "q", "r", {}, user_id=bad)


def test_reset_of_thread_a_keeps_thread_ab(mem):
    _turn(mem, "a", "KRAS question", user_id="alice")
    _turn(mem, "ab", "EGFR question", user_id="alice")
    mem.clear_thread_memory("a", user_id="alice")
    assert not mem.get_conversation_history("a", user_id="alice")
    assert mem.get_conversation_history("ab", user_id="alice")


def test_connections_are_closed(tmp_path):
    root = tmp_path / "closing"
    engine = ClinicalMemoryEngine(storage_dir=str(root))
    _turn(engine, "t", "KRAS question", {"synapta": "KRAS G12D"}, user_id="alice")
    engine.search_memory("KRAS", user_id="alice")
    engine.get_context_package("t", user_id="alice", query="KRAS")
    gc.collect()
    # On Windows a leaked connection keeps the database file locked and this removal fails.
    for name in os.listdir(root):
        os.remove(root / name)


# --- scoped instructions and history -------------------------------------------------------------------------

def test_current_turn_constraint_not_recalled_by_new_investigation(mem):
    _turn(mem, "t1", "Find KRAS papers only. Do not run simulations.", user_id="alice")
    fresh = mem.get_context_package("t2", user_id="alice")
    assert not fresh.user_instructions and not fresh.query_history
    same = mem.get_context_package("t1", user_id="alice")
    assert not same.user_instructions  # current_turn: stored for audit, never a standing directive
    assert same.query_history == ["Find KRAS papers only. Do not run simulations."]
    assert "Active User Constraints" not in same.to_system_prompt_injection()
    assert "history only; not instructions" in same.to_system_prompt_injection()


def test_scoped_instructions_persist_only_within_their_scope(mem):
    _turn(mem, "t1", "What is KRAS? For this investigation, only use human data.", user_id="alice")
    _turn(mem, "t1", "Always cite PMIDs in your answers.", user_id="alice")
    same = mem.get_context_package("t1", user_id="alice")
    assert any("only use human data" in i for i in same.user_instructions)
    other = mem.get_context_package("t2", user_id="alice")
    assert other.user_instructions == ["Always cite PMIDs in your answers."]
    assert not mem.get_context_package("t1", user_id="bob").user_instructions


def test_classify_instructions_scopes():
    scopes = dict((s, scope) for s, scope in classify_instructions(
        "Find KRAS papers only. From now on, exclude preprints. Always use SI units. Does KRAS always drive resistance?"
    ))
    assert scopes["Find KRAS papers only."] == "current_turn"
    assert scopes["From now on, exclude preprints."] == "investigation"
    assert scopes["Always use SI units."] == "profile"
    assert "Does KRAS always drive resistance?" not in scopes


def test_recall_is_bounded_and_prefers_recent_or_relevant(mem):
    for i in range(10):
        _turn(mem, "t", f"question {i}", {"synapta": f"Finding number {i} about pathway{i}"}, user_id="alice",
              is_turn_verified=True)
    pkg = mem.get_context_package("t", user_id="alice")
    assert len(pkg.verified_evidence) <= 4 and len(pkg.query_history) <= 5
    assert "pathway9" in pkg.verified_evidence[0]
    assert pkg.query_history[-1] == "question 9"
    ranked = mem.get_context_package("t", user_id="alice", query="pathway2")
    assert "pathway2" in ranked.verified_evidence[0]


# --- active subject ------------------------------------------------------------------------------------------

def test_active_subject_round_trip_and_replacement(mem):
    _turn(mem, "t", "Osimertinib mechanism in EGFR-mutant NSCLC", user_id="alice",
          active_subject={"drug": "osimertinib", "gene": "EGFR", "disease": "NSCLC", "junk": "x"})
    assert mem.get_active_subject("alice", "t") == {"gene": "EGFR", "drug": "osimertinib", "disease": "NSCLC"}
    assert mem.get_context_package("t", user_id="alice").resolved_subject == "EGFR"
    mem.record_active_subject("alice", "t", {"gene": "BRAF"})
    assert mem.get_active_subject("alice", "t") == {"gene": "BRAF"}  # KRAS/EGFR not carried over
    assert mem.get_active_subject("bob", "t") is None
    mem.clear_thread_memory("t", user_id="alice")
    assert mem.get_active_subject("alice", "t") is None


def test_async_wrappers(mem):
    async def run() -> None:
        await mem.consolidate_turn("t", "KRAS question", "resp", {"synapta": "KRAS G12D"}, user_id="alice",
                                   active_subject={"gene": "KRAS"})
        assert (await mem.arecall("t", "alice", "KRAS")).query_history == ["KRAS question"]
        assert await mem.asearch_memory("KRAS", "t", "alice")
        assert await mem.aget_active_subject("alice", "t") == {"gene": "KRAS"}
        assert await mem.aget_conversation_history("t", "alice")
        assert isinstance(await mem.aget_verified_identifiers("t", "alice"), dict)

    asyncio.run(run())


# --- entities, summaries, contradictions ----------------------------------------------------------------------

def test_gene_regex_ignores_english_words():
    ents = ClinicalMemoryEngine.extract_entities("The model met the criteria; ret and atm are words. MET amplification.")
    assert ents["targets"] == ["MET"]


def test_structured_summary_keeps_values_and_drops_blobs():
    data = {
        "model_id": "BIOMD0000000010",
        "final_concentrations": {"Mos": 1.23456, "Erk2-PP": 0.5},
        "plot_base64_png": "A" * 5000,
        "dataset_path": "/srv/private/file.h5ad",
        "ranked_passages": [{"title": "KRAS resistance study", "pmid": "123"}],
    }
    text = summarize_tool_data(data, "vivarix")
    assert "model_id=BIOMD0000000010" in text and "Mos=1.235" in text
    assert "ranked_passages[1: KRAS resistance study]" in text
    assert "AAAA" not in text and "/srv/private" not in text and len(text) <= 480


def test_contradiction_requires_same_context():
    prior = "Tumor model is sensitive to KRAS switch-II monotherapy."
    assert assertions_conflict(prior, "Tumor model is resistant to KRAS switch-II monotherapy through SOS1 rebound.", "KRAS")
    assert not assertions_conflict(prior, "Colon organoids with KRAS loss were resistant to cetuximab in vivo.", "KRAS")
    assert not assertions_conflict(prior, "Tumor model is not resistant to KRAS switch-II monotherapy.", "KRAS")
    assert not assertions_conflict("MET amplification is sensitive to capmatinib.", "We met resistant clones.", "MET")


def test_contradiction_demotes_only_conflicting_node(mem):
    _turn(mem, "t", "q1", {"pilot": "Tumor model is sensitive to KRAS switch-II monotherapy."}, user_id="alice")
    _turn(mem, "t", "q2", {"other": "KRAS expression is high in organoids that were resistant to chemotherapy."},
          user_id="alice")
    assert mem.get_memory_stats(user_id="alice")["total_contradictions_resolved"] == 0
    _turn(mem, "t", "q3", {"vivarix": "Tumor model is resistant to KRAS switch-II monotherapy."}, user_id="alice")
    assert mem.get_memory_stats(user_id="alice")["total_contradictions_resolved"] == 1


# --- backend API ---------------------------------------------------------------------------------------------

ALICE = {"Authorization": "Bearer alice-token"}


class _FakeResult:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)


class _FakeOrchestrator:
    def __init__(self, payload: Dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    async def execute_inquiry(self, **kwargs: Any) -> _FakeResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeResult(self.payload)


@pytest.fixture
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    engine = ClinicalMemoryEngine(storage_dir=str(tmp_path / "api_mem"))
    monkeypatch.setattr(backend_main, "get_clinical_memory_engine", lambda: engine)
    monkeypatch.delenv("PHRONESIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PHRONESIS_JWT_SECRET", raising=False)
    monkeypatch.setenv("PHRONESIS_API_TOKENS", "alice-token:alice,bob-token:bob")
    return TestClient(backend_main.app), backend_main, engine


def _stream_events(client, body: Dict[str, Any]) -> List[Dict[str, Any]]:
    with client.stream("POST", "/api/chat/stream", json=body, headers=ALICE) as resp:
        assert resp.status_code == 200
        return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def test_api_rejects_oversized_message_and_unknown_model(api):
    client, backend_main, _ = api
    assert client.post("/api/chat", json={"message": "x" * 20_001}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": ""}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": "hi", "model": "o1-pro-max"}, headers=ALICE).status_code == 422
    assert client.post("/api/chat", json={"message": "hi", "thread_id": "t" * 201}, headers=ALICE).status_code == 422
    resp = client.post("/api/memory/search", json={"query": "KRAS", "top_k": 500}, headers=ALICE)
    assert resp.status_code == 422


def test_api_accepts_unknown_model_with_own_key(api, monkeypatch):
    client, backend_main, _ = api
    fake = _FakeOrchestrator({"response": "ok"})
    monkeypatch.setattr(backend_main, "get_authoritative_orchestrator", lambda: fake)
    resp = client.post("/api/chat", json={"message": "hi", "model": "o1-pro-max", "api_key": "sk-real"}, headers=ALICE)
    assert resp.status_code == 200 and fake.calls[0]["model_name"] == "o1-pro-max"


def test_task_status_passes_through_chat_and_stream(api, monkeypatch):
    client, backend_main, _ = api
    status = {"task_status": "failed", "requested_operation": "parameter_scan", "executed_operation": "time_course",
              "request_match": False, "reason": "scan not run", "grounding_status": "supported"}
    fake = _FakeOrchestrator({"response": "r", "task_status": status})
    monkeypatch.setattr(backend_main, "get_authoritative_orchestrator", lambda: fake)
    assert client.post("/api/chat", json={"message": "scan"}, headers=ALICE).json()["task_status"] == status
    done = next(e for e in _stream_events(client, {"message": "scan"}) if e["type"] == "done")
    assert done["result"]["task_status"] == status


def test_errors_do_not_leak_exception_text(api, monkeypatch):
    client, backend_main, _ = api
    fake = _FakeOrchestrator(error=RuntimeError("upstream said: invalid key sk-secret-123 at https://internal"))
    monkeypatch.setattr(backend_main, "get_authoritative_orchestrator", lambda: fake)
    resp = client.post("/api/chat", json={"message": "hi"}, headers=ALICE)
    assert resp.status_code == 500
    assert "sk-secret" not in resp.text and "request id" in resp.json()["detail"]
    assert resp.headers["X-Request-ID"] in resp.json()["detail"]
    err = next(e for e in _stream_events(client, {"message": "hi"}) if e["type"] == "error")
    assert "sk-secret" not in err["detail"] and err["request_id"] in err["detail"]


def test_chat_concurrency_limit_returns_429_and_releases(api, monkeypatch):
    from app.backend.limits import UserConcurrencyLimiter

    client, backend_main, _ = api
    limiter = UserConcurrencyLimiter(1)
    monkeypatch.setattr(backend_main, "_chat_slots", limiter)
    monkeypatch.setattr(backend_main, "get_authoritative_orchestrator", lambda: _FakeOrchestrator({"response": "r"}))
    assert limiter.try_acquire("alice")
    assert client.post("/api/chat", json={"message": "hi"}, headers=ALICE).status_code == 429
    assert client.post("/api/chat/stream", json={"message": "hi"}, headers=ALICE).status_code == 429
    limiter.release("alice")
    assert client.post("/api/chat", json={"message": "hi"}, headers=ALICE).status_code == 200
    _stream_events(client, {"message": "hi"})
    assert limiter.active("alice") == 0


def test_api_reset_matches_exact_thread(api):
    client, _, engine = api
    _turn(engine, "a", "KRAS", user_id="alice")
    _turn(engine, "ab", "EGFR", user_id="alice")
    assert client.post("/api/reset", json={"thread_id": "a"}, headers=ALICE).status_code == 200
    assert client.get("/api/memory/thread/a", headers=ALICE).json()["count"] == 0
    assert client.get("/api/memory/thread/ab", headers=ALICE).json()["count"] > 0


def test_health_probe_is_cached(api, monkeypatch):
    client, backend_main, _ = api
    calls: List[int] = []

    class _Probe:
        async def async_check_service_readiness(self) -> Dict[str, Any]:
            calls.append(1)
            return {"mcp_remote_active": False, "direct_adapters": {}}

    monkeypatch.setattr(backend_main, "BioContextMCPClient", _Probe)
    monkeypatch.setitem(backend_main._health_cache, "readiness", None)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert len(calls) == 1
    assert client.get("/api/health/live").json()["status"] == "alive"
    assert len(calls) == 1


def test_sse_heartbeat_then_terminal_event():
    from app.backend.main import sse_frames

    async def run() -> List[str]:
        queue: asyncio.Queue = asyncio.Queue()
        frames: List[str] = []
        gen = sse_frames(queue, heartbeat_s=0.01)
        frames.append(await gen.__anext__())
        await queue.put({"type": "thought", "data": {}})
        await queue.put({"type": "done", "result": {}})
        frames.extend([f async for f in gen])
        return frames

    frames = asyncio.run(run())
    assert frames[0] == ": ping\n\n"
    assert [f for f in frames if f.startswith("data: ")][-1] == 'data: {"type": "done", "result": {}}\n\n'


def test_rerun_locks_do_not_grow_without_bound():
    from app.backend.routers import export as export_router

    async def run() -> None:
        sem = export_router._rerun_lock("user-x")
        async with sem:
            assert export_router._rerun_locks.get("user-x") is sem

    asyncio.run(run())
    gc.collect()
    assert "user-x" not in export_router._rerun_locks._items


def test_rerun_rejects_unknown_model_without_key():
    from pydantic import ValidationError

    from app.backend.routers.export import RerunRequest

    with pytest.raises(ValidationError):
        RerunRequest(mode="full", model="o1-pro-max")
    assert RerunRequest(mode="full", model="o1-pro-max", api_key="sk-real").model == "o1-pro-max"


# --- LLM factory ---------------------------------------------------------------------------------------------

def test_placeholder_keys_fail_fast(monkeypatch):
    from phronesis.utils.llm.llm_factory import LLMUnavailableError, get_llm

    monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    with pytest.raises(LLMUnavailableError):
        get_llm("gpt-4o")
    with pytest.raises(LLMUnavailableError):
        get_llm("nvidia/nemotron-3-super-120b-a12b")
    monkeypatch.setenv("NVIDIA_API_KEY", "your-nvidia-key")
    with pytest.raises(LLMUnavailableError):
        get_llm("nvidia/nemotron-3-super-120b-a12b")
    assert get_llm("gpt-4o", api_key="sk-live-abc").model_name == "gpt-4o"


def test_stream_total_timeout(monkeypatch):
    from phronesis.utils.llm import llm_factory

    class _SlowLLM:
        async def astream(self, _messages):
            from langchain_core.messages import AIMessageChunk

            while True:
                await asyncio.sleep(0.01)
                yield AIMessageChunk(content="x")

    monkeypatch.setattr(llm_factory, "STREAM_TOTAL_TIMEOUT", 0.1)
    with pytest.raises(llm_factory.LLMStreamTimeoutError):
        asyncio.run(llm_factory.astream_with_reasoning(_SlowLLM(), []))
