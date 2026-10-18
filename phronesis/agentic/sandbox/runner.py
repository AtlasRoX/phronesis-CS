"""
Run one cell of model-written analysis code in a separate, constrained Python process.

Layers (after the AST policy in policy.py), following the CodeAct action loop (Wang et al., ICML 2024,
arXiv:2402.01030) and the notebook-sandbox harness of BixBench (Mitchener et al. 2025, arXiv:2503.00096):

1. A fresh temporary directory per run: work/ (the child's cwd, holding read-only copies of the input files only),
   io/ (bootstrap, code, config, captured output) and tmp/ (TEMP for libraries). Everything is deleted afterwards.
2. `sys.executable -I` (isolated mode: no PYTHON* variables, no user site, the script dir is not on sys.path;
   `-S` is not used because the scientific stack lives in site-packages).
3. An environment scrubbed to an allowlist (PATH, SYSTEMROOT/WINDIR, TEMP/TMP, PYTHONHASHSEED and the
   thread/backend settings the runner sets itself), so API keys and tokens never reach the child.
4. A bootstrap that, before user code runs: applies resource limits (POSIX RLIMIT_AS/CPU/NOFILE/FSIZE; on Windows
   a Job Object with a per-process memory, CPU-time and active-process limit, best effort), replaces
   socket.socket/create_connection/getaddrinfo with raising stubs, selects the matplotlib Agg backend, caps
   stdout/stderr, and executes the code with a restricted builtins dict (allowlisted __import__, read-only open()
   inside the work dir, getattr that refuses dunder and banned names, no eval/exec/compile/vars/globals).
5. A wall-clock timeout that kills the whole process tree (taskkill /T on Windows, the process group on POSIX).

The code's `result` variable (JSON-serialisable, else its repr), a `tables` dict of DataFrames and up to three open
matplotlib figures (PNG, base64) are returned. None of this is a security boundary against a determined attacker;
see docs/agentic/sandbox-security.md for the threat model and the container setup production should use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional

from .policy import (
    ALLOWED_MODULES,
    BANNED_ATTRIBUTES,
    BANNED_CALLS,
    BANNED_NAMES,
    READER_FUNCTIONS,
    READER_PATH_KWARGS,
    check_code,
    safe_relative_path,
)

logger = logging.getLogger(__name__)

MAX_FIGURES = 3
MAX_TABLES = 5
MAX_TABLE_ROWS = 50
MAX_FIGURE_BYTES = 1_500_000
STDERR_TAIL_BYTES = 8 * 1024
_INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "PATHEXT")

_BOOTSTRAP = r'''
import base64 as _base64
import builtins as _builtins
import io as _io
import json as _json
import math as _math
import os as _os
import sys as _sys
import time as _time
import traceback as _traceback

with open(_sys.argv[1], encoding="utf-8") as _fh:
    _CFG = _json.load(_fh)
_os.remove(_sys.argv[1])
_WORK = _os.path.realpath(_CFG["work_dir"])
_OUT = _CFG["out_path"]
_dumps = _json.dumps
_b64 = _base64.b64encode
_real_open = _builtins.open
_real_import = _builtins.__import__
_real_getattr = _builtins.getattr
_LIMITS = {"memory": "none", "cpu": "none", "processes": "unrestricted"}


def _posix_limits(memory_mb, cpu_s):
    import resource
    mem = int(memory_mb) * 1024 * 1024
    for name, value in (("RLIMIT_AS", mem), ("RLIMIT_CPU", int(cpu_s)), ("RLIMIT_NOFILE", 256),
                        ("RLIMIT_FSIZE", 256 * 1024 * 1024)):
        lim = getattr(resource, name, None)
        if lim is None:
            continue
        try:
            resource.setrlimit(lim, (value, value))
        except (ValueError, OSError):
            pass
    _LIMITS.update(memory=f"RLIMIT_AS {memory_mb} MB", cpu=f"RLIMIT_CPU {int(cpu_s)} s")


_JOB = None


def _windows_job(memory_mb, cpu_s):
    global _JOB
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return
    info = _Extended()
    # PROCESS_TIME | ACTIVE_PROCESS | PROCESS_MEMORY
    info.BasicLimitInformation.LimitFlags = 0x00000002 | 0x00000008 | 0x00000100
    info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_s * 10_000_000)
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return
    if k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        _JOB = job
        _LIMITS.update(memory=f"job object {memory_mb} MB", cpu=f"job object {int(cpu_s)} s", processes="1")


try:
    if _os.name == "posix":
        _posix_limits(_CFG["memory_mb"], _CFG["cpu_s"])
    elif _os.name == "nt":
        _windows_job(_CFG["memory_mb"], _CFG["cpu_s"])
except Exception:
    pass


def _block_network():
    import socket as _socket

    def _deny(*_a, **_k):
        raise PermissionError("network access is disabled in the analysis sandbox")

    class _BlockedSocket(_socket.socket):
        def __init__(self, *_a, **_k):
            _deny()

    _socket.socket = _BlockedSocket
    for _name in ("create_connection", "create_server", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                  "gethostbyaddr", "socketpair", "fromfd"):
        if hasattr(_socket, _name):
            setattr(_socket, _name, _deny)
    try:
        import _socket as _csocket
        for _name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            if hasattr(_csocket, _name):
                setattr(_csocket, _name, _deny)
    except Exception:
        pass


_block_network()


class _Capped(_io.TextIOBase):
    def __init__(self, inner, limit):
        self._inner, self._left, self.truncated = inner, int(limit), False

    def write(self, s):
        s = str(s)
        if self._left <= 0:
            self.truncated = True
            return len(s)
        piece = s[: self._left]
        self._left -= len(piece)
        if len(piece) < len(s):
            self.truncated = True
        self._inner.write(piece)
        return len(s)

    def flush(self):
        self._inner.flush()


_sys.stdout = _Capped(_sys.stdout, _CFG["max_output_bytes"])
_sys.stderr = _Capped(_sys.stderr, _CFG["max_output_bytes"])

_ALLOWED = frozenset(_CFG["allowed_modules"])
_BANNED_ATTRS = frozenset(_CFG["banned_attributes"]) | frozenset(_CFG["banned_names"])
_READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})


_READER_FUNCS = tuple(_CFG.get("reader_functions") or ())
_READER_KWARGS = tuple(_CFG.get("reader_path_kwargs") or ())
_READER_ROOTS = frozenset({"pandas", "numpy", "anndata", "scanpy", "scipy", "matplotlib", "sklearn"})
_READER_WRAPPERS = {}


def _inside_work(path):
    real = _os.path.realpath(_os.path.join(_WORK, path))
    try:
        return _os.path.commonpath([_os.path.normcase(real), _os.path.normcase(_WORK)]) == _os.path.normcase(_WORK)
    except ValueError:
        return False


def _reader_target_ok(target):
    if hasattr(target, "__fspath__"):
        target = _os.fspath(target)
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    if not isinstance(target, str):
        return True  # a file object (opened through the guarded open()), an array, a buffer
    text = target.strip()
    if "://" in text or text.startswith("~") or "\x00" in text:
        return False
    return _inside_work(text)


def _wrap_reader(fn, name):
    def _guarded_reader(*args, **kwargs):
        target = args[0] if args else next((kwargs[k] for k in _READER_KWARGS if k in kwargs), None)
        if target is not None and not _reader_target_ok(target):
            raise PermissionError(f"{name}() may only read files inside the work directory")
        return fn(*args, **kwargs)
    _guarded_reader.__name__ = name
    _guarded_reader.__doc__ = getattr(fn, "__doc__", None)
    _guarded_reader._phronesis_guarded = True
    return _guarded_reader


def _guard_readers():
    """Replace the file-reading functions of the loaded scientific modules with path-checking wrappers."""
    for mod_name, mod in list(_sys.modules.items()):
        if mod is None or mod_name.split(".", 1)[0] not in _READER_ROOTS:
            continue
        try:
            ns = vars(mod)
        except TypeError:
            continue
        for attr in _READER_FUNCS:
            fn = ns.get(attr)
            if fn is None or isinstance(fn, type) or not callable(fn) or getattr(fn, "_phronesis_guarded", False):
                continue
            if type(fn).__name__ == "module":
                continue
            wrapper = _READER_WRAPPERS.get(id(fn))
            if wrapper is None:
                wrapper = _READER_WRAPPERS[id(fn)] = (_wrap_reader(fn, attr), fn)
            try:
                setattr(mod, attr, wrapper[0])
            except Exception:
                pass


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or str(name).split(".", 1)[0] not in _ALLOWED:
        raise ImportError(f"import of '{name}' is not allowed in the analysis sandbox")
    for item in fromlist or ():
        if str(item).startswith("__") or item in _BANNED_ATTRS:
            raise ImportError(f"importing '{item}' is not allowed in the analysis sandbox")
    module = _real_import(name, globals, locals, fromlist, level)
    if str(name).split(".", 1)[0] in _READER_ROOTS:
        _guard_readers()
    return module


def _guarded_open(file, mode="r", *args, **kwargs):
    if not isinstance(file, str) or mode not in _READ_MODES:
        raise PermissionError("open() is read-only in the analysis sandbox and takes a file name")
    real = _os.path.realpath(_os.path.join(_WORK, file))
    if not _inside_work(file):
        raise PermissionError("open() may only read files inside the work directory")
    return _real_open(real, mode, *args, **kwargs)


_MISSING = object()


def _guarded_getattr(obj, name, default=_MISSING):
    if not isinstance(name, str) or name.startswith("__") or name in _BANNED_ATTRS:
        raise AttributeError(f"attribute '{name}' is not available in the analysis sandbox")
    if default is _MISSING:
        return _real_getattr(obj, name)
    return _real_getattr(obj, name, default)


def _guarded_hasattr(obj, name):
    try:
        _guarded_getattr(obj, name)
        return True
    except AttributeError:
        return False


def _restricted_builtins():
    banned = set(_CFG["banned_calls"]) | {"open", "getattr", "hasattr", "__import__", "__loader__", "__spec__"}
    safe = {k: v for k, v in vars(_builtins).items() if k not in banned}
    safe.update(__import__=_guarded_import, open=_guarded_open, getattr=_guarded_getattr, hasattr=_guarded_hasattr)
    return safe


def _scalar(v):
    if isinstance(v, float):
        return v if _math.isfinite(v) else None
    return v


def _jsonable(v, depth=0):
    if depth > 6:
        return repr(v)[:200]
    if v is None or isinstance(v, (bool, int, str)):
        return v[:4000] if isinstance(v, str) else v
    if isinstance(v, float):
        return _scalar(v)
    pd = _sys.modules.get("pandas")
    np = _sys.modules.get("numpy")
    if pd is not None and isinstance(v, pd.DataFrame):
        return {"table": "result", "shape": [int(v.shape[0]), int(v.shape[1])]}
    if pd is not None and isinstance(v, pd.Series):
        return {str(k): _jsonable(x, depth + 1) for k, x in list(v.items())[:MAX_ROWS]}
    if np is not None and isinstance(v, np.ndarray):
        return _jsonable(v.ravel()[:200].tolist() if v.ndim != 1 else v[:200].tolist(), depth + 1)
    if np is not None and isinstance(v, np.generic):
        return _jsonable(v.item(), depth + 1)
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in list(v.items())[:200]}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x, depth + 1) for x in list(v)[:200]]
    return repr(v)[:2000]


MAX_ROWS = _CFG["max_table_rows"]


def _table(name, df):
    pd = _sys.modules.get("pandas")
    if pd is None:
        return None
    if isinstance(df, pd.Series):
        df = df.to_frame()
    if not isinstance(df, pd.DataFrame):
        return None
    head = df.head(MAX_ROWS)
    index_name = str(head.index.name or "index")
    columns = [index_name] + [str(c) for c in head.columns]
    rows = []
    for idx, row in zip(head.index, head.itertuples(index=False, name=None)):
        rows.append([_jsonable(idx)] + [_jsonable(x) for x in row])
    return {"name": str(name)[:80], "columns": columns[:60], "rows": [r[:60] for r in rows],
            "n_rows": int(df.shape[0]), "n_columns": int(df.shape[1])}


def _figures(limit):
    plt = _sys.modules.get("matplotlib.pyplot")
    out = []
    if plt is None:
        return out
    for num in list(plt.get_fignums())[:limit]:
        try:
            buf = _io.BytesIO()
            plt.figure(num).savefig(buf, format="png", dpi=100, bbox_inches="tight")
            data = buf.getvalue()
            if len(data) <= _CFG["max_figure_bytes"]:
                out.append({"name": f"figure_{num}.png", "png_base64": _b64(data).decode("ascii")})
        except Exception:
            continue
    return out


def _main():
    with _real_open(_CFG["code_path"], encoding="utf-8") as fh:
        src = fh.read()
    lines = src.splitlines()
    g = {"__builtins__": _restricted_builtins(), "__name__": "__analysis__", "INPUT_FILES": list(_CFG["inputs"])}
    status, error = "ok", None
    t0 = _time.perf_counter()
    try:
        exec(compile(src, "<analysis>", "exec"), g)
    except BaseException as exc:  # noqa: BLE001 - SystemExit/KeyboardInterrupt from user code are failures too
        status = "error"
        frames = [f for f in _traceback.extract_tb(exc.__traceback__) if f.filename == "<analysis>"]
        parts = []
        for f in frames[-3:]:
            text = lines[f.lineno - 1].strip() if f.lineno and 0 < f.lineno <= len(lines) else ""
            parts.append(f"  line {f.lineno}: {text}")
        parts.append("".join(_traceback.format_exception_only(type(exc), exc)).strip())
        error = "\n".join(parts)[-4000:]
    seconds = _time.perf_counter() - t0
    tables, result = [], None
    try:
        pd = _sys.modules.get("pandas")
        raw = g.get("result")
        if pd is not None and isinstance(raw, pd.DataFrame):
            t = _table("result", raw)
            if t:
                tables.append(t)
        result = _jsonable(raw)
        extra = g.get("tables")
        if isinstance(extra, dict):
            for name, df in list(extra.items())[: _CFG["max_tables"]]:
                t = _table(name, df)
                if t and len(tables) < _CFG["max_tables"]:
                    tables.append(t)
    except Exception as exc:  # noqa: BLE001
        result = f"<result could not be serialised: {type(exc).__name__}>"
    figures = []
    try:
        figures = _figures(_CFG["max_figures"])
    except Exception:
        pass
    payload = {"status": status, "error": error, "result": result, "tables": tables, "figures": figures,
               "exec_seconds": round(seconds, 3), "limits": _LIMITS,
               "stdout_truncated": bool(getattr(_sys.stdout, "truncated", False))}
    try:
        text = _dumps(payload, allow_nan=False, default=str)
    except ValueError:
        payload["result"] = repr(result)[:4000]
        text = _dumps(payload, default=str)
    with _real_open(_OUT, "w", encoding="utf-8") as fh:
        fh.write(text)


_main()
'''


def sandbox_python() -> str:
    return sys.executable


def _child_env(root: str, tmp: str) -> Dict[str, str]:
    """The child's whole environment: an allowlist, never API keys or tokens."""
    env: Dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    mpl_dir = os.path.join(tempfile.gettempdir(), "phronesis-sandbox-mpl")  # font cache shared across runs
    try:
        os.makedirs(mpl_dir, exist_ok=True)
    except OSError:
        mpl_dir = os.path.join(root, "mpl")
    env.update(
        {
            "TEMP": tmp,
            "TMP": tmp,
            "TMPDIR": tmp,
            "PYTHONHASHSEED": "0",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": mpl_dir,
            "NUMBA_CACHE_DIR": tmp,
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _rmtree(path: str) -> None:
    def onerror(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onerror)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=onerror)


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _read_capped(path: str, limit: int, tail: bool = False) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if tail and size > limit:
                fh.seek(size - limit)
            data = fh.read(limit)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text = ("...(truncated)\n" + text) if tail else (text + "\n...(truncated)")
    return text


def _empty(status: str, error: Optional[str], **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "status": status,
        "stdout": "",
        "stderr": "",
        "result": None,
        "figures": [],
        "tables": [],
        "seconds": 0.0,
        "error": error,
        "violations": [],
        "limits": {},
    }
    out.update(extra)
    return out


def run_code(
    code: str,
    input_files: Optional[Dict[str, str]] = None,
    timeout_s: float = 60,
    max_output_kb: int = 256,
    memory_mb: int = 2048,
    *,
    max_figures: int = MAX_FIGURES,
    _unsafe_skip_policy: bool = False,
) -> Dict[str, Any]:
    """
    Execute one analysis cell. input_files maps the name the code reads (a plain file name) to a server path the
    caller has already validated; each is copied read-only into the work dir. Returns
    {status: ok|error|timeout|policy_violation|failed, stdout, stderr, result, figures[{name,png_base64}], tables,
    seconds, error, violations, limits}.

    _unsafe_skip_policy (tests only) disables the AST policy and the runtime import/builtins guards so a test can
    inspect the child's environment; the subprocess, env scrubbing and network stub still apply.
    """
    inputs = dict(input_files or {})
    for name in inputs:
        if not _INPUT_NAME_RE.match(str(name)) or not safe_relative_path(str(name)):
            return _empty("failed", f"invalid input file name {str(name)[:60]!r}")
    if not _unsafe_skip_policy:
        violations = check_code(code, inputs.keys())
        if violations:
            return _empty("policy_violation", "the code was rejected by the sandbox policy", violations=violations)
    timeout_s = max(1.0, float(timeout_s))
    max_bytes = max(1, int(max_output_kb)) * 1024

    root = tempfile.mkdtemp(prefix="phronesis-sbx-")
    work, io_dir, tmp = (os.path.join(root, d) for d in ("work", "io", "tmp"))
    t0 = time.perf_counter()
    try:
        for d in (work, io_dir, tmp):
            os.makedirs(d, exist_ok=True)
        for name, src in inputs.items():
            if not src or not os.path.isfile(src):
                return _empty("failed", f"input file {name} is not available")
            dst = os.path.join(work, name)
            shutil.copyfile(src, dst)
            os.chmod(dst, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

        token = secrets.token_hex(8)
        code_path = os.path.join(io_dir, f"cell_{token}.py")
        boot_path = os.path.join(io_dir, f"bootstrap_{token}.py")
        cfg_path = os.path.join(io_dir, f"config_{token}.json")
        out_path = os.path.join(io_dir, f"out_{token}.json")
        with open(code_path, "w", encoding="utf-8") as fh:
            fh.write(code)
        boot = _BOOTSTRAP
        if _unsafe_skip_policy:
            boot = boot.replace('g = {"__builtins__": _restricted_builtins(),', 'g = {"__builtins__": _builtins,')
        with open(boot_path, "w", encoding="utf-8") as fh:
            fh.write(boot)
        cfg = {
            "work_dir": work,
            "out_path": out_path,
            "code_path": code_path,
            "inputs": sorted(inputs),
            "memory_mb": int(memory_mb),
            "cpu_s": int(timeout_s) + 5,
            "max_output_bytes": max_bytes,
            "max_figures": max(0, min(MAX_FIGURES, int(max_figures))),
            "max_figure_bytes": MAX_FIGURE_BYTES,
            "max_tables": MAX_TABLES,
            "max_table_rows": MAX_TABLE_ROWS,
            "allowed_modules": sorted(ALLOWED_MODULES),
            "banned_attributes": sorted(BANNED_ATTRIBUTES),
            "banned_names": sorted(BANNED_NAMES),
            "banned_calls": sorted(BANNED_CALLS),
            "reader_functions": sorted(READER_FUNCTIONS),
            "reader_path_kwargs": list(READER_PATH_KWARGS),
        }
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

        stdout_path, stderr_path = os.path.join(io_dir, "stdout.txt"), os.path.join(io_dir, "stderr.txt")
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        timed_out = False
        with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
            proc = subprocess.Popen(
                [sandbox_python(), "-I", boot_path, cfg_path],
                cwd=work,
                env=_child_env(root, tmp),
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                **kwargs,
            )
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
        seconds = round(time.perf_counter() - t0, 3)
        stdout = _read_capped(stdout_path, max_bytes)
        stderr = _read_capped(stderr_path, STDERR_TAIL_BYTES, tail=True)
        if timed_out:
            return _empty("timeout", f"the analysis exceeded the {timeout_s:g} s time limit and was stopped",
                          stdout=stdout, stderr=stderr, seconds=seconds)
        try:
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            tail = stderr.strip().splitlines()[-1:] if stderr.strip() else []
            reason = tail[0][:300] if tail else f"exit code {proc.returncode}"
            killed = proc.returncode is not None and (proc.returncode < 0 or proc.returncode in (137, 3221225477))
            if killed:
                reason = f"the process was killed (exit code {proc.returncode}); it may have exceeded the memory limit"
            return _empty("error", f"the analysis process ended without a result: {reason}",
                          stdout=stdout, stderr=stderr, seconds=seconds)
        return {
            "status": "ok" if payload.get("status") == "ok" else "error",
            "stdout": stdout,
            "stderr": stderr,
            "result": payload.get("result"),
            "figures": list(payload.get("figures") or [])[:MAX_FIGURES],
            "tables": list(payload.get("tables") or [])[:MAX_TABLES],
            "seconds": seconds,
            "error": payload.get("error"),
            "violations": [],
            "limits": payload.get("limits") or {},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sandbox run failed: %s", exc, exc_info=True)
        return _empty("failed", f"{type(exc).__name__}: {str(exc)[:200]}", seconds=round(time.perf_counter() - t0, 3))
    finally:
        _rmtree(root)


__all__ = ["MAX_FIGURES", "run_code", "sandbox_python"]
