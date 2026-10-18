"""
Clarifying questions (feature 15, human in the loop; stage before_evidence, order 10).

Selective clarification: detect ambiguity first and ask only when it is high, so a clear research question is
never interrupted.

- CLAM (Kuhn et al. 2022, arXiv:2212.07769): decide whether the question is ambiguous, ask only if it is,
  then answer the clarified question.
- Clarify When Necessary (Zhang & Choi, Findings NAACL 2025, arXiv:2311.09469): ask only when the expected
  ambiguity over plausible intents is high. Here the strong deterministic signals ask directly; a borderline
  signal is confirmed by an optional model check (`[phase:clarify]`) and is dropped when no model is available.
- Aliannejadi et al. (SIGIR 2019, arXiv:1907.06554): pick the clarifying question from a curated bank
  (gene family member, model to simulate, dataset to analyse, research topic) instead of free generation.

Deterministic ambiguity signals, from the parsed QueryContext:
- the scope is "unclear" (no biomedical topic found);
- an ambiguous gene family (`ctx.genes` with `ambiguous_with`) and no resolved primary target;
- simulation intent with no model (no BIOMD id, no uploaded SBML) and no searchable term;
- several attached tabular datasets and nothing in the question that says which one to use.

Rules: never when `agent_options.allow_clarification` is false; never twice in a row in one thread (a
per-thread marker is kept when a question is asked, and the next turn is treated as the answer: its text and
the original question go into the research brief); never for a research question with clear entities. The
stop response is the question plus numbered options; artifact `clarification`; task_status `needs_input`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .guardrails import neutralise, spotlight
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import AgentState

logger = logging.getLogger(__name__)

MAX_OPTIONS = 5
_MARKER_TTL_SECONDS = 6 * 3600
_MARKER_MAX = 5000

# (user_id, thread_id) -> {"question", "options", "query", "at"}: the last turn of that thread asked a question.
_ASKED: Dict[Tuple[str, str], Dict[str, Any]] = {}
_ASKED_LOCK = threading.Lock()

_COMPARE_RE = re.compile(r"\b(compare|comparison|versus|vs\.?|isoforms?|paralogs?|family|members?|all\s+(?:three|four|of\s+them))\b", re.I)
_NUMBER_ANSWER_RE = re.compile(r"^\s*(?:option\s*)?(\d)\s*[.)]?\s*$", re.I)


class ClarifyVerdict(BaseModel):
    ambiguous: bool = False
    question: str = Field(default="", max_length=400)
    options: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------
# Per-thread marker ("never twice in a row")
# ---------------------------------------------------------------------------------------------------------

def _marker_key(state: AgentState) -> Tuple[str, str]:
    return (str(state.user_id), str(state.thread_id))


def remember_question(user_id: str, thread_id: str, question: str, options: List[str], query: str) -> None:
    now = time.time()
    with _ASKED_LOCK:
        for key, value in list(_ASKED.items()):
            if now - float(value.get("at", 0)) > _MARKER_TTL_SECONDS:
                _ASKED.pop(key, None)
        if len(_ASKED) >= _MARKER_MAX:
            oldest = min(_ASKED, key=lambda k: float(_ASKED[k].get("at", 0)))
            _ASKED.pop(oldest, None)
        _ASKED[(user_id, thread_id)] = {"question": question, "options": list(options), "query": query[:1000], "at": now}


def pop_question(user_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
    with _ASKED_LOCK:
        value = _ASKED.pop((user_id, thread_id), None)
    if value is not None and time.time() - float(value.get("at", 0)) > _MARKER_TTL_SECONDS:
        return None
    return value


def clear_markers() -> None:
    with _ASKED_LOCK:
        _ASKED.clear()


# ---------------------------------------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------------------------------------

def _mentions_attachment(query: str, att: Any) -> bool:
    q = (query or "").lower()
    name = str(getattr(att, "display_name", "") or "").lower()
    stem = re.sub(r"\.[a-z0-9]+$", "", name)
    ident = str(getattr(att, "id", "") or "").lower()
    return any(tok and tok in q for tok in (name, stem, ident) if len(tok) >= 3)


def detect_signals(ctx: Any, query: str) -> List[Dict[str, Any]]:
    """Ambiguity signals for one question: [{"signal", "strength": high|borderline, "question", "options", "reason"}]."""
    out: List[Dict[str, Any]] = []
    scope = getattr(ctx, "scope", "research")
    if scope == "unclear":
        out.append(
            {
                "signal": "scope_unclear",
                "strength": "high",
                "question": "I could not find a biomedical topic in your message. What would you like me to investigate?",
                "options": [
                    "A gene or protein target (for example KRAS)",
                    "A drug or compound and its mechanism",
                    "A disease and its targets or trials",
                    "A kinetic model simulation (a BioModels id or an uploaded SBML file)",
                ],
                "reason": "No biomedical topic was found in the question.",
            }
        )
        return out
    if scope != "research":
        return out

    genes = list(getattr(ctx, "genes", []) or [])
    families = [g for g in genes if getattr(g, "ambiguous_with", None)]
    has_model = bool(getattr(ctx, "biomodel_id", None) or getattr(ctx, "model_attachment", None))
    # A named model or a simulation request makes the family mention a description ("the MAPK cascade"), not a target.
    family_matters = not has_model and not getattr(ctx, "is_simulation_intent", False)
    if families and family_matters and not getattr(ctx, "primary_target", None) and not _COMPARE_RE.search(query or ""):
        fam = families[0]
        members = [str(m) for m in fam.ambiguous_with][: MAX_OPTIONS - 1]
        other_entities = bool(getattr(ctx, "drugs", None) or getattr(ctx, "diseases", None) or getattr(ctx, "variants", None))
        out.append(
            {
                "signal": "ambiguous_gene_family",
                "strength": "borderline" if other_entities else "high",
                "question": f"“{fam.mention}” names a gene family. Which member do you mean?",
                "options": members + [f"All of them (compare the {fam.label} family)"],
                "reason": f"{fam.mention} is ambiguous between {', '.join(members)}.",
            }
        )

    searchable = bool(getattr(ctx, "primary_target", None) or genes or getattr(ctx, "drugs", None) or getattr(ctx, "diseases", None))
    if getattr(ctx, "is_simulation_intent", False) and not has_model and not searchable:
        out.append(
            {
                "signal": "simulation_without_model",
                "strength": "high",
                "question": "Which model should I simulate?",
                "options": [
                    "Give a BioModels id (for example BIOMD0000000010)",
                    "Attach an SBML model file",
                    "Name the pathway or protein to search BioModels for",
                ],
                "reason": "A simulation was requested but no model or searchable topic was named.",
            }
        )

    tables = [a for a in (getattr(ctx, "attachments", None) or []) if getattr(a, "kind", "") in ("h5ad", "table")]
    if len(tables) >= 2 and not any(_mentions_attachment(query, a) for a in tables):
        names = [str(a.display_name) for a in tables][:MAX_OPTIONS]
        out.append(
            {
                "signal": "multiple_datasets",
                "strength": "high",
                "question": "You attached several datasets. Which one should I analyse?",
                "options": names,
                "reason": f"{len(tables)} tabular datasets are attached and the question does not say which to use.",
            }
        )
    return out


def format_question(question: str, options: List[str]) -> str:
    lines = [question.strip(), ""]
    lines.extend(f"{i}. {opt}" for i, opt in enumerate(options, 1))
    lines.extend(["", "Reply with a number or in your own words, and I will continue from there."])
    return "\n".join(lines)


async def _model_check(state: AgentState, signal: Dict[str, Any]) -> Optional[ClarifyVerdict]:
    if not state.llm.available:
        return None
    system = (
        "[phase:clarify] You decide whether a biomedical research question is too ambiguous to answer without "
        "asking the researcher first. Ask only when plausible readings lead to clearly different answers. "
        'Reply with JSON only: {"ambiguous": true|false, "question": "...", "options": ["...", "..."]} '
        f"with at most {MAX_OPTIONS} short options."
    )
    user = (
        f"Question:\n{spotlight(state.clean_query[:2000], 'user_question')}\n\n"
        f"Detected signal: {signal['signal']} ({signal['reason']})\n"
        f"Suggested options: {', '.join(signal['options'])}"
    )
    try:
        return await state.llm.json(system, user, schema=ClarifyVerdict, phase="clarify")
    except Exception as exc:  # noqa: BLE001
        logger.info("Clarify model check failed: %s", exc)
        return None


def _is_answer(answer: str, options: List[str], scope: str) -> bool:
    """The reply answers the question (rather than asking something new)."""
    low = answer.lower().strip()
    if not low:
        return False
    if any(low in o.lower() or o.lower() in low for o in options):
        return True
    return scope != "research" or len(low.split()) <= 6


async def _continue_after_answer(state: AgentState, previous: Dict[str, Any]) -> None:
    """CLAM's last step: answer the clarified question. A short reply ("2", "KRAS") is merged with the original
    question and the routing context is rebuilt in place, so the turn searches for what was meant."""
    answer = state.clean_query.strip()
    options = [str(o) for o in previous.get("options") or []]
    m = _NUMBER_ANSWER_RE.match(answer)
    if m and 1 <= int(m.group(1)) <= len(options):
        answer = options[int(m.group(1)) - 1]
    original = str(previous.get("query", ""))
    state.brief_notes.append(
        f"Earlier in this thread the researcher asked: {neutralise(original)[:600]!r}. You asked: "
        f"{neutralise(str(previous.get('question', '')))[:300]!r}. Their answer: {neutralise(answer)[:600]!r}."
    )
    state.bag["clarification_answer"] = {"question": previous.get("question"), "answer": answer}
    ctx = state.ctx
    if not original or not _is_answer(answer, options, getattr(ctx, "scope", "research")):
        return
    clarified = f"{original} ({answer})"
    try:
        from ..agents import query_planner

        new_ctx = await query_planner.build_query_context(
            query=clarified,
            clean_query=clarified,
            registry=state.registry,
            enable_web_search=getattr(ctx, "enable_web_search", True),
            attachments=list(getattr(ctx, "attachments", None) or []),
            attachment_errors=list(getattr(ctx, "attachment_errors", None) or []),
            user_id=state.user_id,
            thread_id=state.thread_id,
            model_name=state.model_name,
            api_key=state.api_key,
            llm_factory=None,  # deterministic routing; no second planner call
        )
        query_planner.cancel_planner(ctx)
        keep = {k: v for k, v in (getattr(ctx, "extras", None) or {}).items() if k == "agentic"}
        new_ctx.extras.update(keep)
        new_ctx.extras["clarified_from"] = {"question": original[:1000], "answer": answer[:300]}
        ctx.__dict__.update(new_ctx.__dict__)  # the orchestrator holds this same object
        state.clean_query = clarified
    except Exception as exc:  # noqa: BLE001
        logger.info("Rebuilding the clarified question failed: %s", exc)


class ClarifyPhase(AgentPhase):
    key = "clarify"
    stage = "before_evidence"
    order = 10

    def applies(self, state: AgentState) -> bool:
        return bool(state.options.get("allow_clarification", True))

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        previous = pop_question(*_marker_key(state))
        if previous is not None or state.bag.get("clarified"):
            # This turn answers the question asked last turn: never ask twice in a row.
            if previous is not None:
                await _continue_after_answer(state, previous)
            return None

        signals = detect_signals(state.ctx, state.clean_query)
        if not signals:
            return None
        chosen = next((s for s in signals if s["strength"] == "high"), None)
        verdict: Optional[ClarifyVerdict] = None
        if chosen is None:
            borderline = signals[0]
            verdict = await _model_check(state, borderline)
            if verdict is None or not verdict.ambiguous:
                return None  # Clarify When Necessary: a borderline signal alone does not justify interrupting
            chosen = borderline

        question = chosen["question"]
        options = list(chosen["options"])
        if verdict is not None and verdict.question.strip():
            question = neutralise(verdict.question.strip())[:400]
            model_opts = [neutralise(str(o)).strip()[:160] for o in verdict.options if str(o).strip()]
            if model_opts:
                options = model_opts
        options = options[:MAX_OPTIONS]
        artifact = {
            "question": question,
            "options": options,
            "reason": chosen["reason"],
            "signals": [s["signal"] for s in signals],
        }
        remember_question(state.user_id, state.thread_id, question, options, state.clean_query)
        state.bag["clarified"] = True
        await state.step("agent_clarify", "Asked a clarifying question", chosen["reason"], agent="Clarifier")
        from ..agents.query_context import TaskOutcome

        return PhaseOutcome(
            stop_response=format_question(question, options),
            stop_task_status=TaskOutcome(
                task_status="needs_input",
                requested_operation="clarification",
                request_match=False,
                reason=chosen["reason"],
            ).model_dump(),
            artifacts={"clarification": artifact},
        )


register_phase(ClarifyPhase())

__all__ = ["ClarifyPhase", "ClarifyVerdict", "clear_markers", "detect_signals", "format_question", "pop_question", "remember_question"]
