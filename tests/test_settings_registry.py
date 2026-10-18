"""The declarative settings registry (phronesis/config/settings.py): coverage, accessors, validation, docs check."""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from phronesis.config import (
    REGISTRY,
    get_bool,
    get_float,
    get_int,
    get_list,
    get_path,
    get_str,
    validate_environment,
)
from phronesis.config import settings as S

ROOT = Path(__file__).resolve().parents[1]


def _gen_module():
    path = ROOT / "scripts" / "gen_env_reference.py"
    spec = importlib.util.spec_from_file_location("gen_env_reference_settings_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def gen():
    return _gen_module()


@pytest.fixture(scope="module")
def scanned(gen):
    return gen.scan()


@pytest.fixture(autouse=True)
def _fresh_warnings():
    S._reset_warnings()
    yield
    S._reset_warnings()


# -- coverage ---------------------------------------------------------------------------------------------------------

def test_every_scanned_variable_is_registered_and_every_registered_one_is_read(gen, scanned):
    assert gen.registry_problems(scanned) == []
    assert set(scanned) == set(REGISTRY)


def test_registry_entries_are_well_formed():
    for name, setting in REGISTRY.items():
        assert setting.name == name
        assert setting.type in S.TYPES, name
        assert setting.description and len(setting.description) <= 200, name
        assert "\n" not in setting.description, name
        assert setting.group in S.GROUPS, name
        if setting.choices:
            assert setting.default is None or setting.default in setting.choices, name
            assert all(target in setting.choices for target in setting.aliases.values()), name
        if setting.min is not None and setting.max is not None:
            assert setting.min <= setting.max, name
        if setting.default is not None and setting.type in ("int", "float"):
            assert isinstance(setting.default, (int, float)) and not isinstance(setting.default, bool), name
            if setting.min is not None:
                assert setting.default >= setting.min, name
            if setting.max is not None:
                assert setting.default <= setting.max, name
        if setting.type == "bool":
            assert isinstance(setting.default, bool), name
    # Every name the scanner calls a secret is registered as one (the registry may mark more).
    for name, setting in REGISTRY.items():
        if _gen_module().SECRET_RE.search(name):
            assert setting.secret, name
    for secret in ("TAVILY_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY", "PHRONESIS_JWT_SECRET", "PHRONESIS_API_TOKENS",
                   "MILVUS_PASSWORD", "MILVUS_TOKEN", "PHRONESIS_EVAL_TOKEN"):
        assert REGISTRY[secret].secret, secret


def test_framework_choices_match_the_agentic_config():
    from phronesis.agentic import config as agent_config

    assert REGISTRY["PHRONESIS_DEFAULT_FRAMEWORK"].choices == agent_config.ALL_FRAMEWORKS


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def test_helper_call_sites_agree_with_the_registry():
    """env_int/env_float/_env_int/_env_float(NAME, default, lo, hi) literals match the registered default and bounds."""
    checked = 0
    for base in ("phronesis", "app"):
        for path in (ROOT / base).rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in ("env_int", "env_float", "_env_int", "_env_float") or len(node.args) != 4:
                    continue
                name, default, lo, hi = (_literal(a) for a in node.args)
                if not isinstance(name, str) or name not in REGISTRY:
                    continue
                setting = REGISTRY[name]
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                if default is not None:
                    assert setting.default == default, where
                if lo is not None:
                    assert setting.min == lo, where
                if hi is not None:
                    assert setting.max == hi, where
                checked += 1
    assert checked >= 25


# -- accessors --------------------------------------------------------------------------------------------------------

def test_get_int_defaults_parsing_clamping_and_overrides(monkeypatch, caplog):
    monkeypatch.delenv("PHRONESIS_JOB_WORKERS", raising=False)
    assert get_int("PHRONESIS_JOB_WORKERS") == 2
    monkeypatch.setenv("PHRONESIS_JOB_WORKERS", "  ")
    assert get_int("PHRONESIS_JOB_WORKERS") == 2
    monkeypatch.setenv("PHRONESIS_JOB_WORKERS", " 7 ")
    assert get_int("PHRONESIS_JOB_WORKERS") == 7
    monkeypatch.setenv("PHRONESIS_JOB_WORKERS", "999")
    assert get_int("PHRONESIS_JOB_WORKERS") == 16  # registered max
    assert get_int("PHRONESIS_JOB_WORKERS", max=None) == 999
    assert get_int("PHRONESIS_JOB_WORKERS", 3, min=1, max=50) == 50
    monkeypatch.setenv("PHRONESIS_JOB_WORKERS", "0")
    assert get_int("PHRONESIS_JOB_WORKERS") == 1
    with caplog.at_level(logging.WARNING, logger="phronesis.config.settings"):
        monkeypatch.setenv("PHRONESIS_JOB_WORKERS", "two")
        assert get_int("PHRONESIS_JOB_WORKERS") == 2
        assert get_int("PHRONESIS_JOB_WORKERS") == 2
        monkeypatch.setenv("PHRONESIS_JOB_WORKERS", "1.5")
        assert get_int("PHRONESIS_JOB_WORKERS", 5) == 5
    bad = [r for r in caplog.records if "PHRONESIS_JOB_WORKERS" in r.getMessage() and "not an integer" in r.getMessage()]
    assert len(bad) == 2  # once per (name, value)


def test_get_float_rejects_nan_and_converts_ints(monkeypatch):
    monkeypatch.setenv("PHRONESIS_JOB_CANCEL_GRACE_S", "nan")
    assert get_float("PHRONESIS_JOB_CANCEL_GRACE_S") == 20.0
    monkeypatch.setenv("PHRONESIS_JOB_CANCEL_GRACE_S", "1e9")
    assert get_float("PHRONESIS_JOB_CANCEL_GRACE_S") == 600.0
    monkeypatch.setenv("PHRONESIS_JOB_CANCEL_GRACE_S", "2.5")
    assert get_float("PHRONESIS_JOB_CANCEL_GRACE_S") == 2.5
    monkeypatch.delenv("PHRONESIS_CONFIDENCE_T", raising=False)
    assert get_float("PHRONESIS_CONFIDENCE_T") is None  # no registered default
    value = get_float("SOME_UNREGISTERED_TEST_VAR", 3)
    assert value == 3.0 and isinstance(value, float)


def test_get_bool_words_and_fallback(monkeypatch, caplog):
    for word, expected in (("1", True), ("TRUE", True), ("yes", True), ("On", True), ("0", False), ("false", False),
                           ("NO", False), ("off", False)):
        monkeypatch.setenv("PHRONESIS_SANDBOX", word)
        assert get_bool("PHRONESIS_SANDBOX") is expected, word
    monkeypatch.delenv("PHRONESIS_SANDBOX", raising=False)
    assert get_bool("PHRONESIS_SANDBOX") is False
    assert get_bool("PHRONESIS_PRIMEKG_PRELOAD") is True or "PHRONESIS_PRIMEKG_PRELOAD" in os.environ
    with caplog.at_level(logging.WARNING, logger="phronesis.config.settings"):
        monkeypatch.setenv("PHRONESIS_SANDBOX", "maybe")
        assert get_bool("PHRONESIS_SANDBOX") is False
    assert any("not a boolean" in r.getMessage() for r in caplog.records)


def test_get_str_choices_aliases_and_plain_values(monkeypatch):
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "TRUE")
    assert get_str("PHRONESIS_RELIABILITY") == "on"  # switch alias
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "No")
    assert get_str("PHRONESIS_RELIABILITY") == "off"
    monkeypatch.setenv("PHRONESIS_RELIABILITY", "sometimes")
    assert get_str("PHRONESIS_RELIABILITY") == "auto"
    monkeypatch.setenv("PHRONESIS_NLI", "on")
    assert get_str("PHRONESIS_NLI") == "auto"
    monkeypatch.setenv("PHRONESIS_NLI", "LLM")
    assert get_str("PHRONESIS_NLI") == "llm"
    monkeypatch.setenv("PHRONESIS_AGENT_PLAN", "1")  # env_flag accepts exactly off|auto|on
    assert get_str("PHRONESIS_AGENT_PLAN") == "auto"
    monkeypatch.setenv("PHRONESIS_SYNTHESIS_MODEL", "  some/Model-Id ")
    assert get_str("PHRONESIS_SYNTHESIS_MODEL") == "some/Model-Id"  # no choices: stripped, case kept
    monkeypatch.delenv("PHRONESIS_SYNTHESIS_MODEL", raising=False)
    assert get_str("PHRONESIS_SYNTHESIS_MODEL") is None
    assert get_str("PHRONESIS_SYNTHESIS_MODEL", "") == ""


