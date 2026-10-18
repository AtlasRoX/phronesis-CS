"""
Background research jobs (feature 13): a durable job store, an ordered event log and an asyncio worker pool.

A job is one orchestrator turn that runs without a client attached. Every event the run emits (the chat
stream's event types: run_control, thought, content_delta, approval_request, ..., then done or error) is
appended to an ordered log (`seq`), so a client can leave and reattach with
`GET /api/jobs/{id}/events?after=N`, which replays the log and then follows it live.

- Durable execution after LangGraph persistence (https://docs.langchain.com/oss/python/langgraph/persistence):
  state is checkpointed outside the process (SQLite here), runs can pause for a human decision and resume. No
  peer-reviewed paper on durable LLM-agent execution was verified, so none is cited.
- Motivation for long-running and repeated research: living systematic reviews (Elliott et al., PLoS Med 2014,
  doi:10.1371/journal.pmed.1001603), which are updated as new evidence appears.

Statuses: queued, running, waiting_approval, succeeded, failed, cancelled, interrupted.
- Workers: `PHRONESIS_JOB_WORKERS` (default 2) asyncio tasks, started lazily inside the running event loop.
- Each job gets its own RunControl (`create_control(user_id, interactive=<a watcher is attached>)`), passed to
  `execute_inquiry(run_control=...)`, so approval requests can be answered through /api/agent/runs while a
  client watches the event stream. When the last watcher leaves, pending approvals are refused (fail closed) and
  the guarded expensive work is withheld; a job nobody ever watched approves automatically (control.py).
- On startup, jobs left running are marked interrupted. `resume` re-executes the job from its stored request
  (tool calls replay from the F19 result cache, so completed upstream work is not repeated). A runtime API key
  supplied with the request is kept in memory only, never in the store, so a resumed job after a restart
  uses the server's keys.
- `PHRONESIS_MAX_JOBS_PER_USER` (default 3) caps queued + running jobs per user; chat slots are separate.
- Single process: the store is safe for several readers, but only one server process should run workers on
  one `PHRONESIS_JOBS_DIR` (startup recovery marks every running job interrupted).

Location: `PHRONESIS_JOBS_DIR`, else `<PHRONESIS_RUNS_DIR>/jobs`, else `<PHRONESIS_DATA_DIR>/jobs`, else
`<workspace>/data/jobs`; pytest runs without any of them use a temporary directory.

Retention: finished jobs (succeeded, failed, cancelled) older than `PHRONESIS_JOB_TTL_DAYS` (default 30; 0
keeps them forever) are deleted with their event logs when the store opens and by the periodic maintenance
sweep (`phronesis.utils.maintenance`). Interrupted jobs are kept, since they can still be resumed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, List, Optional

from .config import AGENTIC, env_float, env_int, is_agentic
from .control import ApprovalDecision, RunControl, create_control, release_control

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{16}$")
STATUSES = ("queued", "running", "waiting_approval", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE = ("queued", "running", "waiting_approval")
TERMINAL = ("succeeded", "failed", "cancelled")
_STREAM_ONLY = ("content_delta", "reasoning_delta")

_BUSY_TIMEOUT_S = 5.0

Runner = Callable[..., Awaitable[Dict[str, Any]]]


class JobError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def default_jobs_dir() -> str:
    env = os.getenv("PHRONESIS_JOBS_DIR")
    if env:
        return env
    runs = os.getenv("PHRONESIS_RUNS_DIR")
    if runs:
        return os.path.join(runs, "jobs")
    from phronesis.utils.paths import data_dir

    data = data_dir("jobs")
    if data is not None:
        return str(data)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return os.path.join(tempfile.gettempdir(), "phronesis_jobs_pytest")
    workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(workspace_root, "data", "jobs")


def job_ttl_days() -> float:
    """PHRONESIS_JOB_TTL_DAYS (default 30; 0 keeps finished jobs forever)."""
    return env_float("PHRONESIS_JOB_TTL_DAYS", 30.0, 0.0, 36500.0)


def purge_finished_jobs(storage_dir: Optional[str] = None, ttl_days: Optional[float] = None) -> int:
    """Apply the finished-job TTL to an existing store without creating one (used by the maintenance sweep)."""
    path = os.path.join(os.path.abspath(storage_dir or default_jobs_dir()), "jobs.db")
    if not os.path.isfile(path):
        return 0
    store = JobStore.__new__(JobStore)
    store.storage_dir, store.db_path = os.path.dirname(path), path
    return store.purge_finished(ttl_days)


# ---------------------------------------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------------------------------------

class JobStore:
    """SQLite job rows and their ordered event log. One connection per operation (thread-safe)."""

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        self.storage_dir = os.path.abspath(storage_dir or default_jobs_dir())
        os.makedirs(self.storage_dir, exist_ok=True)
        self.db_path = os.path.join(self.storage_dir, "jobs.db")
        self._init_db()
        try:
            from phronesis.utils.maintenance import maintenance_enabled, schedule_maintenance

            if maintenance_enabled():
                self.purge_finished()
            schedule_maintenance()
        except Exception:  # noqa: BLE001  # retention never blocks opening the store
            logger.debug("Job retention sweep failed", exc_info=True)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, check_same_thread=False)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
            with conn:
                yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    title TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT 'api',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (job_id, seq)
                )
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row, with_result: bool = True) -> Dict[str, Any]:
        try:
            request = json.loads(row["request_json"] or "{}")
        except ValueError:
            request = {}
        out: Dict[str, Any] = {
            "job_id": row["id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "framework": row["framework"],
            "title": row["title"],
            "origin": row["origin"],
            "request": request,
            "error": row["error"],
            "attempts": int(row["attempts"] or 0),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]),
        }
        if with_result:
            try:
                out["result"] = json.loads(row["result_json"]) if row["result_json"] else None
            except ValueError:
                out["result"] = None
        return out

    def create(self, user_id: str, request: Dict[str, Any], *, origin: str = "api", job_id: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        jid = job_id or new_job_id()
        message = str(request.get("message", ""))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, status, run_id, thread_id, framework, title, request_json, origin, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    jid, user_id, run_id or new_run_id(), str(request.get("thread_id") or ""), str(request.get("framework") or AGENTIC),
                    " ".join(message.split())[:160], json.dumps(request, default=str), origin[:16], now, now,
                ),
            )
        return self.get(jid) or {}

    def get(self, job_id: str, user_id: Optional[str] = None, with_result: bool = True) -> Optional[Dict[str, Any]]:
        if not JOB_ID_RE.match(job_id or ""):
            return None
        with self._connect() as conn:
            if user_id is None:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return self._row(row, with_result) if row is not None else None

    def list(self, user_id: str, limit: int = 50, status: Optional[str] = None, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM jobs WHERE user_id = ?", [user_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if thread_id:
            sql += " AND thread_id = ?"
            args.append(thread_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row(r, with_result=False) for r in rows]

    def ids_with_status(self, statuses: List[str]) -> List[str]:
        marks = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT id FROM jobs WHERE status IN ({marks}) ORDER BY created_at ASC", statuses).fetchall()
        return [r["id"] for r in rows]

    def count_active(self, user_id: str) -> int:
        marks = ",".join("?" for _ in ACTIVE)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND status IN ({marks})", (user_id, *ACTIVE)).fetchone()
        return int(row["n"] or 0)

    def update(self, job_id: str, *, expect: Optional[List[str]] = None, **fields: Any) -> bool:
        """Set fields (status, result, error, run_id, started_at, finished_at, attempts_inc); expect guards the old status."""
        sets, args = ["updated_at = ?"], [time.time()]
        for key, value in fields.items():
            if key == "result":
                sets.append("result_json = ?")
                args.append(json.dumps(value, default=str) if value is not None else None)
            elif key == "attempts_inc":
                sets.append("attempts = attempts + ?")
                args.append(int(value))
            elif key in ("status", "error", "run_id", "started_at", "finished_at"):
                sets.append(f"{key} = ?")
                args.append(value)
        sql = f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?"
        args.append(job_id)
        if expect:
            sql += f" AND status IN ({','.join('?' for _ in expect)})"
            args.extend(expect)
        with self._connect() as conn:
            return conn.execute(sql, args).rowcount > 0

    def delete(self, job_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            n = conn.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).rowcount
            if n:
                conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
        return n > 0

    def purge_finished(self, ttl_days: Optional[float] = None, now: Optional[float] = None) -> int:
        """Delete finished jobs (and their events) older than ttl_days (default PHRONESIS_JOB_TTL_DAYS, 30)."""
        days = job_ttl_days() if ttl_days is None else max(0.0, float(ttl_days))
        if days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - days * 86400.0
        marks = ",".join("?" for _ in TERMINAL)
        with self._connect() as conn:
            ids = [
                r["id"] for r in conn.execute(
                    f"SELECT id FROM jobs WHERE status IN ({marks}) AND COALESCE(finished_at, updated_at) < ?",
                    (*TERMINAL, cutoff),
                ).fetchall()
            ]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                q = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM job_events WHERE job_id IN ({q})", chunk)
                conn.execute(f"DELETE FROM jobs WHERE id IN ({q})", chunk)
        if ids:
            logger.info("Purged %d finished job(s) older than %g day(s)", len(ids), days)
        return len(ids)

    def mark_interrupted(self) -> int:
        """Startup recovery: jobs a previous process left running cannot still be running."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE jobs SET status = 'interrupted', updated_at = ?, error = COALESCE(error, 'The server restarted while the job was running.') "
                "WHERE status IN ('running', 'waiting_approval')",
                (time.time(),),
            ).rowcount

    # Event log ------------------------------------------------------------------------------------------------
    def last_seq(self, job_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(seq) AS s FROM job_events WHERE job_id = ?", (job_id,)).fetchone()
        return int(row["s"] or 0)

    def attempt_start_seq(self, job_id: str) -> int:
        """Seq of the "queued" event that started the current attempt (a resume starts a new one)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(seq) AS s FROM job_events WHERE job_id = ? AND type = 'job' AND event_json LIKE '%\"status\": \"queued\"%'",
                (job_id,),
            ).fetchone()
        return int(row["s"] or 0)

    def append_event(self, job_id: str, seq: int, event: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO job_events (job_id, seq, type, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, seq, str(event.get("type", ""))[:40], json.dumps(event, default=str), time.time()),
            )

    def events(self, job_id: str, after: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, event_json FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (job_id, max(0, int(after)), max(1, min(10_000, int(limit)))),
            ).fetchall()
        out = []
        for r in rows:
            try:
                ev = json.loads(r["event_json"])
            except ValueError:
                continue
            ev["seq"] = int(r["seq"])
            out.append(ev)
        return out


# ---------------------------------------------------------------------------------------------------------
# Default runner
# ---------------------------------------------------------------------------------------------------------

async def orchestrator_runner(
    request: Dict[str, Any],
    *,
    user_id: str,
    event_callback: Callable[[Dict[str, Any]], Awaitable[None]],
    run_control: RunControl,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one stored ChatRequest-shaped request through the authoritative orchestrator."""
    from ..agents.authoritative_orchestrator import get_authoritative_orchestrator
    from ..utils.llm.llm_factory import DEFAULT_ORCHESTRATOR_MODEL

    orch = get_authoritative_orchestrator()
    res = await orch.execute_inquiry(
        query=str(request["message"]),
        thread_id=str(request.get("thread_id") or uuid.uuid4()),
        model_name=request.get("model") or DEFAULT_ORCHESTRATOR_MODEL,
        enable_web_search=bool(request.get("enable_web_search", True)),
        api_key=api_key,
        event_callback=event_callback,
        user_id=user_id,
        dataset_ids=list(request.get("dataset_ids") or []),
        framework=request.get("framework") or AGENTIC,
        project_id=request.get("project_id"),
        agent_options=dict(request.get("agent_options") or {}),
        run_control=run_control,
        interactive=run_control.interactive,
    )
    return res.to_dict()


# ---------------------------------------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------------------------------------

class JobManager:
    def __init__(self, store: Optional[JobStore] = None, runner: Optional[Runner] = None, workers: Optional[int] = None) -> None:
        self.store = store or JobStore()
        self.runner: Runner = runner or orchestrator_runner
        self.n_workers = workers or env_int("PHRONESIS_JOB_WORKERS", 2, 1, 16)
        self.max_per_user = env_int("PHRONESIS_MAX_JOBS_PER_USER", 3, 1, 100)
        self.max_events = env_int("PHRONESIS_JOB_MAX_EVENTS", 20_000, 100, 1_000_000)
        self.cancel_grace = env_float("PHRONESIS_JOB_CANCEL_GRACE_S", 20.0, 0.0, 600.0)
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional["asyncio.Queue[str]"] = None
        self._workers: List["asyncio.Task[None]"] = []
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._controls: Dict[str, RunControl] = {}
        self._watchers: Dict[str, int] = {}
        self._signals: Dict[str, asyncio.Event] = {}
        self._seq: Dict[str, int] = {}
        self._secrets: Dict[str, Dict[str, Any]] = {}
        self._deleted: set = set()
        self.recovered = self.store.mark_interrupted()
        if self.recovered:
            logger.info("Marked %d job(s) interrupted after a restart", self.recovered)

    # Workers ------------------------------------------------------------------------------------------------
    def _ensure_started(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop and self._workers and not all(w.done() for w in self._workers):
            return
        if self._loop is not None and self._loop is not loop:
            # The previous loop is gone (tests, reloads): its in-flight jobs cannot finish.
            for job_id in list(self._tasks):
                self.store.update(job_id, expect=list(ACTIVE), status="interrupted", error="The worker loop stopped.")
            self._tasks.clear()
            self._controls.clear()
            self._signals.clear()
            self._watchers.clear()
        self._loop = loop
        self._queue = asyncio.Queue()
        self._workers = [loop.create_task(self._worker(i), name=f"phronesis-job-worker-{i}") for i in range(self.n_workers)]
        for job_id in self.store.ids_with_status(["queued"]):
            self._queue.put_nowait(job_id)

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        queue = self._queue
        while True:
            job_id = await queue.get()
            try:
                task = asyncio.get_running_loop().create_task(self._execute(job_id), name=f"phronesis-job-{job_id}")
                self._tasks[job_id] = task
                try:
                    await task  # a user cancel ends the job task normally; only a shutdown cancels the worker
                except asyncio.CancelledError:
                    if not task.done():
                        task.cancel()
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Job worker %d failed on %s", index, job_id)
            finally:
                self._tasks.pop(job_id, None)
                queue.task_done()

    # Events -------------------------------------------------------------------------------------------------
    def _signal(self, job_id: str) -> asyncio.Event:
        ev = self._signals.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._signals[job_id] = ev
        return ev

    def _notify(self, job_id: str) -> None:
        ev = self._signals.pop(job_id, None)
        if ev is not None:
            ev.set()

    def _append(self, job_id: str, event: Dict[str, Any]) -> int:
        if job_id in self._deleted:
            return 0
        with self._lock:
            seq = self._seq.get(job_id)
            if seq is None:
                seq = self.store.last_seq(job_id)
            if seq >= self.max_events and event.get("type") in _STREAM_ONLY:
                return seq  # past the cap only the stream's token deltas are dropped
            seq += 1
            self._seq[job_id] = seq
            self.store.append_event(job_id, seq, event)
        self._notify(job_id)
        return seq

    def _status_event(self, job_id: str, status: str, **extra: Any) -> None:
        self._append(job_id, {"type": "job", "job_id": job_id, "status": status, **extra})

    # Lifecycle ----------------------------------------------------------------------------------------------
    async def submit(self, user_id: str, request: Dict[str, Any], *, api_key: Optional[str] = None, origin: str = "api") -> Dict[str, Any]:
        if not str(request.get("message") or "").strip():
            raise JobError("A job needs a message", 422)
        self._ensure_started()
        if self.store.count_active(user_id) >= self.max_per_user:
            raise JobError(f"You already have {self.max_per_user} jobs queued or running; wait for one to finish.", 429)
        request = {k: v for k, v in request.items() if k != "api_key"}
        request.setdefault("thread_id", f"thread_{uuid.uuid4().hex[:12]}")
        job = self.store.create(user_id, request, origin=origin)
        if api_key:
            self._secrets[job["job_id"]] = {"api_key": api_key}
        self._status_event(job["job_id"], "queued", run_id=job["run_id"])
        assert self._queue is not None
        self._queue.put_nowait(job["job_id"])
        return job

    async def _execute(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or not self.store.update(job_id, expect=["queued"], status="running", started_at=time.time(), attempts_inc=1, error=None):
            return  # cancelled or deleted while queued, or already picked up
        user_id = job["user_id"]
        control = create_control(user_id, interactive=self._watchers.get(job_id, 0) > 0, run_id=job["run_id"])
        self._controls[job_id] = control
        self._status_event(job_id, "running", run_id=control.run_id)
        status, error, result = "failed", None, None

        async def on_event(event: Dict[str, Any]) -> None:
            try:
                kind = event.get("type")
                self._append(job_id, dict(event))
                if kind == "approval_request":
                    self.store.update(job_id, expect=["running"], status="waiting_approval")
                    self._status_event(job_id, "waiting_approval", approval_id=event.get("approval_id"))
                elif kind in ("thought", "content_delta", "content_reset"):
                    if self.store.update(job_id, expect=["waiting_approval"], status="running"):
                        self._status_event(job_id, "running")
            except Exception as exc:  # noqa: BLE001 - logging must never break the run
                logger.warning("Job %s event logging failed: %s", job_id, exc)

        final_event: Dict[str, Any]
        try:
            secrets = self._secrets.get(job_id) or {}
            result = await self.runner(job["request"], user_id=user_id, event_callback=on_event, run_control=control, api_key=secrets.get("api_key"))
            task_status = ((result or {}).get("task_status") or {}).get("task_status")
            status = "cancelled" if control.cancelled or task_status == "cancelled" else "succeeded"
            final_event = {"type": "done", "result": result}
        except asyncio.CancelledError:
            if not control.cancelled:
                status, error = "interrupted", "The server stopped while the job was running."
                self.store.update(job_id, status=status, error=error)
                self._status_event(job_id, status)
                raise
            status, error = "cancelled", control.cancel_reason or "cancelled"
            final_event = {"type": "error", "detail": "The job was cancelled.", "cancelled": True}
        except Exception as exc:  # noqa: BLE001
            request_id = uuid.uuid4().hex[:12]
            logger.error("Job %s failed [request %s]", job_id, request_id, exc_info=True)
            status, error = "failed", f"The job failed on the server (request id {request_id}); details are in the server log."
            final_event = {"type": "error", "detail": error, "request_id": request_id, "exception": type(exc).__name__}
        finally:
            self._controls.pop(job_id, None)
            release_control(control.run_id)
            self._secrets.pop(job_id, None)
        # The row is final before the last events go out, so a client that stops at "done" reads the final status.
        self.store.update(job_id, status=status, error=error, result=result, finished_at=time.time())
        self._status_event(job_id, status, run_id=control.run_id)
        self._append(job_id, final_event)

    def get(self, job_id: str, user_id: str, with_result: bool = True) -> Optional[Dict[str, Any]]:
        job = self.store.get(job_id, user_id, with_result=with_result)
        if job is not None:
            job["watchers"] = self._watchers.get(job_id, 0)
            job["last_seq"] = self._seq.get(job_id) or self.store.last_seq(job_id)
            control = self._controls.get(job_id)
            job["pending_approvals"] = control.pending() if control is not None else []
        return job

    def list(self, user_id: str, limit: int = 50, status: Optional[str] = None, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.list(user_id, limit=limit, status=status, thread_id=thread_id)

    async def cancel(self, job_id: str, user_id: str, reason: str = "cancelled by the user") -> Optional[Dict[str, Any]]:
        job = self.store.get(job_id, user_id, with_result=False)
        if job is None:
            return None
        if job["status"] in ("queued", "interrupted"):
            if self.store.update(job_id, expect=["queued", "interrupted"], status="cancelled", error=reason, finished_at=time.time()):
                self._status_event(job_id, "cancelled")
                self._append(job_id, {"type": "error", "detail": "The job was cancelled.", "cancelled": True})
        elif job["status"] in ("running", "waiting_approval"):
            control = self._controls.get(job_id)
            if control is not None:
                control.cancel(reason)
            task = self._tasks.get(job_id)
            framework = (job.get("request") or {}).get("framework") or job.get("framework")
            if task is not None and not task.done():
                if control is None or not is_agentic(framework):
                    task.cancel()  # the classic pipeline has no checkpoints
                elif self._loop is not None:
                    self._loop.call_later(self.cancel_grace, lambda t=task: t.done() or t.cancel())
        return self.get(job_id, user_id, with_result=False)

    async def resume(self, job_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        job = self.store.get(job_id, user_id, with_result=False)
        if job is None:
            return None
        if job["status"] not in ("interrupted", "failed", "cancelled"):
            raise JobError(f"Only interrupted, failed or cancelled jobs can be resumed (this one is {job['status']}).", 409)
        self._ensure_started()
        if self.store.count_active(user_id) >= self.max_per_user:
            raise JobError(f"You already have {self.max_per_user} jobs queued or running; wait for one to finish.", 429)
        run_id = new_run_id()
        if not self.store.update(job_id, expect=["interrupted", "failed", "cancelled"], status="queued", run_id=run_id, result=None, error=None, finished_at=None):
            raise JobError("The job changed state; try again.", 409)
        self._status_event(job_id, "queued", run_id=run_id, resumed=True)
        assert self._queue is not None
        self._queue.put_nowait(job_id)
        return self.get(job_id, user_id, with_result=False)

    async def delete(self, job_id: str, user_id: str) -> bool:
        job = self.store.get(job_id, user_id, with_result=False)
        if job is None:
            return False
        if job["status"] in ACTIVE:
            await self.cancel(job_id, user_id, reason="deleted by the user")
            task = self._tasks.get(job_id)
            if task is not None and not task.done():
                task.cancel()
        self._deleted.add(job_id)
        self._seq.pop(job_id, None)
        self._secrets.pop(job_id, None)
        return self.store.delete(job_id, user_id)

    # Watching -----------------------------------------------------------------------------------------------
    def attach_watcher(self, job_id: str) -> None:
        self._watchers[job_id] = self._watchers.get(job_id, 0) + 1
        control = self._controls.get(job_id)
        if control is not None:
            control.set_interactive(True)

    def detach_watcher(self, job_id: str) -> None:
        n = self._watchers.get(job_id, 0) - 1
        if n > 0:
            self._watchers[job_id] = n
            return
        self._watchers.pop(job_id, None)
        control = self._controls.get(job_id)
        if control is not None:
            # Nobody is left to answer: pending approvals are refused and the guarded work is withheld (AGT-005).
            control.set_interactive(False, "the watcher left; the guarded work was not run")

    def events(self, job_id: str, after: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        return self.store.events(job_id, after=after, limit=limit)

    async def follow(self, job_id: str, after: int = 0, heartbeat_s: float = 15.0) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Replay events after `after`, then follow live ones; yields None as a heartbeat while idle.

        Ends after the current attempt's done/error event (a resumed job's earlier attempts are replayed, not
        stopped at), or once the job is no longer active and the log is drained."""
        self._ensure_started()
        last = max(0, int(after))
        attempt_start = self.store.attempt_start_seq(job_id)
        while True:
            signal = self._signal(job_id)
            batch = self.store.events(job_id, after=last, limit=500)
            for ev in batch:
                last = ev["seq"]
                yield ev
                if ev.get("type") in ("done", "error") and ev["seq"] > attempt_start:
                    return
            if batch:
                continue
            job = self.store.get(job_id, with_result=False)
            if job is None or job["status"] not in ACTIVE:
                if not self.store.events(job_id, after=last, limit=1):
                    return
                continue
            try:
                await asyncio.wait_for(signal.wait(), timeout=heartbeat_s)
            except asyncio.TimeoutError:
                yield None

    async def wait(self, job_id: str, timeout: float) -> Optional[Dict[str, Any]]:
        """The job row once it leaves the active statuses, or the current row after `timeout` seconds."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            job = self.store.get(job_id)
            if job is None or job["status"] not in ACTIVE:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return job
            signal = self._signal(job_id)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(signal.wait(), timeout=min(remaining, 1.0))


_MANAGER: Optional[JobManager] = None
_PINNED = False
_MANAGER_LOCK = threading.Lock()


def get_job_manager() -> JobManager:
    """Process-wide manager; a new one is made when the jobs directory changes (tests point it at tmp_path),
    unless one was installed with set_job_manager."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None and _PINNED:
            return _MANAGER
        path = os.path.abspath(default_jobs_dir())
        if _MANAGER is None or _MANAGER.store.storage_dir != path:
            _MANAGER = JobManager(JobStore(path))
        return _MANAGER


def set_job_manager(manager: Optional[JobManager]) -> None:
    """Install a manager (tests: injected runner and store); None restores the default."""
    global _MANAGER, _PINNED
    with _MANAGER_LOCK:
        _MANAGER = manager
        _PINNED = manager is not None


def public_job(job: Dict[str, Any], *, include_result: bool = True) -> Dict[str, Any]:
    """The client view: no user id; the stored request never holds an API key."""
    out = {k: v for k, v in job.items() if k != "user_id"}
    if not include_result:
        out.pop("result", None)
    return out


__all__ = [
    "ACTIVE",
    "JOB_ID_RE",
    "STATUSES",
    "TERMINAL",
    "JobError",
    "JobManager",
    "JobStore",
    "get_job_manager",
    "orchestrator_runner",
    "public_job",
    "set_job_manager",
]
