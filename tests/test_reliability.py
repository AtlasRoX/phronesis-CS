"""
Offline fault-injection tests for the tool reliability layer (phronesis/tools/reliability.py, feature 19).

In the spirit of ToolEmu (arXiv:2309.15817) and ToolSword (arXiv:2402.10753), the upstream is emulated: scripted
executors return timeouts, 5xx-style upstream errors, not-found answers and noisy partial payloads, and the tests
check the circuit breaker (Fowler 2014), full-jitter retries, the result cache, fallbacks, the API-Bank failure
labels (arXiv:2304.08244) and that the run recorder still sees exactly one call per execute. Sleeping, jitter and
clocks are patched; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

import pytest
from pydantic import BaseModel, Field

from phronesis.provenance import run_manifest as rm
from phronesis.tools import reliability as rel
from phronesis.tools.registry import AdapterType, BiomedicalToolRegistry, ErrorType, ToolResult, ToolUniverseEntry
from tests.fakes import build_recording_registry


class QueryInput(BaseModel):
    query: str
    size: int = Field(default=5, ge=1, le=50)


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def policies(monkeypatch, tmp_path):
    """Policies on (they default to off under pytest), no real sleeping, deterministic jitter and clocks."""
    for name in ("PHRONESIS_TOOL_CACHE_DIR", "PHRONESIS_RUNS_DIR", "PHRONESIS_TOOL_CACHE_TTL_S", "PHRONESIS_TOOL_RETRIES",
                 "PHRONESIS_BREAKER_THRESHOLD", "PHRONESIS_BREAKER_COOLDOWN_S"):
        monkeypatch.delenv(name, raising=False)
    # A per-test data dir: the session-wide one (tests/conftest.py) would let one test's SQLite cache rows be
    # replayed as hits in the next.
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "on")
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE", "on")
    sleeps: List[float] = []
    jitter: List[tuple] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_uniform(a: float, b: float) -> float:
        jitter.append((a, b))
        return b / 2

    mono, wall = Clock(), Clock(1_700_000_000.0)
    monkeypatch.setattr(rel, "_sleep", fake_sleep)
    monkeypatch.setattr(rel, "_uniform", fake_uniform)
    monkeypatch.setattr(rel, "_monotonic", mono)
    monkeypatch.setattr(rel, "_now", wall)
    rel.reset_reliability()
    yield {"sleeps": sleeps, "jitter": jitter, "mono": mono, "wall": wall}
    rel.reset_reliability()


def scripted_tool(
    reg: BiomedicalToolRegistry,
    name: str,
    outcomes: List[Any],
    *,
    adapter: AdapterType = AdapterType.DIRECT_REST,
    tags: Optional[List[str]] = None,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Register a tool whose successive calls return the scripted outcomes (the last one repeats)."""
    calls: List[Dict[str, Any]] = []

    async def executor(**kwargs: Any) -> Any:
        calls.append(kwargs)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        if callable(out):
            out = out(kwargs)
            if asyncio.iscoroutine(out):
                out = await out
        if isinstance(out, ToolResult):
            return out.model_copy(deep=True)
        return json.loads(json.dumps(out))

    reg.register(
        ToolUniverseEntry(
            name=name, description=f"scripted {name}", domain="test", adapter_type=adapter, input_schema=QueryInput,
            timeout_seconds=timeout, tags=tags or [], is_async=True,
        ),
        executor=executor,
    )
    return calls


def upstream_error(name: str = "t", msg: str = "HTTP 503 Service Unavailable") -> ToolResult:
    return ToolResult(success=False, tool_name=name, error=msg, error_type=ErrorType.UPSTREAM_API_ERROR)


def timeout_error(name: str = "t") -> ToolResult:
    return ToolResult(success=False, tool_name=name, error="exceeded timeout", error_type=ErrorType.TIMEOUT)


OK = {"results": [{"id": "hsa04010", "description": "MAPK signaling pathway - Homo sapiens (human)"}]}


