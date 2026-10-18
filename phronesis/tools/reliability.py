"""
Reliability policies around every registry tool call (feature 19): a result cache, retries with backoff,
per-upstream circuit breakers, fallbacks and failure telemetry. `BiomedicalToolRegistry.execute` hands every
call to `execute_with_policies`; `call()` re-runs one validated execution (`_execute_inner`) and the registry
records exactly one tool call per `execute`, whatever happened in here.

What applies to which tool:

- Only networked adapters (DIRECT_REST, DIRECT_GRAPHQL, REMOTE_MCP) get policies. LOCAL_COMPUTE tools (COPASI,
  Scanpy, PrimeKG, the sandbox, the test fakes) pass straight through: they are never cached, retried or broken.
- **Circuit breaker** (Fowler 2014, https://martinfowler.com/bliki/CircuitBreaker.html, after Nygard's
  *Release It!*): one breaker per upstream key, which is the tool name unless the entry carries a tag
  ``upstream:<name>`` (tools sharing one API share one breaker). Closed: calls go through and consecutive
  TIMEOUT / UPSTREAM_API_ERROR outcomes are counted. Open after PHRONESIS_BREAKER_THRESHOLD (5) of them: calls
  fail fast with ``metadata={"breaker": "open"}`` and are not made. Half-open after PHRONESIS_BREAKER_COOLDOWN_S
  (60 s): exactly one trial call goes through (no retries); success closes the breaker, failure re-opens it.
  A validation error never reached the upstream and is neutral; any other answer from the upstream (including
  NOT_FOUND) counts as the upstream working. Breakers are per upstream and shared by all users, so a streak only
  opens one when it shows the upstream itself is unhealthy (ORC-014): at least one input-independent failure
  (connect/DNS error, HTTP 502/503/504) or failures from two or more distinct callers (the run's user, hashed).
  One user's input-dependent timeouts are retried and counted but cannot fail-fast everyone else's calls.
- **Retries**: PHRONESIS_TOOL_RETRIES (2) extra attempts, only after TIMEOUT or UPSTREAM_API_ERROR (or an
  INTERNAL_ERROR whose message is an httpx transport failure), with exponential backoff and full jitter
  (delay = uniform(0, min(4 s, 0.5 s * 2**attempt))). Never past a run deadline set with `run_deadline()`.
- **Cache**: in-memory LRU (512 entries) plus an optional SQLite store in PHRONESIS_TOOL_CACHE_DIR (default
  ``<PHRONESIS_RUNS_DIR>/tool_cache`` when the runs dir is set, else ``<PHRONESIS_DATA_DIR>/tool_cache``, else
  memory only), so a resumed job (F13) replays completed upstream calls. Expired rows are deleted the first
  time a process opens the store and by the periodic maintenance sweep (`purge_expired_cache`, which also
  VACUUMs once a quarter of the file is free pages). TTL per adapter: 6 h for REST and GraphQL, 1 h for remote MCP
  (PHRONESIS_TOOL_CACHE_TTL_S overrides both). Key = tool name + sha256 of the canonical JSON (sorted keys,
  default=str) of the validated kwargs (the tool's input schema applied here, so defaults never split
  entries; kwargs that fail validation are never cached). Only successful, complete results are stored (a
  payload whose per-source statuses report a failure is not), never LOCAL_COMPUTE and never tools tagged
  "upload" or "user_data". A hit returns a deep copy with ``metadata["cache"] = "hit"``; a stored miss has
  ``metadata["cache"] = "miss"``. The cache is shared across users on purpose: only networked public-API tools
  are cached, keyed by their validated inputs alone, and tools that read user data (tags "upload", "user_data")
  are excluded, so a hit can only replay a public answer to identical public inputs (ORC-014). Payloads that
  report an upstream error (``upstream_failed`` / a top-level ``error`` other than NOT_FOUND) or a failed
  per-source status (list- or mapping-shaped) are never stored (ORC-001, ORC-012).
- **Fallbacks**: `register_fallback(tool, fallback_tool, arg_mapper)`; used only while the tool's breaker is
  open, the result carries ``metadata["fallback_from"]``. None is registered by default: e.g. a web-search
  fallback for literature would ignore a researcher who switched web search off.
- **Telemetry**: failed results get ``metadata["failure_class"]`` from the API-Bank error taxonomy (Li et al.,
  EMNLP 2023, arXiv:2304.08244): ``no_call`` (the call was not made: breaker open), ``wrong_name`` (tool not
  registered), ``bad_params`` (input validation, invalid identifier), ``upstream`` (upstream error or other
  failure of a networked tool), ``timeout``; LOCAL_COMPUTE failures are not labelled. `reliability_snapshot()`
  returns breaker states, cache stats, retry and failure counts for a health route. The fault-injection tests
  (tests/test_reliability.py) follow ToolEmu (Ruan et al., ICLR 2024, arXiv:2309.15817) and ToolSword
  (Ye et al., ACL 2024, arXiv:2402.10753): emulated timeouts, 5xx outages and noisy results.

Switches: PHRONESIS_RELIABILITY=off|auto|on (auto = on, except while pytest runs a test, so suites that count
calls on fake registries or expect a raw TIMEOUT are unaffected; tests opt in with "on"), PHRONESIS_TOOL_CACHE
=off|auto|on (same auto rule), PHRONESIS_TOOL_RETRIES, PHRONESIS_BREAKER_THRESHOLD, PHRONESIS_BREAKER_COOLDOWN_S,
PHRONESIS_TOOL_CACHE_TTL_S, PHRONESIS_TOOL_CACHE_DIR.

Concurrency: the registry runs on one event loop while sync tools run in worker threads, and tests run several
loops, so shared state is guarded by a threading lock that is never held across an await (asyncio locks are
bound to one loop). The half-open trial slot is claimed atomically under that lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, Optional, Tuple

from phronesis.config.settings import SWITCH, get_float, get_int, get_str

logger = logging.getLogger(__name__)

# Indirection points for tests (no real sleeping, deterministic jitter, controllable clocks).
_sleep = asyncio.sleep
_uniform = random.uniform
_now = time.time  # wall clock: cache expiry survives a restart through SQLite
_monotonic = time.monotonic  # breaker cool-down and run deadline

BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 4.0
MEMORY_CACHE_MAX = 512
_TTL_DEFAULTS_S = {"DIRECT_REST": 6 * 3600.0, "DIRECT_GRAPHQL": 6 * 3600.0, "REMOTE_MCP": 3600.0}
_NETWORKED = frozenset(_TTL_DEFAULTS_S)
_NO_CACHE_TAGS = frozenset({"upload", "uploads", "user_data"})
_TRANSPORT_ERROR_RE = re.compile(
    r"\b(ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|ReadError|RemoteProtocolError|"
    r"NetworkError|TimeoutException|Connection (?:refused|reset|aborted)|Temporary failure in name resolution)\b"
)


# -----------------------------------------------------------------------------
# Configuration (read per call so tests and operators can change it at runtime)
# -----------------------------------------------------------------------------

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    return get_int(name, default, min=lo, max=hi)


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    return get_float(name, default, min=lo, max=hi)


def _switch(name: str) -> bool:
    """off | auto | on (also 0/1, false/true, no/yes); auto and unknown values mean on outside pytest."""
    value = get_str(name, "auto", choices=SWITCH)
    if value == "off":
        return False
    if value == "on":
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def policies_enabled() -> bool:
    return _switch("PHRONESIS_RELIABILITY")


def cache_enabled() -> bool:
    return policies_enabled() and _switch("PHRONESIS_TOOL_CACHE")


def _max_retries() -> int:
    return _env_int("PHRONESIS_TOOL_RETRIES", 2, 0, 5)


def _threshold() -> int:
    return _env_int("PHRONESIS_BREAKER_THRESHOLD", 5, 1, 100)


def _cooldown_s() -> float:
    return _env_float("PHRONESIS_BREAKER_COOLDOWN_S", 60.0, 0.0, 3600.0)


def _ttl_s(adapter: str) -> float:
    override = os.getenv("PHRONESIS_TOOL_CACHE_TTL_S", "").strip()
    if override:
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    return _TTL_DEFAULTS_S.get(adapter, 0.0)


def _cache_dir() -> Optional[Path]:
    explicit = os.getenv("PHRONESIS_TOOL_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    runs = os.getenv("PHRONESIS_RUNS_DIR", "").strip()
    if runs:
        return Path(runs) / "tool_cache"
    from phronesis.utils.paths import data_dir

    return data_dir("tool_cache")


# Optional deadline for the current run (monotonic seconds); retries never sleep past it.
_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar("phronesis_tool_deadline", default=None)


@contextlib.contextmanager
def run_deadline(seconds: Optional[float]) -> Iterator[None]:
    """Within this block no retry is attempted that could end after `seconds` from now (None = no deadline)."""
    token = _DEADLINE.set(None if seconds is None else _monotonic() + max(0.0, float(seconds)))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

_LOCK = threading.Lock()


@dataclass
class _Breaker:
    state: str = "closed"  # closed | open | half_open
    failures: int = 0  # consecutive counted failures
    opened_at: float = 0.0
    trial_in_flight: bool = False
    trips: int = 0
    last_error: str = ""
    # ORC-014: who contributed to the current failure streak, and whether any failure was input-independent
    # (connection refused, DNS, 502/503/504). A streak of only input-dependent failures (timeouts, other upstream
    # errors) from a single caller never opens the shared breaker, so one user's slow inputs cannot deny the
    # upstream to everyone else.
    callers: set = field(default_factory=set)
    hard_failure: bool = False

    def to_dict(self, now: float) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state, "consecutive_failures": self.failures, "trips": self.trips}
        if self.failures:
            out["distinct_callers"] = len(self.callers)
        if self.state == "open":
            out["retry_after_s"] = round(max(0.0, _cooldown_s() - (now - self.opened_at)), 1)
        if self.last_error:
            out["last_error"] = self.last_error
        return out


@dataclass
class _Stats:
    cache_hits: int = 0
    cache_misses: int = 0
    cache_stores: int = 0
    cache_evictions: int = 0
    sqlite_errors: int = 0
    retries: int = 0
    fail_fast: int = 0
    fallbacks: int = 0
    retries_by_tool: Dict[str, int] = field(default_factory=dict)
    failures_by_class: Dict[str, int] = field(default_factory=dict)


_BREAKERS: Dict[str, _Breaker] = {}
_MEMORY: "OrderedDict[str, Tuple[float, float, Any]]" = OrderedDict()  # key -> (expires, stored_at, ToolResult)
_STATS = _Stats()
_FALLBACKS: Dict[str, Tuple[str, Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]]] = {}


def reset_reliability(*, fallbacks: bool = True) -> None:
    """Forget breaker states, the in-memory cache and statistics (tests); the SQLite store is left alone."""
    global _STATS
    with _LOCK:
        _BREAKERS.clear()
        _MEMORY.clear()
        _STATS = _Stats()
        if fallbacks:
            _FALLBACKS.clear()


def register_fallback(
    tool_name: str,
    fallback_tool: str,
    arg_mapper: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    """While tool_name's breaker is open, run fallback_tool with arg_mapper(kwargs) (identity when None)."""
    if not tool_name or not fallback_tool or tool_name == fallback_tool:
        raise ValueError("a fallback needs two different tool names")
    with _LOCK:
        _FALLBACKS[tool_name] = (fallback_tool, arg_mapper)


