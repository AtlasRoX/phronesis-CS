"""
The planned tool DAG of the plan-and-execute phase: nodes, references between them, parsing from a model
reply, and validation against the registry and the turn's hard policy before anything runs.

Design after LLMCompiler (Kim et al., ICML 2024, arXiv:2312.04511): the planner emits numbered tasks
{id, tool, args, depends_on, purpose}; a task's arguments may name an earlier task's output with a
placeholder, and the task-fetching unit (executor.py) fills the placeholder when that task finishes. Planning
the whole chain up front with evidence variables is ReWOO (Xu et al. 2023, arXiv:2305.18323).

Reference syntax (the exact grammar the planner is told about)
--------------------------------------------------------------
    ref      := "$" node_id path | "${" node_id path "}"
    node_id  := a node id of this plan (or of an earlier round), e.g. n1; a bare number "$1" means "n1"
    path     := ( "." segment | "[" index "]" )*
    segment  := a dict key ([A-Za-z_][A-Za-z0-9_-]*), a list index (-?digits) or "*"

- The path starts at the node's ToolResult.data. A first segment "data" or "canonical_ids" addresses that
  ToolResult field explicitly ("$n1.data.results.0.id" and "$n1.results.0.id" are the same value).
- "*" maps the rest of the path over every item of a list ("$n1.results.*.id" is the list of ids).
- An argument whose whole string is one reference takes the referenced value with its JSON type (a dict,
  list, number or string). A reference inside a longer string is replaced by the value's text (a list is
  joined with ", ", a dict is JSON).
- "$name" where name is not a node id is ordinary text and left unchanged.
- A reference adds an implicit dependency on its node, as LLMCompiler infers dependencies from placeholders.

Validation (validate_plan) rejects a node, with a reason, when its tool is not registered, its arguments do
not match the tool's input schema (unknown names, wrong types, missing required fields; fields holding a
reference are checked again after resolution), it breaks the hard policy (no simulation tool when the user
forbade simulation; only allowlisted sources on a "papers only" turn; no web tool with web search off; no
source the researcher switched off), it repeats a call the pipeline or an earlier node already made, it is
part of a dependency cycle, it depends on a rejected or unknown node, or it falls beyond max_plan_nodes.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, ValidationError

from ..tools.arg_repair import repair_arguments

logger = logging.getLogger(__name__)

NODE_STATUSES = ("pending", "running", "done", "error", "skipped")
# Arguments that would collide with AgentState.call_tool's own keyword parameters.
RESERVED_ARGS = frozenset({"phase", "purpose", "key", "store", "tool_name"})

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,23}$")
_SEG = r"(?:\.(?:[A-Za-z_][A-Za-z0-9_\-]*|-?\d+|\*)|\[(?:-?\d+|\*)\])"
_REF_RE = re.compile(
    r"\$\{(?P<bnode>[A-Za-z][A-Za-z0-9_]*|\d+)(?P<bpath>" + _SEG + r"*)\}"
    r"|\$(?P<node>[A-Za-z][A-Za-z0-9_]*|\d+)(?P<path>" + _SEG + r"*)"
)
_SEG_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*|-?\d+|\*)|\[(-?\d+|\*)\]")


class RefError(ValueError):
    """A reference could not be resolved against the finished node results."""


class PlanNode(BaseModel):
    id: str
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[str] = Field(default_factory=list)
    purpose: str = ""
    status: str = "pending"  # pending | running | done | error | skipped
    result_key: Optional[str] = None
    error: Optional[str] = None
    seconds: Optional[float] = None
    round: int = 0  # 0 = first plan, 1.. = replans
    resolved_args: Optional[Dict[str, Any]] = None  # args with references filled in, set when the node runs
    adjusted: List[str] = Field(default_factory=list)  # argument repairs (tools.arg_repair), e.g. "size 100→50"
    fill_from_run: List[str] = Field(default_factory=list)  # drug arguments left blank: filled at run time

    def call_args(self) -> Dict[str, Any]:
        return self.resolved_args if self.resolved_args is not None else self.args

    def to_artifact(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "tool": self.tool,
            "args": _json_safe(self.args),
            "depends_on": list(self.depends_on),
            "purpose": self.purpose,
            "status": self.status,
            "result_key": self.result_key,
            "error": self.error,
            "seconds": self.seconds,
        }
        if self.resolved_args is not None and self.resolved_args != self.args:
            out["resolved_args"] = _json_safe(self.resolved_args)
        if self.adjusted:
            out["adjusted"] = list(self.adjusted)
        return out


class PlanGraph(BaseModel):
    rationale: str = ""
    nodes: List[PlanNode] = Field(default_factory=list)
    rejected: List[Dict[str, str]] = Field(default_factory=list)  # [{"node", "reason"}]

    def get(self, node_id: str) -> Optional[PlanNode]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def ids(self) -> List[str]:
        return [n.id for n in self.nodes]

    def reject(self, node: Any, reason: str) -> None:
        nid = node.id if isinstance(node, PlanNode) else str(node)
        self.rejected.append({"node": nid, "reason": reason[:300]})
        self.nodes = [n for n in self.nodes if n.id != nid]


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def canonical(value: Any) -> str:
    """Canonical JSON of a call's arguments, for duplicate detection."""
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))


