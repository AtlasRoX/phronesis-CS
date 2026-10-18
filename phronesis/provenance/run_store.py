"""
Per-user store of run manifests (owned by EXPORT, feature 9).

Layout: <workspace>/.phronesis_runs/{user}/{run_id}.json (override the root with PHRONESIS_RUNS_DIR, else
<PHRONESIS_DATA_DIR>/runs). Retention (`RunStore.prune`, run by the periodic maintenance sweep): each user keeps
their newest PHRONESIS_MAX_RUNS_PER_USER (default 500) manifests, and a global cap PHRONESIS_MAX_RUNS (default 5000)
evicts fairly, always from the user holding the most; 0 disables either cap.
Files use .json, never .manifest, which .gitignore ignores. The stored copy keeps full server paths and
the answer text so exports can check content integrity and full re-runs can diff the answer; neither is
returned by GET /api/runs/{run_id}.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Mirrors app.backend.auth._USER_ID_PATTERN (phronesis does not import the web layer).
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@:\-]{1,128}$")
_PLAIN_DIR_RE = re.compile(r"^[A-Za-z0-9_.@\-]{1,128}$")
_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$", re.IGNORECASE)
_RESPONSE_KEY = "_response_text"
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / ".phronesis_runs"


def _default_root() -> Path:
    env_root = os.getenv("PHRONESIS_RUNS_DIR")
    if env_root:
        return Path(env_root)
    from phronesis.utils.paths import data_dir

    data = data_dir("runs")
    if data is not None:
        return data
    # Test runs that did not set PHRONESIS_RUNS_DIR never write into the workspace store.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(tempfile.gettempdir()) / "phronesis_runs_pytest"
    return _WORKSPACE_ROOT


def max_runs() -> int:
    """PHRONESIS_MAX_RUNS: global cap over all users, evicted fairly (default 5000; 0 = unlimited)."""
    try:
        return max(0, int(os.getenv("PHRONESIS_MAX_RUNS", "") or 5000))
    except ValueError:
        return 5000


def max_runs_per_user() -> int:
    """PHRONESIS_MAX_RUNS_PER_USER: newest manifests kept per user (default 500; 0 = unlimited)."""
    try:
        return max(0, int(os.getenv("PHRONESIS_MAX_RUNS_PER_USER", "") or 500))
    except ValueError:
        return 500


class RunStoreError(ValueError):
    pass


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(run_id or ""))


def _user_dir_name(user_id: str) -> str:
    if not _USER_ID_RE.match(user_id or ""):
        raise RunStoreError("invalid user id")
    # ':' is a valid user-id character but not a valid Windows file name character; '~' never occurs in
    # user ids, so hashed names cannot collide with plain ones. Windows folds case and drops trailing dots,
    # so "Alice" or "alice." would share alice's folder: those ids are hashed too.
    if (
        not _PLAIN_DIR_RE.match(user_id)
        or user_id != user_id.lower()
        or user_id.endswith(".")
        or _WINDOWS_RESERVED.match(user_id)
    ):
        return "~h_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
    return user_id


class RunStore:
    def __init__(self, root: Optional[os.PathLike | str] = None) -> None:
        self.root = Path(root) if root is not None else _default_root()

    def _path(self, user_id: str, run_id: str) -> Path:
        if not valid_run_id(run_id):
            raise RunStoreError("invalid run id")
        return self.root / _user_dir_name(user_id) / f"{run_id}.json"

    def save(self, user_id: str, manifest: Dict[str, Any], response: Optional[str] = None) -> Path:
        path = self._path(user_id, str(manifest.get("run_id", "")))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(manifest)
        if response is not None:
            record[_RESPONSE_KEY] = response
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            from phronesis.utils.maintenance import schedule_maintenance

            schedule_maintenance()
        except Exception:  # noqa: BLE001  # maintenance never fails a save
            pass
        return path

    def prune(self, keep: Optional[int] = None, *, keep_per_user: Optional[int] = None) -> int:
        """Retention sweep (PRV-005): per-user quota first, then a global cap with fair eviction.

        1. Each user keeps their newest `keep_per_user` manifests (default PHRONESIS_MAX_RUNS_PER_USER).
        2. While the total exceeds `keep` (default PHRONESIS_MAX_RUNS), the oldest manifest of the user who holds the
           most is removed. One user's activity therefore never deletes another user's runs while that other user
           holds fewer runs than they do. 0 disables either cap. Returns the number of files deleted."""
        limit = max_runs() if keep is None else max(0, int(keep))
        per_user = max_runs_per_user() if keep_per_user is None else max(0, int(keep_per_user))
        if not self.root.is_dir():
            return 0
        by_user: Dict[str, List[Tuple[float, Path]]] = {}
        for path in self.root.glob("*/*.json"):
            if not _RUN_ID_RE.match(path.stem):
                continue
            try:
                by_user.setdefault(path.parent.name, []).append((path.stat().st_mtime, path))
            except OSError:
                continue
        doomed: List[Path] = []
        for files in by_user.values():
            files.sort(key=lambda item: item[0], reverse=True)  # newest first
            if per_user > 0 and len(files) > per_user:
                doomed.extend(p for _, p in files[per_user:])
                del files[per_user:]
        total = sum(len(files) for files in by_user.values())
        if limit > 0 and total > limit:
            # Fair eviction: always from the largest holding (ties: the user whose oldest run is oldest).
            heap = [(-len(files), files[-1][0], user) for user, files in by_user.items() if files]
            heapq.heapify(heap)
            while total > limit and heap:
                _neg, _oldest, user = heapq.heappop(heap)
                files = by_user[user]
                doomed.append(files.pop()[1])
                total -= 1
                if files:
                    heapq.heappush(heap, (-len(files), files[-1][0], user))
        removed = 0
        for path in doomed:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("Run-store retention removed %d manifest(s)", removed)
        return removed

    def _load_record(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._path(user_id, run_id)
        except RunStoreError:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def load(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """The stored manifest (full server copy, without the answer text), or None."""
        record = self._load_record(user_id, run_id)
        if record is None:
            return None
        record.pop(_RESPONSE_KEY, None)
        return record

    def load_response(self, user_id: str, run_id: str) -> Optional[str]:
        record = self._load_record(user_id, run_id)
        value = (record or {}).get(_RESPONSE_KEY)
        return value if isinstance(value, str) else None


_STORE: Optional[RunStore] = None
_LOCK = threading.Lock()


def get_run_store(storage_dir: Optional[os.PathLike | str] = None) -> RunStore:
    """Process-wide store; the root is re-read on each call so tests can point PHRONESIS_RUNS_DIR at tmp_path."""
    global _STORE
    if storage_dir is not None:
        return RunStore(storage_dir)
    root = _default_root()
    with _LOCK:
        if _STORE is None or _STORE.root != root:
            _STORE = RunStore(root)
        return _STORE
