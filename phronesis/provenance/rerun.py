"""
Re-run a recorded inquiry and diff it against the original (owned by EXPORT, feature 9).

mode=tools replays the exact recorded ToolCallRecords through the registry (no LLM): differences then come
from upstream data drift or code changes. mode=full re-runs the whole inquiry (done by the router); the
answer text is compared as a unified diff because LLM output is not deterministic.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import time
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field

from .run_manifest import (
    RunRecorder,
    activate_recorder,
    bypass_tool_cache,
    canonical_json_sha256,
    client_view,
    reset_recorder,
    restore_tool_cache,
    utc_now,
)

IDENTICAL_TOL = 1e-9
WITHIN_TOL = 0.05  # the verifier's numeric tolerance
MAX_BIOMODEL_PINS = 5
_REMOTE_ADAPTERS = {"DIRECT_REST", "DIRECT_GRAPHQL", "REMOTE_MCP"}

# -----------------------------------------------------------------------------
# Replay policy (SEC-001): one definition shared by the MCP endpoint and both re-run endpoints
# -----------------------------------------------------------------------------

# Tools that write, upload, execute code, dock/generate (paid GPU calls), calibrate against uploaded data or
# export are never run as a direct, LLM-free RPC, whatever registry or client asks for them.
UNSAFE_TOOL_RE = re.compile(r"(upload|write|delete|remove|dock|generate|run_python|sandbox|calibrat|export)", re.I)
# Registry tags that mark a tool as reading per-user data or executing code.
UNSAFE_TOOL_TAGS = frozenset({"upload", "uploads", "user_data", "sandbox", "code_execution", "write"})
# Input keys that name server files. A client manifest may only carry `dataset_path`, and only inside the typed
# dataset roots (the same policy as a path typed into a question); every other path-valued input is refused.
_PATH_KEY_RE = re.compile(r"(^|_)(path|paths|file|files|dir|directory|filename)$", re.I)
_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2}[^\\/]|~[\\/]|file:)")


class ReplayPolicyError(ValueError):
    """A recorded tool call that the replay policy refuses to execute."""


def tool_replay_refusal(name: str, entry: Any) -> Optional[str]:
    """Why a tool may not be replayed (None when it may): unregistered, unsafe name or unsafe registry tags."""
    if entry is None:
        return f"Tool {str(name)[:60]!r} is not a registered tool."
    if UNSAFE_TOOL_RE.search(str(name)):
        return f"Tool {str(name)[:60]!r} writes, executes code, generates or reads uploads and is never replayed."
    tags = {str(t).lower() for t in (getattr(entry, "tags", None) or [])}
    if tags & UNSAFE_TOOL_TAGS:
        return f"Tool {str(name)[:60]!r} reads per-user data or executes code and is never replayed."
    return None


def _path_like(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_ABS_PATH_RE.match(value.strip())) or ".." in value.replace("\\", "/").split("/")
    if isinstance(value, (list, tuple)):
        return any(_path_like(v) for v in value)
    return False


def input_refusal(values: Dict[str, Any], *, client_supplied: bool) -> Optional[str]:
    """Why a call's inputs may not be replayed. Server-stored inputs were produced by the server's own run and
    only uploaded-model paths are refused; client inputs may name no server file except a typed dataset path."""
    if values.get("sbml_path"):
        return "Uploaded model paths cannot be replayed."
    if not client_supplied:
        return None
    from phronesis.datasets.paths import is_allowed_typed_path

    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        if key == "dataset_path":
            if not isinstance(value, str) or not is_allowed_typed_path(value):
                return "dataset_path is outside the allowed dataset directories."
            continue
        if _PATH_KEY_RE.search(str(key)) or _path_like(value):
            return f"Input {str(key)[:40]!r} names a server file and cannot be replayed from a client manifest."
    return None


def check_replayable(manifest: Dict[str, Any], registry: Any, *, client_supplied: bool) -> List[Dict[str, Any]]:
    """The calls a replay would run; raises ReplayPolicyError for the first refused call (client manifests) or
    returns the refused ones as notes (stored manifests: the run is still compared on the replayable calls)."""
    get_entry = getattr(registry, "get_entry", None)
    listed = {getattr(e, "name", None): e for e in (registry.list_tools() if hasattr(registry, "list_tools") else [])}
    refused: List[Dict[str, Any]] = []
    for call in manifest.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise ReplayPolicyError("Malformed tool call record.")
        name = str(call.get("tool_name") or "")
        entry = get_entry(name) if callable(get_entry) else None
        if entry is None:
            entry = listed.get(name)
        values = {**(call.get("raw_inputs") or {}), **(call.get("inputs") or {})}
        reason = tool_replay_refusal(name, entry) or input_refusal(values, client_supplied=client_supplied)
        if reason is None:
            continue
        if client_supplied:
            raise ReplayPolicyError(reason)
        refused.append({"seq": call.get("seq"), "tool_name": name, "reason": reason})
    return refused


def without_calls(manifest: Dict[str, Any], refused: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A copy of the manifest without the refused calls (matched by seq)."""
    if not refused:
        return manifest
    skip = {r.get("seq") for r in refused}
    out = dict(manifest)
    out["tool_calls"] = [c for c in manifest.get("tool_calls") or [] if not (isinstance(c, dict) and c.get("seq") in skip)]
    return out

