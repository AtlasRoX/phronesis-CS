"""
Task-fetching unit and executor for a planned tool DAG (LLMCompiler, Kim et al., ICML 2024, arXiv:2312.04511).

Each node is dispatched as soon as every node it depends on has finished successfully; ready nodes run
concurrently on the event loop (bounded by PHRONESIS_AGENT_PLAN_CONCURRENCY, default 4), each through
AgentState.call_tool so it is checkpointed, budget-charged, traced, stored under an agent.plan_execute.* key
and labelled with its purpose for the evidence block. References ("$n1.results.0.id", see plan_graph) are
filled in from the finished nodes' ToolResult.data just before a node runs. A node whose dependency failed
or was skipped is skipped, and so are its own dependents. No model is involved here: as in LLMCompiler and
ReWOO (arXiv:2305.18323), observations go back to the planner only through the joiner (plan_execute.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import env_int
from .control import RunCancelled
from ..tools.arg_repair import adjustment_text
from .plan_graph import PlanGraph, PlanNode, RefError, canonical, resolve_args, validated_inputs

logger = logging.getLogger(__name__)

PHASE = "plan_execute"


def _concurrency() -> int:
    return env_int("PHRONESIS_AGENT_PLAN_CONCURRENCY", 4, 1, 16)


def _short_args(args: Dict[str, Any], limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Plan arguments that name drugs: when their reference comes back empty, drugs the run already found stand in.
from .plan_graph import DRUG_ARG_KEYS as _DRUG_ARG_KEYS  # noqa: E402


def run_known_drugs(state: Any, limit: int = 8) -> List[str]:
    """Drugs this run has already found, best first: ChEMBL approved drugs acting on the target, drugs resolved in
    ChEMBL, DGIdb approved interactions (by score), then drugs named in the question."""
    found: List[str] = []

    def add(name: Any) -> None:
        text = " ".join(str(name or "").split())
        if 2 <= len(text) <= 60 and text.lower() not in {f.lower() for f in found}:
            found.append(text)

    results = list((getattr(state, "tool_results", None) or {}).values())
    for res in results:
        data = getattr(res, "data", None)
        if getattr(res, "success", False) and isinstance(data, dict):
            for a in data.get("target_approved_drugs") or []:
                add(a.get("pref_name"))
    for res in results:
        data = getattr(res, "data", None)
        if getattr(res, "success", False) and isinstance(data, dict):
            for d in data.get("drugs") or []:
                if isinstance(d, dict):
                    add(d.get("pref_name"))
    for res in results:
        data = getattr(res, "data", None)
        if getattr(res, "success", False) and isinstance(data, dict) and "missing_genes" in data:  # DGIdb
            items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("approved")]
            for i in sorted(items, key=lambda i: -(i.get("score") or 0.0)):
                add(i.get("drug"))
    for ent in getattr(getattr(state, "ctx", None), "drugs", None) or []:
        add(getattr(ent, "label", None) or getattr(ent, "mention", None))
    return found[:limit]


_GENE_ARG_KEYS = {"gene", "genes", "symbol", "symbols", "target_symbol", "gene_symbol", "target", "targets",
                  "target_chembl_id"}


class _NeedsTarget(str):
    """A gene whose ChEMBL target id is looked up just before the call."""

    def __new__(cls, gene: str) -> "_NeedsTarget":
        obj = super().__new__(cls, gene)
        obj.gene = gene  # type: ignore[attr-defined]
        return obj


def run_known_genes(state: Any, referenced_tools: Iterable[str] = (), limit: int = 3) -> List[str]:
    """Genes for an argument whose reference came back empty: when it pointed at SynLethDB, its partners with screen
    or experimental evidence (PARP1 for BRCA2 loss); otherwise the question's genes, primary first."""
    out: List[str] = []
    if any("synleth" in t for t in referenced_tools):
        for res in (getattr(state, "tool_results", None) or {}).values():
            data = getattr(res, "data", None)
            if getattr(res, "success", False) and isinstance(data, dict) and "n_experimental" in data:
                out += [str(p["partner"]) for p in data.get("items") or [] if isinstance(p, dict) and p.get("experimental")]
    ctx = getattr(state, "ctx", None)
    primary = getattr(ctx, "primary_symbol", None)
    if not out:
        out += [primary] if primary else []
        out += [str(getattr(g, "label", "") or getattr(g, "mention", "")) for g in getattr(ctx, "genes", None) or []]
    return [g for g in dict.fromkeys(x for x in out if x)][:limit]


def _drug_value(key: str, drugs: List[str], state: Any, tool: str) -> Any:
    """The fallback in the shape the tool's argument takes: a list, one drug, or alternatives for a trial search."""
    entry = state.registry.get_entry(tool) if getattr(state, "registry", None) is not None else None
    field = (getattr(getattr(entry, "input_schema", None), "model_fields", {}) or {}).get(key)
    annotation = str(getattr(field, "annotation", "")) if field is not None else ""
    if "List" in annotation or "list" in annotation or (not annotation and key.lower().endswith("s")):
        return drugs[:5]
    if key.lower() == "intervention":
        return " OR ".join(drugs[:3])
    return drugs[0]