def normalise_id(raw: Any) -> str:
    text = str(raw if raw is not None else "").strip().lstrip("$")
    if text.isdigit():
        return f"n{int(text)}"
    return text


# References ---------------------------------------------------------------------------------------------------
def _parse_path(path: str) -> List[Any]:
    segs: List[Any] = []
    for m in _SEG_RE.finditer(path or ""):
        seg = m.group(1) if m.group(1) is not None else m.group(2)
        if seg == "*":
            segs.append("*")
        elif re.fullmatch(r"-?\d+", seg):
            segs.append(int(seg))
        else:
            segs.append(seg)
    return segs


def _iter_refs(text: str, known: Optional[Set[str]] = None) -> Iterable[Tuple[re.Match, str, List[Any]]]:
    for m in _REF_RE.finditer(text):
        node = normalise_id(m.group("bnode") or m.group("node"))
        if known is not None and node not in known:
            continue
        yield m, node, _parse_path(m.group("bpath") if m.group("bnode") else m.group("path") or "")


def refs_in(value: Any, known: Optional[Set[str]] = None) -> Set[str]:
    """Node ids referenced anywhere in an argument value (only ids in `known` when given)."""
    out: Set[str] = set()
    if isinstance(value, str):
        out.update(node for _m, node, _p in _iter_refs(value, known))
    elif isinstance(value, dict):
        for v in value.values():
            out |= refs_in(v, known)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= refs_in(v, known)
    return out


# Field names planners guess for a result they cannot see ("$n1.results", "$n1.items.0.name"), mapped to what the
# tools actually return. Only used when the guessed field is missing, so exact paths always win.
_COLLECTION_GUESSES = {"results", "result", "items", "records", "hits", "data", "entries", "list", "rows", "outputs"}
_FIELD_ALIASES = {
    "name": ("pref_name", "drug_name", "query_name", "label", "symbol", "title"),
    "drug": ("pref_name", "drug_name", "query_name"),
    "id": ("chembl_id", "id", "accession", "nct_id", "pmid", "ensembl_id"),
    "chembl": ("chembl_id",),
    "smiles": ("canonical_smiles", "smiles"),
}