# -----------------------------------------------------------------------------
# Defaults and scope
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_policies_default_off_under_pytest(monkeypatch):
    monkeypatch.delenv("PHRONESIS_RELIABILITY", raising=False)
    monkeypatch.delenv("PHRONESIS_TOOL_CACHE", raising=False)
    assert not rel.policies_enabled() and not rel.cache_enabled()
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error(), OK])
    res = await reg.execute("rest_tool", query="x")
    assert not res.success and len(calls) == 1 and "failure_class" not in res.metadata
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "on")
    assert rel.policies_enabled() and not rel.cache_enabled()  # the cache needs its own opt-in under pytest
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "off")
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE", "on")
    assert not rel.cache_enabled()  # the master switch wins


@pytest.mark.asyncio
async def test_local_compute_is_never_cached_or_retried():
    reg, calls = build_recording_registry(
        extra_tools={"fake_timeout": (QueryInput, lambda kw: timeout_error("fake_timeout"))}
    )
    for _ in range(2):
        res = await reg.execute("synapta_kegg_pathways", target_symbol="KRAS")
        assert res.success and "cache" not in res.metadata
    bad = await reg.execute("fake_timeout", query="x")
    assert bad.error_type == ErrorType.TIMEOUT and "failure_class" not in bad.metadata and "retries" not in bad.metadata
    assert [n for n, _ in calls] == ["synapta_kegg_pathways", "synapta_kegg_pathways", "fake_timeout"]
    snap = rel.reliability_snapshot()
    assert snap["breakers"] == {} and snap["cache"]["stores"] == 0 and snap["retries"]["total"] == 0


# -----------------------------------------------------------------------------
# Retries
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retries_with_exponential_backoff_and_full_jitter(policies):
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error(), timeout_error(), OK])
    res = await reg.execute("rest_tool", query="KRAS")
    assert res.success and len(calls) == 3 and res.metadata["retries"] == 2
    # Full jitter: uniform(0, min(cap, base * 2**attempt)) for attempts 0 and 1.
    assert policies["jitter"] == [(0.0, 0.5), (0.0, 1.0)]
    assert policies["sleeps"] == [0.25, 0.5]
    assert rel.reliability_snapshot()["retries"] == {"total": 2, "by_tool": {"rest_tool": 2}}


@pytest.mark.asyncio
async def test_retries_stop_after_the_configured_count(monkeypatch, policies):
    monkeypatch.setenv("PHRONESIS_TOOL_RETRIES", "1")
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error()])
    res = await reg.execute("rest_tool", query="KRAS")
    assert not res.success and len(calls) == 2 and res.metadata["retries"] == 1
    assert res.metadata["failure_class"] == "upstream"


@pytest.mark.asyncio
async def test_transport_exception_is_retried_but_other_failures_are_not():
    reg = BiomedicalToolRegistry()
    import httpx

    flaky = scripted_tool(reg, "flaky", [httpx.ConnectError("Connection refused"), OK])
    assert (await reg.execute("flaky", query="x")).success and len(flaky) == 2
    nf = scripted_tool(reg, "nf", [ToolResult(success=False, tool_name="nf", error="no record", error_type=ErrorType.NOT_FOUND)])
    assert (await reg.execute("nf", query="x")).error_type == ErrorType.NOT_FOUND and len(nf) == 1
    boom = scripted_tool(reg, "boom", [ValueError("bad payload")])
    res = await reg.execute("boom", query="x")
    assert res.error_type == ErrorType.INTERNAL_ERROR and len(boom) == 1
    bad = await reg.execute("nf", size=5)  # missing required 'query'
    assert bad.error_type == ErrorType.VALIDATION_ERROR and bad.metadata["failure_class"] == "bad_params" and len(nf) == 1


@pytest.mark.asyncio
async def test_retries_never_run_past_the_run_deadline(policies):
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error(), OK], timeout=10.0)
    with rel.run_deadline(2.0):  # a retry would need its backoff plus a 10 s timeout
        res = await reg.execute("rest_tool", query="x")
    assert not res.success and len(calls) == 1 and policies["sleeps"] == []
    with rel.run_deadline(60.0):
        res = await reg.execute("rest_tool", query="y")
    assert res.success


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_and_miss_use_validated_kwargs_and_return_copies():
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [OK])
    first = await reg.execute("rest_tool", query="KRAS")
    assert first.success and first.metadata["cache"] == "miss"
    second = await reg.execute("rest_tool", query="KRAS", size=5)  # the default spelled out: same key
    assert second.metadata["cache"] == "hit" and second.data == OK and len(calls) == 1
    second.data["results"].clear()  # mutating a hit must not corrupt the cache
    third = await reg.execute("rest_tool", query="KRAS")
    assert third.data == OK and third.metadata["cache"] == "hit"
    other = await reg.execute("rest_tool", query="BRAF")
    assert other.metadata["cache"] == "miss" and len(calls) == 2
    snap = rel.reliability_snapshot()["cache"]
    assert snap["hits"] == 2 and snap["misses"] == 2 and snap["stores"] == 2
    assert rel.cache_key("rest_tool", {"b": 1, "a": 2}) == rel.cache_key("rest_tool", {"a": 2, "b": 1})


