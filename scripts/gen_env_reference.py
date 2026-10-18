"""
Generate the environment-variable reference: docs/configuration.md and a root .env.example.

Descriptions, types, defaults, groups and secret flags come from the settings registry
(phronesis/config/settings.py, REGISTRY). The code is still scanned with `ast` (phronesis/ and app/, tests excluded)
for where each variable is read and to catch variables the registry does not know:

- os.getenv("NAME", default) / os.environ.get("NAME", default) / os.environ["NAME"] / "NAME" in os.environ;
- the registry accessors get_str / get_int / get_float / get_bool / get_path / get_list (phronesis.config);
- the project's env helpers: env_int / env_float / env_flag (phronesis.agentic.config), _env_int / _env_float /
  _env_mb / _switch (module-private helpers), whose second argument is the default.

A name given as a module-level string constant (e.g. DATA_DIR_ENV = "PHRONESIS_DATA_DIR") is resolved. Values are
never read from the environment or from a real .env: only registered or code defaults are reported, and
secrets (registered as secret, or named *_API_KEY, *_TOKEN, *_SECRET, *_PASSWORD, ...) are always left blank.

Usage:
    python scripts/gen_env_reference.py           # rewrite docs/configuration.md and .env.example
    python scripts/gen_env_reference.py --check   # exit 1 if either file is out of date, a variable read in the
                                                  # code is not registered, or a registered one is no longer read
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("phronesis", "app")
SKIP_PARTS = {"tests", "__pycache__", "node_modules", ".venv"}
DOC_PATH = ROOT / "docs" / "configuration.md"
EXAMPLE_PATH = ROOT / ".env.example"

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
SECRET_RE = re.compile(r"(API_KEY|_KEY$|_TOKEN$|^TOKEN|_TOKEN_|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)")
ACCESSORS = {"get_str", "get_int", "get_float", "get_bool", "get_path", "get_list"}  # phronesis.config
DEFAULT_HELPERS = {"env_int", "env_float", "env_flag", "_env_int", "_env_float", "_env_mb", "_env_flag", "_env_bool",
                   *ACCESSORS}
NO_DEFAULT_HELPERS = {"_switch"}  # off|auto|on switches whose default is "auto"
HELPER_DEFAULTS = {"_switch": "auto", "env_flag": "auto"}

# Variables that must appear even if a future refactor hides them from the scan, with a short description.
REQUIRED = ("TAVILY_API_KEY", "NVIDIA_API_KEY", "PHRONESIS_SYNLETHDB_DIR", "PHRONESIS_DEPMAP_DIR", "PHRONESIS_DATA_DIR",
            "PHRONESIS_AUTH_MODE")

SETTINGS_PATH = ROOT / "phronesis" / "config" / "settings.py"


def load_settings() -> Any:
    """phronesis/config/settings.py loaded on its own (stdlib only), without importing the heavy phronesis package."""
    name = "_phronesis_settings_registry"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SETTINGS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@dataclass
class Var:
    name: str
    defaults: Set[str] = field(default_factory=set)
    locations: List[str] = field(default_factory=list)

    @property
    def setting(self) -> Any:
        return load_settings().REGISTRY.get(self.name)

    @property
    def secret(self) -> bool:
        setting = self.setting
        return bool(SECRET_RE.search(self.name)) or bool(setting is not None and setting.secret)


def _iter_files() -> Iterable[Path]:
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if SKIP_PARTS.intersection(path.relative_to(ROOT).parts) or path.name.startswith("test_"):
                continue
            yield path


def _module_constants(tree: ast.Module) -> Dict[str, Any]:
    """Module-level NAME = <str | int | float> assignments (plain or annotated)."""
    out: Dict[str, Any] = {}
    for node in tree.body:
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, (str, int, float)) and not isinstance(value.value, bool):
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = value.value
    return out


def _is_os_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"


def _func_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _default_text(node: Optional[ast.AST], source: str, consts: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if node is None:
        return None
    if consts:
        if isinstance(node, ast.Name) and node.id in consts:
            return str(consts[node.id])
        # str(CONST) / float(CONST) / int(CONST)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "int", "float")
                and len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id in consts):
            return str(consts[node.args[0].id])
    if isinstance(node, ast.Constant):
        if node.value is None:
            return None
        if isinstance(node.value, bool):
            return str(node.value).lower()
        return str(node.value)
    seg = ast.get_source_segment(source, node) or ""
    return f"`{seg}`" if seg else None


def scan() -> Dict[str, Var]:
    found: Dict[str, Var] = {}

    def add(name: Optional[str], default: Optional[str], where: str) -> None:
        if not name or not NAME_RE.match(name):
            return
        var = found.setdefault(name, Var(name))
        if default not in (None, ""):
            var.defaults.add(default)
        if where not in var.locations:
            var.locations.append(where)

    for path in _iter_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        consts = _module_constants(tree)
        # `os.getenv(NAME, "") or "auto"`: the right-hand constant is the effective default.
        or_defaults: Dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
                left, right = node.values
                if isinstance(left, ast.Call) and isinstance(right, ast.Constant) and right.value not in (None, ""):
                    or_defaults[id(left)] = right
        rel = path.relative_to(ROOT).as_posix()

        def name_of(node: Optional[ast.AST]) -> Optional[str]:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name) and isinstance(consts.get(node.id), str):
                return consts[node.id]
            return None

        for node in ast.walk(tree):
            where = f"{rel}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Call) and node.args:
                fname = _func_name(node.func)
                is_getenv = fname == "getenv" and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                is_environ_get = fname == "get" and isinstance(node.func, ast.Attribute) and _is_os_environ(node.func.value)
                if is_getenv or is_environ_get or fname in DEFAULT_HELPERS:
                    dnode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "default"), None)
                    default = _default_text(dnode, source, consts)
                    if default in (None, "") and id(node) in or_defaults:
                        default = _default_text(or_defaults[id(node)], source, consts)
                    if default is None and fname in HELPER_DEFAULTS:
                        default = HELPER_DEFAULTS[fname]
                    add(name_of(node.args[0]), default, where)
                elif fname in NO_DEFAULT_HELPERS:
                    add(name_of(node.args[0]), HELPER_DEFAULTS.get(fname), where)
            elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                if isinstance(node.ctx, ast.Load):
                    add(name_of(node.slice), None, where)
            elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
                if node.comparators and _is_os_environ(node.comparators[0]):
                    add(name_of(node.left), None, where)
    missing = [n for n in REQUIRED if n not in found]
    if missing:
        raise SystemExit(f"required variables not found in the code: {', '.join(missing)}")
    return found


UNREGISTERED = "Unregistered"


def grouped(found: Dict[str, Var]) -> List[Tuple[str, List[Var]]]:
    """(heading, variables) in the registry's group order; unregistered variables come last."""
    settings = load_settings()
    groups: Dict[str, List[Var]] = defaultdict(list)
    for var in found.values():
        setting = var.setting
        groups[setting.group if setting is not None else UNREGISTERED].append(var)
    order = [g for g in settings.GROUPS if g in groups] + ([UNREGISTERED] if UNREGISTERED in groups else [])
    out: List[Tuple[str, List[Var]]] = []
    for group in order:
        prefix = settings.GROUPS.get(group, "")
        heading = f"{prefix}_*: {group}" if prefix else group
        out.append((heading, sorted(groups[group], key=lambda v: v.name)))
    return out