def test_get_path_and_get_list(monkeypatch, tmp_path):
    monkeypatch.delenv("PHRONESIS_DATA_DIR", raising=False)
    assert get_path("PHRONESIS_DATA_DIR") is None
    monkeypatch.setenv("PHRONESIS_DATA_DIR", str(tmp_path))
    assert get_path("PHRONESIS_DATA_DIR") == tmp_path
    monkeypatch.setenv("PHRONESIS_DATA_DIR", "~/phronesis-data")
    assert get_path("PHRONESIS_DATA_DIR") == Path(os.path.expanduser("~/phronesis-data"))
    monkeypatch.delenv("PHRONESIS_CORS_ORIGINS", raising=False)
    assert get_list("PHRONESIS_CORS_ORIGINS") == ["http://localhost:3000", "http://127.0.0.1:3000"]
    monkeypatch.setenv("PHRONESIS_CORS_ORIGINS", " https://a.example , ,https://b.example")
    assert get_list("PHRONESIS_CORS_ORIGINS") == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("PHRONESIS_DATASET_ROOTS", os.pathsep.join(["/data/a", "", "/data/b"]))
    assert get_list("PHRONESIS_DATASET_ROOTS") == ["/data/a", "/data/b"]


def test_unregistered_names_need_an_explicit_default(monkeypatch):
    monkeypatch.delenv("PHRONESIS_NOT_A_SETTING", raising=False)
    with pytest.raises(KeyError):
        get_int("PHRONESIS_NOT_A_SETTING")
    assert get_int("PHRONESIS_NOT_A_SETTING", 4) == 4
    monkeypatch.setenv("PHRONESIS_NOT_A_SETTING", "nope")
    assert get_str("PHRONESIS_NOT_A_SETTING", "x", choices=("x", "y")) == "x"