def _field_alias(value: Dict[str, Any], seg: str) -> Optional[str]:
    """The real field a guessed name most plausibly means, or None when nothing fits unambiguously."""
    low = seg.lower()
    if low in _COLLECTION_GUESSES:
        lists = [k for k, v in value.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if len(lists) == 1:
            return lists[0]
        return None
    for cand in _FIELD_ALIASES.get(low, ()):
        if cand in value and value[cand] not in (None, "", []):
            return cand
    return None


def _navigate(value: Any, segs: Sequence[Any], where: str) -> Any:
    for i, seg in enumerate(segs):
        if seg == "*":
            if not isinstance(value, list):
                raise RefError(f"{where}: '*' needs a list")
            out = []
            for item in value:
                try:
                    out.append(_navigate(item, segs[i + 1:], where))
                except RefError:
                    continue
            return out
        if isinstance(value, dict):
            if seg in value:
                value = value[seg]
            elif str(seg) in value:
                value = value[str(seg)]
            elif (alias := _field_alias(value, str(seg))) is not None:
                value = value[alias]
            else:
                raise RefError(f"{where}: no field '{seg}'")
        elif isinstance(value, list):
            if not isinstance(seg, int):
                raise RefError(f"{where}: '{seg}' is not a list index")
            try:
                value = value[seg]
            except IndexError as exc:
                raise RefError(f"{where}: index {seg} out of range ({len(value)} items)") from exc
        else:
            raise RefError(f"{where}: cannot read '{seg}' of a {type(value).__name__}")
    return value


def lookup(results: Dict[str, Any], node: str, segs: Sequence[Any]) -> Any:
    """The value a reference names: path into the node's ToolResult.data (or an explicit ToolResult field)."""
    if node not in results:
        raise RefError(f"${node} has no result")
    res = results[node]
    where = f"${node}"
    segs = list(segs)
    if segs and segs[0] in ("data", "canonical_ids") and hasattr(res, segs[0]):
        root = getattr(res, segs[0])
        segs = segs[1:]
    else:
        root = getattr(res, "data", res) if hasattr(res, "data") else res
    value = _navigate(root, segs, where)
    if value is None or value == "" or (isinstance(value, (list, dict)) and not value):
        # An empty list or string is no input either (e.g. no drugs were found): the step is skipped, not run
        # with [] and rejected by the tool's schema.
        raise RefError(f"{where}: value is empty")
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def resolve_args(value: Any, results: Dict[str, Any], known: Optional[Set[str]] = None) -> Any:
    """A deep copy of value with every reference to a node in `results` (or `known`) substituted."""
    known = set(known) if known is not None else set(results)
    if isinstance(value, str):
        matches = list(_iter_refs(value, known))
        if not matches:
            return value
        if len(matches) == 1 and matches[0][0].span() == (0, len(value)):
            _m, node, segs = matches[0]
            return copy.deepcopy(lookup(results, node, segs))
        out, last = [], 0
        for m, node, segs in matches:
            out.append(value[last:m.start()])
            out.append(_as_text(lookup(results, node, segs)))
            last = m.end()
        out.append(value[last:])
        return "".join(out)
    if isinstance(value, dict):
        return {k: resolve_args(v, results, known) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_args(v, results, known) for v in value]
    return copy.deepcopy(value)


# Parsing ------------------------------------------------------------------------------------------------------
def _first(d: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def parse_plan(value: Any, *, round_no: int = 0, taken_ids: Optional[Set[str]] = None) -> PlanGraph:
    """A PlanGraph from a planner reply ({"rationale", "nodes"|"tasks"|"plan"} or a bare list of nodes).

    Malformed entries are listed in `rejected`. Ids are normalised ("1" -> "n1"). In a replan, a new node
    whose id is already used by an earlier round (`taken_ids`) is renamed "r<round>_<id>"; references and
    depends_on entries naming that id keep pointing at the earlier round's node, which is the one the
    planner was shown.
    """
    taken = set(taken_ids or ())
    graph = PlanGraph()
    if isinstance(value, dict):
        graph.rationale = str(_first(value, "rationale", "reasoning", "thought") or "")[:1200]
        raw_nodes = _first(value, "nodes", "tasks", "plan", "steps")
    else:
        raw_nodes = value
    if raw_nodes is None:
        return graph
    if not isinstance(raw_nodes, list):
        graph.rejected.append({"node": "?", "reason": "the plan's nodes were not a list"})
        return graph

    staged: List[Tuple[PlanNode, str]] = []
    seen: Set[str] = set()
    for i, raw in enumerate(raw_nodes, 1):
        if not isinstance(raw, dict):
            graph.rejected.append({"node": f"#{i}", "reason": "node is not an object"})
            continue
        orig = normalise_id(_first(raw, "id", "node", "task_id") or f"n{i}")
        tool = str(_first(raw, "tool", "tool_name", "name", "function") or "").strip()
        args = _first(raw, "args", "arguments", "input", "parameters")
        deps = _first(raw, "depends_on", "deps", "dependencies", "dependency") or []
        if not _ID_RE.match(orig):
            graph.rejected.append({"node": orig[:40] or f"#{i}", "reason": "invalid node id"})
            continue
        if orig in seen:
            graph.rejected.append({"node": orig, "reason": "duplicate node id in the plan"})
            continue
        seen.add(orig)
        if not tool:
            graph.rejected.append({"node": orig, "reason": "node names no tool"})
            continue
        if args is None:
            args = {}
        if not isinstance(args, dict):
            graph.rejected.append({"node": orig, "reason": "args must be an object"})
            continue
        if isinstance(deps, (str, int)):
            deps = [deps]
        if not isinstance(deps, list):
            deps = []
        nid = orig
        if nid in taken:
            nid = f"r{round_no}_{orig}"
            k = 2
            while nid in taken or nid in seen:
                nid = f"r{round_no}_{orig}_{k}"
                k += 1
        taken.add(nid)
        node = PlanNode(
            id=nid,
            tool=tool,
            args=args,
            depends_on=[normalise_id(d) for d in deps if str(d).strip()],
            purpose=str(_first(raw, "purpose", "description", "why") or "")[:240],
            round=round_no,
        )
        staged.append((node, orig))

    for node, _orig in staged:
        graph.nodes.append(node)
    return graph


# Policy -------------------------------------------------------------------------------------------------------
# The pipeline source (ctx.decisions / ctx.source_allowlist key) each tool's evidence counts as.
TOOL_SOURCES: Dict[str, str] = {
    "lithoscribe_literature_synthesis": "lithoscribe",
    "lithoscribe_clinical_trials_search": "clinical_trials",
    "websurfer_search": "websurfer",
    "openfda_drug_safety": "openfda",
    "chembl_resolve_drugs": "chembl",
    "chembl_drug_profile": "chembl",
    "opentargets_disease_targets": "disease_targets",
    "primekg_investigate": "primekg",
    "primekg_query": "primekg",
    "lumicell_analyze_single_cell": "lumicell",
}


def tool_source(entry: Any) -> str:
    name = str(getattr(entry, "name", entry) or "")
    if name in TOOL_SOURCES:
        return TOOL_SOURCES[name]
    if "clinical_trial" in name:
        return "clinical_trials"
    if name.startswith("primekg"):
        return "primekg"
    if name.startswith("vivarix") or "calibrat" in name:
        return "vivarix"
    return str(getattr(entry, "domain", "") or name.split("_", 1)[0]).lower()


def is_simulation_tool(name: str) -> bool:
    return name.startswith("vivarix_") or "calibrat" in name or "simulat" in name


def is_expensive_tool(name: str) -> bool:
    """Tools that need the researcher's approval on an interactive run: simulations, scans, calibration, code
    execution and structure docking or generation."""
    n = name.lower()
    return (
        n.startswith("vivarix_")
        or "calibrat" in n
        or n.startswith("analysis_")
        or ("structure" in n and ("dock" in n or "generat" in n))
        or "diffdock" in n
        or "molmim" in n
    )


def single_cell_dataset_path(ctx: Any) -> Optional[str]:
    """The AnnData file Lumicell may read this turn: the researcher's attached .h5ad or a typed path that already
    passed is_allowed_typed_path. None when neither exists; a path written by the model is never used."""
    for att in getattr(ctx, "attachments", None) or []:
        if getattr(att, "kind", "") == "h5ad" and getattr(att, "analysis_path", None):
            return str(att.analysis_path)
    typed = getattr(ctx, "dataset_path", None)
    return typed if isinstance(typed, str) and typed.strip() else None


# Omics analyses a "no omics" question excludes: expression, association and dependency screens. Literature,
# chemistry and pathway lookups share the extension domain but are not omics analyses.
OMICS_TOOL_PREFIXES = ("gwas", "gtex", "depmap")


def excluded_by_question(entry: Any, ctx: Any) -> Optional[str]:
    """Why the question's own exclusions ("No FDA sweep, omics ... is needed") rule this tool out, or None."""
    excluded = getattr(ctx, "excluded_modules", None) or set()
    if not excluded:
        return None
    name = str(getattr(entry, "name", entry) or "")
    if "omics" in excluded and name.startswith(OMICS_TOOL_PREFIXES):
        return "omics analysis was excluded at your request"
    if "fda_sweep" in excluded and tool_source(entry) == "openfda":
        from ..drugintel.names import drug_mentions_for

        if not drug_mentions_for(ctx):
            return "the FDA candidate sweep was excluded at your request"
    return None


def policy_violation(entry: Any, ctx: Any) -> Optional[str]:
    """Why the turn's hard policy forbids this tool, or None."""
    name = str(getattr(entry, "name", entry) or "")
    if ctx is None:
        return None
    constraints = getattr(ctx, "simulation_constraints", None)
    if is_simulation_tool(name) and (
        getattr(ctx, "suppress_simulation", False) or bool(getattr(constraints, "simulation_forbidden", False))
    ):
        return "simulation was forbidden for this question"
    source = tool_source(entry)
    allow = getattr(ctx, "source_allowlist", None)
    if allow is not None and source not in allow:
        reason = getattr(ctx, "source_restriction_reason", "") or "restricted sources"
        return f"source '{source}' is outside this turn's restriction ({reason})"
    if source == "websurfer" and not getattr(ctx, "enable_web_search", True):
        return "web search is switched off"
    if source == "lumicell" and (getattr(ctx, "extras", {}) or {}).get("single_cell_forbidden"):
        return "single-cell analysis was excluded at your request"
    if source == "lumicell" and not single_cell_dataset_path(ctx):
        return "no single-cell dataset (.h5ad) is attached"
    excluded = excluded_by_question(entry, ctx)
    if excluded:
        return excluded
    decision = (getattr(ctx, "decisions", {}) or {}).get(source)
    if decision is not None and not getattr(decision, "run", True) and "switched off" in str(getattr(decision, "reason", "")):
        return f"source '{source}' was switched off by the researcher"
    return None


# Schema checks ------------------------------------------------------------------------------------------------
def schema_problem(entry: Any, args: Dict[str, Any], ref_fields: Set[str]) -> Optional[str]:
    """A validation problem with args against the tool's input schema, ignoring fields that hold references."""
    schema = getattr(entry, "input_schema", None)
    if schema is None:
        return None
    fields = getattr(schema, "model_fields", {}) or {}
    extra_ok = str((getattr(schema, "model_config", {}) or {}).get("extra", "")) == "allow"
    unknown = [k for k in args if k not in fields]
    if unknown and not extra_ok:
        return f"unknown argument(s) {', '.join(sorted(unknown)[:5])}; the tool takes {', '.join(fields) or 'none'}"
    plain = {k: v for k, v in args.items() if k not in ref_fields}
    try:
        schema.model_validate(plain)
    except ValidationError as exc:
        errs = [e for e in exc.errors() if not (e.get("loc") and str(e["loc"][0]) in ref_fields)]
        if errs:
            first = errs[0]
            loc = ".".join(str(x) for x in first.get("loc", ())) or "args"
            return f"argument {loc}: {first.get('msg', 'invalid')}"
    except Exception as exc:  # noqa: BLE001
        return f"arguments could not be checked: {exc}"
    return None


def validated_inputs(entry: Any, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The tool's validated kwargs (as the registry records them), or None when they do not validate."""
    schema = getattr(entry, "input_schema", None)
    if schema is None:
        return dict(args)
    try:
        return _json_safe(schema.model_validate(args).model_dump())
    except Exception:  # noqa: BLE001
        logger.debug("Arguments do not validate as given; trying the repaired ones", exc_info=True)
    # The registry repairs trivially fixable arguments before running the tool; the call it makes is this one.
    repaired = repair_arguments(schema, args)
    if repaired is None:
        return None
    try:
        return _json_safe(schema.model_validate(repaired[0]).model_dump())
    except Exception:  # noqa: BLE001
        logger.debug("Repaired arguments do not validate either", exc_info=True)
        return None


# Cycles -------------------------------------------------------------------------------------------------------
def topological_order(nodes: Sequence[PlanNode], external: Optional[Set[str]] = None) -> Tuple[List[str], Set[str]]:
    """(order, cyclic): Kahn's algorithm over in-plan dependencies; ids left over sit on or behind a cycle."""
    external = external or set()
    ids = {n.id for n in nodes}
    indeg = {n.id: 0 for n in nodes}
    children: Dict[str, List[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for d in n.depends_on:
            if d in ids and d not in external:
                indeg[n.id] += 1
                children[d].append(n.id)
    queue = [n.id for n in nodes if indeg[n.id] == 0]
    order: List[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    return order, ids - set(order)


def _cycle_members(nodes: Sequence[PlanNode], leftover: Set[str]) -> Set[str]:
    """The leftover ids that lie on a cycle (the rest only depend on one)."""
    deps = {n.id: [d for d in n.depends_on if d in leftover] for n in nodes if n.id in leftover}
    on_cycle: Set[str] = set()
    for start in leftover:
        stack, seen = list(deps.get(start, [])), set()
        while stack:
            cur = stack.pop()
            if cur == start:
                on_cycle.add(start)
                break
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(deps.get(cur, []))
    return on_cycle


# Validation ---------------------------------------------------------------------------------------------------
# Plan arguments that name drugs (shared with executor.run_known_drugs fallbacks).
DRUG_ARG_KEYS = {"names", "name", "drug", "drugs", "drug_name", "drug_names", "ingredient", "ingredients", "intervention"}


def _blank(value: Any) -> bool:
    if isinstance(value, str):
        return len(value.strip()) < 2
    if isinstance(value, (list, tuple)):
        return not any(isinstance(v, str) and len(v.strip()) >= 2 for v in value)
    return value is None


def validate_plan(
    graph: PlanGraph,
    registry: Any,
    ctx: Any = None,
    *,
    max_nodes: int = 12,
    prior_calls: Optional[Iterable[Tuple[str, Dict[str, Any]]]] = None,
    external_ids: Optional[Set[str]] = None,
) -> PlanGraph:
    """Drop every node that may not run (see the module docstring); keeps the rest in dependency order.

    prior_calls: (tool_name, validated inputs) of calls already made this turn (pipeline and earlier nodes).
    external_ids: ids of finished nodes from earlier rounds that this plan may depend on and reference.
    """
    external = set(external_ids or ())
    seen_calls: Set[Tuple[str, str]] = {(t, canonical(a)) for t, a in (prior_calls or [])}

    def all_known() -> Set[str]:
        return set(graph.ids()) | external

    # 1. Per-node checks: tool, policy, arguments, references, duplicates.
    for node in list(graph.nodes):
        entry = registry.get_entry(node.tool) if registry is not None else None
        if entry is None:
            graph.reject(node, f"tool '{node.tool}' is not registered")
            continue
        violation = policy_violation(entry, ctx)
        if violation:
            graph.reject(node, f"policy: {violation}")
            continue
        clash = sorted(RESERVED_ARGS & set(node.args))
        if clash:
            graph.reject(node, f"argument name(s) {', '.join(clash)} are reserved")
            continue
        known = all_known()
        referenced = refs_in(node.args, known)
        if node.id in referenced or node.id in node.depends_on:
            graph.reject(node, "node depends on itself")
            continue
        for r in sorted(referenced):
            if r not in node.depends_on:
                node.depends_on.append(r)  # LLMCompiler: a placeholder is a dependency
        unknown = [d for d in node.depends_on if d not in known]
        if unknown:
            graph.reject(node, f"depends on unknown node(s) {', '.join(unknown)}")
            continue
        ref_fields = {k for k, v in node.args.items() if refs_in(v, known)}
        # A drug argument the planner left blank ("", "x", []) is filled at run time with the drugs the run has
        # found (executor.run_known_drugs) rather than rejecting the step.
        for k in [k for k, v in node.args.items() if k.lower() in DRUG_ARG_KEYS and k not in ref_fields and _blank(v)]:
            node.args.pop(k)
            node.fill_from_run.append(k)
            ref_fields.add(k)
        problem = schema_problem(entry, node.args, ref_fields)
        if problem:
            # One conservative repair (limits clamped to their bounds, over-long lists cut, {"name": ..} objects
            # where a string is expected, unknown arguments dropped) before the node is rejected.
            fixed = repair_arguments(getattr(entry, "input_schema", None), node.args, drop_unknown=True,
                                     ignore_fields=ref_fields)
            if fixed is not None and schema_problem(entry, fixed[0], ref_fields) is None:
                node.args, node.adjusted = fixed[0], [*node.adjusted, *fixed[1]]
                problem = None
        if problem:
            graph.reject(node, f"schema: {problem}")
            continue
        if not ref_fields:
            inputs = validated_inputs(entry, node.args)
            sig = (node.tool, canonical(inputs if inputs is not None else node.args))
            if sig in seen_calls:
                graph.reject(node, "an identical call was already made this turn")
                continue
            seen_calls.add(sig)

    # 2. Cycles.
    order, leftover = topological_order(graph.nodes, external)
    if leftover:
        cyclic = _cycle_members(graph.nodes, leftover)
        for nid in sorted(cyclic):
            graph.reject(nid, "dependency cycle")

    # 3. Dependents of rejected nodes (repeat until stable).
    changed = True
    while changed:
        changed = False
        alive = all_known()
        for node in list(graph.nodes):
            missing = [d for d in node.depends_on if d not in alive]
            if missing:
                graph.reject(node, f"depends on rejected node(s) {', '.join(missing)}")
                changed = True

    # 4. Order and cap.
    order, _left = topological_order(graph.nodes, external)
    by_id = {n.id: n for n in graph.nodes}
    ordered = [by_id[i] for i in order if i in by_id]
    cap = max(0, int(max_nodes))
    for node in ordered[cap:]:
        graph.rejected.append({"node": node.id, "reason": f"plan node cap of {cap} reached"})
    graph.nodes = ordered[:cap]
    return graph


def drop_nodes(graph: PlanGraph, ids: Iterable[str], reason: str) -> List[str]:
    """Remove nodes (and, transitively, every node depending on them); returns the removed ids."""
    removed: List[str] = []
    targets = {i for i in ids if graph.get(i) is not None}
    for nid in sorted(targets):
        graph.reject(nid, reason)
        removed.append(nid)
    changed = bool(removed)
    while changed:
        changed = False
        gone = set(removed)
        for node in list(graph.nodes):
            hit = [d for d in node.depends_on if d in gone]
            if hit:
                graph.reject(node, f"depends on dropped node(s) {', '.join(hit)}")
                removed.append(node.id)
                changed = True
    return removed


__all__ = [
    "NODE_STATUSES",
    "drop_nodes",
    "PlanGraph",
    "PlanNode",
    "RefError",
    "TOOL_SOURCES",
    "canonical",
    "is_expensive_tool",
    "is_simulation_tool",
    "lookup",
    "normalise_id",
    "parse_plan",
    "policy_violation",
    "refs_in",
    "resolve_args",
    "schema_problem",
    "tool_source",
    "topological_order",
    "validate_plan",
    "validated_inputs",
]
