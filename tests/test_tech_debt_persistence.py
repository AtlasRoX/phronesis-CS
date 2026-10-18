"""Track P: one data root (PHRONESIS_DATA_DIR), retention sweeps and bounded ad-hoc caches."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from phronesis.agentic import jobs
from phronesis.datasets import store as dataset_store
from phronesis.provenance import run_store
from phronesis.tools import reliability
from phronesis.utils import maintenance, paths
from phronesis.utils.http_limits import TTLCache

_SPECIFIC = (
    "PHRONESIS_MEMORY_DIR",
    "PHRONESIS_RUNS_DIR",
    "PHRONESIS_UPLOAD_ROOT",
    "PHRONESIS_JOBS_DIR",
    "PHRONESIS_TOOL_CACHE_DIR",
    "PHRONESIS_DATA_DIR",
    "PHRONESIS_MAINTENANCE",
    "PHRONESIS_MAINTENANCE_INTERVAL_H",
    "PHRONESIS_JOB_TTL_DAYS",
    "PHRONESIS_MAX_RUNS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _SPECIFIC:
        monkeypatch.delenv(name, raising=False)
    maintenance._reset_for_tests()
    reliability._PURGED_PATHS.clear()
    yield
    maintenance._reset_for_tests()
    reliability._PURGED_PATHS.clear()


# -----------------------------------------------------------------------------
# Data root
# -----------------------------------------------------------------------------

def test_data_dir_unset_is_none():
    assert paths.data_root() is None
    assert paths.data_dir("runs") is None


def test_defaults_unchanged_without_any_env():
    # Historical defaults (pytest guards included) are kept when nothing is configured.
    assert run_store._default_root().name == "phronesis_runs_pytest"
    assert dataset_store.default_upload_root() == Path(dataset_store.__file__).resolve().parents[2] / ".phronesis_uploads"
    assert os.path.basename(jobs.default_jobs_dir()) == "phronesis_jobs_pytest"
    assert reliability._cache_dir() is None


def test_data_dir_drives_every_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path))
    assert run_store._default_root() == tmp_path / "runs"
    assert dataset_store.default_upload_root() == tmp_path / "uploads"
    assert Path(jobs.default_jobs_dir()) == tmp_path / "jobs"
    assert reliability._cache_dir() == tmp_path / "tool_cache"

    from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine

    engine = ClinicalMemoryEngine()
    assert Path(engine.storage_dir) == tmp_path / "memory"
    assert (tmp_path / "memory" / "clinical_memory_v2.db").is_file()

    from phronesis.vivarix.engine import VivarixSimulationEngine

    assert VivarixSimulationEngine._cache_dir() == tmp_path / "biomodels_cache"


def test_specific_env_wins_over_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PHRONESIS_RUNS_DIR", str(tmp_path / "r"))
    monkeypatch.setenv("PHRONESIS_UPLOAD_ROOT", str(tmp_path / "u"))
    monkeypatch.setenv("PHRONESIS_MEMORY_DIR", str(tmp_path / "m"))
    assert run_store._default_root() == tmp_path / "r"
    assert dataset_store.default_upload_root() == tmp_path / "u"
    # The runs dir keeps priority for the jobs store and tool cache, as before.
    assert Path(jobs.default_jobs_dir()) == tmp_path / "r" / "jobs"
    assert reliability._cache_dir() == tmp_path / "r" / "tool_cache"
    monkeypatch.setenv("PHRONESIS_JOBS_DIR", str(tmp_path / "j"))
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE_DIR", str(tmp_path / "c"))
    assert Path(jobs.default_jobs_dir()) == tmp_path / "j"
    assert reliability._cache_dir() == tmp_path / "c"

    from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine

    assert Path(ClinicalMemoryEngine().storage_dir) == tmp_path / "m"


# -----------------------------------------------------------------------------
# Tool cache retention
# -----------------------------------------------------------------------------

def _seed_cache(path: Path, expired: int, live: int, payload_size: int = 2000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(reliability._CACHE_SQL)
    now = time.time()
    rows = [(f"old{i}", "t", now - 10, now - 100, "x" * payload_size) for i in range(expired)]
    rows += [(f"new{i}", "t", now + 3600, now, "y" * payload_size) for i in range(live)]
    conn.executemany("INSERT INTO tool_cache VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
    finally:
        conn.close()


def test_purge_expired_cache_deletes_and_vacuums(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE_DIR", str(tmp_path))
    db = tmp_path / "tool_cache.sqlite3"
    _seed_cache(db, expired=400, live=5)
    before = db.stat().st_size
    out = reliability.purge_expired_cache(vacuum=True)
    assert out == {"deleted": 400, "vacuumed": True}
    assert _count(db) == 5
    assert db.stat().st_size < before


def test_purge_expired_cache_without_store_is_noop(monkeypatch, tmp_path):
    assert reliability.purge_expired_cache() == {"deleted": 0, "vacuumed": False}
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE_DIR", str(tmp_path / "missing"))
    assert reliability.purge_expired_cache(vacuum=True) == {"deleted": 0, "vacuumed": False}
    assert not (tmp_path / "missing").exists()


def test_first_open_purges_expired_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE_DIR", str(tmp_path))
    db = tmp_path / "tool_cache.sqlite3"
    _seed_cache(db, expired=3, live=2, payload_size=10)
    reliability._cache_get("new0")  # any SQLite access triggers the startup purge
    assert _count(db) == 2
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO tool_cache VALUES ('late', 't', ?, 0, 'z')", (time.time() - 1,))
    conn.commit()
    conn.close()
    reliability._sqlite(lambda c: c.execute("SELECT 1").fetchone())
    assert _count(db) == 3  # only the first open in a process purges; later rows wait for the sweep


# -----------------------------------------------------------------------------
# Job retention
# -----------------------------------------------------------------------------

def _job(store: jobs.JobStore, status: str, finished_days_ago: float) -> str:
    job = store.create("alice", {"message": "q"})
    jid = job["job_id"]
    ts = time.time() - finished_days_ago * 86400
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, finished_at = ?, updated_at = ? WHERE id = ?", (status, ts, ts, jid))
    store.append_event(jid, 1, {"type": "done"})
    return jid


def test_finished_jobs_past_ttl_are_purged(tmp_path):
    store = jobs.JobStore(str(tmp_path))
    old_ok = _job(store, "succeeded", 40)
    old_fail = _job(store, "failed", 31)
    recent = _job(store, "succeeded", 2)
    old_interrupted = _job(store, "interrupted", 90)
    old_queued = _job(store, "queued", 90)
    assert store.purge_finished() == 2
    assert store.get(old_ok) is None and store.get(old_fail) is None
    assert store.events(old_ok) == []
    for jid in (recent, old_interrupted, old_queued):
        assert store.get(jid) is not None
    assert store.events(recent)


def test_job_ttl_env_and_zero_disables(monkeypatch, tmp_path):
    store = jobs.JobStore(str(tmp_path))
    jid = _job(store, "cancelled", 3)
    monkeypatch.setenv("PHRONESIS_JOB_TTL_DAYS", "0")
    assert store.purge_finished() == 0
    monkeypatch.setenv("PHRONESIS_JOB_TTL_DAYS", "1")
    assert jobs.purge_finished_jobs(str(tmp_path)) == 1
    assert store.get(jid) is None
    assert jobs.purge_finished_jobs(str(tmp_path / "none")) == 0
    assert not (tmp_path / "none").exists()


def test_job_store_open_purges_when_maintenance_on(monkeypatch, tmp_path):
    store = jobs.JobStore(str(tmp_path))
    jid = _job(store, "succeeded", 45)
    jobs.JobStore(str(tmp_path))  # maintenance is off under pytest by default
    assert store.get(jid) is not None
    monkeypatch.setenv("PHRONESIS_MAINTENANCE", "on")
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(maintenance, "schedule_maintenance", lambda: False)
    jobs.JobStore(str(tmp_path))
    assert store.get(jid) is None


# -----------------------------------------------------------------------------
# Run-store cap
# -----------------------------------------------------------------------------

def _save_runs(store: run_store.RunStore, n: int, user: str = "alice") -> list:
    ids = []
    for i in range(n):
        rid = f"{i:032x}"
        path = store.save(user, {"run_id": rid})
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
        ids.append(rid)
    return ids


def test_run_store_prune_keeps_newest(monkeypatch, tmp_path):
    store = run_store.RunStore(tmp_path)
    ids = _save_runs(store, 6)
    (tmp_path / "alice" / "notes.json").write_text("{}")  # not a run manifest: never touched
    assert store.prune(keep=4) == 2
    assert store.load("alice", ids[0]) is None and store.load("alice", ids[1]) is None
    assert all(store.load("alice", rid) is not None for rid in ids[2:])
    assert (tmp_path / "alice" / "notes.json").exists()
    monkeypatch.setenv("PHRONESIS_MAX_RUNS", "2")
    assert store.prune() == 2
    monkeypatch.setenv("PHRONESIS_MAX_RUNS", "0")
    assert store.prune() == 0
    assert run_store.RunStore(tmp_path / "missing").prune(keep=1) == 0


def test_run_store_cap_counts_all_users(tmp_path):
    store = run_store.RunStore(tmp_path)
    a = _save_runs(store, 3, "alice")
    path = store.save("bob", {"run_id": "f" * 32})
    os.utime(path, (2_000_000, 2_000_000))
    assert store.prune(keep=2) == 2
    assert store.load("bob", "f" * 32) is not None and store.load("alice", a[2]) is not None


# -----------------------------------------------------------------------------
# Maintenance sweep
# -----------------------------------------------------------------------------

def test_maintenance_disabled_under_pytest_by_default():
    assert maintenance.maintenance_enabled() is False
    assert maintenance.run_maintenance() == {"ran": False, "reason": "disabled"}
    assert maintenance.schedule_maintenance() is False


def test_run_maintenance_applies_every_rule_and_rate_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PHRONESIS_MAX_RUNS", "1")
    _seed_cache(tmp_path / "tool_cache" / "tool_cache.sqlite3", expired=2, live=1, payload_size=10)
    jstore = jobs.JobStore()
    assert jstore.storage_dir == str(tmp_path / "jobs")
    jid = _job(jstore, "failed", 60)
    _save_runs(run_store.RunStore(tmp_path / "runs"), 3)
    monkeypatch.setenv("PHRONESIS_MAINTENANCE", "on")

    out = maintenance.run_maintenance()
    assert out["ran"] is True
    assert out["tool_cache"]["deleted"] == 2
    assert out["jobs"] == {"deleted": 1}
    assert out["runs"] == {"deleted": 2}
    assert jstore.get(jid) is None
    assert maintenance.stamp_path() == tmp_path / ".maintenance.json"
    assert maintenance.stamp_path().is_file()

    assert maintenance.run_maintenance() == {"ran": False, "reason": "not_due"}
    assert maintenance.run_maintenance(force=True)["ran"] is True
    monkeypatch.setenv("PHRONESIS_MAINTENANCE_INTERVAL_H", "0")
    assert maintenance.run_maintenance()["ran"] is True


def test_schedule_maintenance_runs_in_background_once(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PHRONESIS_MAINTENANCE", "on")
    calls = []
    monkeypatch.setattr(maintenance, "run_maintenance", lambda **kw: calls.append(kw) or {"ran": True})
    assert maintenance.schedule_maintenance() is True
    for _ in range(200):
        if calls and not maintenance._running:
            break
        time.sleep(0.01)
    assert len(calls) == 1
    assert maintenance.schedule_maintenance() is False  # re-checked at most once a minute


def test_schedule_skips_when_stamp_is_recent(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PHRONESIS_MAINTENANCE", "on")
    maintenance._mark(time.time())
    assert maintenance.schedule_maintenance() is False


def test_stamp_defaults_to_temp_without_data_dir():
    import tempfile

    assert maintenance.stamp_path().parent == Path(tempfile.gettempdir())


# -----------------------------------------------------------------------------
# Bounded ad-hoc caches
# -----------------------------------------------------------------------------

def test_source_caches_are_bounded_ttl_caches():
    from phronesis.sources_extra import bindingdb, depmap

    for cache in (bindingdb._CACHE, bindingdb._KEY_CACHE, depmap._HEADER_CACHE, depmap._RESULT_CACHE):
        assert isinstance(cache, TTLCache)
        assert cache.maxsize <= 100_000
    assert bindingdb._CACHE.maxsize == bindingdb._CACHE_MAX
    assert bindingdb._CACHE.ttl_s == bindingdb._CACHE_TTL_S


def test_bindingdb_key_cache_remembers_none(monkeypatch):
    from phronesis.sources_extra import bindingdb

    bindingdb._KEY_CACHE.clear()
    calls = []

    def fake_key(smi):
        calls.append(smi)
        return None

    monkeypatch.setattr(bindingdb, "skeleton_key", fake_key)
    monkeypatch.setattr(bindingdb, "heavy_atoms", lambda smi: 3)
    rows = [{"smiles": "CCO"}, {"smiles": "CCO"}]
    assert bindingdb._match_keys(rows, {"drug": "3:abc"}) == {}
    assert calls == ["CCO"]  # a None key is cached too
    bindingdb._KEY_CACHE.clear()


# -----------------------------------------------------------------------------
# Generated configuration reference
# -----------------------------------------------------------------------------

def _gen_module():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "gen_env_reference.py"
    spec = importlib.util.spec_from_file_location("gen_env_reference", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    import sys

    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_env_reference_scan_and_secrets_blank():
    gen = _gen_module()
    found = gen.scan()
    for name in gen.REQUIRED + ("PHRONESIS_JOB_TTL_DAYS", "PHRONESIS_MAX_RUNS", "PHRONESIS_RUNS_DIR"):
        assert name in found, name
    assert found["PHRONESIS_JOB_TTL_DAYS"].defaults == {"30.0"}
    assert found["PHRONESIS_MAINTENANCE"].defaults == {"auto"}  # `os.getenv(...) or "auto"`
    example = gen.render_example(found)
    for line in example.splitlines():
        if line.startswith("# ") and "=" in line and not line.startswith("# ---"):
            name, _, value = line[2:].partition("=")
            if gen.SECRET_RE.search(name):
                assert value == "", line
    assert "# TAVILY_API_KEY=\n" in example and "# PHRONESIS_DATA_DIR=\n" in example
    assert not gen.SECRET_RE.search("PHRONESIS_AGENT_MAX_TOKENS")
    doc = gen.render_markdown(found)
    assert "## PHRONESIS_JOB" in doc and "`phronesis/utils/paths.py`" in doc
