"""
Auto routing: choose the orchestration architecture for one question, and escalate mid-run on explicit triggers.

1. Features. Deterministic signals from the question and the planner's query context, blended with semantic
   features from one structured model call (optionally several, averaged: self-consistency). Safety flags are
   OR-ed, so the model can raise them but never clear them; specialist flags (including computational work) keep a keyword hit and discount a flag
   only the model raised; structural features (parts, sequence, breadth) lean on the model.
2. Scores. Each architecture gets a weighted sum of the features (weights grounded in the evidence that task shape
   predicts the best agent architecture), plus a capped nudge from past outcomes on similar questions
   (router_store). A softmax turns the scores into probabilities; the top probability is the router's confidence.
3. Guardrails and adjudication. A safety-critical question never goes to the swarm; when the top two are close, a
   model call adjudicates between them, and any failure keeps the scored choice.
4. Escalation. After the first draft, an Auto run hands its draft to the critic debate when the evidence conflicts,
   the question is safety-critical, the route was uncertain, the core phase fell back, or most sub-questions still
   lack evidence.

Design and sources: docs/agentic/auto-routing.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, ValidationError

from .budget import BudgetExhausted, RunBudget
from .config import AGENTIC, AUTO, DEBATE, SUPERVISOR, SWARM, agent_model, env_flag, env_int
from .decompose import complexity_signals, heuristic_split, strip_output_instructions
from .llm import agent_model_kwargs, settle_reply
from .router_store import adjustments, get_store, outcome_quality
from .runtime import AgentPhase, PhaseOutcome, register_phase

logger = logging.getLogger(__name__)

ARCHITECTURES = (AGENTIC, SUPERVISOR, SWARM, DEBATE)
ROUTER_LLM_TIMEOUT_S = 8.0
ROUTER_MAX_OUTPUT_TOKENS = 1024  # the feature reply is a small JSON object
SOFTMAX_TEMPERATURE = 0.4
LLM_WEIGHT = 0.65  # share of the semantic (model) features in the structural blend; the rest is the deterministic signals
# A specialist flag only the model raised counts this much: on the routing benchmark the model over-called
# "exploratory" and "open-ended" for plain lookups, while the keyword patterns for these flags are high-precision.
SEMANTIC_ONLY_WEIGHT = 0.45
ADJUDICATE_BELOW = 0.5  # top probability under this, with a close runner-up: ask the model to adjudicate
ADJUDICATE_MARGIN = 0.2
UNCERTAIN_BELOW = 0.45  # escalation trigger: the route itself was uncertain
# The escalated debate needs one call per critic plus the rewrite (topologies.DebatePhase).
ESCALATION_MIN_LLM_CALLS = 4
# Share of Debate's hypothesis/contested terms removed when the question is a brief of BRIEF_MIN_PARTS+ parts.
DEBATE_PARTS_DAMPING = 0.6
BRIEF_MIN_PARTS = 3
# Shown with a Debate route so the researcher sees what it costs before approving the plan.
DEBATE_COST_NOTE = (
    "Debate adds a critic round (2-3 critics, up to two rounds) and at least one whole-answer rewrite on top of "
    "plan & execute, so it uses more model calls, tokens and time; choose Plan & execute to skip it."
)
SCORES_NOTE = "Suitability scores from heuristic and model features; they are not calibrated probabilities of the best route."

FRAMEWORK_LABELS: Dict[str, str] = {
    AGENTIC: "Plan & execute",
    SUPERVISOR: "Supervisor",
    SWARM: "Swarm",
    DEBATE: "Debate",
}

# Planner intents and search-plan question types grouped into the clinical domains a question touches.
_INTENT_DOMAINS: Dict[str, str] = {
    "literature": "literature",
    "clinical_trials": "clinical",
    "safety": "clinical",
    "target_assessment": "molecular",
    "variant_interpretation": "molecular",
    "drug_mechanism": "molecular",
    "simulation": "computational",
    "steady_state": "computational",
    "single_cell": "computational",
}
_QUESTION_TYPE_DOMAINS: Dict[str, str] = {
    "treatment": "clinical",
    "diagnosis": "clinical",
    "prognosis": "clinical",
    "trials": "clinical",
    "safety": "clinical",
    "guideline": "clinical",
    "regulatory": "clinical",
    "epidemiology": "clinical",
    "mechanism": "molecular",
    "genetics": "molecular",
    "target": "molecular",
    "biomarker": "molecular",
    "pharmacology": "molecular",
}
_COMPUTATIONAL_INTENTS = frozenset({"simulation", "steady_state", "single_cell"})
DOMAINS = ("literature", "clinical", "molecular", "computational")

_HYPOTHESIS = re.compile(
    r"\b(hypothes[ie]s|hypothesi[sz]e|(?:novel|new) (?:[a-z-]+ )?(?:targets?|mechanisms?|therap\w*|combinations?|indications?)"
    r"|repurpos\w*|candidate (?:targets?|drugs?)|propose|testable|what (?:could|might) explain|untested)\b",
    re.I,
)
_CONTESTED = re.compile(
    r"\b(controvers\w*|conflicting|contradict\w*|disputed|debated?|is it (?:true|really)|really (?:works?|effective)"
    r"|overstated|superior|inferior|non-?inferior|better than|worse than|head[- ]to[- ]head|which is (?:better|best|preferred)"
    r"|should (?:we|i|clinicians|patients|one)|evidence (?:for and against|against))\b",
    re.I,
)
_STAKES = re.compile(
    r"\b(my patient|\d{1,3}[- ]?(?:year|yr)s?[- ]?old|pregnan\w*|breast-?feeding|lactat\w*|p(?:a)?ediatric|children|infants?"
    r"|neonat\w*|elderly|dos(?:e|es|ing|age)|mg/kg|renal (?:impairment|failure)|hepatic (?:impairment|failure)|dialysis"
    r"|contraindicat\w*|drug[- ]drug|interactions?|overdose|black[- ]box|boxed warning"
    r"|should (?:i|we) (?:start|stop|give|prescribe|switch))\b",
    re.I,
)
_RECOMMENDATION = re.compile(
    r"\b(should|recommend\w*|preferred|first[- ]line|best (?:option|treatment|therapy|choice))\b", re.I
)
_TARGET_EVALUATION = re.compile(
    r"\b(as an? (?:drug |therapeutic |druggable )?target|target (?:assessment|validation|evaluation|prioriti[sz]ation)"
    r"|druggab\w*|tractab\w*)\b",
    re.I,
)
_EXPLORATORY = re.compile(
    r"\b(explore|exploratory|brainstorm|connections? between|links? between|map out|how (?:is|are) .{1,60}? (?:connected|linked))\b",
    re.I,
)
_OPEN_ENDED = re.compile(
    r"\b(landscape|overview|evaluate|assess|investigate|comprehensive|deep dive|state of the art|what is known|everything)\b",
    re.I,
)
_EXPLICIT_PARTS = frozenset({"several questions", "enumeration", "conjoined question clauses"})
# Complexity cues about the question's structure (the others count entities, intents or words).
_STRUCTURAL = frozenset({"several questions", "enumeration", "conjoined question clauses", "comparison", "multi-hop chain"})

# The feature vector every score reads, each in [0, 1].
FEATURES = (
    "hypothesis", "contested", "high_stakes", "recommendation", "comparison", "target_evaluation", "exploratory",
    "open_ended", "computational", "explicit_parts", "decomposable", "sequential", "breadth", "multi_domain",
)
# OR-ed in the blend: the model may raise these but never clear what the deterministic signals found.
_SAFETY_FEATURES = frozenset({"high_stakes"})
# A keyword hit stands; a flag only the model raised is discounted (SEMANTIC_ONLY_WEIGHT).
_SPECIALIST_FEATURES = frozenset(
    {"hypothesis", "contested", "exploratory", "open_ended", "target_evaluation", "recommendation", "comparison",
     # the planner detects simulations, single-cell work and attached data reliably; the model over-calls it
     "computational"}
)

FEATURE_WORDS: Dict[str, str] = {
    "hypothesis": "asks for hypotheses",
    "contested": "contested claim",
    "high_stakes": "safety-critical",
    "recommendation": "asks for a recommendation",
    "comparison": "head-to-head comparison",
    "target_evaluation": "target evaluation",
    "exploratory": "exploratory",
    "open_ended": "open-ended",
    "computational": "computational analysis",
    "explicit_parts": "explicit parts",
    "decomposable": "independent parts",
    "sequential": "sequential steps",
    "breadth": "several facets",
    "multi_domain": "several domains",
    "brief": "multi-part brief",
    "bias": "default backbone",
    "history": "past outcomes",
}


# -- deterministic signals -------------------------------------------------------------------------------------------

@dataclass
class RoutingSignals:
    domains: List[str] = field(default_factory=list)
    entity_types: List[str] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)
    question_type: str = ""
    complexity: List[str] = field(default_factory=list)
    computational: List[str] = field(default_factory=list)
    hypothesis: bool = False
    contested: bool = False
    high_stakes: bool = False
    recommendation: bool = False
    target_evaluation: bool = False
    exploratory: bool = False
    open_ended: bool = False
    parts: int = 0  # sub-questions the deterministic splitter finds in the question (output instructions removed)

    @property
    def explicit_parts(self) -> bool:
        return bool(_EXPLICIT_PARTS.intersection(self.complexity))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domains": self.domains,
            "entity_types": self.entity_types,
            "intents": self.intents,
            "question_type": self.question_type,
            "complexity": self.complexity,
            "computational": self.computational,
            "hypothesis": self.hypothesis,
            "contested": self.contested,
            "high_stakes": self.high_stakes,
            "recommendation": self.recommendation,
            "target_evaluation": self.target_evaluation,
            "exploratory": self.exploratory,
            "open_ended": self.open_ended,
            "parts": self.parts,
        }


def routing_signals(query: str, ctx: Any) -> RoutingSignals:
    """Deterministic routing signals from the question (its output instructions removed: "cite the evidence for
    every claim" asks nothing contested) and the planner's query context."""
    q, _ = strip_output_instructions(query or "")
    intents = sorted(str(i) for i in (getattr(ctx, "intents", None) or set()))
    plan = getattr(ctx, "search_plan", None)
    qtype = str(getattr(plan, "question_type", "") or "")
    domains = {_INTENT_DOMAINS[i] for i in intents if i in _INTENT_DOMAINS}
    if qtype in _QUESTION_TYPE_DOMAINS:
        domains.add(_QUESTION_TYPE_DOMAINS[qtype])
    entity_types = sorted({str(getattr(e, "type", "")) for e in (getattr(ctx, "entities", None) or []) if getattr(e, "type", "")})
    computational = sorted(_COMPUTATIONAL_INTENTS.intersection(intents))
    if getattr(ctx, "is_simulation_intent", False) and "simulation" not in computational:
        computational.append("simulation")
    if getattr(ctx, "has_single_cell_intent", False) and "single_cell" not in computational:
        computational.append("single_cell")
    if getattr(ctx, "attachments", None):
        computational.append("attached data")
    if computational:
        domains.add("computational")
    return RoutingSignals(
        domains=sorted(domains),
        entity_types=entity_types,
        intents=intents,
        question_type=qtype,
        complexity=complexity_signals(q, ctx),
        computational=computational,
        hypothesis=bool(_HYPOTHESIS.search(q)),
        contested=bool(_CONTESTED.search(q)),
        high_stakes=bool(_STAKES.search(q)),
        recommendation=bool(_RECOMMENDATION.search(q)),
        target_evaluation=bool(_TARGET_EVALUATION.search(q)),
        exploratory=bool(_EXPLORATORY.search(q)),
        open_ended=bool(_OPEN_ENDED.search(q)),
        parts=len(heuristic_split(q, max_sub_questions=8)),
    )


def _multi_domain(n: int) -> float:
    return 0.0 if n <= 1 else 0.5 if n == 2 else 1.0


def deterministic_features(s: RoutingSignals) -> Dict[str, float]:
    structural = [c for c in s.complexity if c in _STRUCTURAL]
    return {
        "hypothesis": float(s.hypothesis),
        "contested": float(s.contested),
        "high_stakes": float(s.high_stakes),
        "recommendation": float(s.recommendation),
        "comparison": float("comparison" in s.complexity or "comparison" in s.intents),
        "target_evaluation": float(s.target_evaluation),
        "exploratory": float(s.exploratory),
        "open_ended": float(s.open_ended),
        "computational": float(bool(s.computational)),
        "explicit_parts": float(s.explicit_parts),
        "decomposable": float(s.explicit_parts or len(structural) >= 2),
        "sequential": float("multi-hop chain" in s.complexity or bool(s.computational)),
        "breadth": float(len(s.domains) >= 2 or len(s.intents) >= 2 or s.target_evaluation),
        "multi_domain": _multi_domain(len(s.domains)),
        # Not a model feature (blend carries it over): a brief of three or more parts, where one hypothesis or
        # contested part does not make the whole answer a debate.
        "brief": float(s.parts >= BRIEF_MIN_PARTS),
    }


# -- semantic features (model) ---------------------------------------------------------------------------------------

QUESTION_KINDS = (
    "treatment", "therapy_comparison", "dosing", "diagnosis", "prognosis", "etiology", "safety", "pharmacology",
    "mechanism", "variant_interpretation", "target_assessment", "trial_landscape", "drug_repurposing",
    "hypothesis_generation", "simulation", "data_analysis", "epidemiology", "guideline", "literature_review",
    "exploration", "other",
)


class SemanticFeatures(BaseModel):
    question_kind: str = "other"
    sub_questions: int = Field(default=1, ge=1, le=12)
    decomposable: bool = False
    sequential: bool = False
    domains: List[str] = Field(default_factory=list)
    needs_computation: bool = False
    hypothesis_generation: bool = False
    contested: bool = False
    high_stakes: bool = False
    asks_recommendation: bool = False
    comparison: bool = False
    exploratory: bool = False
    open_ended: bool = False

    def to_features(self) -> Dict[str, float]:
        domains = {d for d in self.domains if d in DOMAINS}
        return {
            "hypothesis": float(self.hypothesis_generation or self.question_kind in ("hypothesis_generation", "drug_repurposing")),
            "contested": float(self.contested),
            "high_stakes": float(self.high_stakes),
            "recommendation": float(self.asks_recommendation),
            "comparison": float(self.comparison or self.question_kind == "therapy_comparison"),
            "target_evaluation": float(self.question_kind == "target_assessment"),
            "exploratory": float(self.exploratory or self.question_kind == "exploration"),
            "open_ended": float(self.open_ended),
            "computational": float(self.needs_computation or self.question_kind in ("simulation", "data_analysis")),
            "explicit_parts": float(self.sub_questions >= 2 and self.decomposable),
            "decomposable": float(self.decomposable),
            "sequential": float(self.sequential),
            "breadth": float(len(domains) >= 2 or self.question_kind == "target_assessment"),
            "multi_domain": _multi_domain(len(domains)),
        }


ROUTER_FEATURES_SYSTEM = f"""[phase:router]
You read one biomedical or clinical research question and describe its shape for a router that picks an agent
architecture. Do not answer the question.
- question_kind: one of {", ".join(QUESTION_KINDS)}
- sub_questions: how many distinct things it asks (1 for a single focused question)
- decomposable: the parts can be researched independently and in parallel
- sequential: later steps need the results of earlier ones (a chain, a computation pipeline)
- domains: any of {", ".join(DOMAINS)} that the answer needs
- needs_computation: a simulation, model fit or analysis of data is requested
- hypothesis_generation: it asks to propose new hypotheses, targets, mechanisms or repurposing candidates
- contested: it asks to weigh a disputed or comparative conclusion ("is X better than Y", conflicting evidence)
- high_stakes: patient-specific care, dosing, pregnancy, children, interactions or contraindications
- asks_recommendation: it asks what should be done or which option is preferred
- comparison: it compares two or more options head to head
- exploratory: open exploration of connections with no single expected answer
- open_ended: broad assessment, overview or landscape rather than specific facts
Reply with only JSON with exactly these keys."""


def _first_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def router_samples() -> int:
    """PHRONESIS_ROUTER_SAMPLES: semantic feature samples averaged per question (self-consistency)."""
    return env_int("PHRONESIS_ROUTER_SAMPLES", 1, 1, 5)


def semantic_enabled() -> bool:
    """PHRONESIS_ROUTER_LLM: off uses the deterministic signals only; auto/on add the model's semantic features."""
    return env_flag("PHRONESIS_ROUTER_LLM") != "off"


ROUTER_PHASE = "router"


async def _ask(llm_factory: Callable[..., Any], model_name: str, api_key: Optional[str], temperature: float,
               system: str, user: str, budget: Optional[RunBudget] = None) -> Optional[Dict[str, Any]]:
    """One router model call. With a run budget the call is admitted first (phase "router"), sent exactly its output
    allocation and settled with its usage; a call the budget refuses is not made and routing keeps the
    deterministic signals."""
    alloc = None
    extra: Dict[str, Any] = {}
    if budget is not None:
        try:
            alloc = budget.admit(ROUTER_PHASE, len(system) + len(user), ROUTER_MAX_OUTPUT_TOKENS)
        except BudgetExhausted as exc:
            logger.info("Router model call skipped: %s", exc)
            return None
        extra["max_tokens"] = alloc.output_cap
    try:
        model = agent_model(model_name)
        llm = llm_factory(model_name=model, api_key=api_key, temperature=temperature, streaming=False,
                          timeout=ROUTER_LLM_TIMEOUT_S, **agent_model_kwargs(model), **extra)
        from langchain_core.messages import HumanMessage, SystemMessage

        reply = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)]), timeout=ROUTER_LLM_TIMEOUT_S
        )
    except BaseException as exc:  # noqa: BLE001 - routing falls back to the deterministic signals
        if alloc is not None:
            budget.settle(alloc, failed=True)
        if not isinstance(exc, Exception):
            raise
        logger.info("Router model call unavailable: %s", exc)
        return None
    text = str(getattr(reply, "content", reply) or "")
    if alloc is not None:
        settle_reply(budget, alloc, reply, text)
    return _first_json(text)


