"""
Multi-agent topologies (feature 4): supervisor, swarm and debate over the specialists in specialists.py.

- SUPERVISOR (augment): the Magentic-One orchestrator loop (Fourney et al. 2024, arXiv:2411.04468). A task ledger
  (facts given, facts gathered, facts to look up, facts to derive, plan) and, at every step, a progress ledger
  answered by the model ([phase:supervisor]: is_done, is_looping, is_progressing, next_specialist, instruction,
  reason). A stall counter goes up on a loop or no progress; above 2 the supervisor reflects, rewrites the ledger
  and replans. Each delegated specialist runs a short ReAct loop ([phase:specialist]: thought, action call|finish,
  tool, args, summary) over its own tools only, at most 3 tool calls, every call through state.call_tool.
- SWARM (augment): AutoGen-style conversable agents (Wu et al., COLM 2024, arXiv:2308.08155) that hand control to
  each other ([phase:swarm]: action call|handoff|finish, target, note). Handoffs are budgeted and the same pair of
  specialists may hand off only once (no ping-pong).
- DEBATE (after_draft): multi-agent debate (Du et al., ICML 2024, arXiv:2305.14325). Two or three critic personas
  with MetaGPT-style SOP role prompts (Hong et al., ICLR 2024, arXiv:2308.00352) critique the draft against the
  evidence ([phase:debate]); in round two each sees the others' points ("Using the opinion of other agents as
  additional advice ..."). Following CRITIC (Gou et al., ICLR 2024, arXiv:2305.11738) and Chain-of-Verification
  (Dhuliawala et al., Findings ACL 2024, arXiv:2309.11495) a critique counts only if it names a claim and the
  evidence key behind it. The answer is rewritten only when a critic asks for a revision and a majority did not
  accept the draft.

Every topology writes state.artifacts["agent_dialogue"] = {"topology", "turns":[{"agent","action","tool","summary"}]}
(at most 40 turns) and emits one readable step per agent turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field, ValidationError

from ..tools.arg_repair import adjustment_text, repair_arguments
from .config import DEBATE, SUPERVISOR, SWARM
from .guardrails import SPOTLIGHT_RULE, neutralise, spotlight
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .specialists import (
    SPECS_BY_NAME,
    Specialist,
    ask_json,
    classify_tool,
    compact,
    evidence_digest,
    model_failing,
    query_entities,
    team_specialists,
    tool_allowed,
)
from .tool_retrieval import retrieve_tools

logger = logging.getLogger(__name__)

MAX_TURNS_RECORDED = 40
MAX_DELEGATIONS = 6
MAX_SPECIALIST_CALLS = 3
STALL_LIMIT = 2  # Magentic-One: replan when the stall counter exceeds 2
MAX_REPLANS = 2
SWARM_MAX_TURNS = 8
SWARM_MAX_HANDOFFS = 3
SWARM_MAX_TOOL_CALLS = 6
SWARM_MAX_ENSURE_CALLS = 5  # fixed calls that make sure each step the question asks for ran


# Model replies -------------------------------------------------------------------------------------------------
class ProgressLedger(BaseModel):
    is_done: bool = False
    is_looping: bool = False
    is_progressing: bool = True
    next_specialist: str = ""
    instruction: str = ""
    reason: str = ""


class SpecialistTurn(BaseModel):
    thought: str = ""
    action: str = "finish"
    tool: str = ""
    args: Optional[Dict[str, Any]] = None
    summary: str = ""


class SwarmTurn(BaseModel):
    action: str = "finish"
    target: str = ""
    tool: str = ""
    args: Optional[Dict[str, Any]] = None
    note: str = ""


class Critique(BaseModel):
    claim: str = ""
    issue: str = ""
    evidence_key: str = ""
    severity: str = "medium"


class DebateReply(BaseModel):
    critiques: List[Critique] = Field(default_factory=list)
    verdict: str = "accept"


# Shared pieces -------------------------------------------------------------------------------------------------
class Dialogue:
    """Transcript summary of one topology run, stored as the agent_dialogue artifact."""

    def __init__(self, state: Any, topology: str) -> None:
        self.state = state
        self.topology = topology
        existing = state.artifacts.get("agent_dialogue")
        prior = existing.get("turns") if isinstance(existing, dict) and existing.get("topology") == topology else None
        self.turns: List[Dict[str, Any]] = list(prior or [])
        self._n = 0

    def add(self, agent: str, action: str, tool: Optional[str] = None, summary: str = "") -> None:
        if len(self.turns) < MAX_TURNS_RECORDED:
            self.turns.append({"agent": agent, "action": action, "tool": tool, "summary": (summary or "")[:400]})
        self.save()

    def save(self) -> None:
        self.state.artifacts["agent_dialogue"] = {"topology": self.topology, "turns": list(self.turns)}

    def step_id(self, prefix: str) -> str:
        self._n += 1
        return f"agent_{prefix}_{self._n}"


def match_specialist(name: str, specialists: Dict[str, Specialist]) -> Optional[Specialist]:
    """A specialist by name or label ('literature', 'Literature specialist', 'target agent')."""
    key = re.sub(r"\b(specialist|agent)\b", "", (name or "").lower()).strip(" :-_")
    if key in specialists:
        return specialists[key]
    for sp in specialists.values():
        if key and (key == sp.label.lower() or key.startswith(sp.name)):
            return sp
    return None


def _signature(tool: str, args: Dict[str, Any]) -> str:
    return tool + json.dumps(args, sort_keys=True, default=str)


# Observation prefix for a repeated identical call: the earlier result is handed back, which is not a failure.
REUSED_PREFIX = "Reused the earlier identical"


def call_step_status(obs: str, executed: bool) -> str:
    """Step status for one specialist tool turn: a reused identical call is neutral ("skipped"), not an error."""
    if executed:
        return "done"
    return "skipped" if obs.startswith(REUSED_PREFIX) else "error"


async def run_specialist_tool(
    state: Any, sp: Specialist, tool: str, args: Optional[Dict[str, Any]], *, phase: str, purpose: str
) -> Tuple[str, Optional[str], bool]:
    """Validate and run one of a specialist's own tools. Returns (observation, result key, executed).

    Arguments failing the schema for a trivially fixable reason are repaired once (tools.arg_repair); a repeated
    identical call is not run again: its earlier result is returned (observation starting with REUSED_PREFIX)."""
    entry = sp.get(tool)
    if entry is None:
        return f"'{tool}' is not one of your tools ({', '.join(sp.tool_names())}).", None, False
    ok, why = tool_allowed(state.ctx, entry)
    if not ok:
        return f"{tool} is not allowed for this question: {why}.", None, False
    args = dict(args or {})
    adjusted: List[str] = []
    try:
        entry.input_schema.model_validate(args)
    except ValidationError as exc:
        repaired = repair_arguments(entry.input_schema, args)
        if repaired is None:
            errs = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors()[:4])
            return f"Invalid arguments for {tool}: {errs}. Check the tool's argument schema.", None, False
        args, adjusted = repaired
    note = f"; {adjustment_text(adjusted)}" if adjusted else ""
    signatures = state.bag.get("specialist_call_signatures")
    if not isinstance(signatures, dict):  # signature -> evidence key of the call that made it
        signatures = state.bag["specialist_call_signatures"] = {}
    sig = _signature(tool, args)
    if sig in signatures:
        earlier = signatures[sig]
        prev = state.tool_results.get(earlier) if earlier else None
        if prev is not None and getattr(prev, "success", False):
            return (f"{REUSED_PREFIX} {tool} call (evidence key {earlier}{note}); its result: "
                    + spotlight(compact(prev.data, 1500), earlier, 1800)), None, False
        why_prev = str(getattr(prev, "error", "") or "no result") if prev is not None else "no result"
        return (f"{REUSED_PREFIX} {tool} call (evidence key {earlier}{note}); it returned no evidence "
                f"({why_prev[:200]}), so it was not repeated."), None, False
    key = state.next_key(phase, tool)
    signatures[sig] = key
    res = await state.call_tool(tool, phase=phase, purpose=purpose[:200], key=key, **args)
    if getattr(res, "success", False):
        return f"{tool} succeeded (evidence key {key}{note}): " + spotlight(compact(res.data, 1500), key, 1800), key, True
    return f"{tool} failed{' (' + note[2:] + ')' if note else ''}: {str(getattr(res, 'error', '') or 'unknown error')[:300]}", key, True


def _cards_text(sp: Specialist) -> str:
    return json.dumps(sp.cards(), default=str, ensure_ascii=False)[:6000]


def _entities_line(state: Any) -> str:
    ents = query_entities(state.ctx)
    parts = [f"{k}: {', '.join(v)}" for k, v in ents.items() if v]
    return "; ".join(parts) or "none resolved"


SPECIALIST_SYSTEM = (
    "[phase:specialist] You are the {label} in a team of biomedical research agents. {role}\n"
    "You may use only these tools (exact names and argument schemas):\n{cards}\n"
    "Work in a short ReAct loop: think, then either call one tool or finish. At most {max_calls} tool calls. "
    "Never invent identifiers, model ids or results; if no tool fits, finish and say so. Reply with only JSON: "
    '{{"thought":"...","action":"call|finish","tool":"<tool name>","args":{{...}},"summary":"<what you found, '
    'with the evidence keys>"}}\n' + SPOTLIGHT_RULE
)


async def run_specialist(
    state: Any,
    sp: Specialist,
    instruction: str,
    *,
    phase: str,
    dialogue: Dialogue,
    context: str = "",
    max_calls: int = MAX_SPECIALIST_CALLS,
) -> Dict[str, Any]:
    """One delegated task: a bounded ReAct loop over the specialist's own tools."""
    system = SPECIALIST_SYSTEM.format(label=sp.label, role=sp.spec.role, cards=_cards_text(sp), max_calls=max_calls)
    observations: List[str] = []
    keys: List[str] = []
    calls = 0
    summary = ""
    for _ in range(max_calls + 2):  # tool calls plus room for one invalid call and the finish
        if state.budget.exhausted():
            summary = summary or "stopped: agent budget used up"
            break
        state.control.checkpoint()
        user = (
            f"Research question: {state.clean_query or state.query}\nEntities: {_entities_line(state)}\n"
            f"Instruction: {instruction}\n"
            + (f"Context:\n{context}\n" if context else "")
            + f"Tool calls left: {max_calls - calls}\n"
            + ("Observations so far:\n" + "\n".join(observations) if observations else "No tool called yet.")
        )
        turn = await ask_json(state, system, user, phase="specialist", schema=SpecialistTurn)
        if not isinstance(turn, SpecialistTurn):
            summary = summary or "no usable reply from the model"
            break
        if turn.action.strip().lower() != "call" or calls >= max_calls:
            summary = turn.summary or turn.thought or "finished"
            break
        obs, key, executed = await run_specialist_tool(
            state, sp, turn.tool.strip(), turn.args, phase=phase, purpose=f"{sp.label}: {instruction}"
        )
        observations.append(f"- {obs}")
        if executed:
            calls += 1
        if key:
            keys.append(key)
        dialogue.add(sp.label, "call", turn.tool.strip() or None, (turn.thought or obs)[:300])
        await state.step(
            dialogue.step_id(phase), f"{sp.label}: {turn.tool.strip() or 'no tool'}",
            re.sub(r"<[^>]+>", "", obs)[:300], agent=sp.label,
            status=call_step_status(obs, executed),
        )
    dialogue.add(sp.label, "finish", None, summary)
    return {"specialist": sp.name, "summary": summary[:600], "keys": keys, "calls": calls}