class PlanExecutor:
    """Runs one PlanGraph round; `results` maps finished node ids (all rounds) to their ToolResult."""

    def __init__(
        self,
        state: Any,
        *,
        phase: str = PHASE,
        results: Optional[Dict[str, Any]] = None,
        failed: Optional[Set[str]] = None,
        prior_calls: Optional[Iterable[Tuple[str, Dict[str, Any]]]] = None,
        concurrency: Optional[int] = None,
    ) -> None:
        self.state = state
        self.phase = phase
        self.results: Dict[str, Any] = results if results is not None else {}
        self.failed: Set[str] = failed if failed is not None else set()
        self.seen_calls: Set[Tuple[str, str]] = {(t, canonical(a)) for t, a in (prior_calls or [])}
        self.concurrency = max(1, int(concurrency or _concurrency()))
        self.dispatch_log: List[str] = []  # node ids in dispatch order (tests and the artifact use it)
        self.finish_log: List[str] = []
        self.known_ids: Set[str] = set(self.results) | set(self.failed)

    async def _node_step(self, node: PlanNode, status: str, text: str) -> None:
        title = f"{node.tool.replace('_', ' ')}" + (f": {node.purpose}" if node.purpose else "")
        await self.state.step(
            f"agent_node_{node.id}",
            title[:160],
            text,
            status=status,
            agent="Planner",
            node_id=node.id,
            tool=node.tool,
            depends_on=list(node.depends_on),
        )

    async def _skip(self, node: PlanNode, reason: str) -> None:
        node.status = "skipped"
        node.error = reason[:300]
        self.failed.add(node.id)
        await self._node_step(node, "skipped", reason)

    def _resolve_with_fallback(self, node: PlanNode) -> Tuple[Any, str]:
        """Resolved arguments, plus a note when a drug argument whose reference came back empty was filled with the
        drugs this run already found (ChEMBL approved drugs for the target, DGIdb approved interactions, drugs
        named in the question). The planner often points "names" at a step that returned no drug field; the drugs
        it meant are usually already in the evidence. Raises RefError when nothing can stand in."""
        args, note = self._resolve_refs(node)
        blank = [k for k in getattr(node, "fill_from_run", []) or [] if k not in args]
        if blank:
            drugs = run_known_drugs(self.state)
            if not drugs:
                raise RefError(f"no drug for {', '.join(blank)}: the planner left it blank and the run found none")
            for k in blank:
                args[k] = _drug_value(k, drugs, self.state, node.tool)
            note = "; ".join(x for x in (note, f"{'/'.join(blank)} filled with drugs found in this run ({', '.join(drugs[:5])})") if x)
        return args, note

    def _resolve_refs(self, node: PlanNode) -> Tuple[Any, str]:
        try:
            return resolve_args(node.args, self.results, known=self.known_ids), ""
        except RefError as first:
            if not isinstance(node.args, dict):
                raise
            out: Dict[str, Any] = {}
            notes: List[str] = []
            for key, value in node.args.items():
                try:
                    out[key] = resolve_args(value, self.results, known=self.known_ids)
                except RefError:
                    k = key.lower()
                    if k in _DRUG_ARG_KEYS:
                        drugs = run_known_drugs(self.state)
                        if not drugs:
                            raise first
                        out[key] = _drug_value(key, drugs, self.state, node.tool)
                        notes.append(f"{key} taken from drugs found earlier in this run ({', '.join(drugs[:5])})")
                    elif k in _GENE_ARG_KEYS:
                        genes = run_known_genes(self.state, self._referenced_tools(value))
                        if not genes:
                            raise first
                        # A ChEMBL target id is looked up from the gene just before the call (_fill_targets).
                        out[key] = _NeedsTarget(genes[0]) if k == "target_chembl_id" else _drug_value(key, genes, self.state, node.tool)
                        notes.append(f"{key} taken from genes found earlier in this run ({', '.join(genes[:3])})")
                    else:
                        raise first
            return out, "; ".join(notes)

    def _referenced_tools(self, value: Any) -> List[str]:
        from .plan_graph import refs_in

        return [str(getattr(self.results.get(n), "tool_name", "") or "") for n in refs_in(value, self.known_ids)]

    async def _fill_targets(self, node: PlanNode, args: Dict[str, Any]) -> None:
        """Replace each pending target with its ChEMBL id (chembl_resolve_drugs); RefError when it cannot be found."""
        for key, value in list(args.items()):
            if not isinstance(value, _NeedsTarget):
                continue
            res = await self.state.call_tool("chembl_resolve_drugs", phase=self.phase, store=False,
                                             purpose=f"ChEMBL target for {value.gene} (plan node {node.id})",
                                             target_symbol=value.gene)
            target = ((getattr(res, "data", None) or {}).get("target") or {}) if getattr(res, "success", False) else {}
            tid = target.get("target_chembl_id") if isinstance(target, dict) else None
            if not tid:
                raise RefError(f"no ChEMBL target found for {value.gene}")
            args[key] = tid
            if "target_symbol" in getattr(getattr(self.state.registry.get_entry(node.tool), "input_schema", None), "model_fields", {}):
                args.setdefault("target_symbol", value.gene)

    async def _run_node(self, node: PlanNode) -> None:
        t0 = time.perf_counter()
        state = self.state
        try:
            args, fallback = self._resolve_with_fallback(node)
            if isinstance(args, dict):
                await self._fill_targets(node, args)
        except RefError as exc:
            # The step it builds on returned nothing usable and the run has no drugs to stand in: nothing failed,
            # the step just has no input, so it is skipped rather than shown as an error.
            node.status, node.error = "skipped", f"skipped: an earlier step gave it no input ({exc})"
            node.seconds = round(time.perf_counter() - t0, 3)
            return
        if fallback:
            node.adjusted = list(dict.fromkeys([*node.adjusted, fallback]))
        node.resolved_args = args if isinstance(args, dict) else dict(node.args)
        call_args = node.resolved_args
        entry = state.registry.get_entry(node.tool) if state.registry is not None else None
        inputs = validated_inputs(entry, call_args) if entry is not None else None
        if inputs is not None:
            sig = (node.tool, canonical(inputs))
            if sig in self.seen_calls:
                node.status, node.error = "skipped", "an identical call was already made this turn"
                node.seconds = round(time.perf_counter() - t0, 3)
                return
            self.seen_calls.add(sig)
        key = state.next_key(self.phase, node.tool)
        res = await state.call_tool(node.tool, phase=self.phase, purpose=node.purpose or f"plan node {node.id}", key=key, **call_args)
        node.result_key = key
        node.seconds = round(time.perf_counter() - t0, 3)
        adjusted = (getattr(res, "metadata", None) or {}).get("arguments_adjusted") or []
        if isinstance(adjusted, list):
            node.adjusted = list(dict.fromkeys([*node.adjusted, *(str(a) for a in adjusted)]))
        if getattr(res, "success", False):
            node.status = "done"
            self.results[node.id] = res
        else:
            node.status = "error"
            node.error = str(getattr(res, "error", "") or "tool call failed")[:300]

    async def run(self, graph: PlanGraph) -> PlanGraph:
        pending: Dict[str, PlanNode] = {n.id: n for n in graph.nodes if n.status == "pending"}
        self.known_ids = set(self.results) | set(self.failed) | set(graph.ids())
        order = {n.id: i for i, n in enumerate(graph.nodes)}
        running: Dict["asyncio.Task[None]", PlanNode] = {}
        state = self.state
        try:
            while pending or running:
                # Skip every pending node behind a failed or skipped dependency (transitively).
                changed = True
                while changed:
                    changed = False
                    for nid, node in list(pending.items()):
                        bad = [d for d in node.depends_on if d in self.failed]
                        if bad:
                            del pending[nid]
                            await self._skip(node, f"skipped: dependency {', '.join(bad)} did not succeed")
                            changed = True

                ready = sorted(
                    (n for n in pending.values() if all(d in self.results for d in n.depends_on)),
                    key=lambda n: order.get(n.id, 0),
                )
                for node in ready:
                    if len(running) >= self.concurrency:
                        break
                    state.control.checkpoint()
                    if state.budget.exhausted():
                        for nid, rest in list(pending.items()):
                            del pending[nid]
                            await self._skip(rest, f"skipped: {state.budget.exhausted_reason or 'agent budget used up'}")
                        break
                    del pending[node.id]
                    node.status = "running"
                    self.dispatch_log.append(node.id)
                    await self._node_step(node, "running", _short_args(node.args))  # planned args, refs shown as written
                    running[asyncio.ensure_future(self._run_node(node))] = node

                if not running:
                    # Nothing can start: whatever is left waits on a node that will never finish.
                    for nid, rest in list(pending.items()):
                        del pending[nid]
                        await self._skip(rest, "skipped: its dependencies could not run")
                    break

                done, _ = await asyncio.wait(list(running), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    node = running.pop(task)
                    exc = task.exception()
                    if isinstance(exc, RunCancelled):
                        raise exc
                    if exc is not None:
                        node.status, node.error = "error", f"{type(exc).__name__}: {str(exc)[:200]}"
                        logger.warning("Plan node %s failed: %s", node.id, exc)
                    if node.status != "done":
                        self.failed.add(node.id)
                    self.finish_log.append(node.id)
                    adjusted = f" · {adjustment_text(node.adjusted)}" if node.adjusted else ""
                    if node.status == "done":
                        await self._node_step(node, "done", f"done in {node.seconds or 0:.1f}s · {node.result_key}{adjusted}")
                    elif node.status == "skipped":
                        await self._node_step(node, "skipped", node.error or "skipped")
                    else:
                        await self._node_step(node, "error", (node.error or "failed") + adjusted)
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        return graph


async def execute_plan(state: Any, graph: PlanGraph, **kwargs: Any) -> PlanExecutor:
    """Run graph's pending nodes; returns the executor (its results, failures and dispatch order)."""
    ex = PlanExecutor(state, **kwargs)
    await ex.run(graph)
    return ex


__all__ = ["PHASE", "PlanExecutor", "execute_plan"]