Tier = Literal["identical", "within_tolerance", "changed", "added", "removed"]


class FieldChange(BaseModel):
    path: str
    before: Any = None
    after: Any = None
    rel_change: Optional[float] = None
    tier: Tier


class ToolDiff(BaseModel):
    key: str
    tool_name: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    status_before: str
    status_after: str
    output_hash_equal: bool
    changes: List[FieldChange] = Field(default_factory=list)


class RerunReport(BaseModel):
    original_run_id: str
    new_run_id: str
    mode: Literal["tools", "full"]
    provenance: Literal["server_store", "client_supplied"]
    started_at: str
    finished_at: str
    overall: Literal["identical", "drifted", "changed"]
    tool_diffs: List[ToolDiff] = Field(default_factory=list)
    missing_in_new: List[str] = Field(default_factory=list)
    new_only: List[str] = Field(default_factory=list)
    environment_changes: List[FieldChange] = Field(default_factory=list)
    source_version_changes: List[FieldChange] = Field(default_factory=list)
    biomodel_changes: List[FieldChange] = Field(default_factory=list)
    verification_delta: Optional[Dict[str, Any]] = None
    answer_unified_diff: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Replay
# -----------------------------------------------------------------------------

def call_kwargs(call: Dict[str, Any]) -> Dict[str, Any]:
    """Validated inputs, or the raw inputs of a call that failed validation (replayed to reproduce it)."""
    inputs = call.get("inputs") or {}
    return dict(inputs if inputs else (call.get("raw_inputs") or {}))


async def replay_tools(manifest: Dict[str, Any], registry: Any, *, user_id: str = "rerun") -> Dict[str, Any]:
    """Re-execute the recorded tool calls in seq order under a fresh recorder; returns the new full manifest."""
    rec = RunRecorder(
        query=str(manifest.get("query", "")),
        thread_id=f"replay:{manifest.get('run_id', '')}",
        model_name=str(manifest.get("model_id", "")),
        enable_web_search=bool(manifest.get("enable_web_search", True)),
        user_id=user_id,
        api_key_supplied=False,
    )
    rec.persist = False
    t0 = time.perf_counter()
    token = activate_recorder(rec)
    cache_token = bypass_tool_cache(True)  # replays re-contact the upstreams instead of reading the tool cache
    try:
        # A run simulates at most a few models; the cap keeps a client-supplied manifest from fanning out.
        for pin in (manifest.get("biomodels") or [])[:MAX_BIOMODEL_PINS]:
            if isinstance(pin, dict) and pin.get("accession"):
                rec.start_biomodel_resolution(str(pin["accession"]))
        for call in sorted(manifest.get("tool_calls") or [], key=lambda c: int(c.get("seq", 0))):
            kwargs = call_kwargs(call)
            try:
                await registry.execute(str(call.get("tool_name")), **kwargs)
            except TypeError as exc:
                # Recorded inputs that cannot be passed as keywords (e.g. a 'tool_name' key); shows as missing.
                rec.note_step("replay_skipped", {"tool_name": str(call.get("tool_name"))}, {"error": str(exc)[:200]})
                continue
            if call.get("tool_name") == "lumicell_analyze_single_cell" and kwargs.get("dataset_path"):
                rec.start_dataset_hash(str(kwargs["dataset_path"]))
        rec.routing = dict(manifest.get("routing") or {})
        rec.memory_context = dict(manifest.get("memory_context") or {})
        await rec.finalize(response="", duration=round(time.perf_counter() - t0, 2), framework="replay-tools")
    finally:
        restore_tool_cache(cache_token)
        reset_recorder(token)
    return rec.full_manifest or {}


# -----------------------------------------------------------------------------
# Diff
# -----------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def numeric_change(path: str, before: float, after: float) -> FieldChange:
    b, a = float(before), float(after)
    if b == a:
        return FieldChange(path=path, before=before, after=after, rel_change=0.0, tier="identical")
    rel = abs(a - b) / abs(b) if b != 0 else None
    if rel is None:
        tier: Tier = "identical" if abs(a - b) <= IDENTICAL_TOL else "changed"
    elif rel <= IDENTICAL_TOL:
        tier = "identical"
    elif rel <= WITHIN_TOL:
        tier = "within_tolerance"
    else:
        tier = "changed"
    return FieldChange(path=path, before=before, after=after, rel_change=None if rel is None else round(rel, 6), tier=tier)


