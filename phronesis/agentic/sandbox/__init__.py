"""
Sandboxed code analysis of attached datasets (feature 10): the AST policy (policy.py), the subprocess runner
(runner.py), and the binding between a run and the uploaded files its code may read.

Input files are never named by the model. The analyst phase binds a SandboxScope (the run's user id and the ids of
the datasets attached to this question) in a context variable; the registry tool resolves the requested dataset
ids through the dataset store for that user only (DatasetStore.resolve_for_user), checks every resulting path lies
under the upload root, and copies them into the sandbox. A call without a bound scope reads no files.

Enabled only when PHRONESIS_SANDBOX=on (default off). Techniques: CodeAct (Wang et al., ICML 2024,
arXiv:2402.01030), Biomni plan-then-code (Huang et al. 2025, doi:10.1101/2025.05.30.656746), BixBench
(arXiv:2503.00096), Data Interpreter step verification (arXiv:2402.18679); threat model after Greshake et al. 2023
(arXiv:2302.12173) and Spotlighting (Hines et al. 2024, arXiv:2403.14720).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple

DATASET_ID_RE = re.compile(r"^ds_[0-9a-f]{12}$")
ANALYSABLE_KINDS = ("table", "h5ad")


def sandbox_enabled() -> bool:
    """PHRONESIS_SANDBOX=on (also 1/true/yes); anything else, including unset, keeps code execution off."""
    return (os.getenv("PHRONESIS_SANDBOX", "") or "").strip().lower() in ("on", "1", "true", "yes")


def sandbox_timeout_s() -> int:
    try:
        value = int(os.getenv("PHRONESIS_SANDBOX_TIMEOUT_S", "") or 60)
    except ValueError:
        value = 60
    return max(5, min(110, value))


def sandbox_memory_mb() -> int:
    try:
        value = int(os.getenv("PHRONESIS_SANDBOX_MEMORY_MB", "") or 2048)
    except ValueError:
        value = 2048
    return max(256, min(16384, value))


@dataclass(frozen=True)
class SandboxScope:
    user_id: str
    dataset_ids: FrozenSet[str] = field(default_factory=frozenset)


_SCOPE: contextvars.ContextVar[Optional[SandboxScope]] = contextvars.ContextVar("phronesis_sandbox_scope", default=None)


@contextlib.contextmanager
def bound_scope(user_id: str, dataset_ids: Iterable[str]) -> Iterator[SandboxScope]:
    """Bind the datasets one run may analyse for the duration of the block (copied into worker threads)."""
    scope = SandboxScope(user_id=str(user_id), dataset_ids=frozenset(i for i in dataset_ids if DATASET_ID_RE.match(str(i))))
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> Optional[SandboxScope]:
    return _SCOPE.get()


def safe_input_name(display_name: str, ds_id: str, ext: str) -> str:
    """A plain file name for the work dir: the upload's display name, sanitised, with its real extension."""
    stem = os.path.splitext(os.path.basename(str(display_name or "")))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80] or ds_id
    return f"{stem}{ext}"


@dataclass
class ResolvedInput:
    dataset_id: str
    name: str  # file name inside the sandbox work dir
    path: str  # server path (never shown to a client or a model)
    kind: str  # table | h5ad
    display_name: str
    sep: Optional[str] = None  # "," or "\t" for tables


def _within(path: str, root: str) -> bool:
    try:
        real, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([real, base]) == base
    except ValueError:
        return False


def resolve_inputs(scope: Optional[SandboxScope], dataset_ids: Iterable[str]) -> Tuple[List[ResolvedInput], List[str]]:
    """Files for the requested dataset ids that the bound scope allows; (inputs, errors)."""
    requested = [str(i) for i in dataset_ids or []]
    if not requested:
        return [], []
    if scope is None:
        return [], ["no datasets are bound to this run; analysis_run_python reads only the question's attachments"]
    errors: List[str] = []
    allowed = [i for i in dict.fromkeys(requested) if i in scope.dataset_ids]
    errors.extend(f"dataset {i[:40]} is not attached to this question" for i in requested if i not in scope.dataset_ids)
    if not allowed:
        return [], errors
    from ...datasets.store import default_upload_root, existing_dataset_store

    store = existing_dataset_store()
    if store is None:
        return [], errors + [f"dataset {i} not found" for i in allowed]
    resolved, store_errors = store.resolve_for_user(scope.user_id, allowed)
    errors.extend(store_errors)
    root = str(default_upload_root())
    out: List[ResolvedInput] = []
    used: Dict[str, int] = {}
    for att in resolved:
        record = att.record
        kind = record.kind.value
        if kind not in ANALYSABLE_KINDS or not att.analysis_path:
            errors.append(f"dataset {record.id} is not a table or h5ad file")
            continue
        path, ext, sep = att.analysis_path, ".h5ad", None
        if kind == "table":
            # The store keeps the uploaded table next to its converted data.h5ad; pandas reads the original.
            original = os.path.join(os.path.dirname(att.analysis_path), f"original{record.original_extension}")
            if record.original_extension in (".csv", ".tsv") and os.path.isfile(original):
                path, ext, sep = original, record.original_extension, ("\t" if record.original_extension == ".tsv" else ",")
        if not _within(path, root) or not os.path.isfile(path):
            errors.append(f"dataset {record.id} file is not available")
            continue
        name = safe_input_name(record.display_name, record.id, ext)
        if name in used:
            used[name] += 1
            name = f"{record.id}{ext}"
        used.setdefault(name, 0)
        out.append(ResolvedInput(dataset_id=record.id, name=name, path=path, kind=kind, display_name=record.display_name, sep=sep))
    return out, errors


__all__ = [
    "ANALYSABLE_KINDS",
    "ResolvedInput",
    "SandboxScope",
    "bound_scope",
    "current_scope",
    "resolve_inputs",
    "safe_input_name",
    "sandbox_enabled",
    "sandbox_memory_mb",
    "sandbox_timeout_s",
]