def registry_problems(found: Dict[str, Var]) -> List[str]:
    """Variables read in the code but not registered, and registered variables no longer read."""
    registry = load_settings().REGISTRY
    problems = [f"not registered in phronesis/config/settings.py: {n} (read at {found[n].locations[0]})"
                for n in sorted(set(found) - set(registry))]
    problems += [f"registered but no longer read in phronesis/ or app/: {n}" for n in sorted(set(registry) - set(found))]
    return problems


def _registered_default(var: Var) -> Optional[str]:
    setting = var.setting
    return load_settings().format_default(setting) if setting is not None else None


def _default_cell(var: Var) -> str:
    if var.secret:
        return "(secret, unset)" if not var.defaults else "(secret)"
    registered = _registered_default(var)
    if registered is not None:
        return f"`{registered}`"
    if not var.defaults:
        return "unset"
    return " / ".join(sorted(d if d.startswith("`") else f"`{d}`" for d in var.defaults))


def _type_cell(var: Var) -> str:
    setting = var.setting
    return load_settings().describe_type(setting) if setting is not None else ""


def _description(var: Var) -> str:
    setting = var.setting
    return setting.description if setting is not None else ""


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(found: Dict[str, Var]) -> str:
    lines = [
        "# Configuration reference",
        "",
        "<!-- Generated by scripts/gen_env_reference.py; do not edit by hand. -->",
        "",
        "Every environment variable read by `phronesis/` and `app/` (tests excluded). Types, defaults and descriptions",
        "come from the settings registry (`phronesis/config/settings.py`); the \"Read at\" column comes from scanning the",
        "code. Regenerate with `python scripts/gen_env_reference.py`; CI can run it with `--check`, which also fails when a",
        "variable read in the code is not registered. A commented template is in",
        "[`.env.example`](https://github.com/AtlasRoX/phronesis-CS/blob/main/.env.example).",
        "",
        "At startup the API validates the environment (`phronesis.config.validate_environment`): unparsable or",
        "out-of-range numbers, unknown choices, non-boolean flags and `PHRONESIS_*` names that no code reads (likely",
        "typos) are logged as warnings, counted in `config_problems` on `/api/health`, and fail startup only with",
        "`PHRONESIS_STRICT_CONFIG=1`. Invalid values fall back to the default.",
        "",
        "Precedence for state directories: a specific variable (`PHRONESIS_MEMORY_DIR`, `PHRONESIS_RUNS_DIR`,",
        "`PHRONESIS_UPLOAD_ROOT`, `PHRONESIS_JOBS_DIR`, `PHRONESIS_TOOL_CACHE_DIR`) wins over `PHRONESIS_DATA_DIR`; with",
        "neither set, every store keeps its historical location under the workspace.",
        "",
        f"{len(found)} variables.",
        "",
    ]
    for heading, items in grouped(found):
        lines += [f"## {heading}", "", "| Variable | Type | Default | Read at | Description |", "| --- | --- | --- | --- | --- |"]
        for var in items:
            # Files only: line numbers would make --check fail on every unrelated edit that shifts a line.
            files = list(dict.fromkeys(loc.rsplit(":", 1)[0] for loc in var.locations))
            locs = files[:3]
            more = f" (+{len(files) - 3})" if len(files) > 3 else ""
            where = "<br>".join(f"`{loc}`" for loc in locs) + more
            cells = (f"`{var.name}`", _md_escape(_type_cell(var)), _md_escape(_default_cell(var)), where,
                     _md_escape(_description(var)))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _example_value(var: Var) -> str:
    if var.secret:
        return ""
    value = _registered_default(var)
    if value is None:
        # A registered variable without a registered default stays blank (code defaults are shown as a hint).
        if var.setting is not None or len(var.defaults) != 1:
            return ""
        value = next(iter(var.defaults))
    return "" if value.startswith("`") or "\n" in value else value


def render_example(found: Dict[str, Var]) -> str:
    lines = [
        "# Phronesis environment template, generated by scripts/gen_env_reference.py (see docs/configuration.md).",
        "# Copy to .env and uncomment what you need. Every line shows the default; secrets are left blank.",
        "# Never commit a filled-in .env.",
        "",
    ]
    for heading, items in grouped(found):
        lines += [f"# --- {heading} ---"]
        for var in items:
            note = _description(var)
            if note:
                lines.append(f"# {note}")
            value = _example_value(var)
            hint = ""
            if not value and var.defaults and not var.secret:
                hint = "  # default: " + " / ".join(sorted(d.strip("`") for d in var.defaults))
            lines.append(f"# {var.name}={value}{hint}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the generated files are out of date")
    args = parser.parse_args(argv)
    found = scan()
    outputs = {DOC_PATH: render_markdown(found), EXAMPLE_PATH: render_example(found)}
    problems = registry_problems(found)
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.check:
        stale = [p for p, text in outputs.items() if not p.is_file() or p.read_text(encoding="utf-8") != text]
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT).as_posix()}", file=sys.stderr)
        return 1 if stale or problems else 0
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    print(f"{len(found)} variables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