def test_secret_values_are_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("MILVUS_PORT", "s3cr3t-value")
    with caplog.at_level(logging.WARNING, logger="phronesis.config.settings"):
        assert get_int("MILVUS_PORT") == 19530
    assert "s3cr3t-value" in caplog.text  # not a secret: the bad value helps the operator
    caplog.clear()
    # A secret read through a numeric accessor (a misuse) still never echoes its value.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-appear")
    with caplog.at_level(logging.WARNING, logger="phronesis.config.settings"):
        assert get_int("OPENAI_API_KEY", 0) == 0
    assert "sk-should-not-appear" not in caplog.text and "redacted" in caplog.text


# -- the migrated helpers keep their behaviour ------------------------------------------------------------------------

def _old_env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _old_env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _old_env_flag(name: str, default: str = "auto") -> str:
    value = (os.getenv(name, "") or default).strip().lower()
    return value if value in ("off", "auto", "on") else default


def _old_switch(name: str) -> bool:
    value = (os.getenv(name, "") or "auto").strip().lower()
    if value in ("off", "0", "false", "no"):
        return False
    if value in ("on", "1", "true", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def _old_lumicell_env_int(name: str, default: int) -> int:
    # CFG-002: lumicell clamps negative/zero values to 1
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return max(1, default)


NUMERIC_INPUTS = (None, "", "  ", "3", " 12 ", "0", "-4", "100000", "abc", "2.5", "1e3", "1_000")
FLAG_INPUTS = (None, "", "off", "OFF", " on ", "auto", "1", "true", "no", "bogus")


def _apply(monkeypatch, name: str, raw: Optional[str]) -> None:
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)