def unregister_fallback(tool_name: str) -> None:
    with _LOCK:
        _FALLBACKS.pop(tool_name, None)


def reliability_snapshot() -> Dict[str, Any]:
    """JSON-safe view of breakers, cache and retry statistics (for a health or agent route)."""
    now = _monotonic()
    with _LOCK:
        stats = _STATS
        return {
            "enabled": policies_enabled(),
            "breakers": {k: b.to_dict(now) for k, b in sorted(_BREAKERS.items())},
            "cache": {
                "enabled": cache_enabled(),
                "memory_entries": len(_MEMORY),
                "memory_max": MEMORY_CACHE_MAX,
                "sqlite_dir": str(_cache_dir()) if _cache_dir() is not None else None,
                "hits": stats.cache_hits,
                "misses": stats.cache_misses,
                "stores": stats.cache_stores,
                "evictions": stats.cache_evictions,
                "sqlite_errors": stats.sqlite_errors,
            },
            "retries": {"total": stats.retries, "by_tool": dict(stats.retries_by_tool)},
            "fail_fast": stats.fail_fast,
            "fallbacks_used": stats.fallbacks,
            "failures_by_class": dict(stats.failures_by_class),
            "fallbacks": {k: v[0] for k, v in sorted(_FALLBACKS.items())},
            "config": {
                "retries": _max_retries(),
                "breaker_threshold": _threshold(),
                "breaker_cooldown_s": _cooldown_s(),
            },
        }


