"""Regression tests for the suite-wide hermeticity guards in tests/conftest.py.

TST-002: every state store resolves to the per-session temporary directory, never to the repository.
TST-003: the developer `.env` is not loaded into the test process, and no LLM/API key is visible offline.
TST-008: non-loopback DNS lookups and connections are refused in offline tests; loopback keeps working.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _conftest():
    """tests/conftest.py as pytest registered it (importing it again would re-run its setup)."""
    target = (REPO_ROOT / "tests" / "conftest.py").resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve() == target:
            return module
    raise AssertionError("tests/conftest.py is not loaded")


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------------------------
# TST-002: state stores
# ---------------------------------------------------------------------------------------------------------------


def test_state_root_is_a_temporary_directory_outside_the_repository():
    root = Path(os.environ["PHRONESIS_DATA_DIR"])
    assert root == _conftest().TEST_STATE_ROOT
    assert root.is_dir() and not _under(root, REPO_ROOT)
    for name in _conftest().STATE_DIR_VARS:
        assert name not in os.environ, f"{name} must not point a store anywhere else during tests"


def test_every_default_store_resolves_under_the_test_state_root():
    from phronesis.agentic.jobs import default_jobs_dir
    from phronesis.agentic.router_store import default_router_dir
    from phronesis.datasets.store import default_upload_root
    from phronesis.provenance.run_store import _default_root
    from phronesis.tools.reliability import _cache_dir
    from phronesis.utils.memory.project_store import default_projects_dir

    root = _conftest().TEST_STATE_ROOT
    for path in (default_jobs_dir(), default_router_dir(), default_upload_root(), _default_root(), _cache_dir(),
                 default_projects_dir()):
        assert path is not None and _under(Path(path), root), path
        assert not _under(Path(path), REPO_ROOT), path


def test_process_wide_memory_engine_writes_outside_the_repository(monkeypatch):
    from phronesis.utils.memory import clinical_memory

    repo_db = REPO_ROOT / ".phronesis_memory" / "clinical_memory_v2.db"
    before = repo_db.stat().st_mtime_ns if repo_db.exists() else None

    monkeypatch.setattr(clinical_memory, "_GLOBAL_CLINICAL_MEMORY", None)
    engine = clinical_memory.get_clinical_memory_engine()
    assert _under(Path(engine.storage_dir), _conftest().TEST_STATE_ROOT)
    asyncio.run(engine.consolidate_turn(thread_id="hermeticity_probe", query="q", response="r",
                                        tool_results={"synapta": "EGFR probe finding"}, user_id="user_probe"))
    assert engine.search_memory("EGFR probe", thread_id="hermeticity_probe", user_id="user_probe")

    after = repo_db.stat().st_mtime_ns if repo_db.exists() else None
    assert after == before, "the workspace memory store changed during a test"


# ---------------------------------------------------------------------------------------------------------------
# TST-003: environment
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(_conftest().LIVE_TESTS, reason="live runs keep the caller's keys")
def test_offline_run_has_no_keys_and_no_registered_settings_from_the_developer_environment():
    scrubbed = _conftest().scrubbed_variable_names
    for key in ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY", "PHRONESIS_AUTH_MODE",
                "PHRONESIS_CONFIDENCE_CALIBRATION", "AIAGENTS4PHARMA_LLM_MODEL", "PHRONESIS_SYNLETHDB_DIR"):
        assert key not in os.environ, key
    # The scrub covers registered settings and any secret-looking name, but not pytest's own variables.
    fake_env = {"PHRONESIS_AUTH_MODE": "disabled", "ANTHROPIC_API_KEY": "x", "SOME_SERVICE_TOKEN": "x",
                "PYTEST_CURRENT_TEST": "t", "PHRONESIS_LIVE_TESTS": "0", "PATH": "/bin"}
    assert scrubbed(fake_env) == ["ANTHROPIC_API_KEY", "PHRONESIS_AUTH_MODE", "SOME_SERVICE_TOKEN"]


# Counts load_dotenv calls made by app/backend/main.py itself (third-party imports such as pymilvus.settings also
# call it; the conftest stub covers those, see the in-process test below).
_IMPORT_PROBE = """
import os, sys
import dotenv
calls = []
def fake_load_dotenv(*a, **k):
    calls.append(os.path.normcase(sys._getframe(1).f_code.co_filename))
    return True