# Deterministic fallbacks ---------------------------------------------------------------------------------------
# When the model gives no usable reply, the topologies still do the work the question asks for: each specialist
# (or each step of the question, in the order the researcher named them) runs a fixed set of its own tools with
# arguments filled from the resolved entities. Every call still goes through run_specialist_tool (policy, schema
# validation, no repeated calls).
LEG_CUES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("target", re.compile(
        r"(?i:\btargets?\b|\bsynthetic[- ]lethal\w*|\bPPIs?\b|\bprotein[- ]protein\b|\binteract\w*|\bhubs?\b"
        r"|\bpartners?\b)|\bSL\b"
    )),
    ("chemistry", re.compile(r"(?i:\bChEMBL\b|\bcompounds?\b|\bsmall[- ]molecules?\b|\bPROTACs?\b|\bdegraders?\b)")),
    ("clinical", re.compile(r"(?i:\btrials?\b|\bopenFDA\b|\bsafety\b|\blabels?\b|\bFAERS\b)")),
)
# Specialists that carry each step, in preference order (the clinical step spans trials and openFDA).
LEG_SPECIALISTS: Dict[str, Tuple[str, ...]] = {
    "target": ("target", "omics"),
    "chemistry": ("chemistry",),
    "clinical": ("literature", "chemistry"),
}
_SL_RE = re.compile(r"(?i:\bsynthetic[- ]lethal\w*|\bdependenc\w*|\bessential\w*|\bCRISPR\b)|\bSL\b")
_PPI_RE = re.compile(r"(?i:\bPPIs?\b|\bprotein[- ]protein\b|\binteract\w*|\bhubs?\b|\bpartners?\b)")
_TRIAL_RE = re.compile(r"(?i:\btrials?\b|\bNCT\d{8}\b|clinicaltrials)")
_SAFETY_RE = re.compile(r"(?i:\bopenFDA\b|\bFAERS\b|\bsafety\b|\blabels?\b|\bwarnings?\b|\badverse\b|\btoxicit\w*|\bboxed\b)")
_DRUG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\- ]{2,40}$")
MAX_FALLBACK_SPECIALISTS = 3
MAX_FALLBACK_CALLS = 2  # per specialist or per question step