# -----------------------------------------------------------------------------
# Classification
# -----------------------------------------------------------------------------

def _etype(result: Any) -> str:
    et = getattr(result, "error_type", None)
    return str(getattr(et, "value", et) or "")


_HARD_FAILURE_RE = re.compile(
    r"\b(ConnectError|ConnectTimeout|Connection (?:refused|reset|aborted)|Temporary failure in name resolution|"
    r"getaddrinfo failed|Name or service not known|50[234]\b|Bad Gateway|Service Unavailable|Gateway Timeout)",
    re.IGNORECASE,
)


def _is_hard_failure(result: Any) -> bool:
    """An upstream failure that does not depend on the request's inputs: the service is unreachable or reports
    itself unavailable (connect/DNS errors, HTTP 502/503/504). A timeout or another upstream error may be caused
    by an expensive input, so it is 'soft' (ORC-014)."""
    if not _is_upstream_failure(result):
        return False
    return bool(_HARD_FAILURE_RE.search(str(getattr(result, "error", "") or "")))


def _caller_id() -> str:
    """A stable, non-identifying id of the user on whose behalf this call runs ('anonymous' when unknown)."""
    user = ""
    try:
        from phronesis.provenance.run_manifest import current_recorder

        rec = current_recorder()
        user = str(getattr(rec, "user_id", "") or "") if rec is not None else ""
    except Exception:  # noqa: BLE001
        user = ""
    if not user:
        try:
            from phronesis.agentic.sandbox import current_scope

            scope = current_scope()
            user = str(getattr(scope, "user_id", "") or "") if scope is not None else ""
        except Exception:  # noqa: BLE001
            user = ""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:16] if user else "anonymous"