dotenv.load_dotenv = fake_load_dotenv
import app.backend.main  # noqa: F401
main_file = os.path.normcase(os.path.abspath(app.backend.main.__file__))
print("LOAD_DOTENV_CALLS=%d" % sum(os.path.abspath(c) == main_file for c in calls))
"""


def _import_main_counting_dotenv_loads(flag: str) -> int:
    env = {**os.environ, "PHRONESIS_LOAD_DOTENV": flag, "PHRONESIS_PRIMEKG_PRELOAD": "0"}
    out = subprocess.run([sys.executable, "-c", _IMPORT_PROBE], cwd=str(REPO_ROOT), env=env, capture_output=True,
                         text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("LOAD_DOTENV_CALLS=")][-1]
    return int(line.split("=", 1)[1])


@pytest.mark.slow
def test_importing_the_api_does_not_load_dotenv_under_pytest():
    assert os.environ.get("PHRONESIS_LOAD_DOTENV") == "0"
    assert _import_main_counting_dotenv_loads("0") == 0
    # The server still loads .env by default (load_dotenv is stubbed in the probe, so no real file is read).
    assert _import_main_counting_dotenv_loads("1") == 1


@pytest.mark.skipif(_conftest().LIVE_TESTS, reason="live runs load .env on purpose")
def test_implicit_dotenv_loads_are_noops_but_explicit_files_still_load(tmp_path, monkeypatch):
    import dotenv

    # load_dotenv() with no path searches upwards from the caller and would find <repo>/.env.
    assert dotenv.load_dotenv() is False
    assert dotenv.load_dotenv(REPO_ROOT / ".env") is False
    env_file = tmp_path / "own.env"
    env_file.write_text("PHRONESIS_HERMETICITY_PROBE=from-own-file\n", encoding="utf-8")
    monkeypatch.delenv("PHRONESIS_HERMETICITY_PROBE", raising=False)
    assert dotenv.load_dotenv(env_file) is True
    assert os.environ["PHRONESIS_HERMETICITY_PROBE"] == "from-own-file"


# ---------------------------------------------------------------------------------------------------------------
# TST-008: network guard
# ---------------------------------------------------------------------------------------------------------------


def test_dns_and_connect_to_internet_hosts_are_refused_and_recorded():
    guard = _conftest().NETWORK_GUARD
    blocked = _conftest().NetworkBlockedError
    with pytest.raises(blocked):
        socket.getaddrinfo("api.platform.opentargets.org", 443)
    with pytest.raises(socket.gaierror):  # HTTP clients see an ordinary resolver error
        socket.gethostbyname("www.ebi.ac.uk")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(blocked):
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routable
        with pytest.raises(blocked):
            sock.connect_ex(("192.0.2.1", 9))
    finally:
        sock.close()
    attempts = guard.take_attempts()  # consumed here so the autouse fixture does not fail this test
    targets = [target for _test, _kind, target in attempts]
    assert "api.platform.opentargets.org" in targets and "www.ebi.ac.uk" in targets
    assert sum(t.startswith("192.0.2.1:") for t in targets) == 2
    assert all(test.startswith("tests/test_audit_fix_hermeticity.py") for test, _k, _t in attempts)


def test_http_clients_fail_fast_offline():
    import httpx

    with pytest.raises(httpx.ConnectError):
        httpx.get("https://www.ebi.ac.uk/", timeout=5)

    async def fetch_ip_literal():
        # The Windows proactor loop connects through IOCP, not socket.connect; both paths are guarded.
        await asyncio.open_connection("192.0.2.1", 9)

    with pytest.raises(OSError):
        asyncio.run(fetch_ip_literal())
    assert len(_conftest().NETWORK_GUARD.take_attempts()) >= 2


def test_loopback_stays_open():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        port = server.getsockname()[1]
        assert socket.getaddrinfo("localhost", port)
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    finally:
        server.close()

    async def roundtrip():
        async def handle(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with srv:
            reader, writer = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
            writer.write(b"ping\n")
            await writer.drain()
            data = await reader.readline()
            writer.close()
            return data

    assert asyncio.run(roundtrip()) == b"ping\n"
    assert _conftest().NETWORK_GUARD.take_attempts() == []


def test_guard_preserves_the_socket_module_identity_for_other_guards():
    # phronesis.evals.cassette restores socket.getaddrinfo after replay and checks it is the socket function.
    assert socket.getaddrinfo.__module__ == "socket"