def diff_values(path: str, before: Any, after: Any) -> List[FieldChange]:
    """Non-identical leaf changes between two digests: numbers by tolerance tier, scalar lists as sets."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: List[FieldChange] = []
        for key in sorted(set(before) | set(after), key=str):
            sub = f"{path}.{key}" if path else str(key)
            if key not in after:
                out.append(FieldChange(path=sub, before=before[key], after=None, tier="removed"))
            elif key not in before:
                out.append(FieldChange(path=sub, before=None, after=after[key], tier="added"))
            else:
                out.extend(diff_values(sub, before[key], after[key]))
        return out
    if isinstance(before, list) and isinstance(after, list) and all(
        not isinstance(v, (dict, list)) for v in before + after
    ):
        b_set, a_set = {json.dumps(v) for v in before}, {json.dumps(v) for v in after}
        out = [FieldChange(path=path, before=json.loads(v), after=None, tier="removed") for v in sorted(b_set - a_set)]
        out += [FieldChange(path=path, before=None, after=json.loads(v), tier="added") for v in sorted(a_set - b_set)]
        return out
    if _is_number(before) and _is_number(after):
        change = numeric_change(path, before, after)
        return [] if change.tier == "identical" else [change]
    if before != after:
        return [FieldChange(path=path, before=before, after=after, tier="changed")]
    return []


def _status(call: Optional[Dict[str, Any]]) -> str:
    if call is None:
        return "missing"
    return "success" if call.get("success") else str(call.get("error_type") or "error")


def _call_key(call: Dict[str, Any]) -> str:
    return f"{call.get('tool_name')}:{canonical_json_sha256(call_kwargs(call))[:12]}"


def _group(calls: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for call in sorted(calls, key=lambda c: int(c.get("seq", 0))):
        groups.setdefault(_call_key(call), []).append(call)
    return groups


def _source_version_changes(before: Dict[str, Any], after: Dict[str, Any]) -> List[FieldChange]:
    out = []
    for key in sorted(set(before or {}) | set(after or {})):
        b = (before or {}).get(key) or {}
        a = (after or {}).get(key) or {}
        if b.get("value") != a.get("value"):
            out.append(FieldChange(path=f"source_versions.{key}", before=b.get("value"), after=a.get("value"), tier="changed"))
    return out


def _biomodel_changes(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> List[FieldChange]:
    b_map = {p.get("accession"): p for p in before or [] if isinstance(p, dict)}
    a_map = {p.get("accession"): p for p in after or [] if isinstance(p, dict)}
    out: List[FieldChange] = []
    for acc in sorted(set(b_map) & set(a_map), key=str):
        for field in ("latest_revision", "main_file_sha256", "local_cache_sha256"):
            b, a = b_map[acc].get(field), a_map[acc].get(field)
            if b != a and (b is not None or a is not None):
                out.append(FieldChange(path=f"biomodels.{acc}.{field}", before=b, after=a, tier="changed"))
    return out


def _verification_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    b, a = (before or {}).get("final") or {}, (after or {}).get("final") or {}
    if not b or not a:
        return None
    delta: Dict[str, Any] = {"before": b, "after": a}
    if _is_number(b.get("score")) and _is_number(a.get("score")):
        delta["score_delta"] = round(float(a["score"]) - float(b["score"]), 6)
    delta["passed_changed"] = bool(b.get("passed")) != bool(a.get("passed"))
    return delta


def answer_diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(), (after or "").splitlines(), "original", "rerun", lineterm="", n=2
    )
    return "\n".join(lines)


def diff_runs(
    original: Dict[str, Any],
    new: Dict[str, Any],
    *,
    mode: Literal["tools", "full"] = "tools",
    provenance: Literal["server_store", "client_supplied"] = "server_store",
    original_response: Optional[str] = None,
    new_response: Optional[str] = None,
    started_at: Optional[str] = None,
) -> RerunReport:
    """Structured comparison of two run manifests (both reduced to their client view first)."""
    orig, cur = client_view(original or {}), client_view(new or {})
    b_groups, a_groups = _group(orig.get("tool_calls") or []), _group(cur.get("tool_calls") or [])

    tool_diffs: List[ToolDiff] = []
    missing: List[str] = []
    new_only: List[str] = []
    severity = 0  # 0 identical, 1 drifted, 2 changed

    for key in list(b_groups) + [k for k in a_groups if k not in b_groups]:
        b_list, a_list = b_groups.get(key, []), a_groups.get(key, [])
        for i in range(max(len(b_list), len(a_list))):
            b = b_list[i] if i < len(b_list) else None
            a = a_list[i] if i < len(a_list) else None
            if a is None:
                missing.append(key)
                severity = 2
                continue
            if b is None:
                new_only.append(key)
                severity = 2
                continue
            changes = diff_values("", b.get("output_digest") or {}, a.get("output_digest") or {})
            hash_equal = b.get("output_sha256") == a.get("output_sha256")
            s_before, s_after = _status(b), _status(a)
            tool_diffs.append(ToolDiff(
                key=key,
                tool_name=str(b.get("tool_name")),
                inputs=call_kwargs(b),
                status_before=s_before,
                status_after=s_after,
                output_hash_equal=hash_equal,
                changes=changes,
            ))
            # Remote evidence (literature sets, association scores) moves between upstream releases: drift.
            # Local computations that change beyond tolerance, or change with no digest to explain it: changed.
            remote = (b.get("adapter") or a.get("adapter")) in _REMOTE_ADAPTERS
            numeric_changed = any(c.tier == "changed" and _is_number(c.before) and _is_number(c.after) for c in changes)
            unexplained = not hash_equal and not changes and not (b.get("output_digest") or {})
            if s_before != s_after or (not remote and (numeric_changed or unexplained)):
                severity = 2
            elif changes or not hash_equal:
                severity = max(severity, 1)

    env_changes = diff_values("environment", orig.get("environment") or {}, cur.get("environment") or {})
    src_changes = _source_version_changes(orig.get("source_versions") or {}, cur.get("source_versions") or {})
    bm_changes = _biomodel_changes(orig.get("biomodels") or [], cur.get("biomodels") or [])
    if env_changes or src_changes or bm_changes:
        severity = max(severity, 1)

    verification = None
    unified = None
    notes: List[str] = []
    if mode == "full":
        verification = _verification_delta(orig.get("verification") or {}, cur.get("verification") or {})
        if verification and verification.get("passed_changed"):
            severity = 2
        if original_response is not None and new_response is not None:
            unified = answer_diff(original_response, new_response)
            if unified:
                severity = max(severity, 1)
        else:
            notes.append("The original answer text is not stored for this run, so no answer diff is shown.")
        notes.append("LLM output is not deterministic; the answer text is compared as a diff, not for equality.")
        n_evidence = int((orig.get("memory_context") or {}).get("n_evidence") or 0)
        if n_evidence:
            notes.append(
                f"The original run recalled {n_evidence} memory item(s); the re-run used no memory, which can change the answer."
            )
    else:
        notes.append("Replayed the recorded tool calls only; synthesis and verification were not re-run.")
    notes.append(f"Compared {len(tool_diffs)} tool call(s).")
    # PRV-003: say when outputs came from the tool cache rather than a live upstream call.
    cached_before = [c for c in orig.get("tool_calls") or [] if ((c.get("reliability") or {}).get("cache") == "hit")]
    cached_after = [c for c in cur.get("tool_calls") or [] if ((c.get("reliability") or {}).get("cache") == "hit")]
    if cached_before:
        ages = [float((c.get("reliability") or {}).get("cache_age_s") or 0) for c in cached_before]
        notes.append(
            f"{len(cached_before)} original tool output(s) were served from the tool cache "
            f"(oldest {max(ages) / 3600:.1f} h old), not fetched live during the original run."
        )
    if cached_after:
        notes.append(f"{len(cached_after)} replayed tool output(s) came from the tool cache, not a live upstream call.")
    elif mode == "tools" and tool_diffs:
        notes.append("Networked tools were re-fetched from their upstreams (the tool cache is bypassed for re-runs).")
    fallbacks = [c for c in (cur.get("tool_calls") or []) if (c.get("reliability") or {}).get("fallback_from")]
    if fallbacks:
        notes.append(f"{len(fallbacks)} replayed call(s) were answered by a fallback tool.")
    if provenance == "client_supplied":
        notes.append("The original manifest was supplied by the client and could not be checked against a stored run.")

    return RerunReport(
        original_run_id=str(orig.get("run_id", "")),
        new_run_id=str(cur.get("run_id", "")),
        mode=mode,
        provenance=provenance,
        started_at=started_at or str(cur.get("started_at") or utc_now()),
        finished_at=utc_now(),
        overall=("identical", "drifted", "changed")[severity],
        tool_diffs=tool_diffs,
        missing_in_new=missing,
        new_only=new_only,
        environment_changes=env_changes,
        source_version_changes=src_changes,
        biomodel_changes=bm_changes,
        verification_delta=verification,
        answer_unified_diff=unified,
        notes=notes,
    )
