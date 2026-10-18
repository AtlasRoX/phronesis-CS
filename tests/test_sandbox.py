"""
Sandboxed code analysis (feature 10): the AST policy, the subprocess runner (result, figures, timeout, network stub,
scrubbed environment, runtime guards), the registry tool's dataset scoping, and the analyst phase end to end.

Offline: no network and no real model. The dataset is tests/fixtures/test_atlas.h5ad exported to CSV, uploaded
through the real dataset store; model replies come from ScriptedLLM.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from phronesis.agentic import config as agent_config
from phronesis.agentic.analyst import AnalystPhase, deterministic_script, has_analysis_intent
from phronesis.agentic.runtime import restore_phases, snapshot_phases
from phronesis.agentic.sandbox import ResolvedInput, bound_scope
from phronesis.agentic.sandbox.policy import check_code
from phronesis.agentic.sandbox.runner import run_code
from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.datasets.models import DatasetProvenanceIn
from phronesis.datasets.store import get_dataset_store, reset_dataset_store
from phronesis.extensions import sandbox as sandbox_ext
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import make_offline_orchestrator

ATLAS = Path(__file__).resolve().parent / "fixtures" / "test_atlas.h5ad"


# =============================================================================
# Policy


@pytest.mark.parametrize(
    "code",
    [
        "import os\n",
        "import subprocess\nsubprocess.run(['ls'])\n",
        "import socket\n",
        "from os import path\n",
        "import importlib\n",
        "import pickle\n",
        "__import__('os')\n",
        "eval('1+1')\n",
        "exec('x = 1')\n",
        "compile('1', 'x', 'eval')\n",
        "x = ().__class__.__bases__[0].__subclasses__()\n",
        "x = getattr(1, '__class__')\n",
        "name = 'sys'\nx = getattr(1, name)\n",
        "f = open('out.txt', 'w')\n",
        "f = open('/etc/passwd')\n",
        "f = open('../secret.txt')\n",
        "import pandas as pd\npd.DataFrame().to_csv('/tmp/x.csv')\n",
        "import pandas as pd\np = 'x.csv'\npd.DataFrame().to_csv(p)\n",
        "import pandas as pd\npd.read_pickle('x.pkl')\n",
        "import numpy as np\nnp.load('x.npy', allow_pickle=True)\n",
        "import pandas.io.common as c\nc.os.system('whoami')\n",
        "globals()['x'] = 1\n",
        "def f():\n    yield 1\ng = f()\nfr = g.gi_frame.f_globals\n",
        "'{0.__class__}'.format(1)\n",
        "breakpoint()\n",
    ],
)
def test_policy_rejects_escapes(code):
    assert check_code(code), code


def test_policy_allows_ordinary_analysis_code():
    code = (
        "import numpy as np\nimport pandas as pd\nfrom scipy import stats\nimport matplotlib.pyplot as plt\n"
        "df = pd.read_csv('data.csv', index_col=0)\n"
        "r, p = stats.pearsonr(df.iloc[:, 0], df.iloc[:, 1])\n"
        "with open('data.csv') as fh:\n    header = fh.readline()\n"
        "df.describe().to_csv('summary.csv')\n"
        "plt.plot(df.iloc[:, 0])\n"
        "result = {'r': float(r), 'p': float(p), 'n': int(df.shape[0])}\n"
    )
    assert check_code(code, ["data.csv"]) == []
    # The deterministic no-model scripts pass the policy for both dataset kinds.
    table = ResolvedInput(dataset_id="ds_0123456789ab", name="atlas.csv", path="", kind="table", display_name="atlas.csv", sep=",")
    h5ad = ResolvedInput(dataset_id="ds_0123456789ab", name="atlas.h5ad", path="", kind="h5ad", display_name="atlas.h5ad")
    assert check_code(deterministic_script(table, "plot the distribution"), ["atlas.csv"]) == []
    assert check_code(deterministic_script(h5ad, "plot the distribution"), ["atlas.h5ad"]) == []


def test_analysis_intent_regex():
    assert has_analysis_intent("Compute the correlation between CD3E and CD8A")
    assert has_analysis_intent("Plot the distribution of counts")
    assert has_analysis_intent("Run a differential expression analysis")
    assert has_analysis_intent("Test whether cluster sizes differ")
    assert not has_analysis_intent("What diseases is KRAS associated with?")


# =============================================================================
# Runner


def _write_csv(path: Path) -> Path:
    path.write_text("cell,GENE_A,GENE_B\nc1,1.0,2.0\nc2,2.0,4.1\nc3,3.0,6.2\nc4,4.0,7.9\n", encoding="utf-8")
    return path


def test_runner_returns_result_tables_and_figure(tmp_path):
    src = _write_csv(tmp_path / "input.csv")
    code = (
        "import pandas as pd\nimport matplotlib.pyplot as plt\n"
        "df = pd.read_csv('data.csv', index_col=0)\n"
        "print(df.shape)\n"
        "result = {'corr': round(float(df['GENE_A'].corr(df['GENE_B'])), 4), 'n': int(df.shape[0])}\n"
        "tables = {'describe': df.describe()}\n"
        "fig, ax = plt.subplots()\nax.scatter(df['GENE_A'], df['GENE_B'])\n"
    )
    out = run_code(code, {"data.csv": str(src)}, timeout_s=90)
    assert out["status"] == "ok", out
    assert out["result"]["n"] == 4 and out["result"]["corr"] > 0.99
    assert "(4, 2)" in out["stdout"]
    assert out["tables"][0]["name"] == "describe" and out["tables"][0]["columns"] == ["index", "GENE_A", "GENE_B"]
    assert len(out["figures"]) == 1 and out["figures"][0]["png_base64"].startswith("iVBORw0KGgo")  # PNG magic
    assert src.read_text(encoding="utf-8").startswith("cell,GENE_A")  # the original is untouched


def test_runner_reports_errors_with_user_frames_only():
    out = run_code("x = {'a': 1}\ny = x['missing']\n", {})
    assert out["status"] == "error"
    assert "KeyError" in out["error"] and "line 2" in out["error"]
    assert "bootstrap" not in out["error"]


def test_runner_kills_infinite_loop_on_timeout():
    out = run_code("while True:\n    pass\n", {}, timeout_s=3)
    assert out["status"] == "timeout"
    assert out["seconds"] < 30


def test_runner_rejects_policy_violations_without_running():
    out = run_code("import os\nos.remove('x')\n", {})
    assert out["status"] == "policy_violation" and out["violations"]
    assert out["seconds"] == 0.0


def test_runtime_open_guard_blocks_computed_paths(tmp_path):
    # A computed path passes the static check; the guarded open() in the child refuses it.
    out = run_code("name = '..' + '/io/x'\nf = open(name)\n", {})
    assert out["status"] == "error" and "PermissionError" in out["error"]


def _probe_child(monkeypatch) -> Dict[str, Any]:
    """Test helper: run with the policy bypassed so the child's environment and socket module can be inspected."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-not-a-real-key")
    monkeypatch.setenv("PHRONESIS_AUTH_TOKEN", "secret-token")
    code = (
        "import os, socket\n"
        "result = {'env': sorted(os.environ), 'values': ' '.join(os.environ.values())}\n"
        "try:\n    socket.create_connection(('example.org', 80), timeout=2)\n    result['net'] = 'connected'\n"
        "except Exception as exc:\n    result['net'] = type(exc).__name__ + ': ' + str(exc)\n"
        "try:\n    socket.socket()\n    result['raw'] = 'created'\n"
        "except Exception as exc:\n    result['raw'] = type(exc).__name__\n"
        "try:\n    socket.getaddrinfo('example.org', 80)\n    result['dns'] = 'resolved'\n"
        "except Exception as exc:\n    result['dns'] = type(exc).__name__\n"
    )
    out = run_code(code, {}, _unsafe_skip_policy=True)
    assert out["status"] == "ok", out
    return out["result"]