@pytest.mark.asyncio
async def test_cache_ttl_expiry(monkeypatch, policies):
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [OK])
    await reg.execute("rest_tool", query="x")
    policies["wall"].t += 6 * 3600 - 1
    assert (await reg.execute("rest_tool", query="x")).metadata["cache"] == "hit"
    policies["wall"].t += 2
    assert (await reg.execute("rest_tool", query="x")).metadata["cache"] == "miss" and len(calls) == 2
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE_TTL_S", "0")
    res = await reg.execute("rest_tool", query="new")
    assert "cache" not in res.metadata


@pytest.mark.asyncio
async def test_cache_disabled_and_excluded_tools(monkeypatch):
    reg = BiomedicalToolRegistry()
    plain = scripted_tool(reg, "rest_tool", [OK])
    upload = scripted_tool(reg, "upload_tool", [OK], tags=["upload"])
    failing = scripted_tool(reg, "failing", [upstream_error()])
    for _ in range(2):
        assert "cache" not in (await reg.execute("upload_tool", query="x")).metadata
        await reg.execute("failing", query="x")
    assert len(upload) == 2 and len(failing) == 6  # failures are never cached (3 attempts each)
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE", "off")
    for _ in range(2):
        assert "cache" not in (await reg.execute("rest_tool", query="x")).metadata
    assert len(plain) == 2


@pytest.mark.asyncio
async def test_partial_payloads_are_not_cached():
    reg = BiomedicalToolRegistry()
    partial = {"items": [1], "sources": [{"name": "a", "status": "ok"}, {"name": "b", "status": "error"}]}
    calls = scripted_tool(reg, "rest_tool", [partial])
    await reg.execute("rest_tool", query="x")
    await reg.execute("rest_tool", query="x")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sqlite_store_survives_a_memory_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_RUNS_DIR", str(tmp_path / "runs"))  # default store: <runs>/tool_cache
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [OK])
    assert (await reg.execute("rest_tool", query="x")).metadata["cache"] == "miss"
    assert (tmp_path / "runs" / "tool_cache" / "tool_cache.sqlite3").is_file()
    rel.reset_reliability()  # a restarted process: memory empty, SQLite kept
    replay = await reg.execute("rest_tool", query="x")
    assert replay.metadata["cache"] == "hit" and replay.data == OK and len(calls) == 1
    rel.clear_cache(sqlite=True)
    rel.reset_reliability()
    assert (await reg.execute("rest_tool", query="x")).metadata["cache"] == "miss"


# -----------------------------------------------------------------------------
# Circuit breaker
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breaker_opens_fails_fast_half_opens_and_closes(monkeypatch, policies):
    monkeypatch.setenv("PHRONESIS_TOOL_RETRIES", "0")
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("PHRONESIS_BREAKER_COOLDOWN_S", "60")
    reg = BiomedicalToolRegistry()
    outcomes: List[Any] = [upstream_error(), timeout_error(), upstream_error()]
    calls = scripted_tool(reg, "rest_tool", [lambda kw: outcomes.pop(0) if outcomes else OK])
    for q in ("a", "b", "c"):
        assert not (await reg.execute("rest_tool", query=q)).success
    assert rel.reliability_snapshot()["breakers"]["rest_tool"]["state"] == "open"

    fast = await reg.execute("rest_tool", query="d")
    assert len(calls) == 3  # not called
    assert fast.error_type == ErrorType.UPSTREAM_API_ERROR and fast.metadata["breaker"] == "open"
    assert fast.metadata["failure_class"] == "no_call" and "Circuit breaker open" in fast.error

    policies["mono"].t += 61  # cool-down over: one trial call goes through and succeeds
    trial = await reg.execute("rest_tool", query="e")
    assert trial.success and len(calls) == 4
    snap = rel.reliability_snapshot()
    assert snap["breakers"]["rest_tool"]["state"] == "closed" and snap["fail_fast"] == 1
    assert snap["breakers"]["rest_tool"]["trips"] == 1


