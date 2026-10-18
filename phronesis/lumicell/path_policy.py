"""
Dataset path policy enforced inside the Lumicell engine (audit finding SCI-024).

A dataset path reaches the engine from several callers (the classic orchestrator, agentic plan nodes, manifest
replays). The engine no longer trusts any of them to have checked it. A path is read only when one of these holds:

1. it is a typed dataset path under the configured dataset roots (PHRONESIS_DATASET_ROOTS, default <cwd>/data) and
   not inside the upload store (phronesis.datasets.paths.resolve_typed_path);
2. a trusted caller resolved it from the requesting user's own attachment and bound it for the duration of the
   call with ``authorized_dataset_paths`` (the orchestrator does this after DatasetStore.resolve_for_user);
3. the run has a bound sandbox scope (agentic runs) and the path is the analysis file of one of the datasets attached
   by that scope's user.

Everything else, including absolute paths chosen by a model, is refused before the file is opened.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
from typing import FrozenSet, Iterable, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

_AUTHORIZED: contextvars.ContextVar[FrozenSet[str]] = contextvars.ContextVar(
    "lumicell_authorized_dataset_paths", default=frozenset()
)

REFUSAL = (
    "The dataset path is not inside an allowed dataset directory and is not an attachment of this request; "
    "attach the file as a dataset or place it under PHRONESIS_DATASET_ROOTS."
)


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


@contextlib.contextmanager
def authorized_dataset_paths(paths: Iterable[Optional[str]]) -> Iterator[FrozenSet[str]]:
    """Allow the engine to read these server paths for the duration of the block (copied into worker threads)."""
    added = frozenset(_key(str(p)) for p in paths if p)
    token = _AUTHORIZED.set(_AUTHORIZED.get() | added)
    try:
        yield added
    finally:
        _AUTHORIZED.reset(token)


def _scope_allows(real_key: str) -> bool:
    # A scope can only be bound through phronesis.agentic.sandbox; when that module was never imported there is none.
    sandbox = sys.modules.get("phronesis.agentic.sandbox")
    current_scope = getattr(sandbox, "current_scope", None)
    if current_scope is None:
        return False
    scope = current_scope()
    if scope is None or not scope.dataset_ids:
        return False
    try:
        from ..datasets.store import existing_dataset_store

        store = existing_dataset_store()
        if store is None:
            return False
        resolved, _errors = store.resolve_for_user(scope.user_id, sorted(scope.dataset_ids))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Sandbox-scope dataset resolution failed: %s", exc)
        return False
    return any(att.analysis_path and _key(att.analysis_path) == real_key for att in resolved)


def resolve_dataset_path(dataset_path: str) -> Tuple[Optional[str], Optional[str]]:
    """(real path to read, None) when the policy allows the path, else (None, refusal reason)."""
    path = (dataset_path or "").strip()
    if not path or "\x00" in path:
        return None, "empty or invalid dataset path"
    try:
        from ..datasets.paths import resolve_typed_path

        typed = resolve_typed_path(path)
    except Exception:  # noqa: BLE001
        typed = None
    if typed:
        return typed, None
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None, REFUSAL
    key = os.path.normcase(real)
    if key in _AUTHORIZED.get() or _scope_allows(key):
        return real, None
    return None, REFUSAL