async def semantic_features(
    query: str, llm_factory: Callable[..., Any], model_name: str, api_key: Optional[str], samples: int = 1,
    budget: Optional[RunBudget] = None,
) -> Tuple[Optional[Dict[str, float]], Optional[str], int]:
    """(averaged features, majority question kind, valid samples); (None, None, 0) when no sample parsed."""
    from ..agents.prompts import utc_date_line

    temperature = 0.0 if samples == 1 else 0.7
    user = f"{utc_date_line()}\nQuestion: {query}"
    replies = await asyncio.gather(
        *(_ask(llm_factory, model_name, api_key, temperature, ROUTER_FEATURES_SYSTEM, user, budget)
          for _ in range(samples))
    )
    parsed: List[SemanticFeatures] = []
    for obj in replies:
        if not obj:
            continue
        try:
            sf = SemanticFeatures.model_validate(obj)
        except ValidationError:
            continue
        if sf.question_kind not in QUESTION_KINDS:
            sf.question_kind = "other"
        parsed.append(sf)
    if not parsed:
        return None, None, 0
    vectors = [p.to_features() for p in parsed]
    avg = {f: round(sum(v[f] for v in vectors) / len(vectors), 3) for f in FEATURES}
    kinds = [p.question_kind for p in parsed]
    kind = max(set(kinds), key=kinds.count)
    return avg, kind, len(parsed)