def test_child_env_is_scrubbed_and_network_blocked(monkeypatch):
    probe = _probe_child(monkeypatch)
    assert "OPENAI_API_KEY" not in probe["env"] and "NVIDIA_API_KEY" not in probe["env"]
    assert "PHRONESIS_AUTH_TOKEN" not in probe["env"]
    assert "sk-test-not-a-real-key" not in probe["values"] and "secret-token" not in probe["values"]
    assert not [k for k in probe["env"] if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    assert probe["env"] and set(probe["env"]) <= {
        "PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "TMPDIR", "PYTHONHASHSEED", "MPLBACKEND",
        "MPLCONFIGDIR", "NUMBA_CACHE_DIR", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "PYTHONIOENCODING",
    }
    assert probe["net"].startswith("PermissionError") and "network access is disabled" in probe["net"]
    assert probe["raw"] == "PermissionError" and probe["dns"] == "PermissionError"


# =============================================================================
# Registry tool and the analyst phase


def _prov() -> DatasetProvenanceIn:
    return DatasetProvenanceIn(source="in_house", attests_no_direct_identifiers=True)


def _atlas_csv() -> bytes:
    import anndata as ad
    import numpy as np
    import pandas as pd

    adata = ad.read_h5ad(ATLAS)
    df = pd.DataFrame(np.asarray(adata.X), index=adata.obs_names, columns=adata.var_names)
    return df.to_csv().encode("utf-8")


def _atlas_genes() -> List[str]:
    import anndata as ad

    return [str(g) for g in ad.read_h5ad(ATLAS).var_names]


@pytest.fixture
def upload_store(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.delenv("PHRONESIS_DATASET_ROOTS", raising=False)
    reset_dataset_store()
    yield get_dataset_store()
    reset_dataset_store()


@pytest.fixture
def analyst_only():
    state = snapshot_phases()
    from phronesis.extensions import load_extensions

    load_extensions()
    restore_phases({"analyst": AnalystPhase()})
    try:
        yield
    finally:
        restore_phases(state)


def _upload(store) -> Any:
    return asyncio.run(store.create_from_stream("alice", [_atlas_csv()], "atlas.csv", _prov()))


def test_tool_is_off_by_default_and_reads_only_bound_datasets(monkeypatch, upload_store):
    from phronesis.tools.registry import BiomedicalToolRegistry

    monkeypatch.delenv("PHRONESIS_SANDBOX", raising=False)
    reg = BiomedicalToolRegistry()
    sandbox_ext.register_tools(reg)
    assert reg.get_entry(sandbox_ext.TOOL_NAME) is None
    off = sandbox_ext.run_analysis("result = 1\n", [])
    assert not off.success and off.data == {"status": "disabled"}

    monkeypatch.setenv("PHRONESIS_SANDBOX", "on")
    sandbox_ext.register_tools(reg)
    assert reg.get_entry(sandbox_ext.TOOL_NAME).domain == "analysis"
    rec = _upload(upload_store)
    unbound = sandbox_ext.run_analysis("result = 1\n", [rec.id])
    assert not unbound.success and unbound.error_type.value == "DATASET_REQUIRED"
    with bound_scope("bob", [rec.id]):  # someone else's upload
        foreign = sandbox_ext.run_analysis("result = 1\n", [rec.id])
    assert not foreign.success and "not found" in foreign.error
    with bound_scope("alice", []):  # not attached to this question
        detached = sandbox_ext.run_analysis("result = 1\n", [rec.id])
    assert not detached.success and "not attached" in detached.error
    with bound_scope("alice", [rec.id]):
        ok = asyncio.run(reg.execute(sandbox_ext.TOOL_NAME, code="import pandas as pd\nresult = int(pd.read_csv('atlas.csv', index_col=0).shape[0])\n", dataset_ids=[rec.id]))
    assert ok.success, ok.error
    assert ok.data["result"] == 40 and ok.data["inputs"][0]["name"] == "atlas.csv"


def _sandbox_calls(res) -> List[Dict[str, Any]]:
    trace = (res.execution_artifacts.get("agent_run") or {}).get("trace") or []
    return [t for t in trace if t["tool"] == sandbox_ext.TOOL_NAME]


def _agentic(monkeypatch, tmp_path, responses=None):
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses=responses)
    sandbox_ext.register_tools(orch_module.get_tool_registry())
    return orch, calls, llm


def test_analyst_codeact_loop_end_to_end(monkeypatch, tmp_path, upload_store, analyst_only):
    monkeypatch.setenv("PHRONESIS_SANDBOX", "on")
    rec = _upload(upload_store)
    g1, g2 = _atlas_genes()[:2]
    bad = "import pandas as pd\ndf = pd.read_csv('atlas.csv', index_col=0)\nresult = {'r': float(df['NOT_A_GENE'].corr(df['%s']))}\n" % g1
    good = (
        "import pandas as pd\ndf = pd.read_csv('atlas.csv', index_col=0)\n"
        f"r = float(df['{g1}'].corr(df['{g2}']))\nresult = {{'pearson_r': round(r, 4), 'n_cells': int(df.shape[0])}}\n"
    )
    replies = iter(
        [
            json.dumps({"plan": ["1. load the table", "2. correlate the two genes"], "code": bad, "done": False}),
            json.dumps({"plan": ["1. load the table", "2. correlate the two genes"], "code": good, "done": True,
                        "answer_summary": "Pearson r computed over all cells"}),
        ]
    )
    orch, calls, llm = _agentic(monkeypatch, tmp_path, {"analyst": lambda s, u: next(replies)})
    res, _events = asyncio.run(
        collect_events(
            orch, query=f"Compute the correlation between {g1} and {g2} in this dataset", thread_id="t-an1",
            framework=agent_config.AGENTIC, user_id="alice", dataset_ids=[rec.id], enable_web_search=False,
        )
    )
    art = res.execution_artifacts["sandbox_analysis"]
    assert art["status"] == "ok" and art["author"] == "llm" and art["iterations"] == 2
    assert [a["status"] for a in art["attempts"]] == ["error", "ok"]
    assert art["result"]["n_cells"] == 40 and -1.0 <= art["result"]["pearson_r"] <= 1.0
    assert art["dataset"]["id"] == rec.id and art["policy"]["network"] == "blocked"
    assert len(_sandbox_calls(res)) == 2
    prompts = llm.prompts("analyst")
    assert len(prompts) == 2
    assert all(c[1].startswith("[phase:analyst]") for c in llm.calls if c[0] == "analyst")
    assert "<untrusted_data" in prompts[0] and "atlas.csv" in prompts[0]
    assert "KeyError" in prompts[1]  # the failed cell's traceback is the next observation (CodeAct)
    synth = llm.prompts("synthesis")[0]
    assert "<sandbox_analysis" in synth and "pearson_r" in synth
    serialized = json.dumps(res.to_dict(), default=str)
    assert str(tmp_path / "uploads") not in serialized and str(tmp_path / "uploads").replace("\\", "\\\\") not in serialized


def test_analyst_rejects_policy_violations_and_allows_insufficient_information(monkeypatch, tmp_path, upload_store, analyst_only):
    monkeypatch.setenv("PHRONESIS_SANDBOX", "on")
    rec = _upload(upload_store)
    replies = iter(
        [
            json.dumps({"plan": ["1. inspect"], "code": "import os\nresult = os.listdir('/')\n", "done": False}),
            json.dumps({"plan": ["1. inspect"], "code": "", "done": True,
                        "answer_summary": "insufficient information: the table has no survival or time-to-event columns"}),
        ]
    )
    orch, calls, _llm = _agentic(monkeypatch, tmp_path, {"analyst": lambda s, u: next(replies)})
    res, _ = asyncio.run(
        collect_events(
            orch, query="Run a survival analysis on this dataset", thread_id="t-an2", framework=agent_config.AGENTIC,
            user_id="alice", dataset_ids=[rec.id], enable_web_search=False,
        )
    )
    art = res.execution_artifacts["sandbox_analysis"]
    assert _sandbox_calls(res) == []  # the rejected cell never reached the sandbox
    assert art["attempts"][0]["status"] == "policy_violation" and art["attempts"][0]["violations"]
    assert art["insufficient"] is True and art["answer_summary"].startswith("insufficient information")


def test_analyst_without_model_runs_deterministic_statistics(monkeypatch, tmp_path, upload_store, analyst_only):
    monkeypatch.setenv("PHRONESIS_SANDBOX", "on")
    monkeypatch.setenv("PHRONESIS_PLANNER", "off")
    rec = _upload(upload_store)
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    sandbox_ext.register_tools(orch_module.get_tool_registry())
    res = asyncio.run(
        orch.execute_inquiry(
            "Compute summary statistics for this dataset", thread_id="t-an3", framework=agent_config.AGENTIC,
            user_id="alice", dataset_ids=[rec.id], enable_web_search=False,
        )
    )
    art = res.execution_artifacts["sandbox_analysis"]
    assert art["author"] == "deterministic" and art["status"] == "ok"
    assert art["result"]["n_rows"] == 40 and art["result"]["n_columns"] == 10
    assert art["tables"][0]["name"] == "describe"
    assert len(_sandbox_calls(res)) == 1


def test_analyst_is_off_by_default_and_respects_the_option(monkeypatch, tmp_path, upload_store, analyst_only):
    rec = _upload(upload_store)
    monkeypatch.delenv("PHRONESIS_SANDBOX", raising=False)
    orch, calls, llm = _agentic(monkeypatch, tmp_path, {"analyst": "{}"})
    res, _ = asyncio.run(
        collect_events(
            orch, query="Compute summary statistics for this dataset", thread_id="t-an4", framework=agent_config.AGENTIC,
            user_id="alice", dataset_ids=[rec.id], enable_web_search=False,
        )
    )
    assert "sandbox_analysis" not in res.execution_artifacts and "analyst" not in llm.phases()

    monkeypatch.setenv("PHRONESIS_SANDBOX", "on")
    orch, calls, llm = _agentic(monkeypatch, tmp_path, {"analyst": "{}"})
    res, _ = asyncio.run(
        collect_events(
            orch, query="Compute summary statistics for this dataset", thread_id="t-an5", framework=agent_config.AGENTIC,
            user_id="alice", dataset_ids=[rec.id], enable_web_search=False, agent_options={"enable_code_analysis": False},
        )
    )
    assert "sandbox_analysis" not in res.execution_artifacts and not _sandbox_calls(res)