@pytest.mark.asyncio
async def test_failed_trial_reopens_without_retry(monkeypatch, policies):
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "1")
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error()])
    await reg.execute("rest_tool", query="a")  # 3 attempts (2 retries), then open
    assert len(calls) == 3 and rel.reliability_snapshot()["breakers"]["rest_tool"]["state"] == "open"
    policies["mono"].t += 61
    trial = await reg.execute("rest_tool", query="b")
    assert not trial.success and len(calls) == 4  # the half-open trial is a single call
    assert rel.reliability_snapshot()["breakers"]["rest_tool"]["state"] == "open"
    assert (await reg.execute("rest_tool", query="c")).metadata.get("breaker") == "open" and len(calls) == 4


@pytest.mark.asyncio
async def test_half_open_admits_exactly_one_concurrent_trial(monkeypatch, policies):
    monkeypatch.setenv("PHRONESIS_TOOL_RETRIES", "0")
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "1")
    reg = BiomedicalToolRegistry()
    gate = asyncio.Event()
    state = {"n": 0}

    async def outcome(kw):
        state["n"] += 1
        if state["n"] == 1:
            return upstream_error()
        await gate.wait()
        return OK

    calls = scripted_tool(reg, "rest_tool", [outcome])
    await reg.execute("rest_tool", query="a")
    policies["mono"].t += 61
    trial = asyncio.ensure_future(reg.execute("rest_tool", query="b"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    other = await reg.execute("rest_tool", query="c")
    assert other.metadata.get("breaker") == "open" and len(calls) == 2
    gate.set()
    assert (await trial).success
    assert rel.reliability_snapshot()["breakers"]["rest_tool"]["state"] == "closed"


@pytest.mark.asyncio
async def test_validation_errors_and_not_found_do_not_trip_the_breaker(monkeypatch):
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "1")
    reg = BiomedicalToolRegistry()
    scripted_tool(reg, "nf", [ToolResult(success=False, tool_name="nf", error="no record", error_type=ErrorType.NOT_FOUND)])
    for _ in range(3):
        await reg.execute("nf", query="x")
        await reg.execute("nf", size=1)  # validation error
    assert rel.reliability_snapshot()["breakers"]["nf"]["state"] == "closed"


@pytest.mark.asyncio
async def test_upstream_tag_shares_one_breaker(monkeypatch):
    monkeypatch.setenv("PHRONESIS_TOOL_RETRIES", "0")
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "2")
    reg = BiomedicalToolRegistry()
    a = scripted_tool(reg, "api_a", [upstream_error()], tags=["upstream:shared_api"])
    b = scripted_tool(reg, "api_b", [OK], tags=["upstream:shared_api"])
    await reg.execute("api_a", query="1")
    await reg.execute("api_a", query="2")
    res = await reg.execute("api_b", query="3")
    assert res.metadata.get("breaker") == "open" and res.metadata["upstream"] == "shared_api" and not b
    assert len(a) == 2 and "shared_api" in rel.reliability_snapshot()["breakers"]


# -----------------------------------------------------------------------------
# Fallbacks, telemetry, recorder
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_runs_only_while_the_breaker_is_open(monkeypatch):
    monkeypatch.setenv("PHRONESIS_TOOL_RETRIES", "0")
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "1")
    reg = BiomedicalToolRegistry()
    lit = scripted_tool(reg, "lit_tool", [upstream_error()])
    web = scripted_tool(reg, "web_tool", [{"results": [{"url": "https://example.org", "title": "t"}]}])
    rel.register_fallback("lit_tool", "web_tool", lambda kw: {"query": f"{kw['query']} review"})
    first = await reg.execute("lit_tool", query="KRAS")
    assert not first.success and not web  # breaker was closed: no fallback
    res = await reg.execute("lit_tool", query="KRAS")
    assert res.success and res.metadata["fallback_from"] == "lit_tool" and res.tool_name == "web_tool"
    assert web == [{"query": "KRAS review", "size": 5}] and len(lit) == 1
    assert rel.reliability_snapshot()["fallbacks_used"] == 1
    with pytest.raises(ValueError):
        rel.register_fallback("x", "x")


