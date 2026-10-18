"""
Plan-and-execute phase (feature 1): after the pipeline's own Stage 1 and 2 results exist, a planner model
writes a DAG of follow-up tool calls, the DAG is validated, optionally approved by the researcher, run in
parallel, and a joiner decides whether to finish or replan.

- LLMCompiler (Kim et al., ICML 2024, arXiv:2312.04511): planner -> task-fetching unit -> executor -> joiner,
  at most budget.max_replans replans (PHRONESIS_AGENT_MAX_REPLANS).
- ReWOO (Xu et al. 2023, arXiv:2305.18323): one up-front plan is the default; the joiner is only consulted
  when a replan is allowed.
- Plan-and-Solve (Wang et al., ACL 2023, arXiv:2305.04091): the planner's trigger sentence.
- ReAct (Yao et al., ICLR 2023, arXiv:2210.03629): PHRONESIS_AGENT_PLAN_MODE=react plans one node at a time
  and observes each result (at most PHRONESIS_AGENT_REACT_STEPS steps, default 4).
- Human in the loop (Magentic-UI action guards, arXiv:2507.22358): on an interactive run, nodes that call
  expensive tools (simulation, scans, calibration, code execution, docking) wait for an approval of kind
  "dag"; the researcher may drop nodes by id.

Settings: PHRONESIS_AGENT_PLAN=off|auto|on (off disables the phase), PHRONESIS_AGENT_PLAN_MODE=dag|react,
PHRONESIS_AGENT_PLAN_CONCURRENCY (executor.py), PHRONESIS_AGENT_APPROVAL_TIMEOUT (seconds, default 300).
Writes execution_artifacts["agent_plan"] and steps "agent_plan" and "agent_node_<id>".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from .budget import BudgetExhausted
from .config import AGENTIC, DEBATE, env_flag, env_float, env_int
from .executor import PlanExecutor
from .llm import AgentLLMUnavailable
from .plan_graph import PlanGraph, PlanNode, drop_nodes, is_expensive_tool, parse_plan, validate_plan, validated_inputs
from .planner import JOIN_PHASE, PLAN_PHASE, build_join_prompt, build_plan_prompt, observations, parse_join, select_tools
from .runtime import AgentPhase, PhaseOutcome, register_phase

logger = logging.getLogger(__name__)


def plan_mode() -> str:
    mode = (os.getenv("PHRONESIS_AGENT_PLAN_MODE", "") or "dag").strip().lower()
    return mode if mode in ("dag", "react") else "dag"


def prior_calls(state: Any, nodes: Optional[List[PlanNode]] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """(tool, validated inputs) of every call made this turn: the run recorder's log when one is active, plus
    the plan's own executed nodes (so a run without a recorder still never repeats a node)."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    try:
        from ..provenance.run_manifest import current_recorder

        rec = current_recorder()
        for c in list(getattr(rec, "_calls", None) or []):
            inputs = getattr(c, "inputs", None)
            if inputs:
                out.append((str(c.tool_name), dict(inputs)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Run recorder calls unavailable: %s", exc)
    for n in nodes or []:
        if n.status in ("done", "error"):
            entry = state.registry.get_entry(n.tool) if state.registry is not None else None
            inputs = validated_inputs(entry, n.call_args()) if entry is not None else None
            if inputs is not None:
                out.append((n.tool, inputs))
    return out


def _dropped_ids(payload: Dict[str, Any], graph: PlanGraph) -> Set[str]:
    """Node ids an edited approval payload removes: {"drop": [...]}, {"keep": [...]}, or an edited "nodes"
    list (entries with run/approved false or drop true are removed; ids missing from the list are removed)."""
    ids = set(graph.ids())
    drop: Set[str] = set()
    for key in ("drop", "dropped", "remove", "drop_nodes"):
        val = payload.get(key)
        if isinstance(val, (list, tuple, set)):
            drop |= {str(v) for v in val}
        elif isinstance(val, str):
            drop.add(val)
    keep = payload.get("keep")
    if isinstance(keep, (list, tuple)):
        drop |= ids - {str(v) for v in keep}
    nodes = payload.get("nodes")
    if isinstance(nodes, list) and nodes:
        listed: Set[str] = set()
        for n in nodes:
            if isinstance(n, dict) and n.get("id") is not None:
                nid = str(n["id"])
                listed.add(nid)
                if n.get("run") is False or n.get("approved") is False or n.get("drop") is True:
                    drop.add(nid)
            elif isinstance(n, str):
                listed.add(n)
        drop |= ids - listed
    return drop & ids


async def request_dag_approval(state: Any, graph: PlanGraph, round_no: int) -> Optional[Dict[str, Any]]:
    """Ask the researcher to approve the nodes before they run; returns the approval record or None."""
    control = state.control
    expensive = [n.id for n in graph.nodes if is_expensive_tool(n.tool)]
    # A run a human was attached to still asks (RunControl refuses at once when nobody is watching), so a job whose
    # watcher left never runs expensive nodes unasked (AGT-005).
    needs = getattr(control, "needs_human_decision", None)
    if not graph.nodes or not (bool(needs) if needs is not None else getattr(control, "interactive", False)):
        return None
    if not expensive and not state.options.get("require_plan_approval"):
        return None
    payload = {
        "round": round_no,
        "nodes": [{**n.to_artifact(), "run": True, "expensive": n.id in expensive} for n in graph.nodes],
        "expensive": expensive,
        "edit": "set run: false on a node to drop it (its dependents are dropped too), or return {'drop': [node ids]}",
    }
    timeout = env_float("PHRONESIS_AGENT_APPROVAL_TIMEOUT", 300.0, 5.0, 3600.0)
    announced: Dict[str, Any] = {}

    async def announce(approval_id: str) -> None:
        announced["approval_id"] = approval_id
        await state.send(
            {"type": "approval_request", "run_id": control.run_id, "approval_id": approval_id, "kind": "dag", "payload": payload}
        )
        await state.step(
            "agent_plan_approval",
            "Waiting for your approval of the planned tool calls",
            f"{len(graph.nodes)} planned · {len(expensive)} expensive ({', '.join(expensive)})" if expensive else f"{len(graph.nodes)} planned",
            status="running",
            agent="Planner",
            approval_id=approval_id,
        )

    decision = await control.request_approval("dag", payload, timeout_seconds=timeout, announce=announce)
    approval_id = announced.get("approval_id")
    drop: Set[str] = _dropped_ids(decision.payload, graph) if isinstance(decision.payload, dict) else set()
    if not decision.approved:
        drop |= set(expensive) if expensive else set(graph.ids())
        # timeout / unattended / error refuse too (fail closed); the status says which.
        status = decision.source if decision.source in ("timeout", "unattended", "error") else "rejected"
    elif drop:
        status = "edited"
    else:
        status = {"auto": "auto", "timeout": "timeout"}.get(decision.source, "approved")
    reason = (
        "dropped by the researcher" if decision.approved
        else "rejected by the researcher" if status == "rejected"
        else f"not approved ({status}); withheld"
    )
    removed = drop_nodes(graph, drop, reason)
    await state.step(
        "agent_plan_approval",
        f"Plan {status}",
        f"dropped {', '.join(removed)}" if removed else (decision.message or ""),
        status="done",
        agent="Planner",
    )
    return {"approval_id": approval_id, "status": status, "dropped": removed, "message": decision.message[:300]}


class PlanExecutePhase(AgentPhase):
    key = "plan_execute"
    stage = "augment"
    order = 10
    frameworks = (AGENTIC, DEBATE)
    needs_llm = True

    def applies(self, state: Any) -> bool:
        if env_flag("PHRONESIS_AGENT_PLAN", "auto") == "off":
            return False
        if state.options.get("enable_plan") is False:
            return False
        return getattr(state.ctx, "scope", "research") == "research"

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        mode = plan_mode()
        budget = state.budget
        artifact: Dict[str, Any] = {"planner": "llm", "mode": mode, "rationale": "", "replans": 0, "nodes": [], "rejected": []}
        all_nodes: List[PlanNode] = []
        results: Dict[str, Any] = {}
        failed: Set[str] = set()
        joins: List[Dict[str, Any]] = []
        approvals: List[Dict[str, Any]] = []
        await state.step("agent_plan", "Planning follow-up tool calls", "", status="running", agent="Planner")
        try:
            tools = select_tools(state, state.sub_questions)
            artifact["tools_offered"] = [e.name for e in tools]
            react_steps = env_int("PHRONESIS_AGENT_REACT_STEPS", 4, 1, 12)
            round_no = 0
            missing: Optional[List[str]] = None
            while True:
                state.control.checkpoint()
                room = budget.max_plan_nodes - len(all_nodes)
                if room <= 0 or budget.exhausted():
                    break
                cap = 1 if mode == "react" else room
                system, user = build_plan_prompt(
                    state,
                    tools=tools,
                    max_nodes=cap,
                    mode=mode,
                    previous=observations(all_nodes, results) if all_nodes else None,
                    missing=missing,
                )
                try:
                    reply = await state.llm.json(system, user, phase=PLAN_PHASE)
                except ValueError as exc:
                    if round_no == 0:
                        artifact["planner"] = "none"
                        artifact["status"] = "planner_unusable"
                    artifact["error"] = f"planner reply unusable: {str(exc)[:200]}"
                    break
                except BudgetExhausted as exc:
                    if round_no == 0:
                        artifact["planner"] = "none"
                        artifact["status"] = "budget"
                    artifact["error"] = str(exc)[:200]
                    break
                except AgentLLMUnavailable as exc:
                    if round_no == 0:
                        artifact["planner"] = "none"
                        artifact["status"] = "planner_failed"
                    artifact["error"] = f"model call failed: {str(exc)[:180]}"
                    break
                graph = parse_plan(reply, round_no=round_no, taken_ids={n.id for n in all_nodes})
                if round_no == 0 or (graph.rationale and not artifact["rationale"]):
                    artifact["rationale"] = graph.rationale
                validate_plan(
                    graph,
                    state.registry,
                    state.ctx,
                    max_nodes=cap,
                    prior_calls=prior_calls(state, all_nodes),
                    external_ids=set(results),
                )
                artifact["rejected"].extend(graph.rejected)
                graph.rejected = []
                if graph.nodes:
                    approval = await request_dag_approval(state, graph, round_no)
                    if approval is not None:
                        approvals.append(approval)
                        artifact["rejected"].extend(graph.rejected)
                        graph.rejected = []
                if not graph.nodes:
                    break
                all_nodes.extend(graph.nodes)
                await state.step(
                    "agent_plan",
                    "Running planned tool calls",
                    f"{len(all_nodes)} node(s) · {len(artifact['rejected'])} rejected",
                    status="running",
                    agent="Planner",
                    nodes=[n.to_artifact() for n in all_nodes][:50],
                )
                executor = PlanExecutor(state, results=results, failed=failed, prior_calls=prior_calls(state, all_nodes))
                await executor.run(graph)

                if mode == "react":
                    round_no += 1
                    if round_no >= react_steps:
                        break
                    continue
                if artifact["replans"] >= budget.max_replans or budget.exhausted():
                    break
                try:
                    jsys, juser = build_join_prompt(state, graph, results, all_nodes)
                    join = parse_join(await state.llm.json(jsys, juser, phase=JOIN_PHASE))
                except (ValueError, BudgetExhausted, AgentLLMUnavailable) as exc:
                    join = {"decision": "finish", "reason": f"joiner unavailable: {str(exc)[:160]}", "missing": []}
                joins.append(join)
                if join["decision"] != "replan":
                    break
                missing = join["missing"] or [join["reason"]]
                artifact["replans"] += 1
                round_no += 1
        except BaseException as exc:
            # Anything that escapes (cancellation, a time-budget stop, an unexpected error) is still recorded, so the
            # plan card never shows an empty plan without saying why.
            artifact.setdefault("error", f"{type(exc).__name__}: {str(exc)[:180]}" if str(exc) else type(exc).__name__)
            artifact.setdefault("status", "planner_failed" if not all_nodes else "interrupted")
            raise
        finally:
            artifact.setdefault("status", "ok")
            artifact["nodes"] = [n.to_artifact() for n in all_nodes][:50]
            artifact["rejected"] = artifact["rejected"][:50]
            if joins:
                artifact["joins"] = joins
            if approvals:
                artifact["approval"] = approvals[-1] if len(approvals) == 1 else approvals
            state.artifacts["agent_plan"] = artifact
            state.bag["agent_plan_results"] = {n.id: n.result_key for n in all_nodes if n.result_key}
        done = sum(1 for n in all_nodes if n.status == "done")
        failed_n = sum(1 for n in all_nodes if n.status == "error")
        skipped = sum(1 for n in all_nodes if n.status == "skipped")
        text = f"{done} done · {failed_n} failed · {skipped} skipped · {len(artifact['rejected'])} rejected"
        if artifact["replans"]:
            text += f" · {artifact['replans']} replan(s)"
        planner_failed = artifact.get("status") in ("planner_failed", "planner_unusable", "budget")
        if not all_nodes:
            text = artifact.get("error") or ("no follow-up calls needed" if artifact["planner"] == "llm" else "no plan")
        if planner_failed:
            reason = {"planner_failed": "model call failed", "planner_unusable": "the planner reply was unusable",
                      "budget": "the agent budget ran out"}[artifact["status"]]
            state.mark_phase(self.key, "budget" if artifact["status"] == "budget" else
                             ("llm_unavailable" if artifact["status"] == "planner_failed" else "error"), reason)
            await state.step("agent_plan", "The planner did not run",
                             f"{reason[:1].upper()}{reason[1:]}; the pipeline's own evidence was used.",
                             status="error", agent="Planner")
            return None
        await state.step("agent_plan", "Planned follow-up tool calls", text, status="done", agent="Planner")
        return None


register_phase(PlanExecutePhase())

__all__ = ["PlanExecutePhase", "plan_mode", "prior_calls", "request_dag_approval"]
