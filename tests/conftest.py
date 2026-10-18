"""Shared pytest setup, including the suite-wide hermeticity guards.

The default (offline) run must not depend on the developer's machine and must not change it:

* State stores (audit TST-002). Before any test module is imported, ``PHRONESIS_DATA_DIR`` points at a fresh
  temporary directory and every store-specific override (``PHRONESIS_MEMORY_DIR``, ``PHRONESIS_RUNS_DIR``, ...)
  is removed. The clinical memory vault, run manifests, uploads, jobs, tool cache, projects, router outcomes and
  the BioModels cache therefore all live under that directory (``phronesis.utils.paths``), never in
  ``<repo>/.phronesis_memory`` or the other workspace defaults. Tests that need their own location still use
  ``monkeypatch.setenv`` / ``tmp_path``.
* Environment (audit TST-003). ``PHRONESIS_LOAD_DOTENV=0`` stops ``app.backend.main`` from merging the developer
  ``.env`` into the test process at import, and every registered setting plus every secret-looking variable
  (``*_API_KEY``, ``*_TOKEN``, ``*SECRET*``, ``*PASSWORD*``) is removed, so offline tests run against code defaults
  exactly as they do in CI, and no LLM/API key is available to a call that a test forgot to fake.
* Network (audit TST-008). A socket guard refuses DNS lookups and connections to anything but loopback. A test
  that tries anyway fails with the blocked targets listed. Tests that genuinely need the internet are marked
  ``@pytest.mark.network``; they are skipped unless ``PHRONESIS_LIVE_TESTS=1``, and only then may they connect.

Live runs (``PHRONESIS_LIVE_TESTS=1``) keep the caller's keys, also load ``<repo>/.env`` when it exists (without
overriding variables already set), and still use the temporary state directory.
"""

from __future__ import annotations

import atexit
import functools
import ipaddress
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _live_tests_enabled() -> bool:
    return os.getenv("PHRONESIS_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes")


LIVE_TESTS = _live_tests_enabled()

# ---------------------------------------------------------------------------------------------------------------
# 1. Environment and state directories (applied at conftest import, before test modules import the app)
# ---------------------------------------------------------------------------------------------------------------

