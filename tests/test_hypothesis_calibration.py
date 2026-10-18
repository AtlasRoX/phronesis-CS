"""WP4: hypothesis -> in-silico experiment loop (F6) and SBML model calibration with identifiability (F11)."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from phronesis.agentic import config as agent_config
from phronesis.agentic.runtime import register_phase, restore_phases, snapshot_phases
from phronesis.vivarix.calibration import CHI2_THRESHOLD_95, calibrate_parameters, load_timecourse
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import called

# -----------------------------------------------------------------------------
# Tiny models: A -> B (mass action), analytic solutions used as ground truth
# -----------------------------------------------------------------------------

TIMES = np.array([0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0])
A0 = 10.0


def _a_to_b(k: float, times: np.ndarray = TIMES) -> Dict[str, np.ndarray]:
    return {"A": A0 * np.exp(-k * times), "B": A0 * (1.0 - np.exp(-k * times))}


def _table(k: float) -> pd.DataFrame:
    return pd.DataFrame({"time": TIMES, **_a_to_b(k)})


def _sbml(params: Dict[str, float], law: str) -> str:
    plist = "".join(f'<parameter id="{n}" value="{v}"/>' for n, v in params.items())
    ci = "".join(f"<ci>{t}</ci>" for t in law.split("*"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4" level="2" version="4">
  <model id="tiny" name="tiny A to B">
    <listOfCompartments><compartment id="c" size="1"/></listOfCompartments>
    <listOfSpecies>
      <species id="A" compartment="c" initialConcentration="{A0}"/>
      <species id="B" compartment="c" initialConcentration="0"/>
    </listOfSpecies>
    <listOfParameters>{plist}</listOfParameters>
    <listOfReactions>
      <reaction id="R1" reversible="false">
        <listOfReactants><speciesReference species="A"/></listOfReactants>
        <listOfProducts><speciesReference species="B"/></listOfProducts>
        <kineticLaw><math xmlns="http://www.w3.org/1998/Math/MathML"><apply><times/>{ci}</apply></math></kineticLaw>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""


def _param(result: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(p for p in result["parameters"] if p["name"] == name)


# -----------------------------------------------------------------------------
# Calibration core (injectable simulator)
# -----------------------------------------------------------------------------

def test_calibration_recovers_rate_and_classifies_identifiable():
    res = calibrate_parameters(
        parameter_names=["k1"], data=_table(0.3), simulate=lambda th: _a_to_b(th["k1"]), initial_values={"k1": 0.05}
    )
    assert res["status"] == "ok"
    k1 = _param(res, "k1")
    assert k1["estimate"] == pytest.approx(0.3, rel=1e-3)
    assert k1["identifiability"] == "identifiable"
    assert k1["ci_low"] < 0.3 < k1["ci_high"]
    # The profile is the chi2 curve itself; the interval bounds sit where it crosses chi2_min + 3.84.
    prof = res["profiles"]["k1"]
    assert len(prof) == 7 and min(p["chi2"] for p in prof) < 1e-6
    assert res["threshold"]["delta_chi2"] == CHI2_THRESHOLD_95
    assert res["objective"]["n_points"] == 16 and res["objective"]["rmse"] < 1e-6
    assert res["objective"]["sigma_rule"]["B"] == "10% of max |observed|"
    assert set(res["fit"]["B"]) == {"time", "observed", "simulated"}
    json.dumps(res)  # JSON-safe


def test_product_of_parameters_is_structurally_non_identifiable():
    res = calibrate_parameters(
        parameter_names=["k1", "k2"],
        data=_table(0.21),
        simulate=lambda th: _a_to_b(th["k1"] * th["k2"]),
        initial_values={"k1": 0.5, "k2": 0.4},
    )
    assert res["status"] == "ok"
    for name in ("k1", "k2"):
        p = _param(res, name)
        assert p["identifiability"] == "structurally_non_identifiable"
        assert p["ci_low"] is None and p["ci_high"] is None
    assert _param(res, "k1")["estimate"] * _param(res, "k2")["estimate"] == pytest.approx(0.21, rel=1e-3)


def test_one_sided_profile_is_practically_non_identifiable():
    # Data that only shows the end state (A fully converted) bound k1 from below, never from above.
    late = np.array([20.0, 30.0, 40.0, 60.0, 80.0])
    df = pd.DataFrame({"time": late, **_a_to_b(2.0, late), "sd_B": 0.05})
    res = calibrate_parameters(
        parameter_names=["k1"], data=df, simulate=lambda th: _a_to_b(th["k1"], late), initial_values={"k1": 1.0},
        bounds={"k1": [1e-3, 1e3]},
    )
    k1 = _param(res, "k1")
    assert k1["identifiability"] == "practically_non_identifiable"
    assert k1["ci_low"] is not None and k1["ci_high"] is None
    assert res["objective"]["sigma_rule"]["B"] == "sd_B column"


def test_calibration_rejects_bad_requests():
    sim = lambda th: _a_to_b(th["k1"])  # noqa: E731
    no_time = calibrate_parameters(parameter_names=["k1"], data=_table(0.3).rename(columns={"time": "minute"}), simulate=sim)
    assert no_time["status"] == "error" and no_time["error_code"] == "DATA_NO_TIME"
    too_many = calibrate_parameters(parameter_names=[f"p{i}" for i in range(6)], data=_table(0.3), simulate=sim)
    assert too_many["error_code"] == "TOO_MANY_PARAMETERS"
    bad_bounds = calibrate_parameters(parameter_names=["k1"], data=_table(0.3), simulate=sim, bounds={"k1": [0, 1]})
    assert bad_bounds["error_code"] == "BOUNDS_INVALID"
    no_model = calibrate_parameters(parameter_names=["k1"], data=_table(0.3))
    assert no_model["error_code"] == "MODEL_REQUIRED"
    with pytest.raises(ValueError):
        load_timecourse(pd.DataFrame({"time": [0.0, 1.0]}))


# -----------------------------------------------------------------------------
# Real COPASI/basico on an uploaded SBML model
# -----------------------------------------------------------------------------

@pytest.fixture
def upload_store(monkeypatch, tmp_path):
    from phronesis.datasets.store import get_dataset_store, reset_dataset_store

    monkeypatch.setenv("PHRONESIS_UPLOAD_ROOT", str(tmp_path / "uploads"))
    reset_dataset_store()
    yield get_dataset_store()
    reset_dataset_store()


def _prov():
    from phronesis.datasets.models import DatasetProvenanceIn

    return DatasetProvenanceIn(source="in_house", attests_no_direct_identifiers=True)


def _upload(store, name: str, payload: bytes):
    return asyncio.run(store.create_from_stream("alice", [payload], name, _prov()))


def _basico_or_skip():
    try:
        import basico  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"basico unavailable: {exc}")


def test_basico_calibration_of_uploaded_sbml(upload_store):
    _basico_or_skip()
    from phronesis.agents.query_context import DatasetRef
    from phronesis.extensions.calibration import attachment_table_path, run_calibration_tool, run_list_parameters_tool

    single = _upload(upload_store, "single.xml", _sbml({"k1": 0.1}, "c*k1*A").encode())
    product = _upload(upload_store, "product.xml", _sbml({"k1": 0.5, "k2": 0.4}, "c*k1*k2*A").encode())
    table = _upload(upload_store, "timecourse.csv", _table(0.21).to_csv(index=False).encode())
    resolved, errors = upload_store.resolve_for_user("alice", [single.id, product.id, table.id])
    assert not errors
    paths = {a.id: a for a in resolved}
    tref = DatasetRef(id=table.id, kind="table", display_name="timecourse.csv", analysis_path=paths[table.id].analysis_path)
    data_path = attachment_table_path(tref)
    assert data_path and data_path.endswith("original.csv")

    listed = run_list_parameters_tool(biomodel_id=single.id, sbml_path=paths[single.id].model_path)
    if not listed.success:
        pytest.skip(f"basico could not load the test model: {listed.error}")
    assert {p["name"] for p in listed.data["global_parameters"]} == {"k1"}

    res = run_calibration_tool(
        biomodel_id=single.id, sbml_path=paths[single.id].model_path, data_path=data_path, parameter_names=["k1"],
        profile=True, profile_points=7, max_nfev=200, n_starts=1,
    )
    assert res.success, res.error
    k1 = _param(res.data, "k1")
    assert k1["estimate"] == pytest.approx(0.21, rel=1e-2)
    assert k1["identifiability"] == "identifiable" and k1["ci_low"] < 0.21 < k1["ci_high"]
    assert res.data["backend"] == "least_squares" and res.data["model_source"] == "user_upload"

    res2 = run_calibration_tool(
        biomodel_id=product.id, sbml_path=paths[product.id].model_path, data_path=data_path, parameter_names=["k1", "k2"],
    )
    assert res2.success, res2.error
    assert {p["identifiability"] for p in res2.data["parameters"]} == {"structurally_non_identifiable"}


def test_basico_copasi_backend_smoke(upload_store, monkeypatch):
    _basico_or_skip()
    from phronesis.extensions.calibration import attachment_table_path, run_calibration_tool
    from phronesis.agents.query_context import DatasetRef

    single = _upload(upload_store, "single.xml", _sbml({"k1": 0.1}, "c*k1*A").encode())
    table = _upload(upload_store, "tc.csv", _table(0.3).to_csv(index=False).encode())
    resolved, _ = upload_store.resolve_for_user("alice", [single.id, table.id])
    paths = {a.id: a for a in resolved}
    data_path = attachment_table_path(DatasetRef(id=table.id, kind="table", display_name="tc.csv", analysis_path=paths[table.id].analysis_path))
    monkeypatch.setenv("PHRONESIS_CALIBRATION_BACKEND", "copasi")
    res = run_calibration_tool(biomodel_id=single.id, sbml_path=paths[single.id].model_path, data_path=data_path, parameter_names=["k1"], profile=False)
    if not res.success:
        pytest.skip(f"COPASI backend unavailable here: {res.error}")
    assert _param(res.data, "k1")["estimate"] == pytest.approx(0.3, rel=2e-2)
    assert res.data["backend"] in ("copasi", "least_squares")  # least_squares only when COPASI's task failed (noted)


def test_calibration_tool_rejects_non_attachment_paths(upload_store, tmp_path):
    from phronesis.extensions.calibration import VivarixCalibrateInput, register_tools, run_calibration_tool
    from phronesis.tools.registry import AdapterType, BiomedicalToolRegistry

    stray = tmp_path / "elsewhere.csv"
    _table(0.3).to_csv(stray, index=False)
    res = run_calibration_tool(biomodel_id="BIOMD0000000010", data_path=str(stray), parameter_names=["k1"])
    assert not res.success and res.metadata["validation_code"] == "DATA_PATH_REJECTED"
    with pytest.raises(ValueError):
        VivarixCalibrateInput(data_path="x.csv", parameter_names=["k1"])  # no model
    reg = BiomedicalToolRegistry()
    register_tools(reg)
    entry = reg.get_entry("vivarix_calibrate_parameters")
    assert entry.adapter_type == AdapterType.LOCAL_COMPUTE and entry.timeout_seconds == 300.0
    assert reg.get_entry("vivarix_list_parameters") is not None


# -----------------------------------------------------------------------------
# Hypothesis phase: helpers
# -----------------------------------------------------------------------------

def test_elo_and_fold_change_helpers():
    from phronesis.agentic.hypothesis import elo_update, log2_fold_change

    a, b = elo_update(1200.0, 1200.0, 1.0)
    assert (a, b) == pytest.approx((1216.0, 1184.0))
    a2, b2 = elo_update(1400.0, 1200.0, 0.5)
    assert a2 + b2 == pytest.approx(2600.0) and a2 < 1400.0
    lfc, eps = log2_fold_change(0.0, 2.0)
    assert math.isfinite(lfc) and lfc > 20 and eps == pytest.approx(2e-9)
    assert log2_fold_change(0.0, 0.0)[0] == 0.0
    assert log2_fold_change(4.0, 1.0)[0] == pytest.approx(-2.0)


# -----------------------------------------------------------------------------
# Hypothesis phase through the offline orchestrator
# Toy model A -(k1)-> B -(k2)-> 0, solved analytically by the fake simulator (overrides are honoured).
# -----------------------------------------------------------------------------

TOY_PARAMS = {"(R1).k1": 0.3, "(R2).k2": 0.1}


def _toy_endpoint(k1: float, k2: float, t: float) -> Dict[str, float]:
    a = A0 * math.exp(-k1 * t)
    b = A0 * k1 / (k2 - k1) * (math.exp(-k1 * t) - math.exp(-k2 * t))
    return {"A": a, "B": b}


def _toy_sim(kw: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(TOY_PARAMS)
    applied = []
    for name, value in (kw.get("parameter_overrides") or {}).items():
        full = name if name in params else next(p for p in params if p.endswith("." + name))
        applied.append({"requested": name, "resolved": full, "previous_value": params[full], "applied_value": value})
        params[full] = float(value)
    final = _toy_endpoint(params["(R1).k1"], params["(R2).k2"], float(kw["duration"]))
    return {
        "model_id": kw["biomodel_id"],
        "model_name": "toy A -> B -> 0",
        "simulation_type": "TIME_COURSE",
        "duration": kw["duration"],
        "duration_seconds": kw["duration"],
        "intervals": kw["intervals"],
        "initial_concentrations": {"A": A0, "B": 0.0},
        "final_concentrations": final,
        "applied_overrides": applied,
    }


def _toy_params(_kw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model_id": "BIOMD0000000010",
        "species": [{"name": "A", "initial_concentration": A0}, {"name": "B", "initial_concentration": 0.0}],
        "global_parameters": [],
        "reaction_parameters": [
            {"name": "(R1).k1", "short_name": "k1", "reaction": "R1", "value": 0.3, "mapped_to": None},
            {"name": "(R2).k2", "short_name": "k2", "reaction": "R2", "value": 0.1, "mapped_to": None},
        ],
        "reactions": [{"name": "R1", "scheme": "A -> B"}, {"name": "R2", "scheme": "B -> "}],
    }


def _toy_tools() -> Dict[str, Any]:
    from phronesis.extensions.calibration import VivarixListParametersInput
    from phronesis.tools.registry import VivarixSimInput

    return {"vivarix_simulate_time_course": (VivarixSimInput, _toy_sim), "vivarix_list_parameters": (VivarixListParametersInput, _toy_params)}


@pytest.fixture
def wp4_phases():
    state = snapshot_phases()
    from phronesis.extensions import load_extensions

    load_extensions()
    from phronesis.agentic.calibrate import CalibratePhase
    from phronesis.agentic.hypothesis import HypothesisPhase

    restore_phases({})
    register_phase(HypothesisPhase())
    register_phase(CalibratePhase())
    try:
        yield
    finally:
        restore_phases(state)


QUESTION = "In BIOMD0000000010, what happens to B if k1 is inhibited? Generate and test hypotheses in silico."


def _overridden(calls) -> List[Dict[str, Any]]:
    return [kw for kw in called(calls, "vivarix_simulate_time_course") if kw.get("parameter_overrides")]


def test_hypothesis_deterministic_loop_runs_and_ranks_experiments(monkeypatch, tmp_path, wp4_phases):
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, extra_tools=_toy_tools())
    res, _events = asyncio.run(
        collect_events(orch, query=QUESTION, thread_id="h1", framework=agent_config.AGENTIC, agent_options={"enable_hypotheses": True})
    )
    art = res.execution_artifacts["hypotheses"]
    assert art["model_id"] == "BIOMD0000000010" and art["method"] == "elo-tournament"
    assert art["threshold_log2fc"] == 0.58 and art["generator"] == "deterministic"
    hyps = {h["intervention"]["parameter"] + ":" + str(h["intervention"]["factor"]): h for h in art["hypotheses"]}
    k1 = hyps["(R1).k1:0.1"]
    k2 = hyps["(R2).k2:0.1"]
    # Inhibiting k1 (which produces B) was predicted to lower B; at t=60 the slower conversion leaves MORE B.
    assert k1["prediction"] == "decrease" and k1["status"] == "tested"
    assert k1["result"]["supports"] is False and k1["result"]["log2_fold_change"] > 0.58
    # Inhibiting the B degradation rate raises B: supported.
    assert k2["prediction"] == "increase" and k2["result"]["supports"] is True
    duration = _overridden(calls)[0]["duration"]
    base, pert = _toy_endpoint(0.3, 0.1, duration)["B"], _toy_endpoint(0.3, 0.01, duration)["B"]
    assert k2["result"]["baseline"] == pytest.approx(base, rel=1e-5)
    assert k2["result"]["log2_fold_change"] == pytest.approx(math.log2(pert / base), abs=1e-3)
    # Ranking: the supported hypothesis wins; the evolved child (partial k2 inhibition) is tested too.
    ranked = sorted(art["hypotheses"], key=lambda h: h["rank"])
    assert ranked[0]["result"]["supports"] is True
    assert sum(h["elo"] for h in art["hypotheses"]) == pytest.approx(1200.0 * len(art["hypotheses"]), abs=0.5)
    evolved = [h for h in art["hypotheses"] if h["source"] == "evolved"]
    assert len(evolved) == 1 and evolved[0]["parent"]
    assert art["experiments_run"] == len(_overridden(calls)) <= 4
    # The measured numbers reach the verifier (tool_results) and the evidence block.
    assert called(calls, "vivarix_list_parameters")
    run = res.execution_artifacts["agent_run"]
    assert any(p["phase"] == "hypothesis" and p["status"] == "ok" for p in run["phases"])
    json.dumps(art)


def test_hypothesis_llm_generation_and_elo_judge(monkeypatch, tmp_path, wp4_phases):
    generated = {
        "reflection": "B is produced by R1 and removed by R2.",
        "research_plan": "Perturb both rate constants and the substrate.",
        "hypotheses": [
            {"statement": "Inhibiting k2 raises B.", "mechanism": "R2 degrades B.",
             "intervention": {"kind": "inhibition", "parameter": "k2", "factor": 0.1}, "readout": "B", "prediction": "increase"},
            {"statement": "Knocking down A lowers B.", "mechanism": "A is the substrate of R1.",
             "intervention": {"kind": "knockdown", "parameter": "A", "factor": 0.1}, "readout": "B", "prediction": "decrease"},
            {"statement": "Inhibiting kX lowers B.", "mechanism": "made up",
             "intervention": {"kind": "inhibition", "parameter": "kX", "factor": 0.1}, "readout": "B", "prediction": "decrease"},
        ],
    }

    def judge(_system: str, user: str) -> str:
        a_line = next(line for line in user.splitlines() if line.startswith("A: "))
        return json.dumps({"winner": "A" if "k2" in a_line else "B", "reason": "k2 has the larger supported effect"})

    orch, calls, llm = make_agentic_orchestrator(
        monkeypatch, tmp_path, responses={"hypothesis_generate": json.dumps(generated), "hypothesis_judge": judge},
        extra_tools=_toy_tools(),
    )
    res, _ = asyncio.run(collect_events(orch, query=QUESTION, thread_id="h2", framework=agent_config.AGENTIC))
    art = res.execution_artifacts["hypotheses"]
    assert art["generator"] == "llm" and art["reflection"].startswith("B is produced")
    assert any("kX" in r["reason"] for r in art["rejected"])  # reflection rejects a parameter the model lacks
    assert {h["intervention"]["parameter"] for h in art["hypotheses"] if h["source"] == "llm"} == {"(R2).k2", "A"}
    assert art["judge"] in ("llm", "mixed") and any(m["judge"] == "llm" for m in art["matches"])
    top = min(art["hypotheses"], key=lambda h: h["rank"])
    assert "(R2).k2" == top["intervention"]["parameter"]
    assert top["elo"] > 1200.0
    assert "hypothesis_judge" in llm.phases() and "hypothesis_generate" in llm.phases()
    assert "[phase:hypothesis_generate]" in next(c[1] for c in llm.calls if c[0] == "hypothesis_generate")
    # The knockdown hypothesis was simulated with the species' initial amount scaled by 0.1.
    assert {"A": pytest.approx(1.0)} in [kw["parameter_overrides"] for kw in _overridden(calls)]
    synth = llm.prompts("synthesis")[0]
    assert "<hypothesis_experiments" in synth and "log2_fold_change" in synth


def test_hypothesis_respects_suppressed_simulation(monkeypatch, tmp_path, wp4_phases):
    from phronesis.agentic.runtime import AgentPhase

    class Suppress(AgentPhase):
        key = "test_suppress"
        stage = "approve"

        async def run(self, state):
            state.ctx.suppress_simulation = True

    register_phase(Suppress())
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, extra_tools=_toy_tools())
    res, _ = asyncio.run(
        collect_events(orch, query=QUESTION, thread_id="h3", framework=agent_config.AGENTIC, agent_options={"enable_hypotheses": True})
    )
    assert not _overridden(calls)
    assert not called(calls, "vivarix_list_parameters")
    art = res.execution_artifacts["hypotheses"]
    assert art["simulation"] == "blocked" and art["experiments_run"] == 0
    assert all(h["status"] == "untested" and h["result"]["supports"] is None for h in art["hypotheses"])


def test_hypothesis_phase_off_by_option(monkeypatch, tmp_path, wp4_phases):
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, extra_tools=_toy_tools())
    res, _ = asyncio.run(
        collect_events(orch, query=QUESTION, thread_id="h4", framework=agent_config.AGENTIC, agent_options={"enable_hypotheses": False})
    )
    assert "hypotheses" not in res.execution_artifacts
    assert not _overridden(calls)


def test_classic_framework_runs_no_hypothesis_or_calibration(monkeypatch, tmp_path, wp4_phases):
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, extra_tools=_toy_tools())
    res, _ = asyncio.run(collect_events(orch, query=QUESTION, thread_id="h5"))
    assert "hypotheses" not in res.execution_artifacts and "model_calibration" not in res.execution_artifacts
    assert not _overridden(calls)


# -----------------------------------------------------------------------------
# Calibration phase end to end: uploaded SBML + uploaded time course, real tool executor
# -----------------------------------------------------------------------------

def test_calibrate_phase_fits_uploaded_model(monkeypatch, tmp_path, upload_store, wp4_phases):
    _basico_or_skip()
    from phronesis.agentic.runtime import unregister_phase
    from phronesis.extensions.calibration import (
        VivarixCalibrateInput,
        VivarixListParametersInput,
        run_calibration_tool,
        run_list_parameters_tool,
    )

    unregister_phase("hypothesis")
    model = _upload(upload_store, "tiny.xml", _sbml({"k1": 0.1}, "c*k1*A").encode())
    table = _upload(upload_store, "tiny_timecourse.csv", _table(0.25).to_csv(index=False).encode())
    tools = {
        "vivarix_calibrate_parameters": (VivarixCalibrateInput, lambda kw: run_calibration_tool(**kw)),
        "vivarix_list_parameters": (VivarixListParametersInput, lambda kw: run_list_parameters_tool(**kw)),
    }
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path, extra_tools=tools)
    res, _ = asyncio.run(
        collect_events(
            orch, query="Calibrate k1 of the uploaded model against the attached time course.", thread_id="c1",
            framework=agent_config.AGENTIC, user_id="alice", dataset_ids=[model.id, table.id], enable_web_search=False,
        )
    )
    art = res.execution_artifacts["model_calibration"]
    if art["status"] == "skipped":
        pytest.fail(art["reason"])
    assert art["status"] == "ok" and art["parameter_selection"]["source"] == "question"
    k1 = _param(art, "k1")
    assert k1["estimate"] == pytest.approx(0.25, rel=1e-2) and k1["identifiability"] == "identifiable"
    assert set(art["fit"]) == {"A", "B"} and art["profiles"]["k1"]
    cal_calls = called(calls, "vivarix_calibrate_parameters")
    assert len(cal_calls) == 1 and cal_calls[0]["data_path"].endswith("original.csv")
    json.dumps(art)


def test_calibrate_phase_never_runs_when_simulation_suppressed(monkeypatch, tmp_path, wp4_phases):
    from phronesis.agentic.runtime import AgentPhase
    from phronesis.extensions.calibration import VivarixCalibrateInput

    class Suppress(AgentPhase):
        key = "test_suppress"
        stage = "approve"

        async def run(self, state):
            state.ctx.suppress_simulation = True

    register_phase(Suppress())
    orch, calls, _llm = make_agentic_orchestrator(
        monkeypatch, tmp_path,
        extra_tools={"vivarix_calibrate_parameters": (VivarixCalibrateInput, lambda kw: {})},
    )
    res, _ = asyncio.run(
        collect_events(orch, query="Calibrate the parameters of BIOMD0000000010 against my data.", thread_id="c2", framework=agent_config.AGENTIC)
    )
    assert not called(calls, "vivarix_calibrate_parameters")
    art = res.execution_artifacts.get("model_calibration")
    assert art is not None and art["status"] == "skipped"


def test_calibration_intent_regex():
    from phronesis.agentic.calibrate import has_calibration_intent

    for q in ("Calibrate the model", "fit the model to my data", "run parameter estimation", "estimate the parameters k1"):
        assert has_calibration_intent(q), q
    assert not has_calibration_intent("What diseases is KRAS associated with?")
    for q in ("Calibrate BIOMD0000000010 against my time-course data", "run model calibration", "calibrate the ODE parameters"):
        assert has_calibration_intent(q), q
    # "calibrated" as an adjective is not a request to fit a model.
    for q in ("synthesize a calibrated therapeutic strategy", "give calibrated confidence for each claim"):
        assert not has_calibration_intent(q), q


def test_tumour_suppressor_hypotheses_target_synthetic_lethal_partners_not_the_gene():
    from types import SimpleNamespace

    from phronesis.agentic.hypothesis import HypothesisPhase, is_loss_of_function_target
    from phronesis.tools.registry import ToolResult

    assert is_loss_of_function_target("BRCA2", "anything") and is_loss_of_function_target("MYC", "MYC loss in X")
    assert not is_loss_of_function_target("KRAS", "KRAS G12C-mutant NSCLC")
    syn = ToolResult(success=True, tool_name="synlethdb_partners", data={"items": [
        {"partner": "PARP1", "experimental": True, "evidence_types": ["CRISPR/CRISPRi"], "pmids": ["27453043"]},
        {"partner": "XYZ", "experimental": False, "evidence_types": ["Text Mining"], "pmids": []}]})
    assoc = ToolResult(success=True, tool_name="synapta", data={"associations": [{"disease_name": "ovarian cancer", "overall_association_score": 0.7}]})
    ctx = SimpleNamespace(primary_symbol="BRCA2", diseases=[SimpleNamespace(mention="ovarian cancer", label="ovarian cancer")], drugs=[])
    state = SimpleNamespace(ctx=ctx, clean_query="druggable synthetic-lethal partners of BRCA2 loss", query="",
                            tool_results={"synlethdb": syn, "synapta": assoc})
    hyps = HypothesisPhase()._evidence_hypotheses(state)
    statements = [h.statement for h in hyps]
    assert statements and all("Inhibiting BRCA2" not in s and "BRCA2 is a candidate therapeutic target" not in s for s in statements)
    assert any(s.startswith("Inhibiting PARP1 is predicted to be selectively lethal") for s in statements)
    assert not any("XYZ" in s for s in statements)  # text-mining-only partners are not hypothesised
