"""
Run manifest recorder (owned by EXPORT, feature 9).

The orchestrator activates one RunRecorder per inquiry (a ContextVar, so concurrent inquiries never share
one) and BiomedicalToolRegistry.execute reports every call through record_tool_call(). finalize() returns a
compact, JSON-safe manifest: validated tool inputs, output hashes and small domain digests, routing, the
BioModels pin, dataset hashes, upstream data-source versions, prompt hashes, verification and environment.
It never holds passages, plots or raw answer text, and it is never shown to the LLM.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import itertools
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "phronesis.run_manifest/1"
FINALIZE_WAIT_S = 5.0
DATASET_HASH_MAX_BYTES = 4 * 1024 ** 3
_ERROR_MAX = 300

_VOLATILE_KEYS = frozenset({
    "plot_base64_png",
    "execution_time_seconds",
    "timestamp",
    "timestamps",
    "retrieved_at",
    "fetched_at",
    "checked_at",
    "generated_at",
    "created_at",
    "started_at",
    "finished_at",
    "resolved_at",
    "timings_ms",  # entity resolver latency per source
    "last_update_posted",  # ClinicalTrials.gov record edits that do not change the trial
    "last_updated",  # openFDA dataset refresh date
})

# Server-side file paths that never leave the RunStore copy (the client copy shows the basename).
_PATH_INPUT_KEYS = ("sbml_path", "dataset_path")

_ENV_PACKAGES = (
    "phronesis",
    "copasi-basico",
    "python-copasi",
    "scanpy",
    "anndata",
    "httpx",
    "langchain-core",
    "langchain-openai",
    "fastapi",
    "pydantic",
)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class ToolCallRecord(BaseModel):
    seq: int
    tool_name: str
    adapter: Optional[str] = None
    timeout_seconds: Optional[float] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    raw_inputs: Optional[Dict[str, Any]] = None  # only when validation failed (inputs is then empty)
    started_at: str
    elapsed_seconds: float
    success: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    canonical_ids: Dict[str, str] = Field(default_factory=dict)
    output_sha256: Optional[str] = None
    output_digest: Dict[str, Any] = Field(default_factory=dict)
    # Reliability-layer facts about how the output was obtained (PRV-003): cache "hit"/"miss" and the cached
    # answer's age, a fallback tool that answered instead, retries, and arguments the registry repaired.
    reliability: Dict[str, Any] = Field(default_factory=dict)


class LLMCallRecord(BaseModel):
    """One chat-model client the run actually instantiated (PRV-002): the model that answered, not the one asked."""

    role: str = "llm"
    requested_model: str
    served_model: str
    provider: str
    base_url: Optional[str] = None
    fallback: bool = False
    fallback_reason: Optional[str] = None
    key_source: Literal["caller", "server"] = "server"
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class StepNote(BaseModel):
    step: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    at: str


class BioModelPin(BaseModel):
    accession: str
    latest_revision: Optional[int] = None
    main_file: Optional[str] = None
    main_file_sha256: Optional[str] = None
    local_cache_sha256: Optional[str] = None
    resolved_at: str
    error: Optional[str] = None


class DatasetPin(BaseModel):
    path_basename: str
    kind: Literal["h5ad", "zarr", "other"]
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    note: Optional[str] = None


class EnvironmentInfo(BaseModel):
    app_version: str
    api_version: str
    # sha256 over phronesis/** and app/backend/** code and config (py, yaml, toml, json, jsonl, txt), pyproject.toml
    # and uv.lock (PRV-007: the earlier fingerprint missed routers, configs and the lock file).
    source_fingerprint: str
    git_commit: Optional[str] = None  # HEAD commit when the deployment is a git checkout with commits
    lockfile_sha256: Optional[str] = None  # uv.lock: the resolved dependency set
    config_fingerprint: Optional[str] = None  # non-Python config and data files only (yaml/toml/json/jsonl/txt)
    python: str
    platform: str
    packages: Dict[str, str] = Field(default_factory=dict)


class RunManifest(BaseModel):
    schema_version: Literal["phronesis.run_manifest/1"] = SCHEMA_VERSION
    run_id: str
    thread_id: str
    user_id_hash: str
    query: str
    query_sha256: str
    model_id: str
    llm_params: Dict[str, Any] = Field(default_factory=dict)
    framework: str
    enable_web_search: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    routing: Dict[str, Any] = Field(default_factory=dict)
    memory_context: Dict[str, Any] = Field(default_factory=dict)
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    steps: List[StepNote] = Field(default_factory=list)
    biomodels: List[BioModelPin] = Field(default_factory=list)
    datasets: List[DatasetPin] = Field(default_factory=list)
    source_versions: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    synthesis: Dict[str, Any] = Field(default_factory=dict)
    verification: Dict[str, Any] = Field(default_factory=dict)
    task_status: Dict[str, Any] = Field(default_factory=dict)  # query_context.TaskOutcome
    response_sha256: str
    environment: EnvironmentInfo
    # The request configuration a full re-run needs to reproduce the run (PRV-004): requested framework, agent
    # options, project memory, interactivity, memory recall/persistence and attached dataset ids.
    request: Dict[str, Any] = Field(default_factory=dict)
    # Every chat-model client the run instantiated, with the model that actually served it (PRV-002).
    llm_calls: List[LLMCallRecord] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Hashing and JSON helpers
# -----------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    """A JSON round-trip copy; unknown objects become strings."""
    return json.loads(json.dumps(value, default=str))


def _is_volatile(key: Any) -> bool:
    return isinstance(key, str) and (key in _VOLATILE_KEYS or key.endswith("_seconds"))


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if not _is_volatile(k)}
    if isinstance(value, (list, tuple)):
        return [strip_volatile(v) for v in value]
    return value


def canonical_json_sha256(value: Any) -> str:
    """sha256 of sorted-key JSON with volatile keys (plots, timings, timestamps) removed."""
    payload = json.dumps(strip_volatile(json_safe(value)), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _basename(path: Any) -> str:
    text = str(path or "").rstrip("/\\")
    return os.path.basename(text.replace("\\", "/")) or text


def _sorted_unique(values: Any, limit: int = 50) -> List[str]:
    return sorted({str(v) for v in values if v not in (None, "")})[:limit]


# -----------------------------------------------------------------------------
# Per-tool output digests (key fields only; never passages or plots)
# -----------------------------------------------------------------------------

def _digest_vivarix(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("model_id", "simulation_type", "units", "final_concentrations", "steady_state_concentrations"):
        if data.get(key) not in (None, {}, []):
            out[key] = data[key]
    convergence = (data.get("diagnostics") or {}).get("convergence_status")
    if convergence:
        out["convergence_status"] = convergence
    return out


def _digest_lithoscribe(data: Dict[str, Any]) -> Dict[str, Any]:
    passages = [p for p in data.get("ranked_passages") or [] if isinstance(p, dict)]
    return {
        "pmids": _sorted_unique(p.get("pmid") for p in passages),
        "dois": _sorted_unique(str(p.get("doi") or "").lower() for p in passages),
        "n_passages": len(passages),
        "evidence_sufficiency": data.get("evidence_sufficiency"),
    }


def _digest_synapta_search(data: Dict[str, Any]) -> Dict[str, Any]:
    hits = [h for h in data.get("results") or [] if isinstance(h, dict)]
    return {"ensembl_ids": [str(h.get("id")) for h in hits[:10] if h.get("id")]}


def _digest_synapta_associations(data: Dict[str, Any]) -> Dict[str, Any]:
    scores: Dict[str, Any] = {}
    for a in (data.get("associations") or [])[:15]:
        if isinstance(a, dict) and a.get("disease_name"):
            scores[str(a["disease_name"])] = a.get("overall_association_score", a.get("score"))
    return {"association_scores": scores}


def _digest_kegg(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"kegg_ids": _sorted_unique(r.get("id") for r in data.get("results") or [] if isinstance(r, dict))}


def _digest_websurfer(data: Dict[str, Any]) -> Dict[str, Any]:
    urls = [r.get("url") for r in data.get("results") or [] if isinstance(r, dict)]
    top = data.get("top_page") if isinstance(data.get("top_page"), dict) else {}
    if top.get("url"):
        urls.append(top["url"])
    return {"urls": _sorted_unique(urls)}


def _digest_lumicell(data: Dict[str, Any]) -> Dict[str, Any]:
    markers: Dict[str, List[str]] = {}
    for cluster, rows in sorted((data.get("top_markers_by_cluster") or {}).items(), key=lambda kv: str(kv[0]))[:20]:
        markers[str(cluster)] = [str(r.get("gene")) for r in (rows or [])[:3] if isinstance(r, dict) and r.get("gene")]
    out = {
        "n_cells_post_qc": data.get("n_cells_post_qc"),
        "n_clusters": data.get("n_clusters"),
        "top_markers": markers,
    }
    if data.get("n_genes") is not None:
        out["n_genes"] = data.get("n_genes")
    return out


def _digest_clinical_trials(data: Dict[str, Any]) -> Dict[str, Any]:
    """The trial set with phase and status, and which trials carry posted results; not totals or edit dates."""
    trials = [t for t in data.get("trials") or [] if isinstance(t, dict)]
    return {
        "trials": {
            str(t.get("nct_id")): f"{'/'.join(t.get('phases') or [])}|{t.get('overall_status') or ''}"
            for t in trials if t.get("nct_id")
        },
        "with_results": _sorted_unique(t.get("nct_id") for t in trials if t.get("has_results")),
        "strategy": (data.get("query") or {}).get("strategy"),
    }


def _digest_chembl_resolve(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "drugs": _sorted_unique(d.get("chembl_id") for d in data.get("drugs") or [] if isinstance(d, dict)),
        "target": ((data.get("target") or {}).get("target_chembl_id")) if isinstance(data.get("target"), dict) else None,
        "unresolved": _sorted_unique(data.get("unresolved_names") or []),
    }


def _digest_chembl_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    potency: Dict[str, Any] = {}
    for d in data.get("drugs") or []:
        if not isinstance(d, dict):
            continue
        for b in d.get("bioactivities") or []:
            if isinstance(b, dict) and b.get("activity_id") is not None:
                potency[f"{d.get('chembl_id')}:{b.get('activity_id')}"] = b.get("pchembl_value")
    return {
        "drugs": {
            str(d.get("chembl_id")): d.get("max_phase") for d in data.get("drugs") or [] if isinstance(d, dict) and d.get("chembl_id")
        },
        "pchembl": potency,
        "approved_for_target": _sorted_unique(a.get("chembl_id") for a in data.get("target_approved_drugs") or [] if isinstance(a, dict)),
    }


def _digest_openfda(data: Dict[str, Any]) -> Dict[str, Any]:
    label = data.get("label") if isinstance(data.get("label"), dict) else {}
    return {
        "total_reports": data.get("total_reports"),
        "serious_reports": data.get("serious_reports"),
        "top_reactions": [str(r.get("term")) for r in (data.get("top_reactions") or [])[:5] if isinstance(r, dict)],
        "label_set_id": label.get("set_id"),
        "boxed_warning": bool(label.get("boxed_warning")) if label else None,
    }


def _digest_variant(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "clinvar": {
            str(c.get("accession")): c.get("germline_classification") or c.get("somatic_clinical_impact")
            for c in data.get("clinvar") or [] if isinstance(c, dict) and c.get("accession")
        },
        "cbioportal": {
            str(c.get("study_id")): f"{c.get('mutated_samples')}/{c.get('profiled_samples')}"
            for c in data.get("cbioportal") or [] if isinstance(c, dict) and c.get("study_id")
        },
        "civic_evidence": _sorted_unique(e.get("eid") for e in ((data.get("civic") or {}).get("evidence") or []) if isinstance(e, dict)),
        "sources": {str(s.get("name")): s.get("status") for s in data.get("sources") or [] if isinstance(s, dict)},
    }


def _digest_disease_targets(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "disease": (data.get("disease") or {}).get("id") if isinstance(data.get("disease"), dict) else None,
        "targets": [str(t.get("symbol")) for t in (data.get("targets") or [])[:15] if isinstance(t, dict)],
        "validated": _sorted_unique(v.get("chembl_id") for v in ((data.get("tiers") or {}).get("validated") or []) if isinstance(v, dict)),
    }


def _digest_primekg(data: Dict[str, Any]) -> Dict[str, Any]:
    prov = data.get("provenance") or {}
    analyses = [a for a in data.get("analyses") or [] if isinstance(a, dict)]
    return {
        "primekg_version": prov.get("dataverse_version"),
        "primekg_edges_sha256": prov.get("edges_sha256"),
        "analyses": [str(a.get("operation")) for a in analyses],
        "failed": [str(a.get("operation")) for a in analyses if a.get("error")],
        "resolved": _sorted_unique(r.get("curie") or r.get("id") for a in analyses for r in a.get("resolved") or []
                                   if isinstance(r, dict)),
    }


_DIGESTERS = {
    "vivarix_simulate_time_course": _digest_vivarix,
    "vivarix_compute_steady_state": _digest_vivarix,
    "lithoscribe_literature_synthesis": _digest_lithoscribe,
    "synapta_opentargets_search": _digest_synapta_search,
    "synapta_opentargets_associations": _digest_synapta_associations,
    "synapta_kegg_pathways": _digest_kegg,
    "websurfer_search": _digest_websurfer,
    "lumicell_analyze_single_cell": _digest_lumicell,
    "lithoscribe_clinical_trials_search": _digest_clinical_trials,
    "chembl_resolve_drugs": _digest_chembl_resolve,
    "chembl_drug_profile": _digest_chembl_profile,
    "openfda_drug_safety": _digest_openfda,
    "synapta_variant_intelligence": _digest_variant,
    "opentargets_disease_targets": _digest_disease_targets,
    "primekg_investigate": _digest_primekg,
}


def output_digest(tool_name: str, data: Any) -> Dict[str, Any]:
    """Domain key fields for diffs; plugin tools rely on the generic output hash and get {}."""
    fn = _DIGESTERS.get(tool_name)
    if fn is None or not isinstance(data, dict):
        return {}
    try:
        return json_safe(fn(data))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Digest for %s failed: %s", tool_name, exc)
        return {}


# -----------------------------------------------------------------------------
# Dataset hashing
# -----------------------------------------------------------------------------

def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def hash_dataset_path(path: str, max_bytes: int = DATASET_HASH_MAX_BYTES) -> DatasetPin:
    """Chunked sha256 of a file, or a tree hash of a directory (.zarr); only the basename is kept."""
    p = Path(path)
    name = _basename(path)
    kind: Literal["h5ad", "zarr", "other"] = (
        "h5ad" if name.lower().endswith(".h5ad") else "zarr" if name.lower().endswith(".zarr") else "other"
    )
    try:
        if p.is_dir():
            files = sorted((f for f in p.rglob("*") if f.is_file()), key=lambda f: f.relative_to(p).as_posix())
            total = sum(f.stat().st_size for f in files)
            if total > max_bytes:
                return DatasetPin(path_basename=name, kind=kind, size_bytes=total, note="not_hashed: too_large")
            tree = hashlib.sha256()
            for f in files:
                rel = f.relative_to(p).as_posix()
                tree.update(f"{rel}\0{f.stat().st_size}\0{_file_sha256(f)}\n".encode("utf-8"))
            return DatasetPin(path_basename=name, kind=kind, size_bytes=total, sha256=tree.hexdigest(), note="tree_hash")
        if p.is_file():
            size = p.stat().st_size
            if size > max_bytes:
                return DatasetPin(path_basename=name, kind=kind, size_bytes=size, note="not_hashed: too_large")
            return DatasetPin(path_basename=name, kind=kind, size_bytes=size, sha256=_file_sha256(p))
        return DatasetPin(path_basename=name, kind=kind, note="not_hashed: not_found")
    except OSError as exc:
        return DatasetPin(path_basename=name, kind=kind, note=f"not_hashed: {type(exc).__name__}")


def dataset_pin_from_ref(ref: Any) -> DatasetPin:
    """An uploaded dataset was hashed when it was stored; reuse that sha256 instead of re-reading it."""
    prov = dict(getattr(ref, "public_provenance", None) or {})
    kind_map = {"h5ad": "h5ad"}
    size = prov.get("size_bytes")
    return DatasetPin(
        path_basename=str(getattr(ref, "display_name", "") or prov.get("display_name") or getattr(ref, "id", "dataset")),
        kind=kind_map.get(str(getattr(ref, "kind", "")), "other"),  # type: ignore[arg-type]
        size_bytes=int(size) if isinstance(size, (int, float)) else None,
        sha256=prov.get("sha256") or None,
        note=f"upload {getattr(ref, 'id', '')}".strip() if prov.get("sha256") else "upload record has no sha256",
    )


# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


_CODE_SUFFIXES = (".py",)
_CONFIG_SUFFIXES = (".yaml", ".yml", ".toml", ".json", ".jsonl", ".txt", ".cfg", ".ini")
_FINGERPRINT_DIRS = ("phronesis", "app/backend")
_FINGERPRINT_FILES = ("pyproject.toml", "uv.lock")
_FINGERPRINT_SKIP_PARTS = {"__pycache__", "tests", ".pytest_cache", "node_modules"}
_FINGERPRINT_MAX_FILE = 64 * 1024 * 1024  # a stray large data file is fingerprinted by size, not read


def _fingerprint_files(root: Path, suffixes: tuple) -> List[Path]:
    files: List[Path] = []
    for rel in _FINGERPRINT_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for f in base.rglob("*"):  # recursive: app/backend/routers/*.py counts (PRV-007)
            if f.suffix.lower() in suffixes and f.is_file() and not (_FINGERPRINT_SKIP_PARTS & set(f.relative_to(root).parts)):
                files.append(f)
    return files


def _hash_files(root: Path, files: List[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(set(files), key=lambda f: f.relative_to(root).as_posix()):
        try:
            size = f.stat().st_size
            digest = hashlib.sha256(f.read_bytes()).digest() if size <= _FINGERPRINT_MAX_FILE else f"size:{size}".encode()
        except OSError:
            continue
        h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(digest)
    return h.hexdigest()


def compute_source_fingerprint(root: Path) -> str:
    """sha256 over the relative path and bytes of every code and config file of the backend under `root`
    (phronesis/** and app/backend/**, recursively; tests and caches excluded) plus pyproject.toml and uv.lock."""
    files = _fingerprint_files(root, _CODE_SUFFIXES + _CONFIG_SUFFIXES)
    files += [root / name for name in _FINGERPRINT_FILES if (root / name).is_file()]
    return _hash_files(root, files)


def compute_config_fingerprint(root: Path) -> str:
    """sha256 over the non-Python config and data files only, so a config change is visible on its own."""
    files = _fingerprint_files(root, _CONFIG_SUFFIXES)
    files += [root / "pyproject.toml"] if (root / "pyproject.toml").is_file() else []
    return _hash_files(root, files)


@functools.lru_cache(maxsize=1)
def source_fingerprint() -> str:
    return compute_source_fingerprint(_REPO_ROOT)


@functools.lru_cache(maxsize=1)
def config_fingerprint() -> str:
    return compute_config_fingerprint(_REPO_ROOT)


@functools.lru_cache(maxsize=1)
def lockfile_sha256() -> Optional[str]:
    lock = _REPO_ROOT / "uv.lock"
    try:
        return hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None
    except OSError:
        return None


def read_git_commit(root: Path) -> Optional[str]:
    """HEAD's commit id read from .git (no subprocess); None without a repository or without commits."""
    git = root / ".git"
    try:
        if git.is_file():  # worktree / submodule: "gitdir: <path>"
            text = git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                git = (root / text.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head.split(":", 1)[1].strip()
    try:
        value = (git / ref).read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            return value
    except OSError:
        pass
    try:
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[0]):
                return parts[0]
    except OSError:
        pass
    return None  # a repository without commits yet


@functools.lru_cache(maxsize=1)
def git_commit() -> Optional[str]:
    return read_git_commit(_REPO_ROOT)


# The user id is hashed with a deployment salt (PHRONESIS_USER_HASH_SALT, else a random salt kept next to the run
# store) so a shared bundle's hash cannot be reversed by hashing candidate e-mail addresses (PRV-007).
_SALT_FILE = ".user_hash_salt"
_salt_cache: Dict[str, str] = {}
_salt_lock = threading.Lock()


def _user_hash_salt() -> str:
    env = (os.getenv("PHRONESIS_USER_HASH_SALT") or "").strip()
    if env:
        return env
    try:
        from .run_store import _default_root

        root = Path(_default_root())
    except Exception:  # noqa: BLE001
        root = _REPO_ROOT / ".phronesis_runs"
    key = str(root)
    with _salt_lock:
        if key in _salt_cache:
            return _salt_cache[key]
        path = root / _SALT_FILE
        salt = ""
        try:
            salt = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not re.fullmatch(r"[0-9a-f]{32,128}", salt):
            import secrets

            salt = secrets.token_hex(32)
            try:
                root.mkdir(parents=True, exist_ok=True)
                path.write_text(salt, encoding="utf-8")
            except OSError as exc:  # an unwritable store still gets a salted (per-process) hash
                logger.warning("Could not persist the user-hash salt: %s", exc)
        _salt_cache[key] = salt
        return salt


def user_id_hash(user_id: str) -> str:
    salt = (os.getenv("PHRONESIS_USER_HASH_SALT") or "").strip()
    if salt:
        import hmac

        return hmac.new(salt.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def _package_versions() -> Dict[str, str]:
    from importlib import metadata

    versions: Dict[str, str] = {}
    for name in _ENV_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return versions


def environment_info() -> EnvironmentInfo:
    packages = _package_versions()
    api_version = "unknown"
    backend = sys.modules.get("app.backend.main")
    if backend is not None and getattr(backend, "app", None) is not None:
        api_version = str(getattr(backend.app, "version", "unknown"))
    return EnvironmentInfo(
        app_version=packages.get("phronesis", "unknown"),
        api_version=api_version,
        source_fingerprint=source_fingerprint(),
        git_commit=git_commit(),
        lockfile_sha256=lockfile_sha256(),
        config_fingerprint=config_fingerprint(),
        python=platform.python_version(),
        platform=platform.platform(terse=True),
        packages=packages,
    )


# -----------------------------------------------------------------------------
# Client view
# -----------------------------------------------------------------------------

def client_view(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The manifest as sent to browsers: server file paths in tool inputs reduced to their basename."""
    from ..datasets.paths import is_allowed_typed_path

    out = json_safe(manifest)
    if not isinstance(out, dict):
        return {}
    calls = out.get("tool_calls") if isinstance(out.get("tool_calls"), list) else []
    for call in calls:
        if not isinstance(call, dict):
            continue
        hidden: Dict[str, str] = {}
        for bucket in ("inputs", "raw_inputs"):
            values = call.get(bucket)
            if not isinstance(values, dict):
                continue
            for key in _PATH_INPUT_KEYS:
                path = values.get(key)
                if not path:
                    continue
                typed_ok = False
                if key == "dataset_path":
                    try:
                        typed_ok = bool(is_allowed_typed_path(str(path)))
                    except Exception:  # noqa: BLE001
                        typed_ok = False
                if not typed_ok:
                    hidden[str(path)] = _basename(path)
                    values[key] = _basename(path)
        # The registry records errors before the upload postprocessors redact them, so an error can still
        # name the server path.
        error = call.get("error")
        if isinstance(error, str) and hidden:
            for path, base in sorted(hidden.items(), key=lambda kv: -len(kv[0])):
                for variant in {path, path.replace("\\", "/"), path.replace("/", "\\")}:
                    error = error.replace(variant, base)
            call["error"] = error
    return out


# -----------------------------------------------------------------------------
# Recorder
# -----------------------------------------------------------------------------

def _compact_claims(report: Any) -> Any:
    """A verification summary with claim records reduced to counts per status plus the failing claim ids."""
    if not isinstance(report, dict) or not isinstance(report.get("claims"), list):
        return report
    out = {k: v for k, v in report.items() if k != "claims"}
    counts: Dict[str, int] = {}
    failing: List[str] = []
    for c in report["claims"]:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status in ("contradicted", "unsupported") and c.get("claim_id"):
            failing.append(str(c["claim_id"]))
    out["claims_summary"] = {"counts": counts, "failing_claim_ids": failing[:50]}
    return out


class RunRecorder:
    def __init__(
        self,
        *,
        query: str,
        thread_id: str,
        model_name: str,
        enable_web_search: bool,
        user_id: str,
        api_key_supplied: bool,
    ) -> None:
        self.query = query
        self.thread_id = thread_id
        self.model_name = model_name
        self.enable_web_search = enable_web_search
        self.user_id = user_id
        self.api_key_supplied = api_key_supplied
        self.run_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self._t0 = time.perf_counter()
        self.persist = True  # rerun replays set False: they are diffed, not stored as runs
        self.routing: Dict[str, Any] = {}
        self.memory_context: Dict[str, Any] = {}
        self.synthesis: Dict[str, Any] = {}
        self.verification: Dict[str, Any] = {}
        self.task_status: Dict[str, Any] = {}
        self.request: Dict[str, Any] = {}
        self._llm_calls: List[LLMCallRecord] = []
        self._calls: List[ToolCallRecord] = []
        self._order = itertools.count(1)
        self._steps: List[StepNote] = []
        self._biomodels: Dict[str, Optional[BioModelPin]] = {}
        self._datasets: List[DatasetPin] = []
        self._tasks: List["asyncio.Task[Any]"] = []
        self._source_versions: Optional[Dict[str, Dict[str, Any]]] = None
        self.full_manifest: Optional[Dict[str, Any]] = None
        self._spawn(self._probe_source_versions())

    # --- background work -----------------------------------------------------------
    def _spawn(self, coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        self._tasks.append(loop.create_task(coro))

    async def _probe_source_versions(self) -> None:
        from .source_versions import get_source_versions

        self._source_versions = await get_source_versions()

    async def _resolve_biomodel(self, accession: str) -> None:
        from .source_versions import resolve_biomodel_pin

        self._biomodels[accession] = BioModelPin(**await resolve_biomodel_pin(accession))

    async def _hash_dataset(self, path: str) -> None:
        self._datasets.append(await asyncio.to_thread(hash_dataset_path, path))

    # --- stage hooks ---------------------------------------------------------------
    def set_routing(self, ctx: Any) -> None:
        dataset_path = getattr(ctx, "analysis_dataset_path", None) or getattr(ctx, "dataset_path", None)
        attachments = getattr(ctx, "attachments", None) or []
        decisions = getattr(ctx, "decisions", None) or {}
        self.routing = json_safe({
            "primary_target": getattr(ctx, "primary_target", None),
            "requested_biomodel_id": getattr(ctx, "biomodel_id", None),
            "sim_duration_s": getattr(ctx, "sim_duration", None),
            "wants_steady_state": getattr(ctx, "wants_steady_state", None),
            "is_simulation_intent": getattr(ctx, "is_simulation_intent", None),
            "suppress_simulation": getattr(ctx, "suppress_simulation", None),
            "dataset_path_basename": _basename(dataset_path) if dataset_path else None,
            "has_single_cell_intent": getattr(ctx, "has_single_cell_intent", None),
            "dataset_ids": [getattr(a, "id", None) for a in attachments if getattr(a, "id", None)],
            "decisions": {
                str(k): bool(getattr(v, "run", False)) for k, v in (decisions.items() if isinstance(decisions, dict) else [])
            },
        })

    def set_memory_context(self, *, n_evidence: int, n_instructions: int, memory_summary: str) -> None:
        self.memory_context = {
            "n_evidence": int(n_evidence),
            "n_instructions": int(n_instructions),
            "memory_summary_sha256": sha256_text(memory_summary or ""),
        }

    def note_step(self, step: str, inputs: Optional[dict] = None, outputs: Optional[dict] = None) -> None:
        self._steps.append(StepNote(step=step, inputs=json_safe(inputs or {}), outputs=json_safe(outputs or {}), at=utc_now()))

    def start_biomodel_resolution(self, accession: str) -> None:
        accession = str(accession or "").strip()
        if not accession or accession in self._biomodels:
            return
        self._biomodels[accession] = None
        self._spawn(self._resolve_biomodel(accession))

    def start_dataset_hash(self, ref: Any) -> None:
        """ref is a typed dataset path (str) or a DatasetRef."""
        if ref is None:
            return
        if isinstance(ref, (str, os.PathLike)):
            if any(d.path_basename == _basename(ref) for d in self._datasets):
                return
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._datasets.append(hash_dataset_path(str(ref)))
                return
            self._spawn(self._hash_dataset(str(ref)))
            return
        self._datasets.append(dataset_pin_from_ref(ref))

    def set_synthesis(self, **fields: Any) -> None:
        self.synthesis.update(json_safe(fields))

    def set_verification(
        self, *, first_pass: dict, final: dict, correction_attempted: bool, correction_error: Optional[str]
    ) -> None:
        self.verification = json_safe({
            "first_pass": _compact_claims(first_pass),
            "final": _compact_claims(final),
            "correction_attempted": bool(correction_attempted),
            "correction_error": (correction_error or None) and str(correction_error)[:_ERROR_MAX],
        })

    def set_task_status(self, outcome: Any) -> None:
        """Record query_context.TaskOutcome (a model or a dict): whether the requested operation ran."""
        try:
            data = outcome.model_dump() if hasattr(outcome, "model_dump") else dict(outcome or {})
        except Exception:  # noqa: BLE001
            return
        self.task_status = json_safe(data)

    def set_request_config(
        self,
        *,
        requested_framework: Optional[str] = None,
        framework: Optional[str] = None,
        agent_options: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        interactive: Optional[bool] = None,
        recall_memory: Optional[bool] = None,
        persist_memory: Optional[bool] = None,
        dataset_ids: Optional[List[str]] = None,
    ) -> None:
        """The request configuration a full re-run must reproduce (PRV-004). Never holds keys or secrets."""
        self.request = json_safe({
            "requested_framework": requested_framework,
            "framework": framework,
            "agent_options": dict(agent_options or {}),
            "project_id": project_id,
            "interactive": None if interactive is None else bool(interactive),
            "recall_memory": None if recall_memory is None else bool(recall_memory),
            "persist_memory": None if persist_memory is None else bool(persist_memory),
            "dataset_ids": [str(d) for d in dataset_ids or [] if d],
        })

    def record_llm_client(self, record: "LLMCallRecord") -> None:
        """One chat-model client the run created. Identical clients (same role, requested and served model,
        provider) are recorded once."""
        key = (record.role, record.requested_model, record.served_model, record.provider, record.fallback)
        if any((r.role, r.requested_model, r.served_model, r.provider, r.fallback) == key for r in self._llm_calls):
            return
        if len(self._llm_calls) < 50:
            self._llm_calls.append(record)

    def served_models(self) -> List[str]:
        return list(dict.fromkeys(r.served_model for r in self._llm_calls))

    def add_tool_call(self, record: ToolCallRecord) -> None:
        self._calls.append(record)

    def next_order(self) -> int:
        return next(self._order)

    # --- finalize ------------------------------------------------------------------
    def _ordered_calls(self) -> List[ToolCallRecord]:
        # seq follows call start (started_at), with recording order breaking ties.
        ordered = sorted(self._calls, key=lambda c: (c.started_at, c.seq))
        return [c.model_copy(update={"seq": i}) for i, c in enumerate(ordered, start=1)]

    async def finalize(
        self, *, response: str, duration: float, framework: str = "authoritative-react-compiler"
    ) -> Optional[Dict[str, Any]]:
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            # A fixed greeting returns at once; it does not wait for version probes.
            wait_s = 0.0 if framework == "conversational-direct" else FINALIZE_WAIT_S
            _done, still = await asyncio.wait(pending, timeout=wait_s) if wait_s else (set(), set(pending))
            for task in still:
                task.cancel()
        for task in self._tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                logger.warning("Run manifest background task failed: %s", task.exception())

        biomodels = [
            pin or BioModelPin(accession=acc, resolved_at=utc_now(), error="resolution did not finish before the run ended")
            for acc, pin in self._biomodels.items()
        ]
        if self._source_versions is None:
            from .source_versions import SOURCE_KEYS

            source_versions = {k: {"value": None, "error": "probe did not finish before the run ended"} for k in SOURCE_KEYS}
        else:
            source_versions = self._source_versions

        environment = await asyncio.to_thread(environment_info)
        uid_hash = await asyncio.to_thread(user_id_hash, str(self.user_id))
        served = self.served_models()
        synthesis_model = self.synthesis.get("synthesis_model")
        synthesis_served = self.synthesis.get("synthesis_served_model") or next(
            (r.served_model for r in reversed(self._llm_calls) if synthesis_model and r.requested_model == synthesis_model),
            None,
        )
        manifest = RunManifest(
            run_id=self.run_id,
            thread_id=self.thread_id,
            user_id_hash=uid_hash,
            query=self.query,
            query_sha256=sha256_text(self.query),
            model_id=self.model_name,
            llm_params={
                "temperature": self.synthesis.get("temperature"),
                "streaming": True,
                "api_key_supplied": bool(self.api_key_supplied),
                # model_id is the model the request named; these are the models that actually answered (PRV-002).
                "requested_model": self.model_name,
                "served_models": served,
                "served_model": served[0] if len(served) == 1 else (synthesis_served or (served[0] if served else None)),
                "synthesis_model": synthesis_model,
                "synthesis_served_model": synthesis_served,
                "fallback_used": any(r.fallback for r in self._llm_calls),
            },
            framework=framework,
            enable_web_search=bool(self.enable_web_search),
            started_at=self.started_at,
            finished_at=utc_now(),
            duration_seconds=float(duration if duration is not None else round(time.perf_counter() - self._t0, 2)),
            routing=self.routing,
            memory_context=self.memory_context,
            tool_calls=self._ordered_calls(),
            steps=list(self._steps),
            biomodels=biomodels,
            datasets=list(self._datasets),
            source_versions=source_versions,
            synthesis=self.synthesis,
            verification=self.verification,
            task_status=self.task_status,
            response_sha256=sha256_text(response or ""),
            environment=environment,
            request=self.request,
            llm_calls=list(self._llm_calls),
        )
        full = manifest.model_dump(mode="json")
        self.full_manifest = full
        if self.persist:
            try:
                from .run_store import get_run_store

                await asyncio.to_thread(get_run_store().save, self.user_id, full, response)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Run manifest could not be stored: %s", exc)
        return client_view(full)


_CURRENT: "contextvars.ContextVar[Optional[RunRecorder]]" = contextvars.ContextVar("phronesis_run_recorder", default=None)


def activate_recorder(rec: RunRecorder) -> contextvars.Token:
    return _CURRENT.set(rec)


def reset_recorder(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def current_recorder() -> Optional[RunRecorder]:
    return _CURRENT.get()


# Replays must contact the upstreams again (PRV-003): while set, the tool cache is not read (results are still stored).
_CACHE_BYPASS: "contextvars.ContextVar[bool]" = contextvars.ContextVar("phronesis_tool_cache_bypass", default=False)


def cache_reads_disabled() -> bool:
    return bool(_CACHE_BYPASS.get())


def bypass_tool_cache(value: bool = True) -> contextvars.Token:
    return _CACHE_BYPASS.set(bool(value))


def restore_tool_cache(token: contextvars.Token) -> None:
    _CACHE_BYPASS.reset(token)


def record_llm_client(
    *,
    requested_model: str,
    served_model: str,
    provider: str,
    base_url: Optional[str] = None,
    fallback: bool = False,
    fallback_reason: Optional[str] = None,
    key_source: str = "server",
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    role: str = "llm",
) -> None:
    """Record a chat-model client on the active recorder (called by the LLM factory). Must never raise."""
    rec = _CURRENT.get()
    if rec is None:
        return
    try:
        rec.record_llm_client(LLMCallRecord(
            role=str(role)[:40],
            requested_model=str(requested_model)[:200],
            served_model=str(served_model)[:200],
            provider=str(provider)[:40],
            base_url=base_url,
            fallback=bool(fallback),
            fallback_reason=(fallback_reason or None) and str(fallback_reason)[:200],
            key_source="caller" if key_source == "caller" else "server",
            temperature=float(temperature) if isinstance(temperature, (int, float)) else None,
            max_tokens=int(max_tokens) if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_llm_client failed: %s", exc)


_RELIABILITY_KEYS = ("cache", "cache_age_s", "fallback_from", "arguments_adjusted", "retries", "breaker", "failure_class")


def _reliability_meta(result: Any) -> Dict[str, Any]:
    meta = getattr(result, "metadata", None)
    if not isinstance(meta, dict):
        return {}
    return json_safe({k: meta[k] for k in _RELIABILITY_KEYS if meta.get(k) not in (None, "", [], {})})


def record_tool_call(
    *,
    tool_name: str,
    entry: Any,
    inputs: Optional[dict],
    raw_inputs: dict,
    result: Any,
    started_at: str,
    elapsed_seconds: float,
) -> None:
    """Record one BiomedicalToolRegistry.execute call on the active recorder. Must never raise."""
    rec = _CURRENT.get()
    if rec is None:
        return
    try:
        order = rec.next_order()
        adapter = getattr(getattr(entry, "adapter_type", None), "value", None)
        timeout = getattr(entry, "timeout_seconds", None)
        data = getattr(result, "data", None)
        error_type = getattr(result, "error_type", None)
        success = bool(getattr(result, "success", False)) if result is not None else False
        error = getattr(result, "error", None) if result is not None else "tool call raised before returning a result"
        canonical = getattr(result, "canonical_ids", None) or {}
        record = ToolCallRecord(
            seq=order,
            tool_name=str(tool_name),
            adapter=adapter,
            timeout_seconds=float(timeout) if timeout is not None else None,
            inputs=json_safe(inputs) if inputs is not None else {},
            raw_inputs=None if inputs is not None else json_safe(raw_inputs or {}),
            started_at=str(started_at),
            elapsed_seconds=round(float(elapsed_seconds), 4),
            success=success,
            error_type=getattr(error_type, "value", error_type) if error_type is not None else None,
            error=str(error)[:_ERROR_MAX] if error else None,
            canonical_ids={str(k): str(v) for k, v in canonical.items()},
            output_sha256=canonical_json_sha256(data) if data is not None else None,
            output_digest=output_digest(str(tool_name), data) if success else {},
            reliability=_reliability_meta(result),
        )
        rec.add_tool_call(record)
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_tool_call failed for %s: %s", tool_name, exc)
