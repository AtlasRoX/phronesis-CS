"""
Track R (backend reliability and security) of docs/plans/2026-10-17-harness-and-tech-debt.md: the shared typography
fold and claim removal, lazy legacy imports, the tool registry's result and pool guarantees, the biocontext sync
wrapper, the sandbox reader path guard, the SSRF peer check, and the API hardening (title limiter, generic 401,
disabled routers).

Offline: no network, no real model.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
from pydantic import BaseModel

REPO = Path(__file__).resolve().parents[1]


# 1. One typography fold ------------------------------------------------------------------------------------------
def test_orchestrator_and_verifier_fold_the_same_characters():
    from phronesis.agents.authoritative_orchestrator import normalize_typography
    from phronesis.phronesis.agents.verifier import ClinicalVerifier
    from phronesis.utils.text import fold_typography

    sample = "Mos\u2011P \u2212 0.4\u2009mM, on\u2010target\u2012x, hyphen\u00adated, et\u202fal., a\u00a0b, c\u2007d, e\u200af"
    folded = fold_typography(sample)
    assert folded == "Mos-P - 0.4 mM, on-target-x, hyphenated, et al., a b, c d, e f"
    assert normalize_typography(sample) == folded
    assert ClinicalVerifier._normalize_typography(sample) == folded
    # Range dashes carry meaning and are kept.
    assert fold_typography("HR 0.23\u20130.41 \u2014 n=10") == "HR 0.23\u20130.41 \u2014 n=10"
    assert fold_typography(None) == "" and fold_typography("") == ""


def _defect(sentence: str) -> Any:
    return SimpleNamespace(category="CLAIM_CONTRADICTED", sentence=sentence, claim=sentence, description="d",
                           severity="CRITICAL", suggested_correction="")


def test_remove_flagged_sentences_matches_through_typography():
    from phronesis.phronesis.agents.verifier import remove_flagged_sentences
    from phronesis.utils.text import fold_typography

    original = ("Intro sentence here. Sotorasib\u00ad cures every KRAS\u2011mutant patient\u2009today. Another fact.\n")
    flagged = fold_typography("Sotorasib\u00ad cures every KRAS\u2011mutant patient\u2009today.")
    assert flagged not in original  # the verifier's sentence is folded; the answer is not
    out, removed = remove_flagged_sentences(original, [_defect(flagged)])
    assert len(removed) == 1
    assert "cures every" not in out.split("Evidence limitations")[0]
    assert "Intro sentence here. Another fact." in out


def test_remove_flagged_sentences_logs_when_a_sentence_is_missing(caplog):
    from phronesis.phronesis.agents.verifier import remove_flagged_sentences

    text = "Intro sentence here. Another fact.\n"
    with caplog.at_level(logging.WARNING, logger="phronesis.phronesis.agents.verifier"):
        out, removed = remove_flagged_sentences(text, [_defect("A sentence that is not there.")])
    assert out == text and removed == []
    assert any("not found" in r.getMessage() for r in caplog.records)


def test_find_folded_maps_positions_back():
    from phronesis.utils.text import find_folded

    hay = "a\u00adb\u2011c d"
    start, end = find_folded(hay, "ab-c")
    assert hay[start:end] == "a\u00adb\u2011c"
    assert find_folded(hay, "zz") == (-1, -1)


# 2. Lazy legacy imports ------------------------------------------------------------------------------------------
def test_legacy_agents_package_does_not_import_main_agent_eagerly():
    init = REPO / "phronesis" / "phronesis" / "agents" / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    top_level_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(
        any(a.name == "main_agent" for a in n.names) for n in top_level_imports
    ), "main_agent must be loaded lazily"
    import phronesis.phronesis.agents as legacy

    assert "main_agent" in dir(legacy)
    with pytest.raises(AttributeError):
        getattr(legacy, "no_such_submodule")


# 3. Registry ---------------------------------------------------------------------------------------------------
class _In(BaseModel):
    x: int = 1


def _registry(name: str, executor: Any, adapter: str = "DIRECT_REST", timeout: float = 5.0) -> Any:
    from phronesis.tools.registry import AdapterType, BiomedicalToolRegistry, ToolUniverseEntry

    reg = BiomedicalToolRegistry.__new__(BiomedicalToolRegistry)
    reg._tools, reg._executors = {}, {}
    reg.register(
        ToolUniverseEntry(name=name, description="t", domain="general", adapter_type=AdapterType(adapter),
                          input_schema=_In, timeout_seconds=timeout, is_async=False),
        executor,
    )
    return reg


def test_networked_sync_tools_run_on_their_own_bounded_pool():
    seen: List[str] = []

    def tool(x: int = 1) -> Dict[str, Any]:
        seen.append(threading.current_thread().name)
        return {"x": x}

    res = asyncio.run(_registry("t_net", tool).execute("t_net", x=2))
    assert res.success and res.data == {"x": 2}
    assert seen and seen[0].startswith("phronesis-net")


def test_execute_always_returns_a_tool_result(monkeypatch):
    from phronesis.tools import reliability
    from phronesis.tools.registry import ErrorType, ToolResult

    reg = _registry("t_none", lambda x=1: {"x": x})

    async def returns_none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(reliability, "execute_with_policies", returns_none)
    res = asyncio.run(reg.execute("t_none"))
    assert isinstance(res, ToolResult) and not res.success and res.error_type == ErrorType.INTERNAL_ERROR

    async def raises(*_a: Any, **_k: Any) -> None:
        raise KeyError("policy bug")

    monkeypatch.setattr(reliability, "execute_with_policies", raises)
    res = asyncio.run(reg.execute("t_none"))
    assert isinstance(res, ToolResult) and not res.success and "KeyError" in (res.error or "")


def test_control_exceptions_still_propagate(monkeypatch):
    from phronesis.agentic.control import RunCancelled
    from phronesis.tools import reliability

    reg = _registry("t_cancel", lambda x=1: {"x": x})

    async def cancelled(*_a: Any, **_k: Any) -> None:
        raise RunCancelled("stop")

    monkeypatch.setattr(reliability, "execute_with_policies", cancelled)
    with pytest.raises(RunCancelled):
        asyncio.run(reg.execute("t_cancel"))


def test_provenance_failure_is_logged_not_swallowed(monkeypatch, caplog):
    from phronesis.provenance import run_manifest

    def broken(**_k: Any) -> None:
        raise RuntimeError("manifest down")

    monkeypatch.setattr(run_manifest, "record_tool_call", broken)
    reg = _registry("t_prov", lambda x=1: {"x": x})
    with caplog.at_level(logging.WARNING, logger="phronesis.tools.registry"):
        res = asyncio.run(reg.execute("t_prov"))
    assert res.success
    assert any("run manifest" in r.getMessage() and r.exc_info for r in caplog.records)


# 4. biocontext sync wrapper ----------------------------------------------------------------------------------
def test_biocontext_sync_wrapper_does_not_deadlock_inside_a_running_loop(monkeypatch):
    from phronesis.utils.biocontext import client as bc

    c = bc.BioContextMCPClient()

    async def fake_assoc(query: str, size: int = 5) -> Dict[str, Any]:
        await asyncio.sleep(0)
        return {"source": "fake", "ensembl_id": query, "associations": [1]}

    monkeypatch.setattr(c, "async_query_opentargets_associations", fake_assoc)
    monkeypatch.setattr(c, "_sync_query_opentargets_fallback", lambda *a, **k: {"source": "fallback"})

    async def main() -> Dict[str, Any]:
        return c.query_opentargets("ENSG00000133703", query_type="associations")

    t0 = time.perf_counter()
    out = asyncio.run(main())
    assert out["source"] == "fake" and time.perf_counter() - t0 < 5
    # And from plain sync code.
    assert c.query_opentargets("ENSG00000133703", query_type="associations")["source"] == "fake"


def test_run_coroutine_sync_works_with_and_without_a_loop():
    from phronesis.utils.biocontext.client import _run_coroutine_sync

    async def value() -> int:
        return 7

    assert _run_coroutine_sync(value) == 7

    async def inside() -> int:
        return _run_coroutine_sync(value, timeout=5)

    assert asyncio.run(inside()) == 7


# 5. Sandbox reader path guard ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code",
    [
        "import pandas as pd\ndf = pd.read_csv('/etc/passwd')\n",
        "import pandas as pd\ndf = pd.read_json('C:/Users/x/secrets.json')\n",
        "import pandas as pd\ndf = pd.read_parquet('../io/x.parquet')\n",
        "import pandas as pd\ndf = pd.read_excel('~/book.xlsx')\n",
        "import pandas as pd\ndf = pd.read_table(filepath_or_buffer='/etc/hosts')\n",
        "import numpy as np\nx = np.load('/tmp/x.npy')\n",
        "import numpy as np\nx = np.loadtxt('..\\\\x.txt')\n",
        "import numpy as np\nx = np.genfromtxt('/data/x.csv')\n",
        "import anndata as ad\na = ad.read_h5ad('/data/atlas.h5ad')\n",
        "import anndata\na = anndata.read('/data/atlas.h5ad')\n",
        "import scanpy as sc\na = sc.read('/data/atlas.h5ad')\n",
        "from pandas import read_csv\ndf = read_csv('/etc/passwd')\n",
        "import pandas as pd\nx = pd.ExcelFile(some_path)\n",
    ],
)
def test_policy_rejects_reader_paths_outside_the_work_dir(code):
    from phronesis.agentic.sandbox.policy import check_code

    assert check_code(code), code


def test_policy_allows_relative_reader_paths_and_file_handles():
    from phronesis.agentic.sandbox.policy import check_code

    code = (
        "import pandas as pd\nimport numpy as np\nimport json\n"
        "df = pd.read_csv('data.csv', index_col=0)\n"
        "df2 = pd.read_csv(INPUT_FILES[0])\n"
        "x = pd.ExcelFile(INPUT_FILES[0])\n"
        "j = pd.read_json('{\"a\": [1, 2]}')\n"
        "with open('data.csv') as fh:\n    head = fh.read(100)\n"
        "with open('data.csv') as fh:\n    meta = json.load(fh)\n"
    )
    assert check_code(code, ["data.csv"]) == []


def _csv(tmp_path: Path) -> str:
    p = tmp_path / "input.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    return str(p)


def test_runtime_reader_guard_blocks_computed_paths(tmp_path):
    from phronesis.agentic.sandbox.runner import run_code

    code = (
        "from pandas import read_csv\nimport numpy as np\n"
        "outside = '/'.join(['', 'etc', 'hosts'])\n"
        "blocked = []\n"
        "for fn in (read_csv, np.loadtxt):\n"
        "    try:\n        fn(outside)\n        blocked.append('read')\n"
        "    except PermissionError:\n        blocked.append('blocked')\n"
        "try:\n    read_csv('..' + '/io/x.csv')\n    blocked.append('read')\n"
        "except PermissionError:\n    blocked.append('blocked')\n"
        "result = {'blocked': blocked, 'rows': int(read_csv(INPUT_FILES[0]).shape[0])}\n"
    )
    out = run_code(code, {"data.csv": _csv(tmp_path)}, timeout_s=90)
    assert out["status"] == "ok", out
    assert out["result"] == {"blocked": ["blocked", "blocked", "blocked"], "rows": 2}


def test_runtime_reader_guard_reports_permission_error(tmp_path):
    from phronesis.agentic.sandbox.runner import run_code

    out = run_code("import pandas as pd\np = '..' + '/io/x'\ndf = pd.read_csv(p)\n", {"data.csv": _csv(tmp_path)})
    assert out["status"] == "error" and "PermissionError" in out["error"]


# 6. SSRF: the connected peer is checked ------------------------------------------------------------------------
class _FakeStream:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_extra_info(self, key: str) -> Any:
        return (self.ip, 80) if key == "server_addr" else None


def _surf(peer_ip: str, monkeypatch) -> Dict[str, Any]:
    from phronesis.tools import web_search

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    html = "<html><head><title>Page</title></head><body><article><p>" + "Real content. " * 40 + "</p></article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html.encode(),
                              extensions={"network_stream": _FakeStream(peer_ip)})

    client = web_search.FreeWebSearchClient(transport=httpx.MockTransport(handler))
    # A literal public IP: the pre-request DNS check passes without a lookup; the rebinding happens at connect.
    return asyncio.run(client.surf_url("http://93.184.216.34/article"))


def test_surf_refuses_a_rebinding_host_that_connected_to_loopback(monkeypatch):
    out = _surf("127.0.0.1", monkeypatch)
    assert "Refused" in str(out.get("error")), out


def test_surf_refuses_private_peer(monkeypatch):
    out = _surf("10.0.0.5", monkeypatch)
    assert "Refused" in str(out.get("error")), out


def test_surf_accepts_a_public_peer(monkeypatch):
    out = _surf("93.184.216.34", monkeypatch)
    assert not out.get("error"), out


# 7. API hardening ----------------------------------------------------------------------------------------------
ALICE = {"Authorization": "Bearer alice-token"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    monkeypatch.delenv("PHRONESIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PHRONESIS_JWT_SECRET", raising=False)
    monkeypatch.setenv("PHRONESIS_API_TOKENS", "alice-token:alice,bob-token:bob")
    return TestClient(backend_main.app), backend_main


def test_title_endpoint_is_limited_per_user(api, monkeypatch):
    from app.backend.limits import UserConcurrencyLimiter
    from phronesis.utils.llm import llm_factory

    client, backend_main = api
    calls: List[str] = []

    def fake_get_llm(**_k: Any) -> Any:
        calls.append("llm")
        raise RuntimeError("no model in tests")

    monkeypatch.setattr(llm_factory, "get_llm", fake_get_llm)
    slots = UserConcurrencyLimiter(1)
    monkeypatch.setattr(backend_main, "_title_slots", slots)
    body = {"message": "KRAS G12D inhibitor resistance in pancreatic cancer"}

    resp = client.post("/api/chat/title", json=body, headers=ALICE)
    assert resp.status_code == 200 and resp.json()["method"] == "heuristic"
    assert calls == ["llm"] and slots.active("alice") == 0  # the slot is released

    assert slots.try_acquire("alice")  # alice is at her limit
    resp = client.post("/api/chat/title", json=body, headers=ALICE)
    assert resp.status_code == 200 and resp.json()["method"] == "heuristic"
    assert calls == ["llm"]  # no model call over the limit
    slots.release("alice")


def test_jwt_errors_are_not_echoed(api, monkeypatch):
    import jwt

    client, _ = api
    secret = "test-secret-with-enough-length-0123456789"
    monkeypatch.setenv("PHRONESIS_JWT_SECRET", secret)
    expired = jwt.encode({"sub": "carol", "exp": int(time.time()) - 60}, secret, algorithm="HS256")
    resp = client.get("/api/memory/stats", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid bearer token."


def test_health_lists_disabled_routers(api, monkeypatch):
    client, backend_main = api

    class _Probe:
        async def async_check_service_readiness(self) -> Dict[str, Any]:
            return {"mcp_remote_active": False, "direct_adapters": {}}

    monkeypatch.setattr(backend_main, "BioContextMCPClient", _Probe)
    monkeypatch.setitem(backend_main._health_cache, "readiness", None)
    monkeypatch.setitem(backend_main.DISABLED_ROUTERS, "app.backend.routers.fake", "ImportError")
    body = client.get("/api/health").json()
    assert body["disabled_routers"].get("app.backend.routers.fake") == "ImportError"


def test_strict_routers_make_a_router_failure_fatal():
    script = (
        "import os, sys, types\n"
        "sys.modules['app.backend.routers.a2a'] = types.ModuleType('app.backend.routers.a2a')  # no .router\n"
        "os.environ.pop('PHRONESIS_STRICT_ROUTERS', None)\n"
        "import app.backend.main as m\n"
        "assert 'app.backend.routers.a2a' in m.DISABLED_ROUTERS, m.DISABLED_ROUTERS\n"
        "del sys.modules['app.backend.main']\n"
        "os.environ['PHRONESIS_STRICT_ROUTERS'] = '1'\n"
        "try:\n    import app.backend.main\nexcept RuntimeError as exc:\n    print('FATAL', exc)\n"
        "else:\n    print('NOT FATAL')\n"
    )
    env = {**os.environ, "PHRONESIS_AUTH_MODE": "disabled"}
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "FATAL" in proc.stdout and "app.backend.routers.a2a" in proc.stdout, proc.stdout
