"""End to end: every framework with every real agentic phase loaded, offline (scripted model, recording registry)."""

from __future__ import annotations

import asyncio
import json

import pytest

from phronesis.agentic import config as agent_config
from phronesis.agentic.control import ApprovalDecision, create_control
from tests.agentic_fakes import collect_events, make_agentic_orchestrator

QUESTION = "What diseases is KRAS associated with, and which pathways is it part of?"


def _phase_errors(res):
    return [p for p in res.execution_artifacts.get("agent_run", {}).get("phases", []) if p["status"] == "error"]


@pytest.mark.parametrize("framework", agent_config.AGENTIC_FRAMEWORKS)
def test_every_framework_completes_with_all_phases(monkeypatch, tmp_path, framework):
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, events = asyncio.run(collect_events(orch, query=QUESTION, thread_id=f"e2e-{framework}", framework=framework))
    assert res.framework_used == framework
    assert res.response.strip()
    assert events[0]["type"] == "run_control"
    run = res.execution_artifacts["agent_run"]
    assert run["framework"] == framework
    assert not _phase_errors(res), _phase_errors(res)
    assert run["budget"]["used"]["llm_calls"] <= run["budget"]["limits"]["llm_calls"]
    json.dumps(res.to_dict(), default=str)  # the API response stays serialisable
    assert any(name == "lithoscribe_literature_synthesis" for name, _ in calls)


def test_framework_specific_phases_run(monkeypatch, tmp_path):
    ran = {}
    for framework in agent_config.AGENTIC_FRAMEWORKS:
        orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path / framework)
        res, _ = asyncio.run(collect_events(orch, query=QUESTION, thread_id=f"fw-{framework}", framework=framework))
        ran[framework] = {p["phase"] for p in res.execution_artifacts["agent_run"]["phases"]}
    assert "plan_execute" in ran[agent_config.AGENTIC] and "plan_execute" in ran[agent_config.DEBATE]
    assert "supervisor" in ran[agent_config.SUPERVISOR] and "plan_execute" not in ran[agent_config.SUPERVISOR]
    assert "swarm" in ran[agent_config.SWARM] and "supervisor" not in ran[agent_config.SWARM]
    assert "debate" in ran[agent_config.DEBATE] and "debate" not in ran[agent_config.AGENTIC]
    for phases in ran.values():
        assert {"decompose", "confidence"} <= phases


def test_suppressed_simulation_is_never_run_by_any_phase(monkeypatch, tmp_path):
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    q = "Find papers on KRAS signalling in BIOMD0000000010 only, do not run any simulation."
    res, _ = asyncio.run(collect_events(orch, query=q, thread_id="nosim", framework=agent_config.AGENTIC))
    assert res.response.strip()
    assert not [name for name, _ in calls if name.startswith("vivarix_")]


def test_classic_default_has_no_agentic_artifacts(monkeypatch, tmp_path):
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, events = asyncio.run(collect_events(orch, query=QUESTION, thread_id="classic"))
    assert res.framework_used == agent_config.CLASSIC
    assert not {"agent_run", "agent_plan", "claim_confidence", "evidence_conflicts"} & set(res.execution_artifacts)
    assert all(phase in ("synthesis", "correction") for phase in llm.phases())


def test_interactive_simulation_waits_for_plan_approval(monkeypatch, tmp_path):
    """An interactive run that routes a simulation pauses until the researcher answers the approval request."""
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    control = create_control("default_user", interactive=True)
    seen = []

    async def scenario():
        async def cb(ev):
            seen.append(ev)
            if ev.get("type") == "approval_request" and ev.get("kind") == "plan":
                # Switch the simulation off, keep everything else.
                payload = json.loads(json.dumps(ev["payload"]))
                for row in payload.get("sources", []):
                    if row.get("key") == "vivarix":
                        row["run"] = False
                control.resolve(ev["approval_id"], ApprovalDecision(approved=True, payload=payload))

        return await orch.execute_inquiry(
            query="Simulate BIOMD0000000010 for 100 seconds and explain the MAPK dynamics.",
            thread_id="hitl",
            framework=agent_config.AGENTIC,
            run_control=control,
            interactive=True,
            event_callback=cb,
        )

    res = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert any(e.get("type") == "approval_request" for e in seen)
    assert res.execution_artifacts["plan_approval"]["status"] in ("edited", "approved")
    assert not [name for name, _ in calls if name.startswith("vivarix_")]