def blend(det: Dict[str, float], sem: Optional[Dict[str, float]]) -> Dict[str, float]:
    if sem is None:
        return dict(det)
    out: Dict[str, float] = {}
    for f in FEATURES:
        if f in _SAFETY_FEATURES:
            out[f] = max(det[f], sem[f])
        elif f in _SPECIALIST_FEATURES:
            out[f] = round(max(det[f], SEMANTIC_ONLY_WEIGHT * sem[f]), 3)
        else:
            out[f] = round(LLM_WEIGHT * sem[f] + (1 - LLM_WEIGHT) * det[f], 3)
    out.update({k: v for k, v in det.items() if k not in out})  # deterministic-only features ("brief")
    return out


# -- scores ------------------------------------------------------------------------------------------------------------

def score_terms(f: Dict[str, float]) -> Dict[str, List[Tuple[str, float]]]:
    """(factor, contribution) per architecture; a score is the sum of its terms.

    Plan & execute is the backbone: decomposable and computational work favour it, since parallel tool plans suit
    independent parts and sequential pipelines degrade under multi-agent coordination. Debate is reserved for
    hypotheses and contested conclusions with stakes, where critique and ranking pay off. The supervisor takes
    open-ended, multi-facet questions without explicit parts. The swarm only takes low-stakes exploration, because
    independent agents amplify errors.

    In a brief of three or more parts, a hypothesis or contested point is one part among several: Debate's terms
    are damped (the "brief" term, -60% of them), so the brief goes to the plan-and-execute backbone and
    Auto's escalation adds critics only when the draft's evidence conflicts. A whole-answer debate is not forced
    by one keyword."""
    contested_weight = max(f["high_stakes"], f["recommendation"], f["comparison"])
    hypothesis_term = 2.4 * f["hypothesis"]
    contested_term = 1.0 * f["contested"] + 1.4 * f["contested"] * contested_weight
    return {
        AGENTIC: [
            ("bias", 1.0),
            ("explicit_parts", 0.8 * f["explicit_parts"]),
            ("decomposable", 0.6 * f["decomposable"]),
            ("computational", 1.2 * f["computational"]),
            ("sequential", 0.5 * f["sequential"]),
        ],
        DEBATE: [
            ("bias", 0.2),
            ("hypothesis", hypothesis_term),
            ("contested", contested_term),
            ("brief", -DEBATE_PARTS_DAMPING * f.get("brief", 0.0) * (hypothesis_term + contested_term)),
            ("computational", -0.8 * f["computational"]),
        ],
        SUPERVISOR: [
            ("bias", 0.2),
            ("open_ended", 1.8 * f["open_ended"] * f["breadth"]),
            ("multi_domain", 0.9 * f["multi_domain"] * (1 - f["explicit_parts"])),
            ("target_evaluation", 0.6 * f["target_evaluation"] * f["open_ended"]),
            ("explicit_parts", -0.9 * f["explicit_parts"]),
            ("sequential", -0.6 * f["sequential"]),
            ("computational", -0.6 * f["computational"]),
        ],
        SWARM: [
            ("bias", -0.2),
            ("exploratory", 2.0 * f["exploratory"] * (1 - f["high_stakes"])),
            ("high_stakes", -1.0 * f["high_stakes"]),
            ("computational", -0.8 * f["computational"]),
            ("explicit_parts", -0.4 * f["explicit_parts"]),
        ],
    }