def _is_upstream_failure(result: Any) -> bool:
    """TIMEOUT / UPSTREAM_API_ERROR, or an executor that raised an httpx transport error."""
    if result is None or getattr(result, "success", False):
        return False
    et = _etype(result)
    if et in ("TIMEOUT", "UPSTREAM_API_ERROR"):
        return True
    return et == "INTERNAL_ERROR" and bool(_TRANSPORT_ERROR_RE.search(str(getattr(result, "error", "") or "")))


def failure_class(result: Any, networked: bool = True) -> Optional[str]:
    """API-Bank-style label for a failed result (None for a success)."""
    if result is None:
        return "upstream" if networked else "internal"
    if getattr(result, "success", False):
        return None
    meta = getattr(result, "metadata", None) or {}
    if meta.get("breaker") == "open":
        return "no_call"
    et = _etype(result)
    if et == "NOT_FOUND" and "not registered" in str(getattr(result, "error", "") or ""):
        return "wrong_name"
    if et in ("VALIDATION_ERROR", "INVALID_IDENTIFIER"):
        return "bad_params"
    if et == "TIMEOUT":
        return "timeout"
    if _is_upstream_failure(result) or networked:
        return "upstream"
    return "internal"


def _label(result: Any, networked: bool) -> Any:
    cls = failure_class(result, networked)
    if cls is not None and isinstance(getattr(result, "metadata", None), dict):
        result.metadata.setdefault("failure_class", cls)
        with _LOCK:
            _STATS.failures_by_class[cls] = _STATS.failures_by_class.get(cls, 0) + 1
    return result


_BENIGN_STATUSES = frozenset({"ok", "skipped", "not_applicable", "empty", "no_data", "not_found", "success_empty", "not_run"})


