"""WP7 platform: clarification, plan approval, run control routes, background jobs, project memory, MCP and A2A."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agentic import approval as approval_mod
from phronesis.agentic import clarify as clarify_mod
from phronesis.agentic import config as agent_config
from phronesis.agentic import jobs as jobs_mod
from phronesis.agentic import project_memory as pm_mod
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.control import ApprovalDecision, RunControl, create_control
from phronesis.agentic.llm import AgentLLM
from phronesis.agentic.runtime import register_phase, restore_phases, snapshot_phases
from phronesis.agentic.state import AgentState
from phronesis.tools.registry import ToolResult
from phronesis.utils.memory.project_store import FactInput, ProjectStore, personalized_pagerank
from tests.agentic_fakes import collect_events, make_agentic_orchestrator
from tests.fakes import build_recording_registry, called

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
KRAS_Q = "What diseases is KRAS associated with?"


# ---------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def only_phases():
    """Run only the phases a test names (other packages' phases stay out of these probes)."""
    saved = snapshot_phases()
    from phronesis.extensions import load_extensions

    load_extensions()
    clarify_mod.clear_markers()

    def install(*phases):
        restore_phases({})
        for p in phases:
            register_phase(p)

    try:
        yield install
    finally:
        restore_phases(saved)
        clarify_mod.clear_markers()


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("PHRONESIS_JOBS_DIR", str(tmp_path / "jobs"))
    yield tmp_path
    jobs_mod.set_job_manager(None)


@pytest.fixture
def api(monkeypatch, stores):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    monkeypatch.delenv("PHRONESIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PHRONESIS_JWT_SECRET", raising=False)
    monkeypatch.setenv("PHRONESIS_API_TOKENS", "alice-token:alice,bob-token:bob")
    with TestClient(backend_main.app) as client:
        yield client


def _state(ctx: Any, tool_results: Dict[str, Any] | None = None, **kw: Any) -> AgentState:
    budget = RunBudget.from_env(None)
    return AgentState(
        query=kw.pop("query", KRAS_Q),
        clean_query=kw.pop("clean_query", KRAS_Q),
        ctx=ctx,
        registry=None,
        user_id=kw.pop("user_id", "alice"),
        thread_id=kw.pop("thread_id", "t-state"),
        model_name="m",
        api_key=None,
        framework=agent_config.AGENTIC,
        budget=budget,
        control=kw.pop("control", None) or RunControl(kw.pop("control_user", "alice"), interactive=False),
        llm=AgentLLM(None, "m", None, budget),
        tool_results=tool_results or {},
        emit=_noop_emit,
        send=_noop_send,
        **kw,
    )


async def _noop_emit(*_a: Any, **_k: Any) -> None:
    return None


async def _noop_send(_ev: Dict[str, Any]) -> None:
    return None


def _ctx(**kw: Any) -> SimpleNamespace:
    base: Dict[str, Any] = {
        "scope": "research", "genes": [], "drugs": [], "diseases": [], "variants": [], "entities": [], "attachments": [],
        "primary_target": None, "primary_symbol": None, "biomodel_id": None, "model_attachment": None, "is_simulation_intent": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------------------------------------
# F15 clarification
# ---------------------------------------------------------------------------------------------------------

def test_clarify_asks_for_ambiguous_family_once(monkeypatch, tmp_path, only_phases):
    only_phases(clarify_mod.ClarifyPhase())
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, _ = asyncio.run(collect_events(orch, query="Tell me about RAS signalling", thread_id="tc1", user_id="u1", framework=agent_config.AGENTIC))
    assert res.task_status["task_status"] == "needs_input"
    assert "gene family" in res.response and "1. HRAS" in res.response and "2. KRAS" in res.response
    art = res.execution_artifacts["clarification"]
    assert art["signals"] == ["ambiguous_gene_family"] and "KRAS" in art["options"]
    assert not calls  # nothing was searched

    # The next turn in the thread is the answer: never ask twice in a row; the answer reaches the brief.
    res2, _ = asyncio.run(collect_events(orch, query="2", thread_id="tc1", user_id="u1", framework=agent_config.AGENTIC))
    assert "clarification" not in res2.execution_artifacts and res2.task_status["task_status"] != "needs_input"
    # CLAM's last step: the chosen option is merged with the original question and the turn searches for it.
    assert res2.query_context["primary_symbol"] == "KRAS"
    assert called(calls, "synapta_kegg_pathways") == [{"target_symbol": "KRAS"}]
    assert any("Their answer: 'KRAS'" in user for phase, _s, user in llm.calls if phase == "synthesis")


def test_clarify_respects_options_and_clear_questions(monkeypatch, tmp_path, only_phases):
    only_phases(clarify_mod.ClarifyPhase())
    orch, _calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    off, _ = asyncio.run(
        collect_events(orch, query="Tell me about RAS signalling", thread_id="tc2", framework=agent_config.AGENTIC,
                       agent_options={"allow_clarification": False})
    )
    assert "clarification" not in off.execution_artifacts
    clear, _ = asyncio.run(collect_events(orch, query=KRAS_Q, thread_id="tc3", framework=agent_config.AGENTIC))
    assert "clarification" not in clear.execution_artifacts and clear.task_status["task_status"] != "needs_input"


def test_clarify_signals_simulation_and_datasets():
    sim = clarify_mod.detect_signals(_ctx(is_simulation_intent=True), "Simulate the model for 100 seconds")
    assert [s["signal"] for s in sim] == ["simulation_without_model"]
    a = SimpleNamespace(kind="table", display_name="counts_a.csv", id="ds_000000000001")
    b = SimpleNamespace(kind="table", display_name="counts_b.csv", id="ds_000000000002")
    two = clarify_mod.detect_signals(_ctx(attachments=[a, b]), "Run a differential analysis")
    assert two[0]["signal"] == "multiple_datasets" and two[0]["options"] == ["counts_a.csv", "counts_b.csv"]
    assert not clarify_mod.detect_signals(_ctx(attachments=[a, b]), "Analyse counts_b.csv")
    # A family next to other entities is only borderline: without a model it never interrupts.
    fam = SimpleNamespace(mention="RAS", label="RAS", ambiguous_with=["HRAS", "KRAS", "NRAS"])
    border = clarify_mod.detect_signals(_ctx(genes=[fam], drugs=[SimpleNamespace(label="sotorasib")]), "RAS inhibitors")
    assert border[0]["strength"] == "borderline"
    assert not clarify_mod.detect_signals(_ctx(genes=[fam], biomodel_id="BIOMD0000000010", is_simulation_intent=True), "Simulate the RAS model")
    state = _state(_ctx(genes=[fam], drugs=[SimpleNamespace(label="sotorasib")]), clean_query="RAS inhibitors", thread_id="tb")
    assert asyncio.run(clarify_mod.ClarifyPhase().run(state)) is None


# ---------------------------------------------------------------------------------------------------------
# F15 plan approval
# ---------------------------------------------------------------------------------------------------------

def _approve_with(control: RunControl, decision_for):
    seen: List[Dict[str, Any]] = []

    async def cb(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "approval_request":
            seen.append(ev)
            # Answer like a client would, through the run's control.
            asyncio.get_running_loop().call_soon(control.resolve, ev["approval_id"], decision_for(ev))

    return cb, seen


def test_plan_approval_edit_switches_a_source_off(monkeypatch, tmp_path, only_phases):
    only_phases(approval_mod.PlanApprovalPhase())
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    control = create_control("alice", interactive=True)

    def edit(ev):
        rows = [dict(r, run=False) if r["key"] == "websurfer" else r for r in ev["payload"]["sources"]]
        return ApprovalDecision(approved=True, payload={"sources": rows}, message="skip the web")

    cb, seen = _approve_with(control, edit)
    res = asyncio.run(
        orch.execute_inquiry(query=KRAS_Q, thread_id="ta1", user_id="alice", framework=agent_config.AGENTIC, interactive=True,
                             run_control=control, agent_options={"require_plan_approval": True}, event_callback=cb)
    )
    assert len(seen) == 1 and seen[0]["run_id"] == control.run_id and seen[0]["kind"] == "plan"
    keys = {r["key"]: r for r in seen[0]["payload"]["sources"]}
    assert keys["websurfer"]["run"] and keys["lithoscribe"]["run"] and keys["websurfer"]["label"] == "Web search"
    art = res.execution_artifacts["plan_approval"]
    assert art["status"] == "edited" and art["switched_off"] == ["websurfer"] and art["approval_id"] == seen[0]["approval_id"]
    assert not called(calls, "websurfer_search")
    assert called(calls, "lithoscribe_literature_synthesis")
    sources = {s["name"]: s for s in res.query_context["sources"]}
    assert sources["websurfer"] == {"name": "websurfer", "run": False, "reason": "switched off by the researcher"}


def test_plan_rejection_keeps_literature_and_noninteractive_is_auto(monkeypatch, tmp_path, only_phases):
    only_phases(approval_mod.PlanApprovalPhase())
    orch, calls, _llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    control = create_control("alice", interactive=True)
    cb, _seen = _approve_with(control, lambda ev: ApprovalDecision(approved=False, message="literature only"))
    res = asyncio.run(
        orch.execute_inquiry(query=KRAS_Q, thread_id="ta2", user_id="alice", framework=agent_config.AGENTIC, interactive=True,
                             run_control=control, agent_options={"require_plan_approval": True}, event_callback=cb)
    )
    assert res.execution_artifacts["plan_approval"]["status"] == "rejected"
    assert not called(calls, "synapta_opentargets_search") and not called(calls, "websurfer_search")
    assert called(calls, "lithoscribe_literature_synthesis")

    calls.clear()
    auto, events = asyncio.run(
        collect_events(orch, query=KRAS_Q, thread_id="ta3", framework=agent_config.AGENTIC, agent_options={"require_plan_approval": True})
    )
    assert auto.execution_artifacts["plan_approval"]["status"] == "auto"
    assert not any(e.get("type") == "approval_request" for e in events)
    assert called(calls, "websurfer_search")
    # Interactive runs without expensive work or the option are not interrupted at all.
    plain, _ = asyncio.run(collect_events(orch, query=KRAS_Q, thread_id="ta4", framework=agent_config.AGENTIC, interactive=True))
    assert "plan_approval" not in plain.execution_artifacts


def test_expensive_work_detection():
    plan = SimpleNamespace(action="parameter_scan")
    ctx = _ctx(should_run=lambda k: k == "vivarix", simulation_plan=plan)
    state = _state(ctx)
    assert approval_mod.expensive_work(state) == ["parameter scan"]
    state.control.interactive = True
    assert approval_mod.PlanApprovalPhase().applies(state)


# ---------------------------------------------------------------------------------------------------------
# Run control routes
# ---------------------------------------------------------------------------------------------------------

def test_agent_run_routes_are_user_scoped(api):
    control = create_control("alice", interactive=True)
    base = f"/api/agent/runs/{control.run_id}"
    assert api.get(base, headers=ALICE).json()["run_id"] == control.run_id
    for method, path, body in (("get", base, None), ("post", base + "/steer", {"message": "x"}), ("post", base + "/cancel", {}),
                               ("post", base + "/approve", {"approval_id": "apr_0123456789", "approved": True})):
        r = getattr(api, method)(path, headers=BOB, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (path, r.status_code)
    assert api.get("/api/agent/runs/not-a-run", headers=ALICE).status_code == 404

    fut = api.portal.start_task_soon(control.request_approval, "plan", {"sources": []}, 10)
    for _ in range(200):
        if control.pending():
            break
        time.sleep(0.01)
    approval_id = control.pending()[0]["approval_id"]
    r = api.post(base + "/approve", headers=ALICE, json={"approval_id": approval_id, "approved": False, "message": "no"})
    assert r.status_code == 200 and r.json()["approved"] is False
    decision = fut.result(timeout=5)
    assert decision.approved is False and decision.source == "user" and decision.message == "no"
    assert api.post(base + "/approve", headers=ALICE, json={"approval_id": approval_id, "approved": True}).status_code == 409

    assert api.post(base + "/steer", headers=ALICE, json={"message": "focus on PDAC"}).json()["queued_steering"] == 1
    assert api.post(base + "/steer", headers=ALICE, json={"message": "x" * 2001}).status_code == 422
    assert api.post(base + "/cancel", headers=ALICE).json()["cancelled"] is True
    assert control.cancelled and api.get(base, headers=ALICE).json()["cancelled"] is True
    rel = api.get("/api/agent/reliability", headers=ALICE)
    assert rel.status_code == 200 and "available" in rel.json()


# ---------------------------------------------------------------------------------------------------------
# F13 background jobs
# ---------------------------------------------------------------------------------------------------------

def _result(text: str = "KRAS answer", status: str = "completed") -> Dict[str, Any]:
    return {"response": text, "thread_id": "t", "task_status": {"task_status": status}, "verification_report": {"passed": True, "verification_score": 1.0},
            "citations": {}, "framework_used": agent_config.AGENTIC}


async def quick_runner(request, *, user_id, event_callback, run_control, api_key=None):
    await event_callback({"type": "run_control", "run_id": run_control.run_id, "framework": request.get("framework"), "interactive": run_control.interactive})
    await event_callback({"type": "thought", "data": {"id": "plan", "title": "Reading the question"}})
    await event_callback({"type": "content_delta", "delta": "KRAS "})
    await event_callback({"type": "content_delta", "delta": "answer"})
    return _result(f"answer to {request['message']}")


async def waiting_runner(request, *, user_id, event_callback, run_control, api_key=None):
    await event_callback({"type": "run_control", "run_id": run_control.run_id})
    while not run_control.cancelled:
        await asyncio.sleep(0.01)
    return _result("The run was cancelled.", status="cancelled")


def _install_jobs(stores, runner, workers: int = 2) -> jobs_mod.JobManager:
    manager = jobs_mod.JobManager(jobs_mod.JobStore(str(stores / "jobs")), runner=runner, workers=workers)
    jobs_mod.set_job_manager(manager)
    return manager


def _wait_job(api, job_id: str, statuses, headers=ALICE, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        job = api.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in statuses or time.time() > deadline:
            return job
        time.sleep(0.02)


def _sse(api, path: str, headers=ALICE) -> List[Dict[str, Any]]:
    out = []
    with api.stream("GET", path, headers=headers) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_job_lifecycle_and_event_replay(api, stores):
    _install_jobs(stores, quick_runner)
    r = api.post("/api/jobs", headers=ALICE, json={"message": KRAS_Q, "framework": agent_config.AGENTIC, "api_key": "sk-secret"})
    assert r.status_code == 201, r.text
    job_id, run_id = r.json()["job_id"], r.json()["run_id"]
    job = _wait_job(api, job_id, jobs_mod.TERMINAL)
    assert job["status"] == "succeeded" and job["result"]["response"] == f"answer to {KRAS_Q}"
    assert job["run_id"] == run_id and "user_id" not in job and "api_key" not in json.dumps(job)
    events = api.get(f"/api/jobs/{job_id}/events?stream=false", headers=ALICE).json()["events"]
    statuses = [e["status"] for e in events if e["type"] == "job"]
    assert statuses == ["queued", "running", "succeeded"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "done" and any(e["type"] == "run_control" and e["run_id"] == run_id for e in events)
    later = api.get(f"/api/jobs/{job_id}/events?stream=false&after=3", headers=ALICE).json()["events"]
    assert [e["seq"] for e in later] == [e["seq"] for e in events if e["seq"] > 3]
    streamed = _sse(api, f"/api/jobs/{job_id}/events?after=2")
    assert [e["seq"] for e in streamed] == [e["seq"] for e in events if e["seq"] > 2] and streamed[-1]["type"] == "done"
    listed = api.get("/api/jobs", headers=ALICE).json()["jobs"]
    assert [j["job_id"] for j in listed] == [job_id] and "result" not in listed[0]
    assert api.get("/api/jobs", headers=BOB).json()["jobs"] == []
    assert api.get(f"/api/jobs/{job_id}", headers=BOB).status_code == 404
    assert api.get(f"/api/jobs/{job_id}/events", headers=BOB).status_code == 404
    assert api.post("/api/jobs", headers=ALICE, json={"message": ""}).status_code == 422
    assert api.post("/api/jobs", headers=ALICE, json={"message": "x", "framework": "nope"}).status_code == 422
    assert api.delete(f"/api/jobs/{job_id}", headers=ALICE).json()["success"] is True
    assert api.get(f"/api/jobs/{job_id}", headers=ALICE).status_code == 404


def test_job_cancel_limit_and_resume(api, stores, monkeypatch):
    monkeypatch.setenv("PHRONESIS_MAX_JOBS_PER_USER", "1")
    manager = _install_jobs(stores, waiting_runner)
    job_id = api.post("/api/jobs", headers=ALICE, json={"message": KRAS_Q}).json()["job_id"]
    assert _wait_job(api, job_id, ("running",))["status"] == "running"
    assert api.post("/api/jobs", headers=ALICE, json={"message": "another"}).status_code == 429
    assert api.post(f"/api/jobs/{job_id}/cancel", headers=BOB).status_code == 404
    api.post(f"/api/jobs/{job_id}/cancel", headers=ALICE)
    job = _wait_job(api, job_id, jobs_mod.TERMINAL)
    assert job["status"] == "cancelled"
    assert api.post(f"/api/jobs/{job_id}/resume", headers=BOB).status_code == 404
    manager.runner = quick_runner
    assert api.post(f"/api/jobs/{job_id}/resume", headers=ALICE).json()["status"] == "queued"
    job = _wait_job(api, job_id, ("succeeded",))
    assert job["status"] == "succeeded" and job["attempts"] == 2
    events = _sse(api, f"/api/jobs/{job_id}/events")  # replays both attempts; stops at the second done only
    assert events[-1]["type"] == "done" and any(e.get("resumed") for e in events)
    assert api.post(f"/api/jobs/{job_id}/resume", headers=ALICE).status_code == 409


def test_jobs_left_running_are_interrupted_on_restart(api, stores):
    store = jobs_mod.JobStore(str(stores / "jobs"))
    job = store.create("alice", {"message": KRAS_Q, "framework": agent_config.AGENTIC, "thread_id": "t-int"})
    store.update(job["job_id"], status="running")
    _install_jobs(stores, quick_runner)  # a new process: recovery runs when the manager starts
    job = api.get(f"/api/jobs/{job['job_id']}", headers=ALICE).json()
    assert job["status"] == "interrupted" and "restarted" in job["error"]
    api.post(f"/api/jobs/{job['job_id']}/resume", headers=ALICE)
    assert _wait_job(api, job["job_id"], jobs_mod.TERMINAL)["status"] == "succeeded"


def test_job_watcher_makes_the_run_interactive(stores):
    async def approving_runner(request, *, user_id, event_callback, run_control, api_key=None):
        payload = {"sources": []}
        task = asyncio.ensure_future(run_control.request_approval("plan", payload, timeout_seconds=10))
        await asyncio.sleep(0)
        pending = run_control.pending()
        if pending:
            await event_callback({"type": "approval_request", "run_id": run_control.run_id, "approval_id": pending[0]["approval_id"], "kind": "plan", "payload": payload})
        decision = await task
        await event_callback({"type": "thought", "data": {"id": "x"}})
        return _result(f"interactive={bool(pending)} source={decision.source}")

    async def scenario():
        manager = jobs_mod.JobManager(jobs_mod.JobStore(str(stores / "jobs2")), runner=approving_runner, workers=1)
        job = await manager.submit("alice", {"message": KRAS_Q})
        manager.attach_watcher(job["job_id"])
        for _ in range(200):
            current = manager.get(job["job_id"], "alice")
            if current["status"] == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        assert current["status"] == "waiting_approval" and current["pending_approvals"]
        manager.detach_watcher(job["job_id"])  # nobody left to answer: unattended (AGT-005)
        done = await manager.wait(job["job_id"], 5)
        assert done["status"] == "succeeded" and done["result"]["response"] == "interactive=True source=unattended"
        statuses = [e["status"] for e in manager.events(job["job_id"]) if e["type"] == "job"]
        assert statuses == ["queued", "running", "waiting_approval", "running", "succeeded"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------------------
# F16 project memory
# ---------------------------------------------------------------------------------------------------------

def test_project_crud_is_user_scoped(api):
    r = api.post("/api/projects", headers=ALICE, json={"name": "KRAS in PDAC", "description": "living review"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert pid.startswith("prj_") and r.json()["facts"] == {"valid": 0, "invalidated": 0}
    assert [p["id"] for p in api.get("/api/projects", headers=ALICE).json()["projects"]] == [pid]
    assert api.get("/api/projects", headers=BOB).json()["projects"] == []
    assert api.get(f"/api/projects/{pid}", headers=BOB).status_code == 404
    assert api.patch(f"/api/projects/{pid}", headers=BOB, json={"name": "x"}).status_code == 404
    assert api.patch(f"/api/projects/{pid}", headers=ALICE, json={"name": "KRAS G12D"}).json()["name"] == "KRAS G12D"
    assert api.get("/api/projects/../../etc", headers=ALICE).status_code == 404
    assert api.get("/api/projects/prj_bad!", headers=ALICE).status_code == 404
    assert api.delete(f"/api/projects/{pid}", headers=BOB).status_code == 404
    assert api.delete(f"/api/projects/{pid}", headers=ALICE).json()["success"] is True
    assert api.get(f"/api/projects/{pid}", headers=ALICE).status_code == 404


def test_fact_invalidation_and_mem0_decisions(tmp_path):
    store = ProjectStore(str(tmp_path))
    pid = store.create_project("alice", "p")["id"]
    first = store.upsert_facts(pid, "alice", [FactInput("KRAS G12D", "classification", "Pathogenic", confidence=0.8)])
    assert [o["op"] for o in first["ops"]] == ["ADD"]
    old_id = first["ops"][0]["fact_id"]
    assert [o["op"] for o in store.upsert_facts(pid, "alice", [FactInput("kras g12d", "classification", "pathogenic", confidence=0.8)])["ops"]] == ["NOOP"]
    assert [o["op"] for o in store.upsert_facts(pid, "alice", [FactInput("KRAS G12D", "classification", "Pathogenic", confidence=0.9, verified=True)])["ops"]] == ["UPDATE"]
    second = store.upsert_facts(pid, "alice", [FactInput("KRAS G12D", "classification", "Likely pathogenic")])
    assert second["invalidated"] == 1 and {o["op"] for o in second["ops"]} == {"INVALIDATE", "ADD"}
    old = store.get_fact(pid, old_id, "alice")
    assert old["invalid_at"] is not None and old["superseded_by"] == second["ops"][-1]["fact_id"]  # kept, not deleted
    assert [f["object"] for f in store.list_facts(pid, "alice")] == ["Likely pathogenic"]
    # Valued relation: a new score for the same pair supersedes the old one; two mechanisms in one batch coexist.
    store.upsert_facts(pid, "alice", [FactInput("KRAS", "association_score_with", "pancreatic carcinoma", value="0.830")])
    changed = store.upsert_facts(pid, "alice", [FactInput("KRAS", "association_score_with", "pancreatic carcinoma", value="0.790")])
    assert changed["invalidated"] == 1
    both = store.upsert_facts(pid, "alice", [FactInput("sotorasib", "mechanism", "GTPase KRas inhibitor"), FactInput("sotorasib", "mechanism", "covalent G12C binder")])
    assert both["written"] == 2 and both["invalidated"] == 0
    with pytest.raises(ValueError):
        store.upsert_facts(pid, "bob", [FactInput("a", "b", "c")])
    assert store.list_facts(pid, "bob") == [] and store.get_fact(pid, old_id, "bob") is None


def test_ppr_recall_follows_graph_links(tmp_path):
    store = ProjectStore(str(tmp_path))
    pid = store.create_project("alice", "p")["id"]
    store.upsert_facts(pid, "alice", [
        FactInput("EGFR", "association_score_with", "non-small cell lung carcinoma", value="0.900", confidence=0.95),
        FactInput("osimertinib", "targets", "EGFR", confidence=0.95),
        FactInput("KRAS", "association_score_with", "pancreatic carcinoma", value="0.830", confidence=0.6),
        FactInput("sotorasib", "targets", "KRAS", confidence=0.6),
        FactInput("NCT03600883", "studies_condition", "KRAS G12C mutant solid tumours", confidence=0.6),
    ])
    recalled = store.recall(pid, "alice", "Is sotorasib active?", entities=["sotorasib"], k=5)
    order = [(f["subject"], f["object"]) for f in recalled]
    # sotorasib -> KRAS -> pancreatic carcinoma is two hops away but still beats the better-scored EGFR facts.
    assert order[0] == ("sotorasib", "KRAS")
    assert order.index(("KRAS", "pancreatic carcinoma")) < order.index(("EGFR", "non-small cell lung carcinoma"))
    ranks = personalized_pagerank([("a", "b", 1.0), ("b", "c", 1.0), ("x", "y", 1.0)], {"a": 1.0})
    assert ranks["c"] > ranks["x"] == 0.0 and ranks["a"] > ranks["c"]


def test_project_memory_routes(api, stores):
    pid = api.post("/api/projects", headers=ALICE, json={"name": "p"}).json()["id"]
    from phronesis.utils.memory.project_store import get_project_store

    store = get_project_store()
    store.upsert_facts(pid, "alice", [FactInput("sotorasib", "targets", "KRAS"), FactInput("EGFR", "associated_with", "NSCLC")])
    ranked = api.get(f"/api/projects/{pid}/memory", headers=ALICE, params={"query": "sotorasib"}).json()
    assert ranked["mode"] == "ranked" and ranked["facts"][0]["subject"] == "sotorasib"
    assert api.get(f"/api/projects/{pid}/memory", headers=BOB).status_code == 404
    fid = ranked["facts"][0]["id"]
    edited = api.patch(f"/api/projects/{pid}/memory/{fid}", headers=ALICE, json={"object": "KRAS G12C"}).json()
    assert edited["object"] == "KRAS G12C" and edited["id"] != fid and "edited" in edited["tags"]
    assert store.get_fact(pid, fid, "alice")["superseded_by"] == edited["id"]
    gone = api.patch(f"/api/projects/{pid}/memory/{edited['id']}", headers=ALICE, json={"invalidate": True, "reason": "retracted"}).json()
    assert gone["invalid_at"] and gone["invalidation_reason"] == "retracted"
    history = api.get(f"/api/projects/{pid}/memory", headers=ALICE, params={"include_invalid": True}).json()["facts"]
    assert len(history) == 3
    assert api.patch(f"/api/projects/{pid}/memory/{fid}", headers=BOB, json={"invalidate": True}).status_code == 404
    assert api.delete(f"/api/projects/{pid}/memory/{fid}", headers=ALICE).json()["success"] is True
    assert api.delete(f"/api/projects/{pid}/memory/fct_bad", headers=ALICE).status_code == 404


def _tool_results() -> Dict[str, Any]:
    return {
        "synapta": ToolResult(success=True, tool_name="synapta_opentargets_associations", canonical_ids={"target_symbol": "KRAS"},
                              data={"associations": [{"disease_name": "pancreatic carcinoma", "overall_association_score": 0.83}]}),
        "chembl": ToolResult(success=True, tool_name="chembl_drug_profile", data={"drugs": [{"chembl_id": "CHEMBL4535757", "pref_name": "SOTORASIB", "mechanisms": [
            {"mechanism_of_action": "GTPase KRas inhibitor", "action_type": "INHIBITOR", "target_chembl_id": "CHEMBL2189121", "target_pref_name": "GTPase KRas"}]}]}),
        "clinical_trials": ToolResult(success=True, tool_name="lithoscribe_clinical_trials_search", data={"trials": [{"nct_id": "NCT03600883", "conditions": ["Non-small Cell Lung Cancer"]}]}),
        "vivarix": ToolResult(success=True, tool_name="vivarix_simulate_time_course", data={"model_id": "BIOMD0000000010", "model_name": "Kholodenko2000 MAPK"}),
        "websurfer": ToolResult(success=False, tool_name="websurfer_search", error="down"),
    }


def test_consolidation_extracts_facts_and_recall_injects_them(stores):
    from phronesis.utils.memory.project_store import get_project_store

    store = get_project_store()
    pid = store.create_project("alice", "KRAS")["id"]
    ctx = _ctx(primary_symbol="KRAS", primary_target="KRAS")
    state = _state(ctx, _tool_results(), project_id=pid, query="From now on always cite PMIDs. " + KRAS_Q)
    state.verification = {"passed": True}
    state.artifacts["hypotheses"] = {"target": "KRAS", "hypotheses": [{"statement": "KRAS knockdown lowers ERK-PP", "result": {"supports": True, "log2_fold_change": -1.2}}]}
    phase = pm_mod.ProjectConsolidatePhase()
    assert phase.applies(state)
    asyncio.run(phase.run(state))
    art = state.artifacts["project_memory"]
    assert art["written"] >= 7 and art["invalidated"] == 0 and art["verified"] is True
    facts = {(f["subject"], f["relation"], f["object"]) for f in store.list_facts(pid, "alice")}
    assert ("KRAS", "association_score_with", "pancreatic carcinoma") in facts
    assert ("SOTORASIB", "mechanism", "GTPase KRas inhibitor") in facts and ("SOTORASIB", "targets", "GTPase KRas") in facts
    assert ("NCT03600883", "studies_condition", "Non-small Cell Lung Cancer") in facts
    assert ("BIOMD0000000010", "models_topic", "KRAS") in facts
    assert ("KRAS", "hypothesis_supported", "KRAS knockdown lowers ERK-PP") in facts
    assert any(r == "instructs" for _s, r, _o in facts)
    assert all(f["verified"] and f["source"]["thread_id"] == "t-state" for f in store.list_facts(pid, "alice"))

    # A later unverified turn with a different score supersedes the old score and is tagged unverified.
    results = _tool_results()
    results["synapta"].data = {"associations": [{"disease_name": "pancreatic carcinoma", "overall_association_score": 0.71}]}
    later = _state(ctx, {"synapta": results["synapta"]}, project_id=pid)
    later.verification = {"passed": False}
    asyncio.run(phase.run(later))
    assert later.artifacts["project_memory"]["invalidated"] == 1
    current = [f for f in store.list_facts(pid, "alice") if f["relation"] == "association_score_with"]
    assert len(current) == 1 and current[0]["value"] == "0.710" and "unverified" in current[0]["tags"]

    recall_state = _state(_ctx(primary_symbol="KRAS", entities=[SimpleNamespace(label="KRAS", mention="KRAS")]), project_id=pid, clean_query="KRAS resistance")
    asyncio.run(pm_mod.ProjectRecallPhase().run(recall_state))
    assert recall_state.project_memory.startswith('<project_memory project="KRAS">') and "pancreatic carcinoma" in recall_state.project_memory
    assert 0 < len(recall_state.artifacts["project_memory"]["recalled"]) <= 12
    stranger = _state(_ctx(), project_id=pid, user_id="bob", control_user="bob")
    asyncio.run(pm_mod.ProjectRecallPhase().run(stranger))
    assert stranger.project_memory == "" and stranger.scope_notes and not pm_mod.ProjectConsolidatePhase().applies(stranger)


def test_project_memory_through_the_orchestrator(monkeypatch, tmp_path, only_phases, stores):
    only_phases(pm_mod.ProjectRecallPhase(), pm_mod.ProjectConsolidatePhase())
    from phronesis.utils.memory.project_store import get_project_store

    pid = get_project_store().create_project("u1", "KRAS")["id"]
    orch, _calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path)
    res, _ = asyncio.run(collect_events(orch, query=KRAS_Q, thread_id="tp1", user_id="u1", framework=agent_config.AGENTIC, project_id=pid))
    assert res.execution_artifacts["project_memory"]["written"] >= 1
    res2, _ = asyncio.run(collect_events(orch, query=KRAS_Q, thread_id="tp2", user_id="u1", framework=agent_config.AGENTIC, project_id=pid))
    assert res2.execution_artifacts["project_memory"]["recalled"]
    assert any("Facts recorded in earlier turns" in system + user for phase, system, user in llm.calls if phase == "synthesis")


# ---------------------------------------------------------------------------------------------------------
# F17 MCP server
# ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def mcp(api, monkeypatch):
    from app.backend.routers import mcp as mcp_router

    reg, calls = build_recording_registry()
    monkeypatch.setattr(mcp_router, "_get_registry", lambda: reg)
    seen: List[Any] = []

    async def fake_research(user_id, args):
        seen.append((user_id, args))
        out = _result(f"Research answer for {args.question}")
        out["citations"] = {"pmids": ["123456"]}
        return out

    monkeypatch.setattr(mcp_router, "research_runner", fake_research)
    return SimpleNamespace(client=api, calls=calls, seen=seen)


def _rpc(client, method, params=None, rid=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method, **({"id": rid} if rid is not None else {}), **({"params": params} if params is not None else {})}
    return client.post("/mcp", headers={**ALICE, **(headers or {})}, json=body)


def test_mcp_handshake_and_tool_listing(mcp):
    c = mcp.client
    init = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {"tools": {"listChanged": False}} and init["serverInfo"]["name"] == "phronesis"
    assert _rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-11-25"
    assert _rpc(c, "notifications/initialized", rid=None).status_code == 202
    assert _rpc(c, "ping").json()["result"] == {}
    disc = _rpc(c, "server/discover", headers={"MCP-Protocol-Version": "2026-07-28"}).json()["result"]
    assert "2026-07-28" in disc["supportedVersions"] and disc["capabilities"]["tools"]
    tools = {t["name"]: t for t in _rpc(c, "tools/list").json()["result"]["tools"]}
    assert "phronesis_research" in tools and "synapta_kegg_pathways" in tools and "lithoscribe_literature_synthesis" in tools
    assert "vivarix_simulate_time_course" not in tools and "lumicell_analyze_single_cell" not in tools
    assert tools["synapta_kegg_pathways"]["inputSchema"]["properties"]["target_symbol"]
    assert tools["phronesis_research"]["inputSchema"]["required"] == ["question"]
    assert tools["synapta_kegg_pathways"]["description"] == "KEGG pathways that contain a gene symbol."


def test_mcp_tool_calls(mcp, monkeypatch):
    c = mcp.client
    out = _rpc(c, "tools/call", {"name": "synapta_kegg_pathways", "arguments": {"target_symbol": "BRAF"}}).json()["result"]
    assert out["isError"] is False and out["structuredContent"]["results"][0]["id"] == "hsa04010"
    assert json.loads(out["content"][0]["text"]) == out["structuredContent"]
    assert called(mcp.calls, "synapta_kegg_pathways") == [{"target_symbol": "BRAF"}]
    bad = _rpc(c, "tools/call", {"name": "synapta_kegg_pathways", "arguments": {}}).json()["result"]
    assert bad["isError"] is True
    unknown = _rpc(c, "tools/call", {"name": "vivarix_simulate_time_course", "arguments": {}}).json()
    assert unknown["error"]["code"] == -32602
    res = _rpc(c, "tools/call", {"name": "phronesis_research", "arguments": {"question": "KRAS?", "framework": agent_config.DEBATE}}).json()["result"]
    assert res["content"][0]["text"] == "Research answer for KRAS?" and res["isError"] is False
    assert res["structuredContent"]["citations"] == {"pmids": ["123456"]} and res["structuredContent"]["verification"]["passed"] is True
    assert mcp.seen[-1][0] == "alice" and mcp.seen[-1][1].framework == agent_config.DEBATE
    invalid = _rpc(c, "tools/call", {"name": "phronesis_research", "arguments": {"question": "x", "framework": "bogus"}}).json()["result"]
    assert invalid["isError"] is True
    # Denied names stay hidden even when an operator lists them.
    monkeypatch.setenv("PHRONESIS_MCP_TOOLS", "synapta_kegg_pathways,analysis_run_python,dataset_upload")
    names = [t["name"] for t in _rpc(c, "tools/list").json()["result"]["tools"]]
    assert names == ["phronesis_research", "synapta_kegg_pathways"]


def test_mcp_errors_and_auth(mcp):
    c = mcp.client
    parse = c.post("/mcp", headers={**ALICE, "Content-Type": "application/json"}, content=b"{not json")
    assert parse.status_code == 400 and parse.json()["error"]["code"] == -32700
    assert c.post("/mcp", headers=ALICE, json={"id": 1, "method": "ping"}).json()["error"]["code"] == -32600
    assert c.post("/mcp", headers=ALICE, json=[]).json()["error"]["code"] == -32600
    assert _rpc(c, "nope/method").json()["error"]["code"] == -32601
    assert _rpc(c, "tools/call", {"name": 3}).json()["error"]["code"] == -32602
    version = _rpc(c, "ping", headers={"MCP-Protocol-Version": "2020-01-01"})
    assert version.status_code == 400 and version.json()["error"]["code"] == -32600
    meta = _rpc(c, "ping", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2020-01-01"}}).json()
    assert meta["error"]["code"] == -32600
    batch = c.post("/mcp", headers=ALICE, json=[{"jsonrpc": "2.0", "id": "a", "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                                {"jsonrpc": "2.0", "id": "b", "method": "missing"}]).json()
    assert [r["id"] for r in batch] == ["a", "b"] and "result" in batch[0] and batch[1]["error"]["code"] == -32601
    assert c.get("/mcp", headers=ALICE).status_code == 405
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert _rpc(c, "ping", headers={"Origin": "https://evil.example"}).status_code == 403


# ---------------------------------------------------------------------------------------------------------
# F17 A2A
# ---------------------------------------------------------------------------------------------------------

def _a2a(client, method, params, headers=ALICE, rid=1):
    return client.post("/a2a", headers=headers, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).json()


def _msg(text: str, **extra: Any) -> Dict[str, Any]:
    return {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": text}], **extra}}


def test_a2a_agent_card(api):
    card = api.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Phronesis" and card["url"] == "http://testserver/a2a"
    assert card["capabilities"]["streaming"] is False and card["defaultInputModes"] == ["text/plain"]
    assert {s["id"] for s in card["skills"]} == {"biomedical-research", "model-simulation"}
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer" and card["protocolVersion"] == "1.0.0"


def test_a2a_tasks_backed_by_jobs(api, stores):
    manager = _install_jobs(stores, quick_runner)
    task = _a2a(api, "SendMessage", _msg(KRAS_Q, contextId="ctx-1"))["result"]
    assert task["kind"] == "task" and task["contextId"] == "ctx-1" and task["status"]["state"] in ("submitted", "working", "completed")
    _wait_job(api, task["id"], jobs_mod.TERMINAL)
    got = _a2a(api, "GetTask", {"id": task["id"]})["result"]
    assert got["status"]["state"] == "completed" and got["artifacts"][0]["parts"][0]["text"] == f"answer to {KRAS_Q}"
    assert _a2a(api, "tasks/get", {"id": task["id"]})["result"]["id"] == task["id"]
    assert _a2a(api, "GetTask", {"id": task["id"]}, headers=BOB)["error"]["code"] == -32001
    assert _a2a(api, "CancelTask", {"id": task["id"]})["error"]["code"] == -32002
    listed = _a2a(api, "ListTasks", {"contextId": "ctx-1"})["result"]
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    blocking = _a2a(api, "message/send", {**_msg("second"), "configuration": {"blocking": True}})["result"]
    assert blocking["status"]["state"] == "completed"
    assert _a2a(api, "SendMessage", {"message": {"role": "user", "parts": []}})["error"]["code"] == -32602
    assert _a2a(api, "Unknown", {})["error"]["code"] == -32601

    manager.runner = waiting_runner
    slow = _a2a(api, "SendMessage", _msg("slow"))["result"]
    _wait_job(api, slow["id"], ("running",))
    _a2a(api, "CancelTask", {"id": slow["id"]})
    _wait_job(api, slow["id"], jobs_mod.TERMINAL)
    assert _a2a(api, "GetTask", {"id": slow["id"]})["result"]["status"]["state"] == "canceled"