def softmax(scores: Dict[str, float], temperature: float = SOFTMAX_TEMPERATURE) -> Dict[str, float]:
    top = max(scores.values())
    exp = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: round(v / total, 4) for k, v in exp.items()}


def bucket_key(kind: str, f: Dict[str, float]) -> str:
    """Coarse question signature for outcome learning: kind plus the strong flags."""
    flags = [n for n in ("hypothesis", "contested", "high_stakes", "computational", "open_ended", "exploratory",
                         "explicit_parts") if f.get(n, 0) >= 0.5]
    return f"{kind or 'general'}|{'+'.join(flags) or 'plain'}"


# -- decision ----------------------------------------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    framework: str
    rule: str
    reason: str
    signals: RoutingSignals
    candidates: List[str] = field(default_factory=list)
    tie_break: str = "none"  # none | llm | llm_failed
    features: Dict[str, float] = field(default_factory=dict)
    probabilities: Dict[str, float] = field(default_factory=dict)
    factors: List[Tuple[str, float]] = field(default_factory=list)
    learned: Dict[str, Dict[str, float]] = field(default_factory=dict)
    semantic: Dict[str, Any] = field(default_factory=dict)
    guardrail: Optional[str] = None
    bucket: str = ""

    @property
    def confidence(self) -> float:
        return self.probabilities.get(self.framework, 1.0)

    def to_artifact(self) -> Dict[str, Any]:
        return {
            "requested": AUTO,
            "chosen": self.framework,
            "label": FRAMEWORK_LABELS.get(self.framework, self.framework),
            "rule": self.rule,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "probabilities": dict(self.probabilities),
            "factors": [{"factor": k, "label": FEATURE_WORDS.get(k, k), "weight": round(w, 2)} for k, w in self.factors],
            "signals": self.signals.to_dict(),
            "features": dict(self.features),
            "semantic": dict(self.semantic),
            "learned": dict(self.learned),
            "guardrail": self.guardrail,
            "bucket": self.bucket,
            "candidates": list(self.candidates),
            "tie_break": self.tie_break,
            "escalation": None,
            "scores_note": SCORES_NOTE,
            "cost_note": DEBATE_COST_NOTE if self.framework == DEBATE else None,
        }