def _bad_status(value: Any) -> bool:
    """A per-source status (a string, or a dict with a ``status`` key) that reports a failure."""
    if isinstance(value, dict):
        value = value.get("status")
    if not isinstance(value, str):  # e.g. a mapping of source -> result list: not a status container
        return False
    status = value.strip().lower()
    return bool(status) and status not in _BENIGN_STATUSES


def _looks_partial(data: Any) -> bool:
    """A 'successful' payload that reports a failed sub-source, or an upstream error, must not be replayed for hours.

    Recognised: ``partial: true``; ``upstream_failed: true`` or a top-level ``error`` (error-as-data executors such
    as the BioContext client, ORC-001), unless it is marked ``error_type: NOT_FOUND`` (a real "no match" answer);
    per-source statuses in ``sources`` / ``source_status`` given as a list of ``{"status": ...}`` items or as a
    mapping ``{source: status}`` (the entity resolver's shape, ORC-012)."""
    if not isinstance(data, dict):
        return False
    if data.get("partial") is True or data.get("upstream_failed") is True:
        return True
    if data.get("error") and str(data.get("error_type") or "").upper() != "NOT_FOUND":
        return True
    for key in ("sources", "source_status"):
        items = data.get(key)
        if isinstance(items, list):
            if any(_bad_status(s) for s in items if isinstance(s, dict)):
                return True
        elif isinstance(items, dict):
            if any(_bad_status(v) for v in items.values()):
                return True
    return False


# -----------------------------------------------------------------------------
# Breakers
# -----------------------------------------------------------------------------

def upstream_key(tool_name: str, entry: Any) -> str:
    for tag in getattr(entry, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith("upstream:") and len(tag) > 9:
            return tag[9:]
    return tool_name


def _claim(key: str) -> Tuple[bool, bool, float]:
    """(allowed, is_trial, retry_after_s) for one call through the breaker at key."""
    now = _monotonic()
    with _LOCK:
        b = _BREAKERS.setdefault(key, _Breaker())
        if b.state == "closed":
            return True, False, 0.0
        if b.state == "open":
            waited = now - b.opened_at
            if waited < _cooldown_s():
                return False, False, _cooldown_s() - waited
            b.state, b.trial_in_flight = "half_open", True
            return True, True, 0.0
        if b.trial_in_flight:  # half-open: one trial at a time
            return False, False, 0.0
        b.trial_in_flight = True
        return True, True, 0.0


def _settle(
    key: str, outcome: str, is_trial: bool, error: str = "", *, hard: bool = True, caller: Optional[str] = None
) -> None:
    """outcome: 'success' (the upstream answered), 'failure' (counted), 'neutral' (never reached it).

    A failure streak opens the breaker at the threshold only when it shows the upstream itself is unhealthy: at
    least one hard (input-independent) failure, or failures seen by two or more distinct callers. A single
    caller's soft failures (timeouts on expensive inputs) are still counted and retried but cannot open the
    breaker shared by every user (ORC-014). A failed half-open trial always re-opens it."""
    now = _monotonic()
    with _LOCK:
        b = _BREAKERS.setdefault(key, _Breaker())
        if is_trial:
            b.trial_in_flight = False
        if outcome == "success":
            b.state, b.failures, b.last_error = "closed", 0, ""
            b.callers, b.hard_failure = set(), False
        elif outcome == "failure":
            b.failures += 1
            b.last_error = error[:200]
            b.callers.add(caller or "anonymous")
            b.hard_failure = b.hard_failure or hard
            upstream_unhealthy = b.hard_failure or len(b.callers) >= 2
            if is_trial or (b.failures >= _threshold() and upstream_unhealthy):
                if b.state != "open":
                    b.trips += 1
                b.state, b.opened_at = "open", now
        # neutral: a half-open breaker stays half-open with its trial slot released


def _outcome(result: Any) -> str:
    if result is None:
        return "failure"
    if getattr(result, "success", False):
        return "success"
    if _is_upstream_failure(result):
        return "failure"
    return "neutral" if _etype(result) in ("VALIDATION_ERROR", "INTERNAL_ERROR") else "success"


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

def canonical_kwargs(kwargs: Dict[str, Any]) -> str:
    return json.dumps(kwargs, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))


