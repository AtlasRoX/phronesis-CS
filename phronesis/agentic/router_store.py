"""
Routing outcomes: what each agentic run looked like, which architecture ran, and how well the answer held up.

The router (phronesis/agentic/router.py) reads these to nudge its scores toward architectures that ran reliably on
similar questions. Every agentic run is recorded, including ones where the user picked the architecture, so routes
Auto did not choose still get observed. Only routing features are stored, never the question text.

What is learned (AGT-004). The learning signal is operational only: whether the run completed and whether the
architecture's core phase ran or fell back. The run's own verification score and claim confidence are graded by
the same system that wrote the answer, so an architecture that merely sounds confident or passes the system's own
checks would reward itself; they are stored for diagnostics (self_verification, self_confidence) but never feed
the nudge. No signal here says whether an answer was right: there is no ground-truth or human outcome yet, so the
history can only favour architectures that finish reliably, never ones that answer correctly. Rows written before
this rule (quality_version 1, which mixed in the self-scored signals) are ignored.

Location: PHRONESIS_ROUTER_DIR, else <PHRONESIS_RUNS_DIR>/router, else <PHRONESIS_DATA_DIR>/router, else
<workspace>/data/router; pytest runs without any of them use a temporary directory. PHRONESIS_ROUTER_LEARNING:
off | auto (on, except under pytest) | on.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from phronesis.config.settings import SWITCH, get_str

from .config import AGENTIC_FRAMEWORKS

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_S = 2.0
MAX_ROWS = 5000  # oldest outcomes are dropped beyond this
# Shrinkage toward the architecture-wide mean: a bucket needs about this many runs before it counts fully.
PRIOR_RUNS = 5.0
MIN_RUNS = 3  # fewer runs of an architecture in a bucket than this: no adjustment for it
MAX_ADJUSTMENT = 0.6  # the most the history can add to or take from an architecture's score
ADJUSTMENT_GAIN = 2.0  # score change per unit of quality difference
# Version of the quality definition rows are written with; stats() reads only this version.
QUALITY_VERSION = 2


def learning_enabled() -> bool:
    value = get_str("PHRONESIS_ROUTER_LEARNING", "auto", choices=SWITCH)
    if value == "off":
        return False
    if value == "on":
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def default_router_dir() -> str:
    env = os.getenv("PHRONESIS_ROUTER_DIR")
    if env:
        return env
    runs = os.getenv("PHRONESIS_RUNS_DIR")
    if runs:
        return os.path.join(runs, "router")
    from phronesis.utils.paths import data_dir

    data = data_dir("router")
    if data is not None:
        return str(data)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return os.path.join(tempfile.gettempdir(), "phronesis_router_pytest")
    workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(workspace_root, "data", "router")


def operational_quality(task_status: str, core_status: str) -> float:
    """How reliably the architecture ran, in [0, 1]: completion and core-phase status, equally weighted."""
    done = 1.0 if task_status == "completed" else 0.5 if task_status == "partial" else 0.0
    core = 1.0 if core_status in ("ok", "") else 0.5 if core_status == "fallback" else 0.0
    return round(0.5 * done + 0.5 * core, 4)


def outcome_quality(
    verification_score: Optional[float],
    answer_confidence: Optional[float],
    task_status: str,
    core_status: str,
) -> float:
    """The learning label for one run: operational_quality only.

    verification_score and answer_confidence are accepted for compatibility and deliberately ignored (AGT-004):
    both are produced by the system grading its own answer, so learning from them rewards architectures that look
    good to the system rather than ones that are right."""
    del verification_score, answer_confidence
    return operational_quality(task_status, core_status)


@dataclass
class BucketStats:
    runs: int
    mean_quality: float


class RouterStore:
    def __init__(self, storage_dir: Optional[str] = None) -> None:
        self.db_path = os.path.join(os.path.abspath(storage_dir or default_router_dir()), "outcomes.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, check_same_thread=False)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
            with conn:
                yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    routed INTEGER NOT NULL,
                    quality REAL NOT NULL,
                    cost REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_bucket ON outcomes(bucket, framework)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)")}
            for name, decl in (
                ("quality_version", "INTEGER NOT NULL DEFAULT 1"),
                ("self_verification", "REAL"),
                ("self_confidence", "REAL"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {decl}")

    def record(
        self,
        bucket: str,
        framework: str,
        routed: bool,
        quality: float,
        cost: float,
        features: Dict[str, Any],
        *,
        self_verification: Optional[float] = None,
        self_confidence: Optional[float] = None,
    ) -> None:
        """Store one run. `quality` must be an independent (operational) label; the self-scored values are kept for
        diagnostics only and never read by stats()."""
        if framework not in AGENTIC_FRAMEWORKS:
            return

        def _num(v: Any) -> Optional[float]:
            return float(v) if isinstance(v, (int, float)) else None

        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO outcomes (bucket, framework, routed, quality, cost, features_json, recorded_at, quality_version,"
                " self_verification, self_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bucket, framework, int(routed), float(quality), float(cost), json.dumps(features, default=str), time.time(),
                 QUALITY_VERSION, _num(self_verification), _num(self_confidence)),
            )
            conn.execute(
                "DELETE FROM outcomes WHERE id NOT IN (SELECT id FROM outcomes ORDER BY id DESC LIMIT ?)", (MAX_ROWS,)
            )

    def stats(self, bucket: str) -> Dict[str, Dict[str, BucketStats]]:
        """{"bucket": {framework: stats}, "global": {framework: stats}}."""
        out: Dict[str, Dict[str, BucketStats]] = {"bucket": {}, "global": {}}
        with self._connect() as conn:
            for fw, n, mean in conn.execute(
                "SELECT framework, COUNT(*), AVG(quality) FROM outcomes WHERE bucket = ? AND quality_version = ? GROUP BY framework",
                (bucket, QUALITY_VERSION),
            ):
                out["bucket"][fw] = BucketStats(int(n), float(mean))
            for fw, n, mean in conn.execute(
                "SELECT framework, COUNT(*), AVG(quality) FROM outcomes WHERE quality_version = ? GROUP BY framework",
                (QUALITY_VERSION,),
            ):
                out["global"][fw] = BucketStats(int(n), float(mean))
        return out


def adjustments(stats: Dict[str, Dict[str, BucketStats]]) -> Dict[str, Dict[str, float]]:
    """Per-architecture score nudges from past outcomes in this bucket: {framework: {"delta", "runs", "quality"}}.

    Each architecture's bucket mean is shrunk toward its own global mean (PRIOR_RUNS pseudo-runs); the nudge is the
    shrunk mean minus the mean over all architectures, scaled and capped, so it only moves an architecture that did
    better or worse here than the router's alternatives do on average."""
    glob = stats.get("global") or {}
    total = sum(s.runs for s in glob.values())
    if not total:
        return {}
    overall = sum(s.runs * s.mean_quality for s in glob.values()) / total
    out: Dict[str, Dict[str, float]] = {}
    for fw, b in (stats.get("bucket") or {}).items():
        if b.runs < MIN_RUNS:
            continue
        prior = glob[fw].mean_quality if fw in glob else overall
        shrunk = (b.runs * b.mean_quality + PRIOR_RUNS * prior) / (b.runs + PRIOR_RUNS)
        delta = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, ADJUSTMENT_GAIN * (shrunk - overall)))
        out[fw] = {"delta": round(delta, 3), "runs": b.runs, "quality": round(b.mean_quality, 3)}
    return out


_STORE: Optional[RouterStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> Optional[RouterStore]:
    """The shared store, or None when learning is off or the store cannot open (routing then uses the rules only)."""
    global _STORE
    if not learning_enabled():
        return None
    with _STORE_LOCK:
        if _STORE is None or os.path.dirname(_STORE.db_path) != os.path.abspath(default_router_dir()):
            try:
                _STORE = RouterStore()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Router outcome store unavailable: %s", exc)
                return None
        return _STORE