def _join(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _explain(framework: str, f: Dict[str, float], s: RoutingSignals, factors: List[Tuple[str, float]]) -> Tuple[str, str]:
    """(rule, reason) from the factors that drove the chosen score."""
    lead = factors[0][0] if factors else "bias"
    if framework == DEBATE:
        if f["hypothesis"] >= f["contested"]:
            return "hypothesis_generation", (
                "The question asks for new hypotheses; independent critics challenge and rank them before the answer is final."
            )
        return "contested_claim", (
            "The question weighs a contested conclusion" + (" with clinical stakes" if f["high_stakes"] >= 0.5 else "")
            + "; independent critics cross-examine the draft against the evidence."
        )
    if framework == SUPERVISOR:
        facets = _join(s.intents[:4]).replace("_", " ") or _join(s.domains) or "several facets"
        return "open_multi_domain", f"An open-ended question across {facets}; a supervisor delegates each part to a domain specialist."
    if framework == SWARM:
        return "exploratory_chain", "An exploratory multi-step question; specialists hand it along the chain, each adding its own source."
    if f["computational"] >= 0.5 or lead == "computational":
        what = _join(s.computational).replace("_", "-") or "an analysis"
        return "computational", f"The question needs a computational analysis ({what}); one planned tool chain handles sequential steps best."
    if f["explicit_parts"] >= 0.5 or f["decomposable"] >= 0.5:
        cues = _join([c for c in s.complexity if c in _STRUCTURAL][:3]) or "several parts"
        return "default_backbone", f"A multi-part question ({cues}); a parallel tool plan answers each part and checks claims against counter-evidence."
    return "default_backbone", "A focused question; a planned tool run answers it and checks its claims against counter-evidence."


def decide(
    s: RoutingSignals,
    features: Dict[str, float],
    *,
    learned: Optional[Dict[str, Dict[str, float]]] = None,
    semantic: Optional[Dict[str, Any]] = None,
    bucket: str = "",
    excluded: Optional[Set[str]] = None,
) -> RoutingDecision:
    """Score every architecture, apply the history nudge and the guardrails, and explain the winner. A question that
    excludes debate ("No ... debate is needed") is never routed to it."""
    terms = score_terms(features)
    learned = learned or {}
    for fw, adj in learned.items():
        if fw in terms and adj.get("delta"):
            terms[fw].append(("history", float(adj["delta"])))
    scores = {fw: sum(w for _, w in t) for fw, t in terms.items()}
    probs = softmax(scores)
    ranked = sorted(ARCHITECTURES, key=lambda fw: (-probs[fw], ARCHITECTURES.index(fw)))
    framework, guardrail = ranked[0], None
    if framework == SWARM and features["high_stakes"] >= 0.5:
        framework = next(fw for fw in ranked if fw != SWARM)
        guardrail = "A safety-critical question is never routed to the swarm."
    if framework == DEBATE and "debate" in (excluded or set()):
        framework = next(fw for fw in ranked if fw != DEBATE)
        guardrail = "The question excludes a debate, so it is not routed to one."
    factors = sorted(((k, w) for k, w in terms[framework] if k != "bias" and abs(w) >= 0.05), key=lambda kv: -kv[1])
    rule, reason = _explain(framework, features, s, factors)
    runner_up = next(fw for fw in ranked if fw != framework and not (fw == DEBATE and "debate" in (excluded or set())))
    candidates = [framework]
    if probs[framework] < ADJUDICATE_BELOW and probs[framework] - probs[runner_up] < ADJUDICATE_MARGIN:
        candidates.append(runner_up)
    return RoutingDecision(
        framework, rule, reason, s, candidates=candidates, features=features, probabilities=probs,
        factors=factors[:5], learned=learned, semantic=semantic or {"used": False}, guardrail=guardrail, bucket=bucket,
    )


def rule_decision(query: str, ctx: Any) -> RoutingDecision:
    """The deterministic route: scores over the deterministic signals only (no model call, no history)."""
    signals = routing_signals(query, ctx)
    if getattr(ctx, "scope", "research") != "research":
        return RoutingDecision(AGENTIC, "not_research", "Not a research question; no architecture is needed.", signals,
                               candidates=[AGENTIC], probabilities={AGENTIC: 1.0})
    features = deterministic_features(signals)
    return decide(signals, features, bucket=bucket_key(signals.question_type, features),
                  excluded=set(getattr(ctx, "excluded_modules", None) or ()))


ROUTER_ADJUDICATE_SYSTEM = """[phase:router_adjudicate]
You choose how a biomedical research assistant answers one question. Options:
{options}
Pick the single best option for this question. Prefer plan_execute unless another option clearly fits better.
Reply with only JSON: {{"route": "<option id>", "reason": "<one sentence for the user>"}}"""

_OPTION_TEXT: Dict[str, str] = {
    AGENTIC: "plan_execute: decompose into sub-questions, run search and analysis tools in parallel, check claims against counter-evidence",
    SUPERVISOR: "supervisor: a manager delegates open-ended parts to domain specialists (literature, targets, trials, omics, simulation)",
    SWARM: "swarm: specialists hand the question along a chain of steps (exploratory, low-stakes multi-hop questions)",
    DEBATE: "debate: plan_execute plus independent critics who cross-examine the draft (hypotheses, contested conclusions)",
}
_OPTION_IDS: Dict[str, str] = {AGENTIC: "plan_execute", SUPERVISOR: "supervisor", SWARM: "swarm", DEBATE: "debate"}


async def _adjudicate(query: str, decision: RoutingDecision, llm_factory: Callable[..., Any], model_name: str,
                      api_key: Optional[str], budget: Optional[RunBudget] = None) -> Optional[Tuple[str, str]]:
    options = "\n".join(f"- {_OPTION_TEXT[fw]}" for fw in decision.candidates if fw in _OPTION_TEXT)
    context = json.dumps({"features": decision.features, "probabilities": decision.probabilities})
    obj = await _ask(llm_factory, model_name, api_key, 0.0, ROUTER_ADJUDICATE_SYSTEM.format(options=options),
                     f"Question: {query}\nRouter context: {context}", budget)
    by_id = {v: k for k, v in _OPTION_IDS.items()}
    chosen = by_id.get(str((obj or {}).get("route") or "").strip())
    if chosen not in decision.candidates:
        return None
    return chosen, str((obj or {}).get("reason") or "").strip()[:300]


async def choose_framework(
    query: str,
    ctx: Any,
    *,
    llm_factory: Optional[Callable[..., Any]] = None,
    model_name: str = "",
    api_key: Optional[str] = None,
    budget: Optional[RunBudget] = None,
) -> RoutingDecision:
    """Route one question: blended features, scores with the history nudge, guardrails, then adjudication if close.

    `budget` is the run's budget: every router model call (semantic samples, adjudication) is charged to it under
    the "router" phase, and a call it refuses is skipped (the deterministic route stands)."""
    if getattr(ctx, "scope", "research") != "research":
        return rule_decision(query, ctx)
    signals = routing_signals(query, ctx)
    det = deterministic_features(signals)
    sem: Optional[Dict[str, float]] = None
    kind: Optional[str] = None
    semantic: Dict[str, Any] = {"used": False}
    if llm_factory is not None and semantic_enabled():
        samples = router_samples()
        question, _ = strip_output_instructions(query)
        sem, kind, valid = await semantic_features(question, llm_factory, model_name, api_key, samples, budget=budget)
        semantic = {"used": sem is not None, "samples": samples, "valid_samples": valid, "question_kind": kind}
    features = blend(det, sem)
    bucket = bucket_key(kind or signals.question_type, features)
    learned: Dict[str, Dict[str, float]] = {}
    store = get_store()
    if store is not None:
        try:
            learned = adjustments(store.stats(bucket))
        except Exception as exc:  # noqa: BLE001 - routing never depends on the history being readable
            logger.info("Router history unavailable: %s", exc)
    decision = decide(signals, features, learned=learned, semantic=semantic, bucket=bucket,
                      excluded=set(getattr(ctx, "excluded_modules", None) or ()))
    if len(decision.candidates) < 2 or llm_factory is None:
        return decision
    picked = await _adjudicate(query, decision, llm_factory, model_name, api_key, budget=budget)
    if picked is None:
        decision.tie_break = "llm_failed"
        return decision
    framework, reason = picked
    decision.tie_break = "llm"
    if framework != decision.framework:
        decision.framework, decision.rule = framework, "llm_tie_break"
        decision.reason = reason or f"Among {_join([FRAMEWORK_LABELS[c] for c in decision.candidates])}, the model judged this the best fit."
        decision.factors = []
    return decision


# -- outcome learning --------------------------------------------------------------------------------------------------

def record_outcome(
    *,
    query: str,
    ctx: Any,
    framework: str,
    routing: Optional[Dict[str, Any]],
    verification_score: Optional[float],
    answer_confidence: Optional[float],
    task_status: str,
    core_status: str,
    llm_calls: int,
    llm_limit: int,
) -> None:
    """Store how reliably this run went under its architecture (Auto-routed or chosen by the user)."""
    store = get_store()
    if store is None:
        return
    if routing:
        features, bucket = dict(routing.get("features") or {}), str(routing.get("bucket") or "")
    else:
        signals = routing_signals(query, ctx)
        features = deterministic_features(signals)
        bucket = bucket_key(signals.question_type, features)
    if not bucket:
        return
    # AGT-004: the label is operational (completion, core phase); the self-scored verification score and answer
    # confidence are stored for diagnostics only and never move the routing.
    quality = outcome_quality(verification_score, answer_confidence, task_status, core_status)
    cost = llm_calls / llm_limit if llm_limit else 0.0
    try:
        store.record(bucket, framework, bool(routing), quality, round(cost, 3), features,
                     self_verification=verification_score, self_confidence=answer_confidence)
    except Exception as exc:  # noqa: BLE001
        logger.info("Router outcome not recorded: %s", exc)


# -- escalation --------------------------------------------------------------------------------------------------------

def escalation_triggers(state: Any) -> List[str]:
    """Why an Auto run should have critics debate its draft ([] = no escalation)."""
    triggers: List[str] = []
    routing = (getattr(state, "bag", None) or {}).get("routing") or {}
    art = (getattr(state, "artifacts", None) or {}).get("evidence_conflicts")
    conflicts = [
        c for c in ((art or {}).get("conflicts") or [])
        if isinstance(c, dict) and c.get("label") == "contradicts" and c.get("classifier") != "heuristic"
    ]
    if conflicts:
        triggers.append(f"{len(conflicts)} contradicting passage{'s' if len(conflicts) != 1 else ''} found by the counter-evidence search")
    features = routing.get("features") or {}
    signals = routing.get("signals") or {}
    if features.get("high_stakes", 0) >= 0.5 or signals.get("high_stakes"):
        triggers.append("a safety-critical question (patient-specific, dosing or interactions)")
    confidence = routing.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < UNCERTAIN_BELOW:
        triggers.append(f"an uncertain route (top choice {round(confidence * 100)}%)")
    core = state.core_phase_status() if hasattr(state, "core_phase_status") else None
    if isinstance(core, dict) and core.get("status") == "fallback":
        triggers.append(f"the {core.get('label') or 'core'} step fell back to a fixed plan")
    subs = list(getattr(state, "sub_questions", None) or [])
    open_subs = [q for q in subs if getattr(q, "status", "open") != "answered"]
    if len(open_subs) >= 2 and len(open_subs) * 2 > len(subs):
        triggers.append(f"{len(open_subs)} of {len(subs)} sub-questions still lack evidence")
    return triggers


class EscalationPhase(AgentPhase):
    """Auto runs only: after the first draft, hand a non-debate run to the critic debate when a trigger fires."""

    key = "escalate"
    stage = "after_draft"
    order = 15  # after gap search (10), before the debate (20) it enables
    needs_llm = True

    def applies(self, state: Any) -> bool:
        routing = state.bag.get("routing") or {}
        return (
            routing.get("requested") == AUTO
            and state.framework != DEBATE
            and state.draft_round == 0
            and not state.bag.get("escalation_checked")
            and getattr(state.ctx, "scope", "research") == "research"
            # "No ... debate is needed": critics are never brought in against the question's own words.
            and "debate" not in (getattr(state.ctx, "excluded_modules", None) or set())
            and bool((state.draft or "").strip())
            and bool(escalation_triggers(state))
        )

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        state.bag["escalation_checked"] = True
        triggers = escalation_triggers(state)
        routing = state.bag.get("routing") or {}
        left = state.budget.max_llm_calls - state.budget.llm_calls
        if left < ESCALATION_MIN_LLM_CALLS:
            routing["escalation"] = {"to": DEBATE, "triggers": triggers, "status": "skipped",
                                     "reason": f"only {left} model call(s) left in the agent budget"}
            state.mark_phase(self.key, "budget", routing["escalation"]["reason"])
            return None
        state.bag["escalate_debate"] = True
        routing["escalation"] = {"to": DEBATE, "triggers": triggers, "status": "escalated"}
        await state.step("escalate", "Escalating to a critic debate", "; ".join(triggers).capitalize(), agent="Router")
        return None


register_phase(EscalationPhase())
