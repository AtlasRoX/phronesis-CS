"""
Periodic retention sweep for local state, triggered lazily (no scheduler or app-startup hook needed).

`run_maintenance()` applies every retention rule once:

- tool cache: expired SQLite rows deleted, VACUUM once a quarter of the file is free pages
  (`phronesis.tools.reliability.purge_expired_cache`);
- jobs: finished jobs older than PHRONESIS_JOB_TTL_DAYS (default 30) deleted with their events
  (`phronesis.agentic.jobs.purge_finished_jobs`);
- run store: only the newest PHRONESIS_MAX_RUNS (default 500) manifests kept (`RunStore.prune`).

`schedule_maintenance()` is called from the job store, the tool cache and the run store. It is cheap (it looks
at a timestamp file at most once a minute) and, when PHRONESIS_MAINTENANCE_INTERVAL_H hours (default 6) have
passed since the last sweep by any process, runs the sweep in a daemon thread. The timestamp lives at
`<PHRONESIS_DATA_DIR>/.maintenance.json`, else in the system temp directory (one file per workspace).

PHRONESIS_MAINTENANCE=off|auto|on: auto runs it except while pytest runs a test (tests opt in with "on").
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict

from phronesis.utils.paths import WORKSPACE_ROOT, data_root

logger = logging.getLogger(__name__)

_RECHECK_S = 60.0  # how often (monotonic) schedule_maintenance looks at the timestamp file

_LOCK = threading.Lock()
_next_check = 0.0
_running = False


def maintenance_enabled() -> bool:
    value = (os.getenv("PHRONESIS_MAINTENANCE", "") or "auto").strip().lower()
    if value in ("off", "0", "false", "no"):
        return False
    if value in ("on", "1", "true", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def interval_s() -> float:
    try:
        hours = float(os.getenv("PHRONESIS_MAINTENANCE_INTERVAL_H", "") or 6.0)
    except ValueError:
        hours = 6.0
    return max(0.0, hours) * 3600.0


def stamp_path() -> Path:
    root = data_root()
    if root is not None:
        return root / ".maintenance.json"
    tag = hashlib.sha256(str(WORKSPACE_ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"phronesis_maintenance_{tag}.json"


def _last_run() -> float:
    try:
        with open(stamp_path(), "r", encoding="utf-8") as fh:
            return float(json.load(fh).get("last", 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _due(now: float) -> bool:
    return now - _last_run() >= interval_s()


def _mark(now: float) -> None:
    path = stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"last": now}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("Could not write the maintenance timestamp %s", path, exc_info=True)


def run_maintenance(*, force: bool = False) -> Dict[str, Any]:
    """Apply every retention rule now (unless disabled or not due; force skips both checks)."""
    now = time.time()
    if not force:
        if not maintenance_enabled():
            return {"ran": False, "reason": "disabled"}
        if not _due(now):
            return {"ran": False, "reason": "not_due"}
    _mark(now)
    results: Dict[str, Any] = {"ran": True}

    try:
        from phronesis.tools.reliability import purge_expired_cache

        results["tool_cache"] = purge_expired_cache(vacuum=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Tool-cache maintenance failed", exc_info=True)
        results["tool_cache"] = {"error": type(exc).__name__}

    try:
        from phronesis.agentic.jobs import purge_finished_jobs

        results["jobs"] = {"deleted": purge_finished_jobs()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Job maintenance failed", exc_info=True)
        results["jobs"] = {"error": type(exc).__name__}

    try:
        from phronesis.provenance.run_store import get_run_store

        results["runs"] = {"deleted": get_run_store().prune()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Run-store maintenance failed", exc_info=True)
        results["runs"] = {"error": type(exc).__name__}

    logger.info("Maintenance sweep: %s", results)
    return results


def _background() -> None:
    global _running
    try:
        run_maintenance()
    except Exception:  # noqa: BLE001
        logger.debug("Maintenance sweep failed", exc_info=True)
    finally:
        with _LOCK:
            _running = False


def schedule_maintenance() -> bool:
    """Start a background sweep when one is due; True when a sweep was started. Never raises."""
    global _next_check, _running
    try:
        if not maintenance_enabled():
            return False
        mono = time.monotonic()
        with _LOCK:
            if _running or mono < _next_check:
                return False
            _next_check = mono + _RECHECK_S
            if not _due(time.time()):
                return False
            _running = True
        threading.Thread(target=_background, name="phronesis-maintenance", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("Could not schedule maintenance", exc_info=True)
        with _LOCK:
            _running = False
        return False


def _reset_for_tests() -> None:
    global _next_check, _running
    with _LOCK:
        _next_check, _running = 0.0, False
