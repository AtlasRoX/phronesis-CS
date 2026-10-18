"""
Record and replay of answer-level runs (cassettes), so the golden suite can run offline and deterministically.

One cassette is a JSON file per case: phronesis/evals/cassettes/<suite>/<case_id>.json (written as .json.gz when it
would exceed 2 MB). It has three channels, each keyed by a hash of the call, with a list of recorded results per key
(replayed in order; when a key is called more often than it was recorded, the last result is reused):

- tools: every registry tool call, hooked at tools.reliability.execute_with_policies, which
  BiomedicalToolRegistry.execute looks up on each call. Above that hook the registry still validates and repairs the
  arguments and writes the run manifest, so a replayed run's manifest, canonical ids and evidence are produced by the
  real code; below it the cache, circuit breakers, retries and the executor are replaced by the recorded ToolResult.
  Key: (tool name, canonical JSON of the arguments without volatile keys). Duck-typed registries (tests) can be
  hooked at their own execute.
- llm: every chat model the orchestrator obtains through get_llm (looked up as a module attribute in every loaded
  phronesis module, so the hook does not depend on the orchestrator's internal layout), whether it is called with
  ainvoke, invoke, astream or astream_with_reasoning. Key: (model, the normalised prompt messages). Replay never
  builds a real client, so it needs no API key.
- calls: side lookups made outside the registry (the BioModels search, the run manifest's upstream version probes).

Prompts and arguments are normalised before hashing: ISO timestamps, today's (and the recording's) date, uuids,
run/job ids, eval thread ids and Unix timestamps are replaced by placeholders, so the retrieval date in a prompt does
not break replay. Secrets (API keys, Authorization headers, bearer tokens, the values of *_KEY/*_TOKEN environment
variables) are redacted from everything that is stored. Only what replay needs is kept: results, and for prompts a
per-message hash plus a short preview (enough to explain a miss), not the full text.

Calls made inside a recorded tool (an extension tool's own model call) are not recorded: replay never runs the tool.

Replay is strict: a call with no recorded result raises CassetteMiss and is counted, and the case fails its
answer.cassette check even when the orchestrator recovers from the exception. Lenient replay (--replay-lenient) answers
a missed model call with the most similar recorded reply of the same model and a missed tool call with a failed
ToolResult, and reports the misses without failing. During replay, outbound network connections are refused and
reported, so an unhooked network path shows up as a failure instead of a silent live call.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as _dt
import difflib
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CASSETTE_VERSION = 1
DEFAULT_DIR = Path(__file__).parent / "cassettes"
GZIP_ABOVE_BYTES = 2 * 1024 * 1024
PREVIEW_CHARS = 160
REPLAY_CHUNK_CHARS = 240

# Applied for the whole record or replay run, so a backend that depends on the local machine (a cross-encoder in
# the HF cache, a sentence-transformer) does not make CI take a different path from the recording.
PINNED_ENV: Dict[str, str] = {"PHRONESIS_NLI": "llm", "PHRONESIS_TOOL_EMBEDDINGS": ""}

# Tool arguments that never identify the call.
VOLATILE_KEYS = frozenset({"request_id", "run_id", "trace_id", "timestamp", "started_at", "_repair_arguments"})


class CassetteMiss(RuntimeError):
    """A call during replay that the cassette has no recorded result for."""


class CassetteRecordedError(RuntimeError):
    """Base of the exceptions replayed from a recording (each replay raises a subclass with the original name)."""


_ACTIVE: contextvars.ContextVar[Optional["Cassette"]] = contextvars.ContextVar("phronesis_eval_cassette", default=None)
_IN_TOOL: contextvars.ContextVar[bool] = contextvars.ContextVar("phronesis_eval_cassette_in_tool", default=False)


def active_cassette() -> Optional["Cassette"]:
    return _ACTIVE.get()


# ---------------------------------------------------------------------------------------------------------------
# Normalisation and redaction
# ---------------------------------------------------------------------------------------------------------------

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_RUN_ID_RE = re.compile(r"\b(run|job|apr|thread|req|trace|span)_[0-9a-fA-F]{8,}\b")
_EVAL_THREAD_RE = re.compile(r"\beval-[0-9a-f]{8}-")
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")
_UNIX_TS_RE = re.compile(r"(?<![\d.])1[5-9]\d{8}(?:\d{3})?(?:\.\d+)?(?![\d])")


def _date_forms(day: _dt.date) -> List[str]:
    month = _MONTHS[day.month - 1]
    return [day.isoformat(), f"{month} {day.day}, {day.year}", f"{day.day} {month} {day.year}",
            f"{month[:3]} {day.day}, {day.year}", f"{day.month:02d}/{day.day:02d}/{day.year}"]


def normalize(text: str, dates: Sequence[_dt.date] = ()) -> str:
    """Replace volatile substrings (timestamps, run dates, uuids, run ids) by placeholders, for hashing."""
    out = _DATETIME_RE.sub("<datetime>", text)
    for day in {*dates, _dt.date.today()}:
        for form in _date_forms(day):
            out = out.replace(form, "<today>")
    out = _UUID_RE.sub("<uuid>", out)
    out = _RUN_ID_RE.sub(lambda m: f"{m.group(1)}_<id>", out)
    out = _EVAL_THREAD_RE.sub("eval-<run>-", out)
    out = _HEX32_RE.sub("<hex>", out)
    out = _UNIX_TS_RE.sub("<ts>", out)
    return out


_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:.*[_-])?(api[_-]?key|apikey|authorization|proxy[_-]authorization|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|bearer|token|secret|client[_-]?secret|password|passwd|cookie|set-cookie)$"
)
_SECRET_VALUE_RES = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=\-]{12,}")
_URL_SECRET_RE = re.compile(r"(?i)([?&](?:api_?key|apikey|key|token|access_token|auth)=)[^&\s\"'#]+")
REDACTED = "<redacted>"


def _env_secrets() -> List[str]:
    out = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            out.append(v)
    return sorted(set(out), key=len, reverse=True)


def redact_text(text: str, env_secrets: Optional[Sequence[str]] = None) -> str:
    for value in env_secrets if env_secrets is not None else _env_secrets():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub(REDACTED, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _URL_SECRET_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def redact(value: Any, env_secrets: Optional[Sequence[str]] = None) -> Any:
    """A JSON-like value with secret-named keys and secret-looking strings replaced by <redacted>."""
    secrets = env_secrets if env_secrets is not None else _env_secrets()
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY_RE.match(k) and v not in (None, "") else redact(v, secrets))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def to_jsonable(value: Any) -> Any:
    """A JSON round-trip of value (pydantic models, enums, sets, numpy scalars become plain JSON)."""
    try:
        from pydantic import BaseModel

        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        pass
    return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            return obj.model_dump()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(obj, "value") and type(obj).__module__ != "builtins":
        return obj.value
    return str(obj)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in VOLATILE_KEYS and not _SECRET_KEY_RE.match(str(k))}


def _message_parts(messages: Any) -> List[Tuple[str, str]]:
    """(role, content) for LangChain messages, OpenAI-style dicts, (role, content) tuples or a plain string."""
    if isinstance(messages, str):
        return [("human", messages)]
    out: List[Tuple[str, str]] = []
    for m in messages or []:
        if isinstance(m, dict):
            role, content = str(m.get("role") or m.get("type") or ""), m.get("content")
        elif isinstance(m, (tuple, list)) and len(m) == 2:
            role, content = str(m[0]), m[1]
        else:
            role, content = str(getattr(m, "type", type(m).__name__)), getattr(m, "content", m)
        out.append((role, content if isinstance(content, str) else _canonical(content)))
    return out


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def _reasoning_of(message: Any) -> str:
    extra = getattr(message, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning"):
        if extra.get(key):
            return str(extra[key])
    return ""


def _recorded_exception(entry: Dict[str, Any]) -> BaseException:
    name = str(entry.get("error_type") or "Error")
    message = str(entry.get("error") or "")
    if name in ("TimeoutError", "CancelledError"):
        return asyncio.TimeoutError(message or "recorded timeout")
    safe = re.sub(r"\W", "_", name) or "Error"
    return type(safe, (CassetteRecordedError,), {})(message)


def _error_entry(exc: BaseException) -> Dict[str, Any]:
    if isinstance(exc, asyncio.CancelledError):
        return {"error_type": "TimeoutError", "error": "cancelled while recording (caller timeout)"}
    return {"error_type": type(exc).__name__, "error": str(exc)[:500]}


# ---------------------------------------------------------------------------------------------------------------
# The cassette
# ---------------------------------------------------------------------------------------------------------------

def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value or "case") or "case"


def cassette_path(directory: Path | str, suite: str, case_id: str) -> Path:
    return Path(directory) / safe_name(Path(suite).stem if suite.endswith(".jsonl") else suite) / f"{safe_name(case_id)}.json"


def find_cassette(directory: Path | str, suite: str, case_id: str) -> Optional[Path]:
    """The cassette file of a case (.json or .json.gz), or None."""
    path = cassette_path(directory, suite, case_id)
    for candidate in (path, path.with_name(path.name + ".gz")):
        if candidate.exists():
            return candidate
    return None


def list_cassettes(directory: Path | str = DEFAULT_DIR) -> List[Path]:
    root = Path(directory)
    return sorted([*root.glob("*/*.json"), *root.glob("*/*.json.gz")]) if root.exists() else []


class Cassette:
    """The recorded calls of one case; mode is "record" or "replay"."""

    def __init__(self, path: Path | str, mode: str, *, lenient: bool = False, data: Optional[Dict[str, Any]] = None,
                 registries: Sequence[Any] = ()) -> None:
        if mode not in ("record", "replay"):
            raise ValueError(f"cassette mode must be record or replay, not {mode!r}")
        self.path = Path(path)
        self.mode = mode
        self.lenient = lenient
        self.data: Dict[str, Any] = data or {"version": CASSETTE_VERSION, "meta": {}, "tools": {}, "llm": {}, "calls": {},
                                             "factory_errors": {}}
        for channel in ("tools", "llm", "calls", "factory_errors"):
            self.data.setdefault(channel, {})
        self.registries = list(registries)
        # Full normalised prompts make a miss easy to diagnose but the file large: off unless asked for.
        self.store_prompts = os.getenv("PHRONESIS_CASSETTE_PROMPTS", "").strip().lower() in ("1", "true", "yes", "on")
        self._lock = threading.Lock()
        self._cursor: Dict[Tuple[str, str], int] = {}
        self._secrets = _env_secrets()
        recorded = self.meta.get("recorded_date")
        self._dates: List[_dt.date] = []
        if recorded:
            try:
                self._dates.append(_dt.date.fromisoformat(str(recorded)))
            except ValueError:
                pass
        self.stats: Dict[str, Any] = {"mode": mode, "tool_hits": 0, "llm_hits": 0, "call_hits": 0, "reused": 0,
                                      "recorded": 0, "misses": [], "lenient": [], "network": []}

    # -- persistence ---------------------------------------------------------------------------------------------
    @property
    def meta(self) -> Dict[str, Any]:
        return self.data.setdefault("meta", {})

    @classmethod
    def load(cls, path: Path | str, *, lenient: bool = False, registries: Sequence[Any] = ()) -> "Cassette":
        path = Path(path)
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if int(data.get("version") or 0) != CASSETTE_VERSION:
            raise ValueError(f"{path}: cassette version {data.get('version')} (expected {CASSETTE_VERSION}); re-record it")
        return cls(path, "replay", lenient=lenient, data=data, registries=registries)

    def save(self, path: Optional[Path | str] = None) -> Path:
        """Write the cassette (gzip when larger than 2 MB); the other variant of the file is removed."""
        target = Path(path) if path else self.path
        if target.suffix == ".gz":
            target = target.with_suffix("")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.meta.setdefault("recorded_date", _dt.date.today().isoformat())
        # Key order is kept: recorded tool data reaches prompts, where the order of its keys matters.
        body = json.dumps(self.data, indent=1, ensure_ascii=False).encode("utf-8")
        gz = target.with_name(target.name + ".gz")
        if len(body) > GZIP_ABOVE_BYTES:
            gz.write_bytes(gzip.compress(body, mtime=0))
            target.unlink(missing_ok=True)
            self.path = gz
        else:
            target.write_bytes(body)
            gz.unlink(missing_ok=True)
            self.path = target
        return self.path

    # -- activation ----------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def activate(self) -> Iterator["Cassette"]:
        """Route hooked calls in this context (and the tasks it starts) through the cassette."""
        _HOOKS.acquire(self)
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)
            _HOOKS.release(self)

    # -- bookkeeping ---------------------------------------------------------------------------------------------
    def _norm(self, text: str) -> str:
        # Pattern-based redaction only: a key must not depend on which secrets this machine's environment holds.
        return redact_text(normalize(text, self._dates), ())

    def _append(self, channel: str, key: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.data[channel].setdefault(key, []).append(redact(to_jsonable(entry), self._secrets))
            self.stats["recorded"] += 1

    def _take(self, channel: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entries = self.data[channel].get(key)
            if not entries:
                return None
            i = self._cursor.get((channel, key), 0)
            self._cursor[(channel, key)] = i + 1
            if i >= len(entries):
                self.stats["reused"] += 1
            return entries[min(i, len(entries) - 1)]

    def _miss(self, kind: str, detail: str) -> None:
        with self._lock:
            (self.stats["lenient"] if self.lenient else self.stats["misses"]).append(f"{kind}: {detail}"[:600])

    def summary(self) -> Dict[str, Any]:
        """Counts for the case's observations (deterministic for a replay)."""
        s = self.stats
        out = {"mode": self.mode, "file": self.path.name, "tool_hits": s["tool_hits"], "llm_hits": s["llm_hits"],
               "call_hits": s["call_hits"], "reused": s["reused"], "misses": list(s["misses"]),
               "lenient_misses": list(s["lenient"]), "network_blocked": list(s["network"])}
        if self.mode == "record":
            out["recorded"] = s["recorded"]
        return out

    @property
    def failures(self) -> List[str]:
        """What makes a strict replay fail: misses and refused network connections."""
        return [*self.stats["misses"], *(f"network: {h}" for h in self.stats["network"])]

    # -- tools ---------------------------------------------------------------------------------------------------
    def tool_key(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        return _sha(f"{tool_name}\n{self._norm(_canonical(_clean_kwargs(kwargs)))}")

    async def tool_call(self, tool_name: str, kwargs: Dict[str, Any], trace: Optional[Dict[str, Any]],
                        run: Callable[[], Any], registry: Any = None) -> Any:
        from ..tools.registry import ErrorType, ToolResult

        key = self.tool_key(tool_name, kwargs)
        if self.mode == "record":
            token = _IN_TOOL.set(True)
            try:
                res = await run()
            except BaseException as exc:
                self._append("tools", key, {"tool": tool_name, "kwargs": _clean_kwargs(kwargs), **_error_entry(exc)})
                raise
            finally:
                _IN_TOOL.reset(token)
            validated = (trace or {}).get("validated_kwargs")
            dumped = _dump_result(res)
            self._append("tools", key, {"tool": tool_name, "kwargs": _clean_kwargs(kwargs), "validated_kwargs": validated,
                                        "result": dumped, "result_type": "ToolResult" if isinstance(res, ToolResult) else "raw"})
            return res

        await asyncio.sleep(0)  # a replayed call still yields, as a real one would
        entry = self._take("tools", key)
        if entry is None:
            detail = f"tool {tool_name} {self._norm(_canonical(_clean_kwargs(kwargs)))[:300]}"
            self._miss("tool", detail)
            if not self.lenient:
                raise CassetteMiss(f"no recorded result for {detail} (re-record the case with --record)")
            return ToolResult(success=False, tool_name=tool_name, error="cassette miss: no recorded result",
                              error_type=ErrorType.INTERNAL_ERROR, metadata={"cassette": "miss"})
        with self._lock:
            self.stats["tool_hits"] += 1
        if trace is not None:
            get_entry = getattr(registry, "get_entry", None)
            if callable(get_entry) and "entry" not in trace:
                trace["entry"] = get_entry(tool_name)
            if entry.get("validated_kwargs") is not None:
                trace["validated_kwargs"] = entry["validated_kwargs"]
        if "error_type" in entry and "result" not in entry:
            raise _recorded_exception(entry)
        result = entry.get("result")
        if entry.get("result_type") == "ToolResult" and isinstance(result, dict):
            return ToolResult(**result)
        return result

    # -- side calls outside the registry -------------------------------------------------------------------------
    def _call_key(self, name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
        return _sha(f"{name}\n{self._norm(_canonical([list(args), kwargs]))}")

    async def async_call(self, name: str, fn: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        key = self._call_key(name, args, kwargs)
        if self.mode == "record":
            try:
                out = await fn(*args, **kwargs)
            except BaseException as exc:
                self._append("calls", key, {"call": name, **_error_entry(exc)})
                raise
            self._append("calls", key, {"call": name, "args": [list(args), kwargs], "result": out})
            return out
        await asyncio.sleep(0)
        return self._replay_call(name, key, args, kwargs)

    def _replay_call(self, name: str, key: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        entry = self._take("calls", key)
        if entry is None:
            detail = f"call {name}{self._norm(_canonical([list(args), kwargs]))[:200]}"
            self._miss("call", detail)
            if not self.lenient:
                raise CassetteMiss(f"no recorded result for {detail}")
            return None
        with self._lock:
            self.stats["call_hits"] += 1
        if "error_type" in entry and "result" not in entry:
            raise _recorded_exception(entry)
        return entry.get("result")

    def sync_call(self, name: str, fn: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        key = self._call_key(name, args, kwargs)
        if self.mode == "record":
            try:
                out = fn(*args, **kwargs)
            except BaseException as exc:
                self._append("calls", key, {"call": name, **_error_entry(exc)})
                raise
            self._append("calls", key, {"call": name, "args": [list(args), kwargs], "result": out})
            return out
        return self._replay_call(name, key, args, kwargs)

    # -- models --------------------------------------------------------------------------------------------------
    def llm_key(self, model: str, messages: Any) -> Tuple[str, List[Tuple[str, str]]]:
        parts = [(role, self._norm(content)) for role, content in _message_parts(messages)]
        return _sha(_canonical([model, parts])), parts

    def _prompt_info(self, parts: List[Tuple[str, str]], *, full: bool = False) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "roles": [r for r, _ in parts], "hashes": [_sha(c)[:16] for _, c in parts], "chars": [len(c) for _, c in parts],
            "preview": [c[:PREVIEW_CHARS] + (" ... " + c[-PREVIEW_CHARS:] if len(c) > 2 * PREVIEW_CHARS else c[PREVIEW_CHARS:])
                        for _, c in parts]}
        if full:
            info["messages"] = [c for _, c in parts]
        return info

    def make_llm(self, factory: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> "CassetteLLM":
        model = _resolve_model(args, kwargs)
        if self.mode == "record":
            try:
                inner = factory(*args, **kwargs)
            except Exception as exc:
                with self._lock:
                    self.data["factory_errors"][model] = {"error_type": type(exc).__name__, "error": redact_text(str(exc)[:300])}
                raise
            return CassetteLLM(self, model, kwargs, inner)
        failed = self.data["factory_errors"].get(model)
        if failed:
            raise _recorded_exception(failed)
        return CassetteLLM(self, model, kwargs, None)

    def _replay_llm(self, model: str, messages: Any) -> Dict[str, Any]:
        key, parts = self.llm_key(model, messages)
        entry = self._take("llm", key)
        if entry is not None:
            with self._lock:
                self.stats["llm_hits"] += 1
            if "error_type" in entry and "text" not in entry:
                raise _recorded_exception(entry)
            return entry
        near, why = self._nearest(model, parts)
        info = self._prompt_info(parts, full=True)
        phase = (info["preview"][0][:60] if info["preview"] else "").replace("\n", " ")
        detail = f"model {model}, {len(parts)} message(s), first: {phase!r}; {why}"
        self._miss("llm", detail)
        if not self.lenient or near is None:
            raise CassetteMiss(f"no recorded reply for {detail} (re-record the case with --record)")
        if "error_type" in near and "text" not in near:
            raise _recorded_exception(near)
        return near

    def _nearest(self, model: str, parts: List[Tuple[str, str]]) -> Tuple[Optional[Dict[str, Any]], str]:
        """The most similar recorded reply of the same model, and which message differs from it."""
        info = self._prompt_info(parts, full=True)
        best, best_score, best_diff = None, -1.0, ""
        for entries in self.data["llm"].values():
            for e in entries:
                if e.get("model") != model:
                    continue
                prompt = e.get("prompt") or {}
                hashes = prompt.get("hashes") or []
                same = sum(1 for a, b in zip(hashes, info["hashes"]) if a == b)
                ratio = difflib.SequenceMatcher(None, " ".join(prompt.get("preview") or []), " ".join(info["preview"])).ratio()
                score = same * 2 + ratio + (1 if len(hashes) == len(info["hashes"]) else 0)
                if score > best_score:
                    differs = [i for i, (a, b) in enumerate(zip(hashes, info["hashes"])) if a != b]
                    best, best_score = e, score
                    best_diff = (f"nearest recording differs in message(s) {differs}" if differs else
                                 f"nearest recording has {len(hashes)} message(s)")
                    recorded = prompt.get("messages")
                    if differs and recorded and differs[0] < len(recorded):
                        a, b = recorded[differs[0]], info["messages"][differs[0]]
                        at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                        best_diff += f" at char {at}: recorded {a[max(0, at - 40):at + 60]!r} vs now {b[max(0, at - 40):at + 60]!r}"
        if best is None:
            return None, "no recording for this model"
        return best, best_diff

    def _record_llm(self, model: str, messages: Any, kind: str, *, text: Any = None, reasoning: str = "",
                    exc: Optional[BaseException] = None) -> None:
        key, parts = self.llm_key(model, messages)
        entry: Dict[str, Any] = {"model": model, "kind": kind, "prompt": self._prompt_info(parts, full=self.store_prompts)}
        if exc is not None:
            entry.update(_error_entry(exc))
        else:
            entry["text"] = text
            if reasoning:
                entry["reasoning"] = reasoning
        self._append("llm", key, entry)

    async def stream_with_reasoning(self, original: Callable[..., Any], llm: "CassetteLLM", messages: Any,
                                    on_reasoning: Any, on_text: Any) -> Tuple[str, str]:
        if self.mode == "record":
            try:
                text, reasoning = await original(llm.inner, messages, on_reasoning=on_reasoning, on_text=on_text)
            except BaseException as exc:
                self._record_llm(llm.model_name, messages, "stream", exc=exc)
                raise
            self._record_llm(llm.model_name, messages, "stream", text=text, reasoning=reasoning)
            return text, reasoning
        entry = self._replay_llm(llm.model_name, messages)
        text, reasoning = _content_text(entry.get("text")), str(entry.get("reasoning") or "")
        for chunk in _chunks(reasoning):
            if on_reasoning:
                await on_reasoning(chunk)
        for chunk in _chunks(text):
            if on_text:
                await on_text(chunk)
        return text, reasoning


def _dump_result(res: Any) -> Any:
    if not hasattr(res, "model_dump"):
        return res
    try:
        return res.model_dump(mode="json")
    except Exception:  # noqa: BLE001  (an executor returned a value pydantic cannot serialise)
        return to_jsonable(res.model_dump())


def _chunks(text: str) -> List[str]:
    return [text[i:i + REPLAY_CHUNK_CHARS] for i in range(0, len(text), REPLAY_CHUNK_CHARS)]


def _resolve_model(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
    model = kwargs.get("model_name") or (args[0] if args and isinstance(args[0], str) else None)
    if model:
        return str(model)
    try:
        from ..utils.llm.llm_factory import DEFAULT_ORCHESTRATOR_MODEL
    except Exception:  # noqa: BLE001
        DEFAULT_ORCHESTRATOR_MODEL = "default"
    return os.getenv("AIAGENTS4PHARMA_LLM_MODEL") or DEFAULT_ORCHESTRATOR_MODEL


class CassetteLLM:
    """Chat model stand-in: records the wrapped model's replies, or replays them without any client."""

    def __init__(self, cassette: Cassette, model: str, kwargs: Dict[str, Any], inner: Any) -> None:
        self._cassette = cassette
        self.model_name = model
        self.model = model
        self.inner = inner
        self.temperature = getattr(inner, "temperature", kwargs.get("temperature", 0.0)) if inner is not None else kwargs.get("temperature", 0.0)
        self.max_tokens = kwargs.get("max_tokens")
        self.streaming = bool(kwargs.get("streaming", False))

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("inner")
        if inner is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(inner, name)

    @staticmethod
    def _message(text: Any, reasoning: str) -> Any:
        from langchain_core.messages import AIMessage

        return AIMessage(content=text if isinstance(text, (str, list)) else _content_text(text),
                         additional_kwargs={"reasoning_content": reasoning} if reasoning else {})

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        c = self._cassette
        if c.mode == "record":
            try:
                reply = await self.inner.ainvoke(messages, *args, **kwargs)
            except BaseException as exc:
                c._record_llm(self.model_name, messages, "invoke", exc=exc)
                raise
            c._record_llm(self.model_name, messages, "invoke", text=getattr(reply, "content", reply), reasoning=_reasoning_of(reply))
            return reply
        await asyncio.sleep(0)
        entry = c._replay_llm(self.model_name, messages)
        return self._message(entry.get("text"), str(entry.get("reasoning") or ""))

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        c = self._cassette
        if c.mode == "record":
            try:
                reply = self.inner.invoke(messages, *args, **kwargs)
            except BaseException as exc:
                c._record_llm(self.model_name, messages, "invoke", exc=exc)
                raise
            c._record_llm(self.model_name, messages, "invoke", text=getattr(reply, "content", reply), reasoning=_reasoning_of(reply))
            return reply
        entry = c._replay_llm(self.model_name, messages)
        return self._message(entry.get("text"), str(entry.get("reasoning") or ""))

    async def astream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessageChunk

        c = self._cassette
        if c.mode == "record":
            if not hasattr(self.inner, "astream"):
                yield AIMessageChunk(content=_content_text(getattr(await self.ainvoke(messages), "content", "")))
                return
            text_parts: List[str] = []
            reasoning_parts: List[str] = []
            try:
                async for chunk in self.inner.astream(messages, *args, **kwargs):
                    text_parts.append(_content_text(getattr(chunk, "content", "")))
                    reasoning_parts.append(_reasoning_of(chunk))
                    yield chunk
            except BaseException as exc:
                c._record_llm(self.model_name, messages, "stream", exc=exc)
                raise
            c._record_llm(self.model_name, messages, "stream", text="".join(text_parts), reasoning="".join(reasoning_parts))
            return
        entry = c._replay_llm(self.model_name, messages)
        reasoning = str(entry.get("reasoning") or "")
        for chunk in _chunks(reasoning):
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": chunk})
        for chunk in _chunks(_content_text(entry.get("text"))):
            yield AIMessageChunk(content=chunk)


# ---------------------------------------------------------------------------------------------------------------
# Hooks: installed while at least one cassette is active
# ---------------------------------------------------------------------------------------------------------------

_LLM_NAMES = ("get_llm", "astream_with_reasoning")
_POLICY_NAME = "execute_with_policies"
# Synchronous lookups the orchestrator makes outside the registry: (module, class, static method).
SYNC_TARGETS: List[Tuple[str, str, str]] = [("phronesis.vivarix.engine", "VivarixSimulationEngine", "search_models")]
# Async lookups outside the registry, called through their module attribute: (module, function). The run manifest's
# upstream version probes run in a background task of the run.
ASYNC_TARGETS: List[Tuple[str, str]] = [("phronesis.provenance.source_versions", "get_source_versions"),
                                        ("phronesis.provenance.source_versions", "resolve_biomodel_pin")]
# Imported before the hooks scan the loaded modules, so names they bind with "from ... import" are found.
_PRELOAD = ("phronesis.utils.llm.llm_factory", "phronesis.tools.reliability", "phronesis.tools.registry",
            "phronesis.agents.authoritative_orchestrator", "phronesis.agents.orchestration")


def registry_tool_names(registry: Any) -> List[str]:
    """Names of the tools a registry has (empty when it cannot list them)."""
    try:
        return sorted(e.name for e in registry.list_tools())
    except Exception:  # noqa: BLE001
        return []


def _phronesis_modules() -> List[Any]:
    return [m for name, m in list(sys.modules.items())
            if m is not None and (name == "phronesis" or name.startswith("phronesis.")) and not name.startswith("phronesis.evals")]


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]")
    if host in ("", "localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _call_site() -> str:
    """The innermost phronesis frame outside the harness (module:function:line), or ""."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if name.startswith("phronesis.") and not name.startswith("phronesis.evals"):
            return f"{name}:{frame.f_code.co_name}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


class _Hooks:
    """Process-wide patches, reference-counted; each patched call looks up the cassette of its own context."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refs = 0
        self._replay_refs = 0
        self._restore: List[Tuple[Any, str, Any]] = []
        self._wrappers: Dict[int, Any] = {}  # id(wrapper) -> original
        self._instance_restore: Dict[int, Tuple[Any, Any, int]] = {}  # id(registry) -> (registry, original, refs)
        self._replaying: List[Cassette] = []
        self._net_restore: List[Tuple[Any, str, Any]] = []

    # -- install / release ----------------------------------------------------------------------------------------
    def acquire(self, cassette: Cassette) -> None:
        with self._lock:
            if self._refs == 0:
                self._install()
            self._refs += 1
            for reg in cassette.registries:
                self._patch_instance(reg)
            if cassette.mode == "replay":
                if self._replay_refs == 0:
                    self._install_network_guard()
                self._replay_refs += 1
                self._replaying.append(cassette)

    def release(self, cassette: Cassette) -> None:
        with self._lock:
            for reg in cassette.registries:
                self._unpatch_instance(reg)
            if cassette.mode == "replay":
                self._replay_refs -= 1
                if cassette in self._replaying:
                    self._replaying.remove(cassette)
                if self._replay_refs == 0:
                    self._remove_network_guard()
            self._refs -= 1
            if self._refs == 0:
                self._uninstall()

    def _set(self, owner: Any, name: str, value: Any) -> None:
        self._restore.append((owner, name, owner.__dict__[name] if isinstance(owner, type) else getattr(owner, name)))
        setattr(owner, name, value)

    def _install(self) -> None:
        import importlib

        for mod in _PRELOAD:
            try:
                importlib.import_module(mod)
            except Exception:  # noqa: BLE001
                pass
        wrapped: Dict[Tuple[str, int], Any] = {}
        for module in _phronesis_modules():
            for name in (*_LLM_NAMES, _POLICY_NAME):
                value = module.__dict__.get(name)
                if not callable(value) or id(value) in self._wrappers:
                    continue
                if name == _POLICY_NAME and not module.__name__.startswith("phronesis.tools"):
                    continue
                key = (name, id(value))
                if key not in wrapped:
                    wrapped[key] = self._make_wrapper(name, value)
                    self._wrappers[id(wrapped[key])] = value
                self._set(module, name, wrapped[key])
        for mod_name, cls_name, attr in SYNC_TARGETS:
            try:
                cls = getattr(importlib.import_module(mod_name), cls_name)
                raw = cls.__dict__[attr]
            except Exception:  # noqa: BLE001
                continue
            self._set(cls, attr, self._wrap_sync(f"{cls_name}.{attr}", raw))
        for mod_name, attr in ASYNC_TARGETS:
            try:
                module = importlib.import_module(mod_name)
                fn = module.__dict__[attr]
            except Exception:  # noqa: BLE001
                continue
            self._set(module, attr, self._wrap_async(f"{mod_name.rsplit('.', 1)[-1]}.{attr}", fn))

    @staticmethod
    def _wrap_async(name: str, fn: Any) -> Any:
        async def async_hook(*args: Any, **kwargs: Any) -> Any:
            cas = _ACTIVE.get()
            if cas is None or _IN_TOOL.get():
                return await fn(*args, **kwargs)
            return await cas.async_call(name, fn, args, kwargs)

        return async_hook

    def _uninstall(self) -> None:
        while self._restore:
            owner, name, value = self._restore.pop()
            setattr(owner, name, value)
        # A module imported while the hooks were in place bound a wrapper with "from ... import": restore it too.
        for module in _phronesis_modules():
            for name in (*_LLM_NAMES, _POLICY_NAME):
                value = module.__dict__.get(name)
                if value is not None and id(value) in self._wrappers:
                    setattr(module, name, self._wrappers[id(value)])
        self._wrappers.clear()

    def _make_wrapper(self, name: str, original: Any) -> Any:
        if name == "get_llm":
            def get_llm_hook(*args: Any, **kwargs: Any) -> Any:
                cas = _ACTIVE.get()
                if cas is None or _IN_TOOL.get():
                    return original(*args, **kwargs)
                return cas.make_llm(original, args, kwargs)

            return get_llm_hook
        if name == "astream_with_reasoning":
            async def astream_hook(llm: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(llm, CassetteLLM):
                    on_reasoning = kwargs.pop("on_reasoning", args[0] if len(args) > 0 else None)
                    on_text = kwargs.pop("on_text", args[1] if len(args) > 1 else None)
                    return await llm._cassette.stream_with_reasoning(original, llm, messages, on_reasoning, on_text)
                return await original(llm, messages, *args, **kwargs)

            return astream_hook

        async def policies_hook(registry: Any, tool_name: str, kwargs: Dict[str, Any], call: Any, *args: Any, **kw: Any) -> Any:
            cas = _ACTIVE.get()
            if cas is None or _IN_TOOL.get():
                return await original(registry, tool_name, kwargs, call, *args, **kw)
            return await cas.tool_call(tool_name, kwargs, kw.get("trace"),
                                       lambda: original(registry, tool_name, kwargs, call, *args, **kw), registry=registry)

        return policies_hook

    @staticmethod
    def _wrap_sync(name: str, raw: Any) -> Any:
        fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw

        def sync_hook(*args: Any, **kwargs: Any) -> Any:
            cas = _ACTIVE.get()
            if cas is None or _IN_TOOL.get():
                return fn(*args, **kwargs)
            return cas.sync_call(name, fn, args, kwargs)

        return staticmethod(sync_hook) if isinstance(raw, staticmethod) else sync_hook

    # -- duck-typed registries (tests) ---------------------------------------------------------------------------
    def _patch_instance(self, registry: Any) -> None:
        held = self._instance_restore.get(id(registry))
        if held is not None:
            self._instance_restore[id(registry)] = (held[0], held[1], held[2] + 1)
            return
        original = registry.execute

        async def execute_hook(tool_name: str, **kwargs: Any) -> Any:
            cas = _ACTIVE.get()
            if cas is None or _IN_TOOL.get():
                return await original(tool_name, **kwargs)
            return await cas.tool_call(tool_name, kwargs, None, lambda: original(tool_name, **kwargs), registry=registry)

        registry.execute = execute_hook
        self._instance_restore[id(registry)] = (registry, original, 1)

    def _unpatch_instance(self, registry: Any) -> None:
        held = self._instance_restore.get(id(registry))
        if held is None:
            return
        if held[2] > 1:
            self._instance_restore[id(registry)] = (held[0], held[1], held[2] - 1)
            return
        del self._instance_restore[id(registry)]
        try:
            del registry.execute  # the instance attribute; the class method shows through again
        except AttributeError:
            registry.execute = held[1]

    # -- network guard (replay) ----------------------------------------------------------------------------------
    def _blocked(self, host: Any) -> OSError:
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        site = _call_site()
        label = f"{host} (from {site})" if site else str(host)
        logger.warning("Cassette replay refused a network connection to %s", label)
        cas = _ACTIVE.get()
        targets = [cas] if cas is not None and cas.mode == "replay" else list(self._replaying)
        for c in targets:
            with c._lock:
                c.stats["network"].append(label[:200])
        return OSError(f"network access is disabled during cassette replay (host {host!r}); this call is not recorded")

    def _install_network_guard(self) -> None:
        guard = self
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not _is_loopback(host):
                raise guard._blocked(host)
            return real_getaddrinfo(host, *args, **kwargs)

        def _check(sock: Any, address: Any) -> None:
            if sock.family in (socket.AF_INET, getattr(socket, "AF_INET6", -1)) and isinstance(address, tuple):
                if not _is_loopback(address[0]):
                    raise guard._blocked(address[0])

        def connect(sock: Any, address: Any) -> Any:
            _check(sock, address)
            return real_connect(sock, address)

        def connect_ex(sock: Any, address: Any) -> Any:
            _check(sock, address)
            return real_connect_ex(sock, address)

        self._net_restore = [(socket, "getaddrinfo", real_getaddrinfo), (socket.socket, "connect", real_connect),
                             (socket.socket, "connect_ex", real_connect_ex)]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]

    def _remove_network_guard(self) -> None:
        while self._net_restore:
            owner, name, value = self._net_restore.pop()
            setattr(owner, name, value)


_HOOKS = _Hooks()


@contextlib.contextmanager
def pinned_env(env: Optional[Dict[str, str]] = None) -> Iterator[None]:
    """Set PINNED_ENV (or env) for the duration; previous values are restored."""
    env = PINNED_ENV if env is None else env
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def new_recording(path: Path | str, meta: Dict[str, Any], *, registries: Sequence[Any] = ()) -> Cassette:
    cas = Cassette(path, "record", registries=registries)
    cas.meta.update(meta)
    cas.meta.update({"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "recorded_date": _dt.date.today().isoformat(),
                     "env": dict(PINNED_ENV)})
    cas._dates = [_dt.date.today()]
    return cas


__all__ = ["CASSETTE_VERSION", "Cassette", "CassetteLLM", "CassetteMiss", "CassetteRecordedError", "DEFAULT_DIR",
           "PINNED_ENV", "active_cassette", "cassette_path", "find_cassette", "list_cassettes", "new_recording",
           "normalize", "pinned_env", "redact", "redact_text", "registry_tool_names"]