def question_legs(question: str) -> List[str]:
    """The research steps the question names (target, chemistry, clinical), in the order it first mentions them."""
    found: List[Tuple[int, str]] = []
    for leg, rx in LEG_CUES:
        m = rx.search(question or "")
        if m:
            found.append((m.start(), leg))
    return [leg for _pos, leg in sorted(found)]


def _named_trials(question: str) -> List[str]:
    """Trials the question names ("SOLO-1"), so the trials search lists and fetches them first."""
    from ..trials.trial_query import named_trials

    return named_trials(question)


def _found_drug_names(state: Any, limit: int = 4) -> List[str]:
    """Drugs the question names, then approved/bioactive compounds ChEMBL returned this turn (names only)."""
    names: List[str] = list(query_entities(state.ctx).get("drugs") or [])
    for res in list(state.tool_results.values()):
        if not getattr(res, "success", False) or not str(getattr(res, "tool_name", "")).startswith("chembl"):
            continue
        data = getattr(res, "data", None)
        if not isinstance(data, dict):
            continue
        for field in ("drugs", "target_approved_drugs", "target_bioactive_compounds"):
            for item in data.get(field) or []:
                name = item.get("pref_name") if isinstance(item, dict) else None
                if isinstance(name, str) and _DRUG_NAME_RE.match(name.strip()) and not name.upper().startswith("CHEMBL"):
                    names.append(name.strip())
    out: List[str] = []
    for n in names:
        if n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out[:limit]


def _tool_ran(state: Any, tool: str, mention: Optional[str] = None) -> bool:
    """Whether a tool already returned evidence this turn (pipeline or agent), optionally mentioning a name."""
    for res in list(state.tool_results.values()):
        if getattr(res, "tool_name", "") != tool or not getattr(res, "success", False):
            continue
        if mention is None or mention.lower() in compact(getattr(res, "data", None), 6000).lower():
            return True
    return False


