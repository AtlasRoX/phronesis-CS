"""
Static (AST) policy for model-written analysis code, the first of the sandbox's isolation layers.

The code is the agent's action (CodeAct, Wang et al., ICML 2024, arXiv:2402.01030), so it is untrusted: it was
written by a model that read retrieved text and uploaded data, either of which can carry injected instructions
(Greshake et al. 2023, arXiv:2302.12173). Before anything runs, the code is parsed and rejected when it:

- imports a module outside the allowlist (numpy, pandas, scipy, sklearn, matplotlib, anndata, scanpy, statsmodels,
  math, statistics, json, re, collections, itertools, functools, typing, datetime);
- names os, sys, subprocess, socket, shutil, ctypes, importlib, builtins, pickle or marshal;
- calls eval, exec, compile, __import__, globals, locals, vars, setattr, delattr, breakpoint or input, or getattr
  with anything but a plain, non-dunder string literal;
- touches any attribute or name that starts with "__", or the frame/code attributes used for sandbox escapes;
- opens a file for writing, or reads one outside the work directory (the provided input files are read by name);
- reads a file through a library reader (pd.read_csv/read_json/read_parquet/read_excel/read_table, np.load/
  loadtxt/genfromtxt, anndata.read_h5ad/read, scanpy.read, ...) with a literal path that is not a safe relative
  path inside the work dir (computed paths are checked at runtime by the wrapped readers in runner.py);
- writes a file (pandas to_*, savefig, np.save, AnnData write) anywhere but a relative path inside the work dir,
  or through a path that is not a string literal;
- uses the pickle-based readers and writers (read_pickle, to_pickle, allow_pickle=True), which execute code.

The policy is defence in depth, not a boundary: the subprocess, the scrubbed environment, the network stub and
the runtime import/getattr guards in runner.py back it up, and production deployments should still run the
runner inside a container (docs/agentic/sandbox-security.md).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable, List, Optional, Set

_BASE_ALLOWED = (
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "matplotlib",
    "anndata",
    "scanpy",
    "math",
    "statistics",
    "json",
    "re",
    "collections",
    "itertools",
    "functools",
    "typing",
    "datetime",
)


def _optional(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


ALLOWED_MODULES: frozenset = frozenset(_BASE_ALLOWED + (("statsmodels",) if _optional("statsmodels") else ()))

BANNED_NAMES: frozenset = frozenset(
    {"os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "builtins", "pickle", "marshal", "pathlib"}
)

BANNED_CALLS: frozenset = frozenset(
    {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "setattr", "delattr", "breakpoint",
     "input", "exit", "quit", "help", "memoryview", "reload"}
)

# Non-dunder attributes that reach interpreter internals (frames, code objects, tracebacks) or run code.
BANNED_ATTRIBUTES: frozenset = frozenset(
    {"f_globals", "f_locals", "f_builtins", "f_back", "f_code", "gi_frame", "gi_code", "cr_frame", "cr_code",
     "ag_frame", "tb_frame", "tb_next", "co_code", "func_globals", "read_pickle", "to_pickle", "eval",
     "system", "popen", "spawnv", "execv", "fork", "ctypeslib", "load_module", "exec_module", "to_sql",
     "read_sql", "read_sql_query", "read_sql_table", "read_clipboard", "to_clipboard"}
)

# Methods/functions that write a file: the output path must be a string literal, relative, inside the work dir.
WRITER_ATTRIBUTES: frozenset = frozenset(
    {"to_csv", "to_excel", "to_json", "to_parquet", "to_hdf", "to_feather", "to_html", "to_latex", "to_markdown",
     "to_stata", "to_xml", "to_orc", "savefig", "imsave", "save", "savez", "savez_compressed", "savetxt", "tofile",
     "write", "write_h5ad", "write_csvs", "write_loom", "write_zarr"}
)
_PATH_KWARGS = ("path", "path_or_buf", "fname", "file", "filename", "excel_writer", "buf", "filepath", "dirname")

# Library functions that read a file by path (pandas, numpy, anndata/scanpy, scipy.io, matplotlib, sklearn). They
# open files themselves, past the guarded open(), so a literal path must be a safe relative path inside the work
# dir, and a computed one is checked at runtime by the wrappers runner.py installs on these functions.
READER_FUNCTIONS: frozenset = frozenset(
    {"read_csv", "read_table", "read_fwf", "read_json", "read_parquet", "read_excel", "read_feather", "read_hdf",
     "read_orc", "read_stata", "read_sas", "read_spss", "read_xml", "read_html",
     "load", "loadtxt", "genfromtxt", "fromfile", "fromregex",
     "read", "read_h5ad", "read_loom", "read_zarr", "read_mtx", "read_text", "read_umi_tools", "read_10x_h5",
     "read_10x_mtx", "read_visium",
     "loadmat", "whosmat", "mmread", "mminfo", "readsav", "hb_read", "imread",
     "load_svmlight_file", "load_svmlight_files", "load_files"}
)
# Reader classes are not wrapped at runtime: their path must be a literal or an INPUT_FILES entry.
READER_CLASSES: frozenset = frozenset({"ExcelFile", "HDFStore", "memmap", "netcdf_file"})
READER_PATH_KWARGS = ("filepath_or_buffer", "path_or_buffer", "path_or_buf", "path", "io", "fname", "file",
                      "filename", "filepath", "file_name", "uri", "f", "container_path", "store")

_READ_MODES = {"r", "rb", "rt", "br", "tr"}
_FORMAT_DUNDER = re.compile(r"\{[^{}]*__")


def safe_relative_path(value: str) -> bool:
    """A path that stays inside the work dir: relative, no drive, no '..', no home or device prefix."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    text = value.strip()
    if text.startswith(("~", "/", "\\")) or ":" in text:
        return False
    for pure in (PurePosixPath(text), PureWindowsPath(text)):
        if pure.is_absolute() or pure.drive or any(part == ".." for part in pure.parts):
            return False
    return True