# Store-specific locations; PHRONESIS_DATA_DIR below replaces all of them.
STATE_DIR_VARS = (
    "PHRONESIS_MEMORY_DIR",
    "PHRONESIS_PROJECTS_DIR",
    "PHRONESIS_RUNS_DIR",
    "PHRONESIS_UPLOAD_ROOT",
    "PHRONESIS_JOBS_DIR",
    "PHRONESIS_TOOL_CACHE_DIR",
    "PHRONESIS_ROUTER_DIR",
)
# Never scrubbed: pytest's own variable and the switch that selects this mode.
_KEEP = {"PYTEST_CURRENT_TEST", "PHRONESIS_LIVE_TESTS"}
SECRET_NAME_RE = re.compile(r"(API_KEY|_KEY$|_TOKEN$|_TOKENS$|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


def _registered_setting_names() -> List[str]:
    """Names in the settings registry, loaded from settings.py alone (stdlib only, no package import)."""
    import importlib.util

    path = REPO_ROOT / "phronesis" / "config" / "settings.py"
    try:
        spec = importlib.util.spec_from_file_location("_conftest_settings_registry", path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[spec.name] = module  # type: ignore[union-attr]
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        finally:
            sys.modules.pop(spec.name, None)  # type: ignore[union-attr]
        return list(module.REGISTRY)
    except Exception:  # noqa: BLE001 - fall back to the secret-name scrub below
        return []


def scrubbed_variable_names(environ: Any = None) -> List[str]:
    """The variables an offline run removes: every registered setting and every secret-looking name."""
    environ = os.environ if environ is None else environ
    names = set(_registered_setting_names()) | {n for n in environ if SECRET_NAME_RE.search(n)}
    return sorted(n for n in names - _KEEP if n in environ)


def _is_workspace_dotenv(dotenv_path: Any) -> bool:
    if dotenv_path is None:
        return True  # python-dotenv searches upwards from the caller and finds <repo>/.env (.venv lives in the repo)
    try:
        return Path(os.fspath(dotenv_path)).resolve() == (REPO_ROOT / ".env").resolve()
    except (TypeError, OSError):
        return False


def _neutralise_implicit_dotenv() -> None:
    """Make implicit `.env` loads no-ops in offline runs.

    Not only app.backend.main: third-party modules load `.env` at import too (pymilvus.settings calls
    ``load_dotenv()``, whose upward search from site-packages reaches ``<repo>/.env``). Explicit loads of another
    file or stream (a test's own temporary .env) still work.
    """
    try:
        import dotenv
        import dotenv.main
    except ImportError:  # pragma: no cover - python-dotenv is a core dependency
        return
    real = dotenv.main.load_dotenv
    if getattr(real, "_phronesis_hermetic", False):
        return

    @functools.wraps(real)
    def load_dotenv(dotenv_path: Any = None, stream: Any = None, *args: Any, **kwargs: Any) -> bool:
        if stream is None and _is_workspace_dotenv(dotenv_path):
            return False
        return real(dotenv_path, stream, *args, **kwargs)

    load_dotenv._phronesis_hermetic = True  # type: ignore[attr-defined]
    dotenv.load_dotenv = load_dotenv  # type: ignore[assignment]
    dotenv.main.load_dotenv = load_dotenv  # type: ignore[assignment]


def _prepare_environment() -> Path:
    if LIVE_TESTS:
        try:
            from dotenv import load_dotenv

            env_file = REPO_ROOT / ".env"
            if env_file.is_file():
                load_dotenv(env_file, override=False)
        except ImportError:  # pragma: no cover - python-dotenv is a core dependency
            pass
    else:
        for name in scrubbed_variable_names():
            os.environ.pop(name, None)
        _neutralise_implicit_dotenv()
    for name in STATE_DIR_VARS:
        os.environ.pop(name, None)
    # The API module loads .env at import unless told not to (app/backend/main.py).
    os.environ["PHRONESIS_LOAD_DOTENV"] = "0"
    state_root = Path(tempfile.mkdtemp(prefix="phronesis-pytest-state-"))
    os.environ["PHRONESIS_DATA_DIR"] = str(state_root)
    atexit.register(shutil.rmtree, state_root, ignore_errors=True)
    return state_root


TEST_STATE_ROOT = _prepare_environment()

# ---------------------------------------------------------------------------------------------------------------
# 2. Network guard
# ---------------------------------------------------------------------------------------------------------------


class NetworkBlockedError(OSError):
    """Raised for a non-loopback connection attempted by an offline test."""


class _NetworkBlockedDNSError(NetworkBlockedError, socket.gaierror):
    """The same refusal for a DNS lookup, as the resolver error type HTTP clients already handle."""


_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_local_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "ignore")
    host = str(host).strip().strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        if host == socket.gethostname().lower():
            return True  # the machine's own name
    except OSError:  # pragma: no cover
        pass
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


class _NetworkGuard:
    def __init__(self) -> None:
        self.allowed = False
        self.attempts: List[Tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self._restore: List[Tuple[Any, str, Any]] = []

    def _refuse(self, kind: str, target: Any, dns: bool) -> OSError:
        test = os.environ.get("PYTEST_CURRENT_TEST", "<collection>")
        with self._lock:
            self.attempts.append((test, kind, str(target)))
        cls = _NetworkBlockedDNSError if dns else NetworkBlockedError
        return cls(f"network access is blocked in offline tests ({kind} {target!r}); mark the test "
                   "@pytest.mark.network and run with PHRONESIS_LIVE_TESTS=1, or use a fake")

    def _check_address(self, sock: Any, address: Any) -> None:
        if self.allowed:
            return
        if getattr(sock, "family", None) in (socket.AF_INET, getattr(socket, "AF_INET6", -1)) \
                and isinstance(address, tuple) and address and not _is_local_host(address[0]):
            raise self._refuse("connect", f"{address[0]}:{address[1] if len(address) > 1 else ''}", dns=False)

    def install(self) -> None:
        if self._restore:
            return
        guard = self
        real_getaddrinfo = socket.getaddrinfo
        real_gethostbyname = socket.gethostbyname
        real_gethostbyname_ex = socket.gethostbyname_ex
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex

        # functools.wraps keeps __module__ == "socket", which other code checks after restoring its own guards.
        @functools.wraps(real_getaddrinfo)
        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not guard.allowed and not _is_local_host(host):
                raise guard._refuse("dns", host, dns=True)
            return real_getaddrinfo(host, *args, **kwargs)

        @functools.wraps(real_gethostbyname)
        def gethostbyname(host: Any) -> Any:
            if not guard.allowed and not _is_local_host(host):
                raise guard._refuse("dns", host, dns=True)
            return real_gethostbyname(host)

        @functools.wraps(real_gethostbyname_ex)
        def gethostbyname_ex(host: Any) -> Any:
            if not guard.allowed and not _is_local_host(host):
                raise guard._refuse("dns", host, dns=True)
            return real_gethostbyname_ex(host)

        def connect(sock: Any, address: Any) -> Any:
            guard._check_address(sock, address)
            return real_connect(sock, address)

        def connect_ex(sock: Any, address: Any) -> Any:
            guard._check_address(sock, address)
            return real_connect_ex(sock, address)

        self._restore = [
            (socket, "getaddrinfo", real_getaddrinfo),
            (socket, "gethostbyname", real_gethostbyname),
            (socket, "gethostbyname_ex", real_gethostbyname_ex),
            (socket.socket, "connect", real_connect),
            (socket.socket, "connect_ex", real_connect_ex),
        ]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.gethostbyname = gethostbyname  # type: ignore[assignment]
        socket.gethostbyname_ex = gethostbyname_ex  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]

        # The Windows proactor event loop connects through IOCP, not socket.connect (IP literals skip DNS).
        if sys.platform == "win32":
            try:
                from asyncio import windows_events

                real_iocp_connect = windows_events.IocpProactor.connect

                def iocp_connect(proactor: Any, conn: Any, address: Any) -> Any:
                    guard._check_address(conn, address)
                    return real_iocp_connect(proactor, conn, address)

                self._restore.append((windows_events.IocpProactor, "connect", real_iocp_connect))
                windows_events.IocpProactor.connect = iocp_connect  # type: ignore[method-assign]
            except (ImportError, AttributeError):  # pragma: no cover
                pass

    def uninstall(self) -> None:
        while self._restore:
            owner, name, value = self._restore.pop()
            setattr(owner, name, value)

    def take_attempts(self) -> List[Tuple[str, str, str]]:
        with self._lock:
            out, self.attempts = self.attempts, []
        return out


NETWORK_GUARD = _NetworkGuard()
NETWORK_GUARD.install()


def pytest_unconfigure(config):
    NETWORK_GUARD.uninstall()


@pytest.fixture(autouse=True)
def _offline_network_guard(request):
    """Fail any test that is not a live @network test and still tried to reach a non-loopback host."""
    may_connect = LIVE_TESTS and request.node.get_closest_marker("network") is not None
    NETWORK_GUARD.take_attempts()  # attempts from collection or background threads of earlier tests
    NETWORK_GUARD.allowed = may_connect
    try:
        yield
    finally:
        NETWORK_GUARD.allowed = False
        attempts = NETWORK_GUARD.take_attempts()
    if attempts and not may_connect:
        shown = "; ".join(f"{kind} {target}" for _test, kind, target in attempts[:5])
        pytest.fail(f"offline test attempted network access ({len(attempts)} blocked: {shown}). Use a fake, or mark "
                    "it @pytest.mark.network so it runs only with PHRONESIS_LIVE_TESTS=1.", pytrace=False)


# ---------------------------------------------------------------------------------------------------------------
# 3. Markers and shared fixtures
# ---------------------------------------------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    # Tests marked `network` call live upstream APIs (and the configured LLM); the default run stays offline.
    if LIVE_TESTS:
        return
    skip = pytest.mark.skip(reason="live network test; set PHRONESIS_LIVE_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def offline_biomd10(monkeypatch, tmp_path):
    """Serve BIOMD0000000010 from the bundled SBML fixture through a private model cache: no network."""
    from phronesis.vivarix.engine import VivarixSimulationEngine

    cache = tmp_path / "biomodels_cache"
    cache.mkdir()
    shutil.copy(Path(__file__).parent / "fixtures" / "datasets" / "BIOMD0000000010_url.xml", cache / "BIOMD0000000010.xml")
    monkeypatch.setattr(VivarixSimulationEngine, "_cache_dir", staticmethod(lambda: cache))
    return cache


@pytest.fixture(autouse=True, scope="session")
def _load_extensions_once():
    # load_extensions() imports each module once per process; doing it here, before any test enters
    # hooks.isolated_hooks(), keeps a first import inside an isolated block from discarding its registrations.
    from phronesis.extensions import load_extensions

    load_extensions()
    yield