def test_agentic_env_helpers_match_the_old_implementation(monkeypatch):
    from phronesis.agentic.config import env_flag, env_float, env_int

    for raw in NUMERIC_INPUTS:
        _apply(monkeypatch, "PHRONESIS_AGENT_MAX_TOOL_CALLS", raw)
        assert env_int("PHRONESIS_AGENT_MAX_TOOL_CALLS", 40, 1, 200) == _old_env_int("PHRONESIS_AGENT_MAX_TOOL_CALLS", 40, 1, 200), raw
        _apply(monkeypatch, "PHRONESIS_JOB_TTL_DAYS", raw)
        assert env_float("PHRONESIS_JOB_TTL_DAYS", 30.0, 0.0, 36500.0) == _old_env_float("PHRONESIS_JOB_TTL_DAYS", 30.0, 0.0, 36500.0), raw
    for raw in FLAG_INPUTS:
        _apply(monkeypatch, "PHRONESIS_AGENT_DECOMPOSE", raw)
        for default in ("auto", "off", "on"):
            assert env_flag("PHRONESIS_AGENT_DECOMPOSE", default) == _old_env_flag("PHRONESIS_AGENT_DECOMPOSE", default), raw


def test_reliability_env_helpers_match_the_old_implementation(monkeypatch):
    from phronesis.tools import reliability

    for raw in NUMERIC_INPUTS:
        _apply(monkeypatch, "PHRONESIS_TOOL_RETRIES", raw)
        assert reliability._max_retries() == _old_env_int("PHRONESIS_TOOL_RETRIES", 2, 0, 5), raw
        _apply(monkeypatch, "PHRONESIS_BREAKER_COOLDOWN_S", raw)
        assert reliability._cooldown_s() == _old_env_float("PHRONESIS_BREAKER_COOLDOWN_S", 60.0, 0.0, 3600.0), raw
    for raw in FLAG_INPUTS + ("YES", "0", "False"):
        _apply(monkeypatch, "PHRONESIS_RELIABILITY", raw)
        assert reliability.policies_enabled() == _old_switch("PHRONESIS_RELIABILITY"), raw


def test_lumicell_env_helper_matches_the_old_implementation(monkeypatch):
    engine = pytest.importorskip("phronesis.lumicell.engine")
    for raw in NUMERIC_INPUTS:
        _apply(monkeypatch, "PHRONESIS_LUMICELL_MAX_CELLS", raw)
        assert engine._env_int("PHRONESIS_LUMICELL_MAX_CELLS", 50_000) == _old_lumicell_env_int(
            "PHRONESIS_LUMICELL_MAX_CELLS", 50_000), raw


# -- validation -------------------------------------------------------------------------------------------------------

def test_validate_environment_clean_environment_has_no_problems():
    assert validate_environment({}) == []
    assert validate_environment({"PHRONESIS_JOB_WORKERS": "4", "PHRONESIS_RELIABILITY": "1", "PHRONESIS_NLI": "on",
                                 "PHRONESIS_SANDBOX": "yes", "PHRONESIS_LIVE_TESTS": "1", "PHRONESIS_WEB_PORT": "3001",
                                 "PHRONESIS_DATA_DIR": "/srv/phronesis", "UNRELATED_VAR": "x",
                                 "PHRONESIS_AGENT_MAX_TOKENS": ""}) == []


def test_validate_environment_reports_typos_with_a_suggestion():
    problems = validate_environment({"PHRONESIS_JOB_WORKER": "3", "PHRONESIS_RELIABILTY": "on", "PHRONESIS_ZZZ": "1"})
    assert len(problems) == 3
    by_name: Dict[str, str] = {p.split(" ", 1)[0]: p for p in problems}
    assert "did you mean PHRONESIS_JOB_WORKERS?" in by_name["PHRONESIS_JOB_WORKER"]
    assert "did you mean PHRONESIS_RELIABILITY?" in by_name["PHRONESIS_RELIABILTY"]
    assert "did you mean" not in by_name["PHRONESIS_ZZZ"]