def _root(module: Optional[str]) -> str:
    return (module or "").split(".", 1)[0]


def _is_input_files_item(node: ast.AST) -> bool:
    """INPUT_FILES[<literal int or slice-free index>]: one of the provided input file names."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "INPUT_FILES"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    )


class _Checker(ast.NodeVisitor):
    def __init__(self, input_names: Set[str]) -> None:
        self.input_names = input_names
        self.violations: List[str] = []

    def flag(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        entry = f"line {line}: {message}" if line else message
        if entry not in self.violations:
            self.violations.append(entry)

    # Imports ------------------------------------------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _root(alias.name) not in ALLOWED_MODULES:
                self.flag(node, f"import of '{alias.name}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.flag(node, "relative imports are not allowed")
        elif _root(node.module) not in ALLOWED_MODULES:
            self.flag(node, f"import from '{node.module}' is not allowed")
        for alias in node.names:
            if alias.name.startswith("__") or alias.name in BANNED_NAMES or alias.name in BANNED_ATTRIBUTES:
                self.flag(node, f"importing '{alias.name}' is not allowed")
        self.generic_visit(node)

    # Names and attributes -----------------------------------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self.flag(node, f"name '{node.id}' is not allowed")
        elif node.id.startswith("__"):
            self.flag(node, f"dunder name '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self.flag(node, f"dunder attribute '.{node.attr}' is not allowed")
        elif node.attr in BANNED_ATTRIBUTES or node.attr in BANNED_NAMES:
            # Allowed packages re-export os/sys/subprocess as module attributes (pandas.io.common.os, ...).
            self.flag(node, f"attribute '.{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            if name.startswith("__"):
                self.flag(node, f"dunder name '{name}' is not allowed")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _FORMAT_DUNDER.search(node.value):
            self.flag(node, "format strings that reach dunder attributes are not allowed")

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == "allow_pickle" and not (isinstance(node.value, ast.Constant) and node.value.value is False):
            self.flag(node.value, "allow_pickle is not allowed")
        self.generic_visit(node)

    # Calls --------------------------------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        attr = func.attr if isinstance(func, ast.Attribute) else None
        if name in BANNED_CALLS:
            self.flag(node, f"call to '{name}()' is not allowed")
        if name in ("getattr", "hasattr"):
            target = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                self.flag(node, f"{name}() needs a plain string attribute name")
            elif target.value.startswith("__") or target.value in BANNED_ATTRIBUTES or target.value in BANNED_NAMES:
                self.flag(node, f"{name}() of '{target.value}' is not allowed")
        if name == "open":
            self._check_open(node)
        if attr in WRITER_ATTRIBUTES:
            self._check_writer(node, attr)
        reader = attr or name
        if reader and (reader in READER_FUNCTIONS or reader in READER_CLASSES):
            self._check_reader(node, reader)
        if attr in ("format", "format_map") and isinstance(func, ast.Attribute):
            recv = func.value
            if isinstance(recv, ast.Constant) and isinstance(recv.value, str) and "__" in recv.value:
                self.flag(node, "format strings that reach dunder attributes are not allowed")
        if attr == "query" and node.args:
            q = node.args[0]
            if not (isinstance(q, ast.Constant) and isinstance(q.value, str)) or "__" in str(getattr(q, "value", "")):
                self.flag(node, ".query() needs a plain string expression")
        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        mode_node = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        if mode_node is not None:
            if not (isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str)):
                self.flag(node, "open() needs a literal read mode")
            elif mode_node.value not in _READ_MODES:
                self.flag(node, f"open() with mode '{mode_node.value}' is not allowed (read-only)")
        path_node = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "file"), None)
        if path_node is None:
            self.flag(node, "open() needs a file name")
            return
        if isinstance(path_node, ast.Constant):
            value = path_node.value
            if not isinstance(value, str) or not safe_relative_path(value):
                self.flag(node, "open() may only read files inside the work directory")
            elif self.input_names and value not in self.input_names:
                self.flag(node, f"open() of '{value}': only the provided input files can be read")
        # A computed path is checked again at runtime by the guarded open() in the child process.

    def _check_reader(self, node: ast.Call, reader: str) -> None:
        path_node = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg in READER_PATH_KWARGS), None
        )
        if path_node is None:
            return  # fh.read(), or a reader called without a path
        if isinstance(path_node, ast.Constant):
            value = path_node.value
            # Literal JSON/HTML/XML content (pd.read_json('{"a": [1]}')) is parsed, not opened.
            if isinstance(value, str) and not value.lstrip().startswith(("{", "[", "<")) and not safe_relative_path(value):
                self.flag(node, f"{reader}() may only read files inside the work directory")
            return  # a non-string literal is not a path (fh.read(1024))
        if reader in READER_CLASSES and not _is_input_files_item(path_node):
            self.flag(node, f"{reader}() needs a literal relative path or an INPUT_FILES entry")
        # A computed path for a reader function is checked again at runtime by the wrapped reader in the child.

    def _check_writer(self, node: ast.Call, attr: str) -> None:
        path_node = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in _PATH_KWARGS), None)
        if path_node is None:
            return  # e.g. df.to_csv() returns a string
        if isinstance(path_node, ast.Constant) and path_node.value is None:
            return
        if not (isinstance(path_node, ast.Constant) and isinstance(path_node.value, str)):
            self.flag(node, f".{attr}() needs a literal relative output path")
            return
        if not safe_relative_path(path_node.value):
            self.flag(node, f".{attr}() may only write inside the work directory")
        elif path_node.value in self.input_names:
            self.flag(node, f".{attr}() may not overwrite the input file '{path_node.value}'")


def check_code(code: str, input_names: Optional[Iterable[str]] = None) -> List[str]:
    """Policy violations of one code cell (empty = allowed). Syntax errors are reported as violations."""
    if not isinstance(code, str) or not code.strip():
        return ["no code"]
    if "\x00" in code:
        return ["code contains a NUL byte"]
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    checker = _Checker({str(n) for n in (input_names or [])})
    checker.visit(tree)
    return checker.violations


__all__ = [
    "ALLOWED_MODULES",
    "BANNED_ATTRIBUTES",
    "BANNED_CALLS",
    "BANNED_NAMES",
    "READER_CLASSES",
    "READER_FUNCTIONS",
    "READER_PATH_KWARGS",
    "WRITER_ATTRIBUTES",
    "check_code",
    "safe_relative_path",
]