def cache_key(tool_name: str, kwargs: Dict[str, Any]) -> str:
    return f"{tool_name}:{hashlib.sha256(canonical_kwargs(kwargs).encode('utf-8')).hexdigest()}"


def _validated(entry: Any, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return entry.input_schema(**kwargs).model_dump()
    except Exception:  # noqa: BLE001
        return None


def _cacheable(entry: Any) -> bool:
    tags = {str(t).lower() for t in getattr(entry, "tags", None) or []}
    return not (tags & _NO_CACHE_TAGS) and _ttl_s(_adapter(entry)) > 0


def _db_path() -> Optional[Path]:
    d = _cache_dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / "tool_cache.sqlite3"


_SQLITE_LOCK = threading.Lock()
_PURGED_PATHS: set = set()  # stores whose expired rows this process already deleted on first open
_CACHE_SQL = "CREATE TABLE IF NOT EXISTS tool_cache (key TEXT PRIMARY KEY, tool TEXT, expires REAL, stored REAL, payload TEXT)"


def _sqlite(op: Callable[[sqlite3.Connection], Any]) -> Any:
    path = _db_path()
    if path is None:
        return None
    try:
        with _SQLITE_LOCK:
            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                conn.execute(_CACHE_SQL)
                if str(path) not in _PURGED_PATHS:  # startup retention: drop rows that expired while down
                    _PURGED_PATHS.add(str(path))
                    conn.execute("DELETE FROM tool_cache WHERE expires < ?", (_now(),))
                out = op(conn)
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATS.sqlite_errors += 1
        logger.debug("Tool cache SQLite operation failed: %s", exc)
        return None
    try:
        from phronesis.utils.maintenance import schedule_maintenance

        schedule_maintenance()
    except Exception:  # noqa: BLE001  # maintenance never fails a tool call
        logger.debug("Maintenance scheduling failed", exc_info=True)
    return out


def purge_expired_cache(*, vacuum: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Delete expired SQLite cache rows; with vacuum, VACUUM when >= 25% of the file is free pages."""
    out: Dict[str, Any] = {"deleted": 0, "vacuumed": False}
    d = _cache_dir()
    path = d / "tool_cache.sqlite3" if d is not None else None
    if path is None or not path.is_file():
        return out
    try:
        with _SQLITE_LOCK:
            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                conn.execute(_CACHE_SQL)
                out["deleted"] = conn.execute(
                    "DELETE FROM tool_cache WHERE expires < ?", (_now() if now is None else now,)
                ).rowcount
                conn.commit()
                if vacuum:
                    pages = conn.execute("PRAGMA page_count").fetchone()[0] or 0
                    free = conn.execute("PRAGMA freelist_count").fetchone()[0] or 0
                    if free and free * 4 >= pages:
                        conn.execute("VACUUM")
                        out["vacuumed"] = True
            finally:
                conn.close()
    except sqlite3.Error as exc:
        logger.debug("Tool cache purge failed: %s", exc)
    return out


def _cache_get(key: str) -> Optional[Tuple[Any, float]]:
    """(a deep copy of the cached ToolResult, its age in seconds) or None."""
    now = _now()
    with _LOCK:
        item = _MEMORY.get(key)
        if item is not None:
            expires, stored, res = item
            if expires >= now:
                _MEMORY.move_to_end(key)
                return res.model_copy(deep=True), now - stored
            del _MEMORY[key]
    row = _sqlite(lambda c: c.execute("SELECT expires, stored, payload FROM tool_cache WHERE key = ?", (key,)).fetchone())
    if not row:
        return None
    expires, stored, payload = row
    if expires < now:
        _sqlite(lambda c: c.execute("DELETE FROM tool_cache WHERE key = ?", (key,)))
        return None
    try:
        from .registry import ToolResult

        res = ToolResult.model_validate_json(payload)
    except Exception:  # noqa: BLE001
        return None
    _memory_put(key, expires, stored, res)
    return res.model_copy(deep=True), now - stored


def _memory_put(key: str, expires: float, stored: float, res: Any) -> None:
    with _LOCK:
        _MEMORY[key] = (expires, stored, res)
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > MEMORY_CACHE_MAX:
            _MEMORY.popitem(last=False)
            _STATS.cache_evictions += 1


def _cache_put(key: str, tool_name: str, res: Any, ttl: float) -> None:
    stored = _now()
    expires = stored + ttl
    try:
        copy = res.model_copy(deep=True)
    except Exception:  # noqa: BLE001  # a payload that cannot be copied is not cached
        return
    copy.metadata.pop("cache", None)
    copy.metadata.pop("retries", None)
    _memory_put(key, expires, stored, copy)
    with _LOCK:
        _STATS.cache_stores += 1
    try:
        payload = copy.model_dump_json()
    except Exception:  # noqa: BLE001  # a payload that is not JSON-safe stays in memory only
        return
    _sqlite(
        lambda c: c.execute(
            "INSERT OR REPLACE INTO tool_cache (key, tool, expires, stored, payload) VALUES (?, ?, ?, ?, ?)",
            (key, tool_name, expires, stored, payload),
        )
    )


def clear_cache(*, sqlite: bool = False) -> None:
    with _LOCK:
        _MEMORY.clear()
    if sqlite:
        _sqlite(lambda c: c.execute("DELETE FROM tool_cache"))


# -----------------------------------------------------------------------------
# Execution
# -----------------------------------------------------------------------------

def _adapter(entry: Any) -> str:
    at = getattr(entry, "adapter_type", None)
    return str(getattr(at, "value", at) or "")


def _fill_trace(
    call: Callable[..., Any],
    kwargs: Dict[str, Any],
    entry: Any,
    validated: Optional[Dict[str, Any]],
    trace: Optional[Dict[str, Any]] = None,
) -> None:
    """When call() never ran (cache hit, fail-fast), give the run recorder the entry and validated inputs.

    Uses the trace dict the registry passed; otherwise (older callers) the dict call() closes over, best effort.
    """
    if trace is not None:
        trace.setdefault("entry", entry)
        if validated is not None:
            trace.setdefault("validated_kwargs", validated)
        return
    for cell in getattr(call, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, dict) and value is not kwargs and (not value or "entry" in value or "validated_kwargs" in value):
            value.setdefault("entry", entry)
            if validated is not None:
                value.setdefault("validated_kwargs", validated)
            return


def _fail_fast(tool_name: str, entry: Any, key: str, retry_after: float) -> Any:
    from .registry import ErrorType, ToolResult

    with _LOCK:
        _STATS.fail_fast += 1
    return ToolResult(
        success=False,
        tool_name=tool_name,
        error=(
            f"Circuit breaker open for upstream '{key}' after repeated timeouts or upstream errors; the call was "
            f"not made (retry in about {max(1, int(retry_after))}s)."
        ),
        error_type=ErrorType.UPSTREAM_API_ERROR,
        metadata={"adapter": _adapter(entry), "breaker": "open", "upstream": key},
    )


async def _run_fallback(registry: Any, tool_name: str, kwargs: Dict[str, Any]) -> Optional[Any]:
    with _LOCK:
        fb = _FALLBACKS.get(tool_name)
    if fb is None:
        return None
    fallback_tool, mapper = fb
    get_entry = getattr(registry, "get_entry", None)
    if not callable(get_entry) or get_entry(fallback_tool) is None:
        return None
    try:
        mapped = dict(mapper(dict(kwargs))) if mapper is not None else dict(kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.info("Fallback argument mapping %s -> %s failed: %s", tool_name, fallback_tool, exc)
        return None
    trace: Dict[str, Any] = {}
    res = await execute_with_policies(
        registry, fallback_tool, mapped, lambda: registry._execute_inner(fallback_tool, mapped, trace), _allow_fallback=False
    )
    if res is not None and isinstance(getattr(res, "metadata", None), dict):
        res.metadata["fallback_from"] = tool_name
        with _LOCK:
            _STATS.fallbacks += 1
    return res


def _may_retry(attempt: int, delay: float, entry: Any) -> bool:
    if attempt >= _max_retries():
        return False
    deadline = _DEADLINE.get()
    if deadline is None:
        return True
    timeout = float(getattr(entry, "timeout_seconds", 0.0) or 0.0)
    return _monotonic() + delay + timeout <= deadline


async def execute_with_policies(
    registry: Any,
    tool_name: str,
    kwargs: Dict[str, Any],
    call: Callable[[], Awaitable[Any]],
    *,
    trace: Optional[Dict[str, Any]] = None,
    _allow_fallback: bool = True,
) -> Any:
    """Run call() (one validated registry execution) under the cache, breaker, retry and fallback policies.

    trace is the registry's per-call trace dict; when call() never runs (cache hit, fail-fast) it is filled here
    so the run recorder still sees the entry and validated inputs.
    """
    if not policies_enabled():
        return await call()
    get_entry = getattr(registry, "get_entry", None)
    entry = get_entry(tool_name) if callable(get_entry) else None
    if entry is None:
        return _label(await call(), networked=False)  # NOT_FOUND: wrong_name
    adapter = _adapter(entry)
    if adapter not in _NETWORKED:
        return await call()  # LOCAL_COMPUTE: no cache, no retry, no breaker

    validated = _validated(entry, kwargs)
    key: Optional[str] = None
    if validated is not None and cache_enabled() and _cacheable(entry):
        key = cache_key(tool_name, validated)
        # Re-runs re-fetch from the upstream (PRV-003): a replay that read the cache would certify nothing.
        from ..provenance.run_manifest import cache_reads_disabled

        hit = None if cache_reads_disabled() else _cache_get(key)
        if hit is not None:
            res, age = hit
            res.metadata["cache"] = "hit"
            res.metadata["cache_age_s"] = round(age, 1)
            with _LOCK:
                _STATS.cache_hits += 1
            _fill_trace(call, kwargs, entry, validated, trace)
            return res
        with _LOCK:
            _STATS.cache_misses += 1

    upstream = upstream_key(tool_name, entry)
    allowed, is_trial, retry_after = _claim(upstream)
    if not allowed:
        if _allow_fallback:
            fb = await _run_fallback(registry, tool_name, kwargs)
            if fb is not None:
                _fill_trace(call, kwargs, entry, validated, trace)
                return fb
        _fill_trace(call, kwargs, entry, validated, trace)
        return _label(_fail_fast(tool_name, entry, upstream, retry_after), networked=True)

    attempt = 0
    res: Any = None
    settled = False
    try:
        while True:
            res = await call()
            if not _is_upstream_failure(res) or is_trial:
                break
            delay = _uniform(0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt)))
            if validated is None or not _may_retry(attempt, delay, entry):
                break
            attempt += 1
            with _LOCK:
                _STATS.retries += 1
                _STATS.retries_by_tool[tool_name] = _STATS.retries_by_tool.get(tool_name, 0) + 1
            await _sleep(delay)
        _settle(
            upstream, _outcome(res), is_trial, str(getattr(res, "error", "") or ""),
            hard=_is_hard_failure(res), caller=_caller_id(),
        )
        settled = True
    finally:
        if not settled and is_trial:  # cancelled mid-trial: release the slot, keep the breaker half-open
            _settle(upstream, "neutral", True)

    if res is None:
        return res
    if attempt and isinstance(getattr(res, "metadata", None), dict):
        res.metadata["retries"] = attempt
    if getattr(res, "success", False):
        if key is not None and not _looks_partial(getattr(res, "data", None)):
            _cache_put(key, tool_name, res, _ttl_s(adapter))
        if key is not None:
            res.metadata["cache"] = "miss"
        return res
    return _label(res, networked=True)


__all__ = [
    "cache_enabled",
    "cache_key",
    "canonical_kwargs",
    "clear_cache",
    "execute_with_policies",
    "failure_class",
    "policies_enabled",
    "register_fallback",
    "reliability_snapshot",
    "reset_reliability",
    "run_deadline",
    "unregister_fallback",
    "upstream_key",
]