def leg_calls(state: Any, leg: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Fixed (tool, args) calls for one research step, from the question's entities; empty args are dropped."""
    question = state.clean_query or state.query or ""
    ents = query_entities(state.ctx)
    genes, drugs, diseases = ents.get("genes") or [], ents.get("drugs") or [], ents.get("diseases") or []
    gene = genes[0] if genes else ""
    calls: List[Tuple[str, Dict[str, Any]]] = []
    if leg == "target":
        if gene and _SL_RE.search(question):
            calls.append(("depmap_gene_effect", {"gene": gene}))
        if gene and _PPI_RE.search(question):
            calls.append(("string_interaction_partners", {"gene": gene}))
        if gene:
            calls.append(("synapta_kegg_pathways", {"target_symbol": gene}))
        calls.append(("primekg_investigate", {"query": question[:300], "genes": genes[:3], "drugs": drugs[:3],
                                              "diseases": diseases[:2]}))
    elif leg == "chemistry":
        if gene:
            calls.append(("chembl_drug_profile", {"target_symbol": gene}))
        if drugs:
            calls.append(("chembl_resolve_drugs", {"names": drugs[:5]}))
    elif leg == "clinical":
        found = _found_drug_names(state)
        if _TRIAL_RE.search(question) or not _SAFETY_RE.search(question):
            calls.append(("lithoscribe_clinical_trials_search", {
                "condition": diseases[0] if diseases else None,
                "intervention": " OR ".join(found[:3]) or None,
                "term": None if found else (gene or None),
                "trial_names": _named_trials(question),
            }))
        if _SAFETY_RE.search(question):
            calls.extend(("openfda_drug_safety", {"ingredient": d}) for d in found[:2])
    elif leg == "literature":
        calls.append(("lithoscribe_literature_synthesis", {"query": question[:300]}))
    elif leg == "omics":
        if gene:
            if _SL_RE.search(question):
                calls.append(("depmap_gene_effect", {"gene": gene}))
            calls.append(("string_interaction_partners", {"gene": gene}))
            calls.append(("gtex_median_expression", {"gene": gene}))
    return [(tool, {k: v for k, v in args.items() if v not in (None, "", [], {})}) for tool, args in calls]


def specialist_calls(state: Any, sp: Specialist) -> List[Tuple[str, Dict[str, Any]]]:
    """Fixed calls a specialist makes when its own model loop gives nothing (only its own tools)."""
    question = state.clean_query or state.query or ""
    legs = {"literature": ["literature"], "target": ["target"], "chemistry": ["chemistry"], "omics": ["omics"]}.get(sp.name, [])
    if sp.name == "literature" and _TRIAL_RE.search(question):
        legs.append("clinical")
    if sp.name == "chemistry" and _SAFETY_RE.search(question):
        legs.append("clinical")
    out: List[Tuple[str, Dict[str, Any]]] = []
    for leg in legs:
        out.extend(c for c in leg_calls(state, leg) if sp.get(c[0]) is not None and c not in out)
    return out


async def run_fixed_calls(
    state: Any,
    specialists: Dict[str, Specialist],
    calls: List[Tuple[str, Dict[str, Any]]],
    *,
    phase: str,
    dialogue: Dialogue,
    purpose: str,
    limit: int = MAX_FALLBACK_CALLS,
) -> Tuple[int, List[str]]:
    """Run fixed calls through their owning specialist; returns (executed calls, result keys)."""
    executed = 0
    keys: List[str] = []
    for tool, args in calls:
        if executed >= limit or state.budget.exhausted():
            break
        owner = next((sp for sp in specialists.values() if sp.get(tool) is not None), None)
        if owner is None:
            continue
        state.control.checkpoint()
        obs, key, ran = await run_specialist_tool(state, owner, tool, args, phase=phase, purpose=f"{owner.label}: {purpose}")
        if not ran:
            continue
        executed += 1
        if key:
            keys.append(key)
        text = re.sub(r"<[^>]+>", "", obs)[:300]
        dialogue.add(owner.label, "call (fixed plan)", tool, text)
        await state.step(dialogue.step_id(phase), f"{owner.label}: {tool}", text, agent=owner.label,
                         status="done" if " succeeded " in obs else "error")
    return executed, keys


# Supervisor ----------------------------------------------------------------------------------------------------
SUPERVISOR_SYSTEM = (
    "[phase:supervisor] You are the Supervisor of a team of biomedical specialist agents (Magentic-One "
    "orchestrator). Read the task ledger and the delegation history, then answer the progress ledger: is the "
    "research request satisfied by the evidence gathered (is_done)? Are we repeating the same requests "
    "(is_looping)? Is the last step adding new evidence (is_progressing)? Which specialist should act next "
    "(next_specialist, one of the listed names) and with what concrete instruction (instruction)? Stop "
    "(is_done=true) as soon as the evidence suffices or no specialist can add anything. Reply with only JSON: "
    '{"is_done":false,"is_looping":false,"is_progressing":true,"next_specialist":"<name>","instruction":"...",'
    '"reason":"..."}\n' + SPOTLIGHT_RULE
)


def task_ledger(state: Any, specialists: Dict[str, Specialist]) -> Dict[str, Any]:
    """Magentic-One task ledger, built deterministically from the question and the evidence so far."""
    ctx = state.ctx
    given = [f"Question: {state.clean_query or state.query}", f"Entities: {_entities_line(state)}"]
    if getattr(ctx, "suppress_simulation", False):
        given.append("Constraint: the researcher asked for no simulation.")
    if getattr(ctx, "source_allowlist", None) is not None:
        given.append(f"Constraint: sources restricted ({getattr(ctx, 'source_restriction_reason', '') or 'allowlist'}).")
    if getattr(ctx, "biomodel_id", None):
        given.append(f"Model: {ctx.biomodel_id}")
    gathered = []
    for key, res in list(state.tool_results.items())[:20]:
        status = "returned evidence" if getattr(res, "success", False) else "failed"
        gathered.append(f"{key} ({getattr(res, 'tool_name', '')}): {status}")
    open_qs = [f"[{q.id}] {q.text}" for q in state.sub_questions if q.status in ("open", "partial")]
    ranked: List[str] = []
    try:
        allowed = [e.name for e in state.registry.list_tools() if not tool_allowed(ctx, e)[0]]
        for e in retrieve_tools(state.registry, state.clean_query or state.query, k=8, exclude=allowed):
            group = classify_tool(e)
            if group in specialists and group not in ranked:
                ranked.append(group)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Supervisor tool ranking failed: %s", exc)
    ranked += [n for n in specialists if n not in ranked]
    return {
        "facts_given": given,
        "facts_gathered": gathered,
        "to_look_up": open_qs or [f"Evidence the pipeline did not already gather for: {state.clean_query or state.query}"],
        "to_derive": ["An evidence-grounded answer to the question, with what remains unknown."],
        "plan": [f"{specialists[n].label}: {', '.join(specialists[n].tool_names()[:6])}" for n in ranked],
        "ranked": list(ranked),
        "replans": 0,
        "notes": [],
    }


def _ledger_text(ledger: Dict[str, Any]) -> str:
    out: List[str] = []
    for title, key in (("Facts given", "facts_given"), ("Evidence gathered", "facts_gathered"),
                       ("To look up", "to_look_up"), ("To derive", "to_derive"), ("Plan", "plan"),
                       ("Reflection", "notes")):
        rows = ledger.get(key) or []
        if rows:
            out.append(f"{title}:\n" + "\n".join(f"- {r}" for r in rows))
    return "\n".join(out)


class SupervisorPhase(AgentPhase):
    key = "supervisor"
    stage = "augment"
    order = 10
    frameworks = (SUPERVISOR,)
    needs_llm = True

    def applies(self, state: Any) -> bool:
        return getattr(state.ctx, "scope", "research") == "research"

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        specialists = team_specialists(state)
        if not specialists:
            return None
        dialogue = Dialogue(state, "supervisor")
        ledger = task_ledger(state, specialists)
        team = ", ".join(sp.label for sp in specialists.values())
        await state.step("agent_supervisor", "Supervisor: planning the team's work", f"Team: {team}",
                         status="running", agent="Supervisor")
        dialogue.add("Supervisor", "task ledger", None, f"team: {team}")
        history: List[Dict[str, Any]] = []
        stall = replans = delegations = bad = 0
        finished = False  # the supervisor itself said the request is satisfied
        for _ in range(MAX_DELEGATIONS + MAX_REPLANS + 4):
            if delegations >= MAX_DELEGATIONS or state.budget.exhausted():
                break
            state.control.checkpoint()
            reply = await ask_json(state, SUPERVISOR_SYSTEM, self._prompt(state, ledger, specialists, history),
                                   phase=self.key, schema=ProgressLedger)
            if not isinstance(reply, ProgressLedger) or not (
                reply.is_done or reply.next_specialist.strip() or reply.instruction.strip()
            ):
                bad += 1
                # A failed call (timeout, provider error) is not retried: the next one would fail the same way.
                if bad >= 2 or model_failing(state, self.key):
                    break
                continue
            if reply.is_done:
                finished = True
                dialogue.add("Supervisor", "finish", None, reply.reason or "request satisfied")
                break
            if reply.is_looping or not reply.is_progressing:
                stall += 1
            else:
                stall = max(0, stall - 1)
            sp = match_specialist(reply.next_specialist, specialists)
            if sp is None:
                stall += 1
                history.append({"specialist": reply.next_specialist[:60], "instruction": reply.instruction[:300],
                                "summary": "not an available specialist", "calls": 0})
            if stall > STALL_LIMIT:
                replans += 1
                if replans > MAX_REPLANS:
                    dialogue.add("Supervisor", "stop", None, "still stalled after replanning")
                    break
                self._replan(ledger, history, replans)
                stall = 0
                dialogue.add("Supervisor", "replan", None, ledger["notes"][-1])
                await state.step(dialogue.step_id("supervisor"), "Supervisor: replanning", ledger["notes"][-1], agent="Supervisor")
                continue
            if sp is None:
                continue
            delegations += 1
            instruction = neutralise(reply.instruction.strip() or f"Find evidence for: {state.clean_query or state.query}")[:500]
            dialogue.add("Supervisor", "delegate", None, f"{sp.label}: {instruction}")
            await state.step(dialogue.step_id("supervisor"), f"Supervisor → {sp.label}", instruction, agent="Supervisor")
            report = await run_specialist(
                state, sp, instruction, phase=self.key, dialogue=dialogue,
                context=self._context(history),
            )
            history.append({"specialist": sp.name, "instruction": instruction, **report})
        fallback = False
        if delegations == 0 and not finished and not state.budget.exhausted():
            # The model never delegated (no usable reply): dispatch the ranked plan's top specialists directly.
            fallback = True
            delegations += await self._fallback(state, ledger, specialists, dialogue, history)
        n_calls = sum(int(h.get("calls") or 0) for h in history)
        state.bag["supervisor"] = {"ledger": ledger, "history": history[-12:], "replans": replans, "fallback": fallback}
        dialogue.save()
        if fallback:
            state.mark_phase(
                self.key, "fallback",
                "the supervisor model gave no usable reply, so the top specialists of the fixed plan were dispatched "
                f"({n_calls} tool call(s)).",
            )
        await state.step(
            "agent_supervisor", "Supervisor: team finished" + (" (fixed plan)" if fallback else ""),
            f"{delegations} delegation(s), {n_calls} tool call(s), {replans} replan(s)."
            + (" The supervisor model gave no usable reply, so the ranked plan was followed." if fallback else ""),
            agent="Supervisor",
        )
        return None

    async def _fallback(
        self, state: Any, ledger: Dict[str, Any], specialists: Dict[str, Specialist], dialogue: Dialogue,
        history: List[Dict[str, Any]],
    ) -> int:
        """Delegate to the top specialists of the ranked plan (question steps first). A specialist whose own model
        loop gives nothing (or when the model is failing) runs its fixed tool calls instead."""
        question = state.clean_query or state.query or ""
        order: List[str] = []
        for leg in question_legs(question):
            order += [n for n in LEG_SPECIALISTS.get(leg, ()) if n in specialists and n not in order][:1]
        order += [n for n in ledger.get("ranked") or [] if n in specialists and n not in order]
        order += [n for n in specialists if n not in order]
        order = [n for n in order if n != "simulation"][:MAX_FALLBACK_SPECIALISTS]
        dialogue.add("Supervisor", "fallback plan", None, "no usable supervisor reply; dispatching " + ", ".join(
            specialists[n].label for n in order))
        failing = model_failing(state, self.key)
        delegated = 0
        for name in order:
            if state.budget.exhausted():
                break
            sp = specialists[name]
            instruction = f"Find evidence for: {question}"[:500]
            delegated += 1
            dialogue.add("Supervisor", "delegate", None, f"{sp.label}: {instruction} (fixed plan)")
            await state.step(dialogue.step_id("supervisor"), f"Supervisor → {sp.label} (fixed plan)", instruction,
                             agent="Supervisor")
            report: Dict[str, Any] = {"specialist": sp.name, "summary": "", "keys": [], "calls": 0}
            if not failing:
                report = await run_specialist(state, sp, instruction, phase=self.key, dialogue=dialogue,
                                              context=self._context(history))
                failing = model_failing(state, "specialist")
            if not report.get("calls"):
                n, keys = await run_fixed_calls(
                    state, specialists, specialist_calls(state, sp), phase=self.key, dialogue=dialogue,
                    purpose=f"fixed plan for {question[:160]}",
                )
                if n:
                    report = {**report, "calls": n, "keys": list(report.get("keys") or []) + keys,
                              "summary": report.get("summary") or f"{n} fixed tool call(s) ran"}
            history.append({"instruction": instruction, **report, "specialist": sp.name})
        return delegated

    @staticmethod
    def _context(history: List[Dict[str, Any]]) -> str:
        notes = [f"{h['specialist']}: {h.get('summary', '')}" for h in history[-4:] if h.get("summary")]
        return spotlight("\n".join(notes), "earlier specialist reports", 2000) if notes else ""

    @staticmethod
    def _prompt(state: Any, ledger: Dict[str, Any], specialists: Dict[str, Specialist], history: List[Dict[str, Any]]) -> str:
        team = "\n".join(
            f"- {sp.name} ({sp.label}): {sp.spec.role.split('.')[0]}. Tools: {', '.join(sp.tool_names()[:8])}"
            for sp in specialists.values()
        )
        done = "\n".join(
            f"{i}. {h['specialist']} <- {h['instruction']} => {h.get('calls', 0)} tool call(s), result keys "
            f"{', '.join(h.get('keys') or []) or 'none'}" for i, h in enumerate(history, 1)
        ) or "none yet"
        reports = "\n".join(f"{h['specialist']}: {h.get('summary', '')}" for h in history[-4:] if h.get("summary"))
        return (
            f"Task ledger:\n{_ledger_text(ledger)}\n\nSpecialists:\n{team}\n\nDelegation history:\n{done}\n"
            + (f"\nLatest specialist reports:\n{spotlight(reports, 'specialist reports', 3000)}\n" if reports else "")
            + f"\nDelegations left: {MAX_DELEGATIONS - len([h for h in history if h.get('specialist') in specialists])}"
        )

    @staticmethod
    def _replan(ledger: Dict[str, Any], history: List[Dict[str, Any]], n: int) -> None:
        tried = sorted({str(h.get("specialist")) for h in history})
        recent = [h.get("instruction", "")[:120] for h in history[-3:]]
        ledger["replans"] = n
        ledger["notes"].append(
            f"Replan {n}: the last steps made no progress. Already tried: {', '.join(tried) or 'nothing'}. "
            f"Do not repeat: {' | '.join(r for r in recent if r) or '-'}. Choose a different specialist or a narrower "
            "instruction, or finish if nothing more can be found."
        )


# Swarm ---------------------------------------------------------------------------------------------------------
SWARM_SYSTEM = (
    "[phase:swarm] You are the {label} in a swarm of biomedical specialist agents that hand control to each other. "
    "{role}\nYour tools (exact names and argument schemas):\n{cards}\nOther specialists you can hand off to:\n{others}\n"
    "Each turn do exactly one thing: call one of your tools, hand off to another specialist with a note saying what "
    "they should find, or finish when the evidence for the question is gathered. Never invent identifiers. Reply "
    'with only JSON: {{"action":"call|handoff|finish","target":"<tool name for call, specialist name for '
    'handoff>","args":{{...}},"note":"..."}}\n' + SPOTLIGHT_RULE
)


class SwarmPhase(AgentPhase):
    key = "swarm"
    stage = "augment"
    order = 10
    frameworks = (SWARM,)
    needs_llm = True

    def applies(self, state: Any) -> bool:
        return getattr(state.ctx, "scope", "research") == "research"

    @staticmethod
    def _start(state: Any, specialists: Dict[str, Specialist]) -> Specialist:
        """The specialist for the first step the question names (target, then chemistry, then clinical, in the
        order the researcher wrote them); otherwise the owner of the best-matching tool."""
        for leg in question_legs(state.clean_query or state.query or ""):
            for name in LEG_SPECIALISTS.get(leg, ()):
                if name in specialists:
                    return specialists[name]
        try:
            excluded = [e.name for e in state.registry.list_tools() if not tool_allowed(state.ctx, e)[0]]
            for e in retrieve_tools(state.registry, state.clean_query or state.query, k=4, exclude=excluded):
                group = classify_tool(e)
                if group in specialists:
                    return specialists[group]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Swarm start selection failed: %s", exc)
        return specialists.get("literature") or next(iter(specialists.values()))

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        specialists = team_specialists(state)
        if not specialists:
            return None
        dialogue = Dialogue(state, "swarm")
        active = self._start(state, specialists)
        await state.step("agent_swarm", "Swarm: specialists working on the question", f"Starting with the {active.label}",
                         status="running", agent=active.label)
        dialogue.add(active.label, "start", None, "selected for the question")
        used_pairs: Set[frozenset] = set()
        handoffs = total_calls = calls_here = bad = 0
        observations: List[str] = []
        fixed_chain = False  # the model stopped giving usable replies: follow the question's steps instead
        for _ in range(SWARM_MAX_TURNS):
            if state.budget.exhausted():
                break
            state.control.checkpoint()
            turn = await ask_json(state, self._system(active, specialists), self._prompt(state, observations, dialogue,
                                  handoffs, used_pairs, active), phase=self.key, schema=SwarmTurn)
            if not isinstance(turn, SwarmTurn):
                bad += 1
                dialogue.add(active.label, "no usable reply", None, "retrying once" if bad < 2 else "following the fixed handoff chain")
                # One retry for an unparseable reply; a failed call (timeout, provider error) is not retried.
                if bad < 2 and not model_failing(state, self.key):
                    observations.append("- Your previous reply could not be used. Reply with only the JSON object.")
                    continue
                fixed_chain = True
                break
            action = turn.action.strip().lower()
            note = neutralise(turn.note or "")[:400]
            if action == "handoff":
                target = match_specialist(turn.target, specialists)
                refusal = self._refuse(active, target, handoffs, used_pairs)
                if refusal or target is None:
                    observations.append(f"- Handoff to '{turn.target[:60]}' refused: {refusal}. Call one of your tools or finish.")
                    dialogue.add(active.label, "handoff refused", None, refusal)
                    continue
                used_pairs.add(frozenset((active.name, target.name)))
                handoffs += 1
                dialogue.add(active.label, "handoff", None, f"to {target.label}: {note}")
                await state.step(dialogue.step_id("swarm"), f"{active.label} → {target.label}", note or "handoff", agent=active.label)
                observations = [f"- Handed over by the {active.label}: {spotlight(note, 'handoff note', 400)}"]
                active, calls_here = target, 0
                continue
            if action == "call":
                tool = (turn.tool or turn.target).strip()
                if calls_here >= MAX_SPECIALIST_CALLS or total_calls >= SWARM_MAX_TOOL_CALLS:
                    observations.append("- Tool-call limit reached: hand off or finish.")
                    dialogue.add(active.label, "call refused", tool or None, "tool-call limit reached")
                    continue
                obs, _key, executed = await run_specialist_tool(
                    state, active, tool, turn.args, phase=self.key, purpose=f"{active.label}: {note or tool}"
                )
                observations.append(f"- {obs}")
                if executed:
                    calls_here += 1
                    total_calls += 1
                dialogue.add(active.label, "call", tool or None, note or re.sub(r"<[^>]+>", "", obs)[:300])
                await state.step(dialogue.step_id("swarm"), f"{active.label}: {tool or 'no tool'}",
                                 re.sub(r"<[^>]+>", "", obs)[:300], agent=active.label,
                                 status=call_step_status(obs, executed))
                continue
            dialogue.add(active.label, "finish", None, note or "finished")
            break
        # Every step the question asked for runs its key tool before the swarm ends (after a model failure, in the
        # order the question names the steps: the deterministic handoff chain).
        legs = question_legs(state.clean_query or state.query or "")
        if fixed_chain and not legs:
            legs = [{"target": "target", "chemistry": "chemistry", "omics": "omics"}.get(active.name, "literature")]
        ensured, active = await self._ensure_legs(state, specialists, dialogue, legs, active, chain=fixed_chain)
        total_calls += ensured
        state.bag["swarm"] = {"handoffs": handoffs, "tool_calls": total_calls, "final": active.name,
                              "fixed_chain": fixed_chain, "legs": legs, "ensured_calls": ensured}
        dialogue.save()
        if fixed_chain:
            state.mark_phase(self.key, "fallback",
                             "the swarm model gave no usable reply, so the specialists followed the question's steps "
                             f"in order ({ensured} tool call(s)).")
        await state.step("agent_swarm", "Swarm: specialists finished" + (" (fixed handoff chain)" if fixed_chain else ""),
                         f"{handoffs} handoff(s), {total_calls} tool call(s); last agent: {active.label}.", agent=active.label)
        return None

    async def _ensure_legs(
        self, state: Any, specialists: Dict[str, Specialist], dialogue: Dialogue, legs: List[str], active: Specialist,
        *, chain: bool,
    ) -> Tuple[int, Specialist]:
        """Run each asked step's fixed calls whose tool has not returned evidence yet; returns (calls, last agent)."""
        total = 0
        for leg in legs:
            if state.budget.exhausted() or total >= SWARM_MAX_ENSURE_CALLS:
                break
            calls = [c for c in leg_calls(state, leg) if any(sp.get(c[0]) is not None for sp in specialists.values())]
            if leg == "clinical":  # trials and each drug's openFDA lookup are separate asks
                pending = [c for c in calls if not _tool_ran(state, c[0], str(c[1].get("ingredient") or "") or None)]
            else:
                swarm_tools = {t.get("tool") for t in state.trace if t.get("phase") == self.key}
                covered = bool(calls) and (_tool_ran(state, calls[0][0]) or any(c[0] in swarm_tools for c in calls))
                pending = [] if covered else calls
            if not pending:
                continue
            owner = next((sp for sp in specialists.values() if sp.get(pending[0][0]) is not None), active)
            if owner.name != active.name:
                dialogue.add(active.label, "handoff (fixed chain)" if chain else "handoff (asked step)", None,
                             f"to {owner.label}: the question asks for the {leg} step")
                await state.step(dialogue.step_id("swarm"), f"{active.label} → {owner.label}",
                                 f"The question asks for the {leg} step.", agent=active.label)
                active = owner
            n, _keys = await run_fixed_calls(
                state, specialists, pending, phase=self.key, dialogue=dialogue,
                purpose=f"{leg} step asked in the question",
                limit=min(MAX_FALLBACK_CALLS, SWARM_MAX_ENSURE_CALLS - total),
            )
            total += n
        return total, active

    @staticmethod
    def _refuse(active: Specialist, target: Optional[Specialist], handoffs: int, used_pairs: Set[frozenset]) -> str:
        if target is None:
            return "no such specialist is available for this question"
        if target.name == active.name:
            return "you cannot hand off to yourself"
        if handoffs >= SWARM_MAX_HANDOFFS:
            return f"the handoff budget ({SWARM_MAX_HANDOFFS}) is used up"
        if frozenset((active.name, target.name)) in used_pairs:
            return f"you and the {target.label} already handed off to each other (no ping-pong)"
        return ""

    @staticmethod
    def _system(active: Specialist, specialists: Dict[str, Specialist]) -> str:
        others = "\n".join(
            f"- {sp.name}: {sp.spec.role.split('.')[0]}" for sp in specialists.values() if sp.name != active.name
        ) or "- none"
        return SWARM_SYSTEM.format(label=active.label, role=active.spec.role, cards=_cards_text(active), others=others)

    @staticmethod
    def _prompt(state: Any, observations: List[str], dialogue: Dialogue, handoffs: int,
                used_pairs: Set[frozenset], active: Specialist) -> str:
        blocked = sorted(next(iter(p - {active.name})) for p in used_pairs if active.name in p and len(p) == 2)
        recent = "\n".join(f"- {t['agent']}: {t['action']}" + (f" {t['tool']}" if t.get("tool") else "")
                           for t in dialogue.turns[-6:])
        return (
            f"Research question: {state.clean_query or state.query}\nEntities: {_entities_line(state)}\n"
            f"Evidence already gathered: {', '.join(list(state.tool_results)[:20]) or 'none'}\n"
            f"Handoffs left: {SWARM_MAX_HANDOFFS - handoffs}"
            + (f"; you may not hand off to: {', '.join(blocked)}" if blocked else "")
            + f"\nRecent swarm turns:\n{recent or '- none'}\n"
            + ("Your observations:\n" + "\n".join(observations) if observations else "No tool called yet.")
        )


# Debate --------------------------------------------------------------------------------------------------------
PERSONAS: Tuple[Tuple[str, str], ...] = (
    ("methodologist", "Judge study design and strength of evidence: model system (in vitro, animal, human), sample "
     "size, controls, statistics; flag claims generalised beyond what the cited study type can show."),
    ("clinical skeptic", "Judge clinical relevance: whether human or trial data exist, trial phase, effect sizes, "
     "safety; flag preclinical findings stated as clinical fact and benefits stated without trial evidence."),
    ("mechanistic biologist", "Judge mechanism: pathway logic, target-disease links, direction of effects and "
     "alternative mechanisms; flag mechanistic leaps the evidence does not show."),
)

DEBATE_SYSTEM = (
    "[phase:debate] You are the {persona}, one of several critic agents reviewing a draft biomedical answer in a "
    "multi-agent debate. Role: {focus}\nStandard operating procedure:\n"
    "1. List the draft's claims that fall within your role.\n"
    "2. For each, find the evidence key ([PMID:...], [DOI:...] or a result key such as [synapta]) in the gathered "
    "evidence that supports or contradicts it.\n"
    "3. Report a critique only when you can name the claim and the evidence key behind your objection; use "
    'evidence_key "none" for a claim that no evidence supports.\n'
    "4. Severity: high = contradicted by the evidence or wrong; medium = overstated or missing a caveat; low = minor.\n"
    '5. Verdict "revise" if a high or medium critique stands, otherwise "accept".\n'
    'Reply with only JSON: {{"critiques":[{{"claim":"...","issue":"...","evidence_key":"...",'
    '"severity":"high|medium|low"}}],"verdict":"accept|revise"}}\n' + SPOTLIGHT_RULE
)

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _norm_key(key: str) -> str:
    k = re.sub(r"\s+", "", (key or "").strip().strip("[]"))
    m = re.match(r"(?i)^(pmid|doi):?(.+)$", k)
    if m:
        return f"{m.group(1).upper()}:{m.group(2).lower() if m.group(1).lower() == 'doi' else m.group(2)}"
    return k


class DebatePhase(AgentPhase):
    key = "debate"
    stage = "after_draft"
    order = 20
    frameworks = None  # the debate framework, or any run Auto escalated (router.EscalationPhase)
    needs_llm = True

    def applies(self, state: Any) -> bool:
        return (
            (state.framework == DEBATE or bool(state.bag.get("escalate_debate")))
            # "No ... debate is needed" in the question wins over the framework choice.
            and "debate" not in (getattr(state.ctx, "excluded_modules", None) or set())
            and getattr(state.ctx, "scope", "research") == "research"
            and bool((state.draft or "").strip())
            and not state.bag.get("debate_done")
        )

    @staticmethod
    def _calls_left(state: Any) -> int:
        """Optional model calls left: the run's calls minus the finalization reserve (first draft + revision)."""
        b = state.budget
        return b.max_llm_calls - getattr(b, "final_reserve_calls", 0) - b.llm_calls

    @staticmethod
    def _critics_that_fit(state: Any, n: int, prompt_chars: int) -> int:
        """How many of n parallel critics fit in the optional token budget, each with its prompt and reply reserved
        up front (they run concurrently, so they must fit together)."""
        from .budget import estimate_tokens
        from .llm import output_reserve_tokens

        left = getattr(state.budget, "optional_remaining_tokens", state.budget.remaining_tokens)()
        per_call = max(1, estimate_tokens(prompt_chars)) + output_reserve_tokens()
        return max(0, min(n, left // per_call)) if per_call else n

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        state.bag["debate_done"] = True
        left = self._calls_left(state)
        n = 3 if left >= 8 else 2
        if left < n + 1:  # keep one model call for the rewrite
            state.mark_phase(self.key, "budget", f"only {left} model call(s) left in the agent budget")
            return None
        dialogue = Dialogue(state, "debate")
        digest, keys = evidence_digest(state)
        valid_keys = {_norm_key(k) for k in [*keys, *state.tool_results.keys()]}
        base = (
            f"Question: {state.clean_query or state.query}\n\nGathered evidence:\n{digest}\n\n"
            f"Draft answer:\n{spotlight(state.draft, 'draft answer', 9000)}"
        )
        system_chars = len(DEBATE_SYSTEM) + 200
        n = self._critics_that_fit(state, n, len(base) + system_chars)
        if n < 2:
            state.mark_phase(
                self.key, "budget", "the token budget left before the finalization reserve does not fit two critics"
            )
            return None
        personas = PERSONAS[:n]
        await state.step("agent_debate", "Debate: critics reviewing the draft",
                         ", ".join(p for p, _ in personas), status="running", agent="Debate moderator")

        async def critic(persona: str, focus: str, user: str) -> Optional[DebateReply]:
            reply = await ask_json(state, DEBATE_SYSTEM.format(persona=persona, focus=focus), user,
                                   phase=self.key, schema=DebateReply)
            return reply if isinstance(reply, DebateReply) else None

        rounds: List[Dict[str, Optional[DebateReply]]] = []
        first = await asyncio.gather(*(critic(p, f, base) for p, f in personas))
        rounds.append(dict(zip((p for p, _ in personas), first, strict=True)))
        await self._record(state, dialogue, rounds[-1], 1)

        answered = {p: r for p, r in rounds[-1].items() if r is not None}
        left = self._calls_left(state)
        second_fits = self._critics_that_fit(state, len(answered), len(base) + system_chars + 4000) >= len(answered)
        if len(answered) >= 2 and left >= len(answered) + 1 and second_fits and not state.budget.optional_exhausted():
            second = await asyncio.gather(*(
                critic(p, f, self._round_two(base, p, answered)) for p, f in personas if p in answered
            ))
            rounds.append(dict(zip((p for p, _ in personas if p in answered), second, strict=True)))
            await self._record(state, dialogue, rounds[-1], 2)

        final: Dict[str, DebateReply] = {}
        for rnd in rounds:
            final.update({p: r for p, r in rnd.items() if r is not None})
        revise = [p for p, r in final.items() if r.verdict.strip().lower() == "revise"]
        accept = [p for p, r in final.items() if r.verdict.strip().lower() != "revise"]
        critiques, dropped = self._valid_critiques(final, valid_keys)
        majority_accept = len(accept) * 2 > len(final)
        state.bag["debate"] = {
            "critics": [p for p, _ in personas], "rounds": len(rounds), "revise": revise, "accept": accept,
            "valid_critiques": len(critiques), "dropped_critiques": dropped,
        }
        dialogue.save()
        if not final:
            failed = model_failing(state, self.key)
            state.mark_phase(
                self.key, "llm_unavailable" if failed else "error",
                "the critics' model calls failed" if failed else "no critic gave a usable reply",
            )
            await state.step(
                "agent_debate", "Debate did not run",
                ("The critics' model calls failed" if failed else "No critic gave a usable reply")
                + "; the answer uses the standard pipeline.",
                status="error", agent="Debate moderator",
            )
            return None
        if not revise or majority_accept or not critiques:
            why = "the critics accepted the draft" if not revise or majority_accept else "no critique named a claim and its evidence"
            dialogue.add("Debate moderator", "accept", None, why)
            await state.step("agent_debate", "Debate: draft accepted",
                             f"{len(accept)} of {len(final)} critics accepted; {why}.", agent="Debate moderator")
            return None
        feedback = self._feedback(critiques)
        dialogue.add("Debate moderator", "revise", None, f"{len(feedback)} consensus point(s)")
        await state.step("agent_debate", "Debate: revision requested",
                         f"{len(revise)} of {len(final)} critics asked for a revision; {len(feedback)} point(s) sent back.",
                         agent="Debate moderator")
        return PhaseOutcome(resynthesize=True, feedback=feedback)

    @staticmethod
    def _round_two(base: str, persona: str, answered: Dict[str, DebateReply]) -> str:
        others = []
        for p, r in answered.items():
            if p == persona:
                continue
            points = "; ".join(f"{c.claim} -> {c.issue} [{c.evidence_key}]" for c in r.critiques[:6]) or "no critiques"
            others.append(f"{p} (verdict {r.verdict}): {points}")
        mine = answered.get(persona)
        own = "; ".join(f"{c.claim} -> {c.issue} [{c.evidence_key}]" for c in (mine.critiques if mine else [])[:6])
        return (
            f"{base}\n\nYour previous critique: {own or 'none'} (verdict {mine.verdict if mine else 'none'})\n\n"
            "These are the critiques from other agents:\n"
            + spotlight("\n".join(others), "other critics", 4000)
            + "\n\nUsing the opinion of other agents as additional advice, can you give an updated critique? Keep "
            "only points you can tie to a claim and an evidence key, drop points the evidence answers, and give "
            "your updated verdict."
        )

    @staticmethod
    def _valid_critiques(final: Dict[str, DebateReply], valid_keys: Set[str]) -> Tuple[List[Dict[str, Any]], int]:
        out: List[Dict[str, Any]] = []
        dropped = 0
        for persona, reply in final.items():
            for c in reply.critiques:
                claim, issue = neutralise(c.claim.strip())[:300], neutralise(c.issue.strip())[:300]
                key = _norm_key(c.evidence_key)
                if not claim or not issue or not key or (key.lower() != "none" and key not in valid_keys):
                    dropped += 1
                    continue
                severity = c.severity.strip().lower() if c.severity.strip().lower() in _SEVERITY_RANK else "medium"
                out.append({"persona": persona, "claim": claim, "issue": issue, "evidence_key": key, "severity": severity})
        return out, dropped

    @staticmethod
    def _feedback(critiques: List[Dict[str, Any]], limit: int = 6) -> List[str]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for c in critiques:
            norm = re.sub(r"[^a-z0-9 ]", "", c["claim"].lower())[:80]
            g = grouped.setdefault(norm, {**c, "personas": []})
            if c["persona"] not in g["personas"]:
                g["personas"].append(c["persona"])
            if _SEVERITY_RANK[c["severity"]] < _SEVERITY_RANK[g["severity"]]:
                g.update(severity=c["severity"], issue=c["issue"], evidence_key=c["evidence_key"])
        ranked = sorted(grouped.values(), key=lambda g: (-len(g["personas"]), _SEVERITY_RANK[g["severity"]]))
        return [
            f"[{g['severity']}] Claim: \"{g['claim']}\" - {g['issue']} (evidence: {g['evidence_key']}; raised by "
            f"{', '.join(g['personas'])})"
            for g in ranked[:limit]
        ]

    @staticmethod
    async def _record(state: Any, dialogue: Dialogue, rnd: Dict[str, Optional[DebateReply]], n: int) -> None:
        for persona, reply in rnd.items():
            agent = f"Critic: {persona}"
            if reply is None:
                dialogue.add(agent, f"critique (round {n})", None, "no usable reply")
                continue
            first = reply.critiques[0].claim[:120] if reply.critiques else ""
            summary = f"verdict {reply.verdict}; {len(reply.critiques)} critique(s)" + (f": {first}" if first else "")
            dialogue.add(agent, f"critique (round {n})", None, summary)
            await state.step(dialogue.step_id("debate"), f"{agent} (round {n})", summary, agent=agent)


SUPERVISOR_PHASE = SupervisorPhase()
SWARM_PHASE = SwarmPhase()
DEBATE_PHASE = DebatePhase()
for _phase in (SUPERVISOR_PHASE, SWARM_PHASE, DEBATE_PHASE):
    register_phase(_phase)

__all__ = [
    "DEBATE_PHASE",
    "SPECS_BY_NAME",
    "SUPERVISOR_PHASE",
    "SWARM_PHASE",
    "DebatePhase",
    "Dialogue",
    "SupervisorPhase",
    "SwarmPhase",
    "match_specialist",
    "run_specialist",
    "REUSED_PREFIX",
    "call_step_status",
    "run_specialist_tool",
    "task_ledger",
]