@pytest.mark.asyncio
async def test_failure_class_labels():
    reg = BiomedicalToolRegistry()
    scripted_tool(reg, "slow", [timeout_error()])
    scripted_tool(reg, "down", [upstream_error()])
    missing = await reg.execute("no_such_tool", query="x")
    assert missing.metadata["failure_class"] == "wrong_name"
    assert (await reg.execute("slow", query="x")).metadata["failure_class"] == "timeout"
    assert (await reg.execute("down", query="x")).metadata["failure_class"] == "upstream"
    assert (await reg.execute("down", nope=1)).metadata["failure_class"] == "bad_params"
    counts = rel.reliability_snapshot()["failures_by_class"]
    assert counts == {"wrong_name": 1, "timeout": 1, "upstream": 1, "bad_params": 1}
    assert rel.failure_class(ToolResult(success=True, tool_name="x")) is None


@pytest.mark.asyncio
async def test_recorder_sees_one_call_per_execute(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE", "on")
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "1")
    reg = BiomedicalToolRegistry()
    calls = scripted_tool(reg, "rest_tool", [upstream_error(), OK])
    scripted_tool(reg, "down", [upstream_error()])
    rec = rm.RunRecorder(query="q", thread_id="t", model_name="m", enable_web_search=False, user_id="alice", api_key_supplied=False)
    rec.persist = False
    token = rm.activate_recorder(rec)
    try:
        retried = await reg.execute("rest_tool", query="KRAS")  # 2 attempts
        hit = await reg.execute("rest_tool", query="KRAS")  # cache hit, no call
        await reg.execute("down", query="x")  # 3 attempts, breaker opens
        fast = await reg.execute("down", query="y")  # fail fast, no call
    finally:
        rm.reset_recorder(token)
    assert retried.success and hit.metadata["cache"] == "hit" and fast.metadata["breaker"] == "open" and len(calls) == 2
    m = await rec.finalize(response="", duration=0.1)
    names = [c["tool_name"] for c in m["tool_calls"]]
    assert names == ["rest_tool", "rest_tool", "down", "down"]
    hit_rec = m["tool_calls"][1]
    assert hit_rec["success"] and hit_rec["adapter"] == "DIRECT_REST" and hit_rec["inputs"] == {"query": "KRAS", "size": 5}
    assert m["tool_calls"][3]["adapter"] == "DIRECT_REST" and m["tool_calls"][3]["error_type"] == "UPSTREAM_API_ERROR"


@pytest.mark.asyncio
async def test_concurrent_sync_tools_share_state_safely(monkeypatch):
    reg = BiomedicalToolRegistry()
    seen: List[str] = []

    def sync_exec(query: str, size: int = 5) -> Dict[str, Any]:
        seen.append(query)
        return {"q": query}

    reg.register(
        ToolUniverseEntry(name="sync_rest", description="sync", domain="test", adapter_type=AdapterType.DIRECT_REST,
                          input_schema=QueryInput, timeout_seconds=5.0, is_async=False),
        executor=sync_exec,
    )
    results = await asyncio.gather(*(reg.execute("sync_rest", query=f"q{i % 5}") for i in range(40)))
    assert all(r.success and r.data == {"q": r.data["q"]} for r in results)
    again = await asyncio.gather(*(reg.execute("sync_rest", query=f"q{i}") for i in range(5)))
    assert all(r.metadata["cache"] == "hit" for r in again)
    snap = rel.reliability_snapshot()
    assert snap["cache"]["hits"] + snap["cache"]["misses"] == 45
    json.dumps(snap)  # JSON-safe for a health route


def test_snapshot_reports_config(monkeypatch):
    monkeypatch.setenv("PHRONESIS_BREAKER_THRESHOLD", "7")
    snap = rel.reliability_snapshot()
    assert snap["enabled"] and snap["config"]["breaker_threshold"] == 7 and snap["config"]["retries"] == 2
    assert snap["cache"]["memory_max"] == 512