def test_validate_environment_reports_bad_values():
    problems = validate_environment({
        "PHRONESIS_JOB_WORKERS": "lots",
        "PHRONESIS_MAX_JOBS_PER_USER": "500",
        "PHRONESIS_SANDBOX_TIMEOUT_S": "1",
        "PHRONESIS_NLI_ENTAIL": "nan",
        "PHRONESIS_PLANNER": "alwys",
        "PHRONESIS_SANDBOX": "maybe",
        "PHRONESIS_AUTH_MODE": "disable",
    })
    text = "\n".join(problems)
    assert len(problems) == 7, text
    assert "PHRONESIS_JOB_WORKERS='lots' is not an integer" in text
    assert "PHRONESIS_MAX_JOBS_PER_USER='500' is above the maximum 100" in text
    assert "PHRONESIS_SANDBOX_TIMEOUT_S='1' is below the minimum 5" in text
    assert "PHRONESIS_NLI_ENTAIL='nan' is not a number" in text
    assert "did you mean always?" in text
    assert "PHRONESIS_SANDBOX='maybe' is not a boolean" in text
    assert "did you mean disabled?" in text


def test_validate_environment_never_echoes_secrets():
    problems = validate_environment({"PHRONESIS_JWT_SECRT": "hunter2"})
    assert problems and "hunter2" not in "\n".join(problems)
    assert "did you mean PHRONESIS_JWT_SECRET?" in problems[0]


def test_validate_environment_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("PHRONESIS_MAX_RUNZ", "10")
    assert any(p.startswith("PHRONESIS_MAX_RUNZ") and "PHRONESIS_MAX_RUNS" in p for p in validate_environment())


# -- startup and health -----------------------------------------------------------------------------------------------

def test_startup_check_warns_and_strict_mode_fails(monkeypatch, caplog):
    from app.backend import main as backend_main

    monkeypatch.setenv("PHRONESIS_JOB_WORKERZ", "2")
    monkeypatch.delenv("PHRONESIS_STRICT_CONFIG", raising=False)
    with caplog.at_level(logging.WARNING, logger="phronesis.backend"):
        problems = backend_main._check_config()
    assert any("PHRONESIS_JOB_WORKERZ" in p for p in problems)
    assert "Configuration: PHRONESIS_JOB_WORKERZ" in caplog.text
    monkeypatch.setenv("PHRONESIS_STRICT_CONFIG", "1")
    with pytest.raises(RuntimeError, match="PHRONESIS_STRICT_CONFIG"):
        backend_main._check_config()


def test_health_reports_the_config_problem_count(monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    class _Probe:
        async def async_check_service_readiness(self) -> Dict[str, Any]:
            return {"mcp_remote_active": False, "direct_adapters": {}}

    monkeypatch.setattr(backend_main, "BioContextMCPClient", _Probe)
    monkeypatch.setitem(backend_main._health_cache, "readiness", None)
    client = TestClient(backend_main.app)
    baseline = client.get("/api/health").json()["config_problems"]
    monkeypatch.setenv("PHRONESIS_HEALTH_CACHE_SECONDS", "5")  # typo of PHRONESIS_HEALTH_CACHE_S
    body = client.get("/api/health").json()
    assert body["config_problems"] == baseline + 1
    assert "PHRONESIS_HEALTH_CACHE_SECONDS" not in str(body)  # count only


# -- docs generation --------------------------------------------------------------------------------------------------

def test_docs_use_registry_metadata_and_keep_secrets_blank(gen, scanned):
    doc = gen.render_markdown(scanned)
    example = gen.render_example(scanned)
    assert f"{len(REGISTRY)} variables." in doc
    assert REGISTRY["PHRONESIS_AGENT_PLAN_CONCURRENCY"].description in doc
    assert "| `PHRONESIS_AGENT_PLAN_CONCURRENCY` | int (1 to 16) | `4` |" in doc
    assert "## Unregistered" not in doc
    for name, setting in REGISTRY.items():
        assert f"# {name}=" in example, name
        if setting.secret:
            assert f"# {name}=\n" in example, name
    assert "# PHRONESIS_JOB_WORKERS=2\n" in example
    assert "# PHRONESIS_SANDBOX=false\n" in example


def test_generated_reference_is_up_to_date(gen):
    assert gen.main(["--check"]) == 0
