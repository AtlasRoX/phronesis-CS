"""
Hypothesis -> in-silico experiment loop (feature 6, stage augment, order 40).

Techniques
- AI co-scientist (Gottweis et al. 2025, arXiv:2502.18864): Generation -> Reflection -> Ranking by an Elo
  tournament (every hypothesis starts at 1200, K = 32; pairwise judge) -> Evolution (the top hypothesis is refined
  once and enters the tournament as a new hypothesis).
- BioDiscoveryAgent (Roohani et al., ICLR 2025, arXiv:2405.17631): the generation prompt carries the research
  problem, the measurement definition (readout, log2 fold-change threshold), the results gathered so far and an
  exclusion list of perturbations already tested; the model returns a reflection, a short research plan and a
  batch of perturbations. Invalid or repeated perturbations are rejected, never silently changed.
- Robin (Ghareeb et al. 2025, arXiv:2505.13400): ranking with a pairwise LLM judge (Robin fits Bradley-Terry;
  Elo is the online approximation used here).

In-silico experiments
- Only when a kinetic model is in play (ctx.biomodel_id, an uploaded SBML model, or the pipeline Vivarix result's
  model_id) and simulation is allowed: never when ctx.suppress_simulation is set, a simulation is forbidden, or
  the turn's source restriction excludes Vivarix.
- Each perturbation is validated against the model's own species and parameters (vivarix_list_parameters) and run
  as vivarix_simulate_time_course with parameter_overrides (knockdown x0.1 of a species' initial amount,
  overexpression x10, inhibition x0.1 of a rate constant) against an unperturbed baseline with the same duration.
- Effect: log2 fold-change of the readout species endpoint, log2((perturbed + eps) / (baseline + eps)) with
  eps = 1e-9 x max(|baseline|, |perturbed|) (reported). Support = the predicted direction and |log2FC| >= 0.58
  (a 1.5-fold change). At most 4 perturbed simulations per run.
- Without a model (or with simulation switched off) hypotheses are ranked on the retrieved evidence only and are
  labelled untested; no experiment result is ever invented.

Without a model call the phase is deterministic: perturbation candidates come from the species and parameters
named in the question, the model species that name the target gene or its aliases, and the rate constants of
reactions that produce or consume the readout; ranking compares measured effects and evidence.
Writes the artifact "hypotheses" and a <hypothesis_experiments> evidence section.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from ..phronesis.agents.verifier import truncate_words
from .budget import BudgetExhausted
from .guardrails import SPOTLIGHT_RULE, neutralise, spotlight
from .llm import AgentLLMUnavailable
from .runtime import AgentPhase, register_phase
from .state import AgentState

logger = logging.getLogger(__name__)

PHASE = "hypothesis"
LIST_TOOL = "vivarix_list_parameters"
SIM_TOOL = "vivarix_simulate_time_course"

ELO_START = 1200.0
ELO_K = 32.0
LOG2FC_THRESHOLD = 0.58
EPS_RELATIVE = 1e-9
MAX_HYPOTHESES = 6
MAX_EXPERIMENTS = 4
MAX_JUDGE_CALLS = 8
KINDS = ("knockdown", "overexpression", "inhibition")
# Hypotheses are predictions, not reports: they must not assert data the evidence does not contain.
GROUNDED_STATEMENT_RULE = (
    "Write each statement as a testable prediction (\"X will ...\", \"if X then Y\"), not a recommendation to "
    "evaluate something. Never say that data exist (trial results, safety profiles, EHR or real-world records, "
    "approvals) unless the evidence given here contains them. For a tumour suppressor or a gene the question calls "
    "lost or deficient (BRCA2, TP53, PTEN), never propose inhibiting that gene: target its synthetic-lethal partners "
    "or restore its pathway. Any test you describe must prespecify the design, the readout, the smallest effect that "
    "matters and the decision rule that refutes the prediction (for example, refuted if the 95% CI of the difference "
    "excludes a 20% reduction); \"no statistically significant difference\" alone is not a falsifier."
)
TEXT_DUPLICATE_JACCARD = 0.8
DEFAULT_FACTORS = {"knockdown": 0.1, "overexpression": 10.0, "inhibition": 0.1}

_INTENT_RE = re.compile(
    r"\bhypothes|\bwhat (?:would|will|might) happen|\bwhat if\b|\bknock[\s-]?(?:down|out)|\boverexpress|\binhibit|"
    r"\bperturb|\bpredict|\bin[\s-]?silico\b|\bexperiment|\bcandidate (?:mechanism|intervention)|\bwhich (?:parameter|node|step)",
    re.I,
)
_READOUT_CUE_RE = re.compile(
    r"(?:happens?\s+to|effect\s+on|impact\s+on|levels?\s+of|readout|measure|response\s+of|output\s+of|concentration\s+of)"
    r"\s+(?:the\s+)?([\w\-\[\]().]+)",
    re.I,
)
_OVEREXPRESS_RE = re.compile(r"\boverexpress|\bupregulat|\bincreas\w* (?:the )?(?:expression|level)", re.I)
_RESISTANCE_RE = re.compile(r"\bresist\w*|\bescape\b|\brefractor\w*|\bacquired\b|\brelapse\w*", re.I)
_COMBINATION_RE = re.compile(r"\bcombin\w*|\bsynerg\w*|\bco-?treat\w*|\bplus\b|\btogether with\b", re.I)
_VARIANT_RE = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY])(\d{1,4})([ACDEFGHIKLMNPQRSTVWY]|del|fs|\*)(?![\w])")
# Words that do not tell two diseases apart ("NSCLC" vs "lung adenocarcinoma" vs "lung cancer" share "lung").
_GENERIC_DISEASE_WORDS = frozenset(
    "cancer cancers carcinoma carcinomas neoplasm neoplasms tumor tumors tumour tumours malignant malignancy disease "
    "diseases disorder cell cells non small large adenocarcinoma squamous primary metastatic advanced of the and".split()
)
_DISEASE_ABBREVIATIONS = {"nsclc": "lung", "sclc": "lung", "luad": "lung", "lusc": "lung", "pdac": "pancrea",
                          "crc": "colorect", "hcc": "liver hepat", "gbm": "glioblastoma glioma", "aml": "myeloid leukemia",
                          "cll": "lymphocytic leukemia", "tnbc": "breast", "mm": "myeloma"}


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass
class Hypothesis:
    id: str
    statement: str
    mechanism: str
    kind: Optional[str]
    parameter: Optional[str]  # the model quantity overridden (species, global or "(reaction).k")
    factor: Optional[float]
    readout: Optional[str]
    prediction: str  # increase | decrease
    source: str = "deterministic"  # llm | deterministic | evidence | evolved
    prediction_basis: str = ""
    parent: Optional[str] = None
    evidence_keys: List[str] = field(default_factory=list)
    evidence_score: float = 0.0
    status: str = "untested"  # untested | tested | failed
    note: str = ""
    result: Optional[Dict[str, Any]] = None
    elo: float = ELO_START
    rank: int = 0

    def perturbation(self) -> Tuple[str, str, float]:
        return (str(self.parameter or ""), str(self.kind or ""), round(float(self.factor or 0.0), 6))

    def to_artifact(self) -> Dict[str, Any]:
        result = self.result or {}
        return {
            "id": self.id,
            "statement": self.statement,
            "mechanism": self.mechanism,
            "intervention": {"kind": self.kind, "parameter": self.parameter, "factor": self.factor},
            "readout": self.readout,
            "prediction": self.prediction,
            "prediction_basis": self.prediction_basis,
            "result": {
                "baseline": result.get("baseline"),
                "perturbed": result.get("perturbed"),
                "log2_fold_change": result.get("log2_fold_change"),
                "supports": result.get("supports") if self.status == "tested" else None,
                "epsilon": result.get("epsilon"),
                "result_key": result.get("result_key"),
            },
            "status": self.status,
            "note": self.note,
            "source": self.source,
            "parent": self.parent,
            "elo": round(self.elo, 1),
            "rank": self.rank,
            "evidence_keys": list(self.evidence_keys)[:10],
        }


@dataclass
class ModelRef:
    biomodel_id: str
    label: str
    sim_kwargs: Dict[str, Any] = field(default_factory=dict)  # sbml_path, model_label, model_provenance


@dataclass
class ModelInfo:
    species: Dict[str, Optional[float]] = field(default_factory=dict)  # name -> initial concentration
    parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # override name -> {value, short, reaction}
    reactions: List[Tuple[str, List[str], List[str]]] = field(default_factory=list)  # (name, substrates, products)
    source: str = "none"  # list_tool | vivarix_result | none

    def short_names(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for full, meta in self.parameters.items():
            out.setdefault(str(meta.get("short") or full).lower(), []).append(full)
        return out

    def resolve(self, name: Optional[str]) -> Optional[Tuple[str, str]]:
        """(override name, 'species'|'parameter') for a species/parameter name as written, or None."""
        if not name:
            return None
        n = str(name).strip()
        if n in self.species:
            return n, "species"
        if n in self.parameters:
            return n, "parameter"
        lower = n.lower()
        sp = [s for s in self.species if s.lower() == lower]
        if len(sp) == 1:
            return sp[0], "species"
        full = [p for p in self.parameters if p.lower() == lower]
        if len(full) == 1:
            return full[0], "parameter"
        short = self.short_names().get(lower, [])
        if len(short) == 1:
            return short[0], "parameter"
        return None

    def baseline_value(self, name: str) -> Optional[float]:
        if name in self.species:
            return self.species[name]
        meta = self.parameters.get(name)
        return meta.get("value") if meta else None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _data(res: Any) -> Dict[str, Any]:
    data = getattr(res, "data", None)
    return data if isinstance(data, dict) and getattr(res, "success", False) else {}


def simulation_block_reason(ctx: Any) -> str:
    """Why no simulation may run this turn ("" when it may)."""
    if getattr(ctx, "suppress_simulation", False):
        return "simulation was switched off at the researcher's request"
    constraints = getattr(ctx, "simulation_constraints", None)
    if constraints is not None and getattr(constraints, "simulation_forbidden", False):
        return "simulation is forbidden for this turn"
    allowed = getattr(ctx, "source_allowed", None)
    if callable(allowed) and not allowed("vivarix"):
        return getattr(ctx, "source_restriction_reason", "") or "the turn's source restriction excludes simulation"
    return ""


def model_in_play(ctx: Any, tool_results: Dict[str, Any]) -> Optional[ModelRef]:
    """The kinetic model this turn is about: an uploaded SBML model, a typed accession, or the Vivarix result's."""
    att = getattr(ctx, "model_attachment", None)
    if att is not None and getattr(att, "model_path", None):
        return ModelRef(
            biomodel_id=att.id,
            label=att.display_name or att.id,
            sim_kwargs={
                "sbml_path": att.model_path,
                "model_label": att.display_name,
                "model_provenance": dict(att.public_provenance or {}),
            },
        )
    bm = getattr(ctx, "biomodel_id", None)
    if bm:
        return ModelRef(biomodel_id=str(bm), label=str(bm))
    for key in ("vivarix", "vivarix_scan"):
        mid = _data(tool_results.get(key)).get("model_id")
        if isinstance(mid, str) and re.match(r"^(?:BIOMD|MODEL)\d{10}$", mid):
            return ModelRef(biomodel_id=mid, label=mid)
    return None


def expected_score(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


def elo_update(ra: float, rb: float, score_a: float, k: float = ELO_K) -> Tuple[float, float]:
    """Elo ratings after one match; score_a is 1 (A wins), 0.5 (draw) or 0 (B wins)."""
    ea = expected_score(ra, rb)
    return ra + k * (score_a - ea), rb + k * ((1.0 - score_a) - (1.0 - ea))


def log2_fold_change(baseline: float, perturbed: float) -> Tuple[float, float]:
    """(log2FC, eps): zeros are guarded with eps = 1e-9 x max(|baseline|, |perturbed|) (or 1e-300 when both are 0)."""
    b, p = max(0.0, float(baseline)), max(0.0, float(perturbed))
    eps = max(EPS_RELATIVE * max(b, p), 1e-300)
    return math.log2((p + eps) / (b + eps)), eps


def _scheme_sides(scheme: str) -> Tuple[List[str], List[str]]:
    body = str(scheme or "").split(";", 1)[0]
    parts = re.split(r"->|=", body, maxsplit=1)
    if len(parts) != 2:
        return [], []

    def names(side: str) -> List[str]:
        out = []
        for term in side.split("+"):
            term = re.sub(r"^\s*\d+(?:\.\d+)?\s*\*?\s*", "", term).strip()
            if term:
                out.append(term)
        return out

    return names(parts[0]), names(parts[1])


def _mentions(text: str, name: str) -> Optional[int]:
    """Position of a whole-name mention in text, or None (case-insensitive from 3 characters; 'A' never matches 'a')."""
    if not name:
        return None
    m = re.search(rf"(?<![\w\-]){re.escape(name)}(?![\w\-])", text, re.I if len(name) >= 3 else 0)
    return m.start() if m else None


def _target_keys(ctx: Any) -> List[str]:
    names = [getattr(ctx, "primary_symbol", None) or ""]
    try:
        names += list(ctx.primary_aliases())
    except Exception:  # noqa: BLE001
        pass
    return [k for k in (re.sub(r"[^a-z0-9]", "", n.lower()) for n in names if n) if k]


# Tumour suppressors / caretakers whose cancer-relevant state is loss of function: inhibiting them is not a therapy.
TUMOUR_SUPPRESSORS = {
    "TP53", "BRCA1", "BRCA2", "PALB2", "ATM", "ATR", "CHEK2", "RAD51C", "RAD51D", "BRIP1", "PTEN", "RB1", "CDKN2A",
    "CDKN1B", "SMAD4", "APC", "VHL", "NF1", "NF2", "STK11", "ARID1A", "ARID1B", "KEAP1", "BAP1", "PBRM1", "SETD2",
    "FBXW7", "WT1", "MLH1", "MSH2", "MSH6", "PMS2", "TSC1", "TSC2", "CDH1", "SMARCA4", "SMARCB1", "KMT2D", "NOTCH1",
}


def is_loss_of_function_target(target: str, question: str) -> bool:
    """True for a tumour suppressor, or a gene the question describes as lost/deficient ("BRCA2 loss")."""
    sym = (target or "").upper()
    if sym in TUMOUR_SUPPRESSORS:
        return True
    q = question or ""
    m = re.search(rf"\b{re.escape(sym)}\b[\s-]*(?:\w+\s+){{0,1}}(loss|lost|deficien\w*|deleted|deletion|null|knock-?out|inactivat\w*)",
                  q, re.IGNORECASE)
    return m is not None


def _disease_key(name: str) -> set:
    """Distinguishing words of a disease name (generic words dropped, common abbreviations expanded)."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    out: set = set()
    for w in words:
        if w in _DISEASE_ABBREVIATIONS:
            out.update(_DISEASE_ABBREVIATIONS[w].split())
        elif w not in _GENERIC_DISEASE_WORDS and len(w) > 2:
            out.add(w[:7])
    return out


def _same_disease(a: set, b: set) -> bool:
    """Near-identical diseases: one name's distinguishing words cover the other's, or one is fully generic."""
    if not a or not b:
        return True  # "cancer" next to any specific cancer adds nothing
    return a <= b or b <= a or bool({w[:6] for w in a} & {w[:6] for w in b})


def _norm(value: Any) -> str:
    return re.sub(r"[\W_]+", " ", str(value or "").lower()).strip()


def _word_jaccard(a: str, b: str) -> float:
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def is_near_duplicate(h: Hypothesis, kept: Hypothesis) -> bool:
    """True when `h` repeats `kept` as an untested hypothesis: the same intervention target and direction, or
    nearly the same text. Tested hypotheses carry their own measured result (a dose-response child shares its
    parent's target), and evidence hypotheses about different diseases share a template, so neither is merged."""
    if h.status != "untested" or kept.status != "untested":
        return False
    target, kept_target = _norm(h.parameter), _norm(kept.parameter)
    if target and kept_target and target != kept_target:
        return False  # different targets are different hypotheses, however alike the wording
    same_intervention = bool(target and h.kind) and h.kind == kept.kind and target == kept_target
    if h.source == "evidence" or kept.source == "evidence":
        return same_intervention and _norm(h.readout) == _norm(kept.readout)
    if same_intervention and h.prediction == kept.prediction:
        return True
    return _word_jaccard(h.statement, kept.statement) >= TEXT_DUPLICATE_JACCARD


def dedupe_ranked(ranked: List[Hypothesis], rejected: List[Dict[str, str]]) -> List[Hypothesis]:
    """`ranked` (best first) without near-duplicates; each dropped one is noted in `rejected`."""
    out: List[Hypothesis] = []
    for h in ranked:
        dup = next((k for k in out if is_near_duplicate(h, k)), None)
        if dup is None:
            out.append(h)
        else:
            rejected.append({"hypothesis": h.statement[:200], "reason": f"near-duplicate of {dup.id}"})
    return out


def _species_names_target(species: str, keys: List[str]) -> bool:
    from ..agents.discovery import _species_names_target as match

    return match(species, keys)


async def fetch_model_info(state: AgentState, model: ModelRef, phase: str) -> ModelInfo:
    """The model's species, parameters and reactions (vivarix_list_parameters), else the Vivarix result's species."""
    info = ModelInfo()
    if state.registry.get_entry(LIST_TOOL) is not None:
        kwargs: Dict[str, Any] = {"biomodel_id": model.biomodel_id}
        if model.sim_kwargs.get("sbml_path"):
            kwargs.update(sbml_path=model.sim_kwargs["sbml_path"], model_label=model.sim_kwargs.get("model_label"))
        res = await state.call_tool(LIST_TOOL, phase=phase, purpose=f"list the parameters of {model.label}", store=False, **kwargs)
        data = _data(res)
        if data:
            info.source = "list_tool"
            for s in data.get("species") or []:
                if isinstance(s, dict) and s.get("name"):
                    info.species[str(s["name"])] = _num(s.get("initial_concentration"))
            globals_ = {str(g["name"]): _num(g.get("value")) for g in data.get("global_parameters") or [] if isinstance(g, dict) and g.get("name")}
            for name, value in globals_.items():
                info.parameters[name] = {"value": value, "short": name, "reaction": ""}
            for p in data.get("reaction_parameters") or []:
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                mapped = p.get("mapped_to")
                if mapped and mapped in info.parameters:
                    info.parameters[mapped].setdefault("reactions", []).append(str(p.get("reaction") or ""))
                    continue
                info.parameters[str(p["name"])] = {"value": _num(p.get("value")), "short": str(p.get("short_name") or p["name"]), "reaction": str(p.get("reaction") or "")}
            for r in data.get("reactions") or []:
                if isinstance(r, dict) and r.get("name"):
                    subs, prods = _scheme_sides(str(r.get("scheme") or ""))
                    info.reactions.append((str(r["name"]), subs, prods))
            return info
    viv = _data(state.tool_results.get("vivarix"))
    if viv.get("initial_concentrations") and viv.get("model_id") == model.biomodel_id:
        info.source = "vivarix_result"
        info.species = {str(k): _num(v) for k, v in viv["initial_concentrations"].items()}
    return info


# -----------------------------------------------------------------------------
# The phase
# -----------------------------------------------------------------------------

class HypothesisPhase(AgentPhase):
    key = PHASE
    stage = "augment"
    order = 40
    frameworks = None  # every agentic framework

    def applies(self, state: AgentState) -> bool:
        ctx = state.ctx
        option = state.options.get("enable_hypotheses")
        if option is False or getattr(ctx, "scope", "research") != "research":
            return False
        if "hypothesis" in (getattr(ctx, "excluded_modules", None) or set()):
            return False  # "No ... hypothesis generation ... is needed": the question's own words win
        if option is True:
            return True
        return bool(_INTENT_RE.search(state.clean_query or state.query or "")) or model_in_play(ctx, state.tool_results) is not None

    async def run(self, state: AgentState) -> None:
        ctx = state.ctx
        question = state.clean_query or state.query
        model = model_in_play(ctx, state.tool_results)
        block = simulation_block_reason(ctx)
        notes: List[str] = []
        artifact: Dict[str, Any] = {
            "target": getattr(ctx, "primary_display", None) or getattr(ctx, "primary_symbol", None),
            "model_id": model.biomodel_id if model else None,
            "method": "elo-tournament",
            "elo": {"start": ELO_START, "k": ELO_K},
            "threshold_log2fc": LOG2FC_THRESHOLD,
            "experiments_run": 0,
            "simulation": "no_model" if model is None else ("blocked" if block else "allowed"),
        }

        info = ModelInfo()
        if model is not None and not block:
            info = await self._model_info(state, model)
        elif model is not None:
            notes.append(f"No in-silico experiment was run: {block}.")
            state.scope_notes.append(f"Hypotheses were not tested in silico because {block}; they are labelled untested.")

        llm_meta: Dict[str, Any] = {}
        hypotheses, rejected = await self._generate(state, question, model, info, llm_meta)
        artifact["generator"] = llm_meta.get("generator", "deterministic")
        if llm_meta.get("reflection"):
            artifact["reflection"] = llm_meta["reflection"]
        if llm_meta.get("research_plan"):
            artifact["research_plan"] = llm_meta["research_plan"]
        if not hypotheses:
            artifact.update(hypotheses=[], rejected=rejected[:10], note="No testable hypothesis could be formed from the question, the model and the retrieved evidence.")
            state.artifacts["hypotheses"] = artifact
            await state.step("agent_hypothesis", "No hypotheses formed", artifact["note"], agent="BioDiscovery")
            return

        self._score_evidence(state, hypotheses)
        experiments = 0
        baseline: Optional[Dict[str, Any]] = None
        can_test = model is not None and not block and info.source != "none"
        if model is not None and not block and info.source == "none":
            notes.append("The model's parameters could not be listed, so no perturbation could be validated or run.")
        tested_set = set()
        if can_test:
            baseline = await self._baseline(state, model)
            if baseline is None:
                notes.append("The baseline simulation failed, so no perturbation was run.")
            else:
                for h in hypotheses:
                    if experiments >= MAX_EXPERIMENTS - 1:  # one run is kept for the evolved hypothesis
                        break
                    if h.kind is None or h.parameter is None or h.perturbation() in tested_set:
                        continue
                    tested_set.add(h.perturbation())
                    experiments += await self._experiment(state, model, info, baseline, h)
        for h in hypotheses:
            if h.status == "untested" and not h.note:
                h.note = (
                    "untested: " + (block if block else "no simulation model was in play" if model is None else
                                    "not run (experiment budget or no validated perturbation)")
                )

        judge_meta: Dict[str, Any] = {"llm": 0, "deterministic": 0, "matches": []}
        await self._tournament(state, question, hypotheses, judge_meta)

        top = max(hypotheses, key=lambda h: (h.elo, self._det_score(h)))
        evolved = await self._evolve(state, question, top, hypotheses, info, tested_set, llm_meta)
        if evolved is not None:
            self._score_evidence(state, [evolved])
            if can_test and baseline is not None and experiments < MAX_EXPERIMENTS and evolved.perturbation() not in tested_set:
                tested_set.add(evolved.perturbation())
                experiments += await self._experiment(state, model, info, baseline, evolved)  # type: ignore[arg-type]
            if evolved.status == "untested" and not evolved.note:
                evolved.note = "untested: " + (block or ("no simulation model was in play" if model is None else "experiment budget used"))
            others = list(hypotheses)
            hypotheses.append(evolved)
            for other in others:
                await self._match(state, question, evolved, other, judge_meta)

        ranked = sorted(hypotheses, key=lambda h: (-h.elo, -self._det_score(h), h.id))
        ranked = dedupe_ranked(ranked, rejected)
        for i, h in enumerate(ranked, 1):
            h.rank = i

        if model is None:
            notes.append("No simulation model was in play, so these hypotheses are ranked on the retrieved evidence only; none was tested in silico.")
        judges = [k for k in ("llm", "deterministic") if judge_meta[k]]
        artifact.update(
            experiments_run=experiments,
            epsilon_rule="eps = 1e-9 x max(|baseline|, |perturbed|)",
            judge="mixed" if len(judges) > 1 else (judges[0] if judges else "none"),
            hypotheses=[h.to_artifact() for h in ranked][:MAX_HYPOTHESES + 1],
            rejected=rejected[:10],
            matches=judge_meta["matches"][:20],
            note=" ".join(notes) or (
                f"{experiments} perturbation(s) simulated against an unperturbed baseline; support means the predicted "
                f"direction with |log2FC| >= {LOG2FC_THRESHOLD}. In-silico results are model predictions, not experimental evidence."
            ),
        )
        if baseline is not None:
            artifact["baseline"] = {"result_key": baseline.get("key"), "duration_seconds": baseline.get("duration")}
        state.artifacts["hypotheses"] = artifact
        self._publish(state, artifact, ranked, model)
        tested = sum(1 for h in ranked if h.status == "tested")
        supported = sum(1 for h in ranked if h.status == "tested" and (h.result or {}).get("supports"))
        await state.step(
            "agent_hypothesis",
            f"Ranked {len(ranked)} hypotheses" + (f"; {tested} tested in silico, {supported} supported" if tested else ""),
            "; ".join(f"#{h.rank} {truncate_words(h.statement, 90)}" for h in ranked[:3]),
            agent="BioDiscovery",
        )

    # -- model ----------------------------------------------------------------------------------------------
    async def _model_info(self, state: AgentState, model: ModelRef) -> ModelInfo:
        return await fetch_model_info(state, model, PHASE)

    def _choose_readout(self, question: str, info: ModelInfo, keys: List[str], baseline_final: Dict[str, Any]) -> Optional[str]:
        species = list(info.species) or list(baseline_final)
        if not species:
            return None
        for m in _READOUT_CUE_RE.finditer(question):
            r = info.resolve(m.group(1).strip(".,;:?)"))
            if r and r[1] == "species":
                return r[0]
        named = sorted((pos, s) for s in species if (pos := _mentions(question, s)) is not None)
        target = [s for s in species if _species_names_target(s, keys)]
        active = [s for s in target if re.search(r"(?:-?p{1,3}|gtp|\*|active)$", s, re.I)]
        if active:
            return active[0]
        if target:
            return target[0]
        if named:
            return named[-1][1]
        initial = info.species
        best, best_change = None, -1.0
        for s in species:
            f, i = _num(baseline_final.get(s)), _num(initial.get(s))
            if f is None or i is None:
                continue
            change = abs(f - i) / max(abs(f), abs(i), 1e-300)
            if change > best_change:
                best, best_change = s, change
        return best or species[0]

    # -- generation -----------------------------------------------------------------------------------------
    async def _generate(
        self, state: AgentState, question: str, model: Optional[ModelRef], info: ModelInfo, meta: Dict[str, Any]
    ) -> Tuple[List[Hypothesis], List[Dict[str, str]]]:
        rejected: List[Dict[str, str]] = []
        viv_final = _data(state.tool_results.get("vivarix")).get("final_concentrations") or {}
        readout = self._choose_readout(question, info, _target_keys(state.ctx), viv_final) if model is not None else None
        hyps: List[Hypothesis] = []
        if state.llm.available:
            try:
                hyps = await self._llm_generate(state, question, model, info, readout, rejected, meta)
            except (AgentLLMUnavailable, BudgetExhausted, ValueError) as exc:
                logger.info("Hypothesis generation model call failed: %s", exc)
                rejected.append({"hypothesis": "model generation", "reason": str(exc)[:160]})
        if hyps:
            meta["generator"] = "llm"
        elif model is not None and (info.parameters or info.species):
            hyps = self._deterministic_model_hypotheses(question, info, readout, _target_keys(state.ctx))
            meta["generator"] = "deterministic"
        if not hyps:
            hyps = self._evidence_hypotheses(state)
            if hyps:
                meta["generator"] = "evidence"
        for i, h in enumerate(hyps, 1):
            h.id = f"H{i}"
        return hyps[:MAX_HYPOTHESES], rejected

    def _deterministic_model_hypotheses(self, question: str, info: ModelInfo, readout: Optional[str], keys: List[str]) -> List[Hypothesis]:
        if readout is None:
            return []
        over = bool(_OVEREXPRESS_RE.search(question))
        picks: List[Tuple[str, str, str]] = []  # (override name, species|parameter, why)

        def add(name: str, kind: str, why: str) -> None:
            if name != readout and all(p[0] != name for p in picks):
                picks.append((name, kind, why))

        named: List[Tuple[int, str, str]] = []
        for s in info.species:
            pos = _mentions(question, s)
            if pos is not None:
                named.append((pos, s, "species"))
        shorts = info.short_names()
        for full, meta in info.parameters.items():
            for cand in {full, str(meta.get("short") or "")}:
                pos = _mentions(question, cand) if cand else None
                if pos is not None and (cand == full or len(shorts.get(cand.lower(), [])) == 1):
                    named.append((pos, full, "parameter"))
                    break
        for _pos, name, kind in sorted(named):
            add(name, kind, "named in the question")
        for s in info.species:
            if _species_names_target(s, keys):
                add(s, "species", "names the target gene")
        for rname, subs, prods in info.reactions:
            if readout in subs or readout in prods:
                for full, meta in info.parameters.items():
                    if meta.get("reaction") == rname or rname in (meta.get("reactions") or []):
                        add(full, "parameter", f"rate constant of {rname}, which {'produces' if readout in prods else 'consumes'} {readout}")
        if not picks:
            for full in list(info.parameters)[:3]:
                add(full, "parameter", "model rate constant")

        out: List[Hypothesis] = []
        for name, qkind, why in picks[: MAX_EXPERIMENTS - 1]:
            kind = "overexpression" if over else ("knockdown" if qkind == "species" else "inhibition")
            factor = DEFAULT_FACTORS[kind]
            direction, basis = self._prior_direction(name, qkind, readout, info)
            if kind == "overexpression":
                direction = "increase" if direction == "decrease" else "decrease"
            verb = {"knockdown": "Knockdown", "overexpression": "Overexpression", "inhibition": "Inhibition"}[kind]
            what = f"of {name}" + (" (initial amount)" if qkind == "species" else "")
            out.append(
                Hypothesis(
                    id="",
                    statement=f"{verb} {what} (x{factor:g}) will {direction} the {readout} endpoint.",
                    mechanism=f"{name} {why}; {basis}.",
                    kind=kind, parameter=name, factor=factor, readout=readout, prediction=direction,
                    source="deterministic", prediction_basis=basis,
                )
            )
        return out

    @staticmethod
    def _prior_direction(name: str, qkind: str, readout: str, info: ModelInfo) -> Tuple[str, str]:
        """Predicted effect of lowering `name` on the readout, from the reaction schemes (first-order reasoning)."""
        if qkind == "parameter":
            meta = info.parameters.get(name, {})
            reactions = [meta.get("reaction")] + list(meta.get("reactions") or [])
            for rname, subs, prods in info.reactions:
                if rname in reactions:
                    if readout in prods:
                        return "decrease", f"lowering the rate of {rname}, which produces {readout}, should lower it"
                    if readout in subs:
                        return "increase", f"lowering the rate of {rname}, which consumes {readout}, should raise it"
            return "decrease", "default prior: the perturbed rate is assumed to drive the readout"
        for rname, subs, prods in info.reactions:
            if name in subs and readout in prods:
                return "decrease", f"{name} is a substrate of {rname}, which produces {readout}"
        return "decrease", f"default prior: less {name} is assumed to lower {readout}"

    def _evidence_hypotheses(self, state: AgentState) -> List[Hypothesis]:
        """Untested hypotheses from the retrieved evidence: resistance or combination hypotheses when the question
        is about those and a retrieved passage names the same variant/drugs, then target-disease association
        hypotheses (one per distinct disease). Nothing is stated that the evidence does not name; no mechanism is
        invented, so fewer hypotheses is the expected outcome for thin evidence."""
        ctx = state.ctx
        target = getattr(ctx, "primary_symbol", None)
        question = state.clean_query or state.query or ""
        out: List[Hypothesis] = self._question_specific_hypotheses(state, question, target)
        if not target:
            return out
        if is_loss_of_function_target(target, question):
            # BRCA2 loss, TP53-mutant, PTEN-deficient: the gene's product is already lost, so "inhibit the gene" is
            # backwards. The tractable hypotheses target its synthetic-lethal partners with screen/experimental support.
            return out + self._synthetic_lethal_hypotheses(state, target)[: max(0, 3 - len(out))]
        syn = _data(state.tool_results.get("synapta"))
        assocs = [a for a in (syn.get("associations") or []) if isinstance(a, dict) and a.get("disease_name")]
        # The question's own disease first, then the strongest associations; near-identical diseases only once.
        asked = [str(getattr(d, "mention", "") or getattr(d, "label", "")) for d in list(getattr(ctx, "diseases", []) or [])]
        asked_keys = [_disease_key(d) for d in asked if d]
        assocs.sort(key=lambda a: (not any(_same_disease(_disease_key(str(a["disease_name"])), k) for k in asked_keys),
                                   -(_num(a.get("overall_association_score")) or 0.0)))
        if asked_keys:
            # The question names its disease: a hypothesis about another one (KRAS in Noonan syndrome for an NSCLC
            # question) answers nothing that was asked.
            assocs = [a for a in assocs if any(_same_disease(_disease_key(str(a["disease_name"])), k) for k in asked_keys)]
        used: List[set] = [_disease_key(h.readout or "") for h in out if h.readout]
        for a in assocs:
            if len(out) >= 3:
                break
            disease = str(a["disease_name"])
            key = _disease_key(disease)
            if any(_same_disease(key, u) for u in used):
                continue
            used.append(key)
            score = _num(a.get("overall_association_score"))
            out.append(
                Hypothesis(
                    id="",
                    statement=f"{target} is a candidate therapeutic target in {disease}; inhibiting it is predicted to slow disease progression.",
                    mechanism=f"OpenTargets associates {target} with {disease}" + (f" (overall score {score:.2f})" if score is not None else "")
                    + "; the association is not evidence that inhibition works.",
                    kind="inhibition", parameter=target, factor=None, readout=f"{disease} phenotype", prediction="decrease",
                    source="evidence", prediction_basis="target-disease association (not a causal or therapeutic estimate)",
                    evidence_keys=["synapta"], evidence_score=score or 0.0,
                )
            )
        return out

    def _synthetic_lethal_hypotheses(self, state: AgentState, target: str) -> List[Hypothesis]:
        """"Inhibiting PARP1 is predicted to be selectively lethal in BRCA2-deficient <disease>" for each SynLethDB
        partner with screen or experimental evidence: partners with an approved drug acting on them first (the drug
        named in the rationale), then the rest, best evidence first. None without such evidence: nothing is invented.
        A tumour-suppressor partner is never proposed for inhibition."""
        ctx = state.ctx
        diseases = [str(getattr(d, "mention", "") or getattr(d, "label", "")) for d in list(getattr(ctx, "diseases", []) or [])]
        setting = f"{target}-deficient {diseases[0]}" if diseases and diseases[0] else f"{target}-deficient cells"
        data = _data(state.tool_results.get("synlethdb"))
        items = [p for p in (data.get("items") or [])
                 if isinstance(p, dict) and p.get("partner") and p.get("experimental")
                 and str(p["partner"]).upper() not in TUMOUR_SUPPRESSORS and str(p["partner"]).upper() != (target or "").upper()]
        druggable = {str(r["partner"]).upper(): r for r in data.get("druggable_partners") or []
                     if isinstance(r, dict) and r.get("partner") and r.get("drugs")}
        # Druggable partners first (in the enrichment's evidence order, including partners ranked below the shown
        # list), then the other experimental partners.
        by_name = {str(p["partner"]).upper(): p for p in items}
        ordered: List[Dict[str, Any]] = []
        for name, r in druggable.items():
            if name in TUMOUR_SUPPRESSORS or name == (target or "").upper():
                continue
            p = by_name.get(name) or {"partner": name, "experimental": True, "evidence_types": r.get("evidence") or [],
                                      "pmids": r.get("sl_pmids") or []}
            ordered.append(p)
        ordered += [p for p in items if str(p["partner"]).upper() not in druggable]
        out: List[Hypothesis] = []
        for p in ordered[:3]:
            partner = str(p["partner"])
            pmids = [str(x) for x in (p.get("pmids") or [])][:3]
            drugs = [d for d in (druggable.get(partner.upper()) or {}).get("drugs") or [] if isinstance(d, dict) and d.get("name")]
            drugs.sort(key=lambda d: d.get("interaction_class") != "inhibits")  # inhibitor-type first (stable)
            drug_note = ""
            if drugs:
                named = []
                for d in drugs[:2]:
                    fda = d.get("fda") or {}
                    when = f", FDA first approval {fda['first_approval_date']}" if fda.get("status") == "approved" and fda.get("first_approval_date") else ""
                    kind = ", ".join(d.get("interaction_types") or []) or "interaction type not stated"
                    named.append(f"{str(d['name']).lower()} ({kind}{when})")
                drug_note = f"; approved drug(s) acting on {partner} (DGIdb): {', '.join(named)}"
            out.append(
                Hypothesis(
                    id="",
                    statement=f"Inhibiting {partner} is predicted to be selectively lethal in {setting}.",
                    mechanism=f"SynLethDB lists {partner} as a synthetic-lethal partner of {target} with "
                    + (", ".join(p.get("evidence_types") or []) or "experimental") + " evidence"
                    + (f" (PMID {', '.join(pmids)})" if pmids else "") + drug_note
                    + "; a pair in screens is not proof of a clinical effect.",
                    kind="inhibition", parameter=partner, factor=None, readout=f"viability of {setting}",
                    prediction="decrease", source="evidence",
                    prediction_basis="synthetic-lethal pair from screens/experiments (not a therapeutic estimate)",
                    evidence_keys=["synlethdb"],
                    evidence_score=min(1.0, 0.5 + 0.1 * len(pmids) + (0.1 if drugs else 0.0)),
                )
            )
        return out

    def _question_specific_hypotheses(self, state: AgentState, question: str, target: Optional[str]) -> List[Hypothesis]:
        """Resistance and combination hypotheses, only when a retrieved passage names the same variant or drugs."""
        from .specialists import iter_passages, passage_source_key

        drugs = [str(getattr(d, "mention", "") or getattr(d, "label", "")) for d in list(getattr(state.ctx, "drugs", []) or [])]
        drugs = [d for d in dict.fromkeys(d.strip() for d in drugs) if d]
        variants = list(dict.fromkeys("".join(m.groups()) for m in _VARIANT_RE.finditer(question)))
        passages = [(passage_source_key(p, k), str(p.get("passage_text") or "")) for k, p in iter_passages(state.tool_results)]
        if not passages:
            return []

        def cite(*terms: str) -> Optional[Tuple[str, str]]:
            for key, text in passages:
                if all(_mentions(text, t) is not None for t in terms if t):
                    return key, text
            return None

        out: List[Hypothesis] = []
        gene = f"{target} " if target else ""
        if _RESISTANCE_RE.search(question):
            for variant in variants[:3]:
                for drug in drugs[:2] or [""]:
                    hit = cite(variant, drug) if drug else None
                    hit = hit if hit is not None and _RESISTANCE_RE.search(hit[1]) else None
                    if hit is None:
                        hit = cite(variant)
                        hit = hit if hit is not None and _RESISTANCE_RE.search(hit[1]) else None
                    if hit is None:
                        continue
                    what = f"resistance to {drug}" if drug and _mentions(hit[1], drug) is not None else "therapeutic resistance"
                    out.append(Hypothesis(
                        id="", statement=f"{gene}{variant} confers {what}.",
                        mechanism=f"A retrieved passage ({hit[0]}) links {variant} to resistance; the mechanism is not established by the retrieved evidence.",
                        kind=None, parameter=f"{gene}{variant}".strip(), factor=None, readout=what, prediction="increase",
                        source="evidence", prediction_basis=f"retrieved literature ({hit[0]}); untested",
                        evidence_keys=[hit[0]], evidence_score=0.6,
                    ))
                    break
        if _COMBINATION_RE.search(question):
            partners = drugs[1:3] if len(drugs) > 1 else ([target] if target and drugs else [])
            for partner in partners:
                hit = cite(drugs[0], partner)
                if hit is None:
                    continue
                out.append(Hypothesis(
                    id="", statement=f"Combining {drugs[0]} with {'inhibition of ' if partner == target else ''}{partner} will improve response over {drugs[0]} alone.",
                    mechanism=f"A retrieved passage ({hit[0]}) discusses {drugs[0]} together with {partner}; the size of any added benefit is not established here.",
                    kind=None, parameter=f"{drugs[0]} + {partner}", factor=None, readout="treatment response", prediction="increase",
                    source="evidence", prediction_basis=f"retrieved literature ({hit[0]}); untested",
                    evidence_keys=[hit[0]], evidence_score=0.5,
                ))
        seen: set = set()
        unique = []
        for h in out:
            if h.statement.lower() not in seen:
                seen.add(h.statement.lower())
                unique.append(h)
        return unique[:3]

    async def _llm_generate(
        self, state: AgentState, question: str, model: Optional[ModelRef], info: ModelInfo, readout: Optional[str],
        rejected: List[Dict[str, str]], meta: Dict[str, Any],
    ) -> List[Hypothesis]:
        system = (
            "[phase:hypothesis_generate] You are the Generation and Reflection agent of an AI co-scientist. Propose "
            "mechanistic perturbation hypotheses that an ODE model can test in silico. "
            + SPOTLIGHT_RULE
            + " Reply with JSON only: {\"reflection\": str, \"research_plan\": str (at most 5 sentences), "
            "\"hypotheses\": [{\"statement\", \"mechanism\", \"intervention\": {\"kind\": \"knockdown|overexpression|inhibition\", "
            "\"parameter\": <exact model species or parameter name>, \"factor\": number}, \"readout\": <exact model species>, "
            "\"prediction\": \"increase|decrease\"}]} with at most 4 hypotheses. Knockdown lowers a species' initial amount "
            "(factor 0.1), overexpression raises it (factor 10), inhibition lowers a rate constant (factor 0.1). Use only "
            "names from the model lists; do not repeat a perturbation from the exclusion list. "
            + GROUNDED_STATEMENT_RULE
        )
        lines = [f"Research question: {neutralise(question)[:1200]}"]
        if model is not None:
            lines.append(f"Model: {model.label}")
            lines.append("Species: " + ", ".join(list(info.species)[:40]))
            lines.append("Parameters: " + ", ".join(list(info.parameters)[:60]))
            if readout:
                lines.append(f"Suggested readout: {readout}")
            lines.append(f"Measurement: log2 fold-change of the readout endpoint vs an unperturbed baseline; support = predicted direction and |log2FC| >= {LOG2FC_THRESHOLD}.")
        else:
            lines.append("No simulation model is available: hypotheses will be ranked on the evidence only and stay untested.")
        lines.append("Exclusion list (already tested): none")
        lines.append("Evidence gathered so far:\n" + spotlight(self._evidence_digest(state), "pipeline_results", 2500))
        reply = await state.llm.json(system, "\n".join(lines), phase=PHASE)
        if not isinstance(reply, dict):
            raise ValueError("hypothesis generation reply was not a JSON object")
        meta["reflection"] = neutralise(str(reply.get("reflection") or ""))[:800]
        meta["research_plan"] = neutralise(str(reply.get("research_plan") or ""))[:800]
        out: List[Hypothesis] = []
        seen = set()
        for raw in (reply.get("hypotheses") or [])[:MAX_HYPOTHESES]:
            h, reason = self._validate(raw, model, info, source="llm")
            if h is None:
                rejected.append({"hypothesis": str((raw or {}).get("statement") if isinstance(raw, dict) else raw)[:200], "reason": reason})
                continue
            if h.perturbation() in seen and h.kind:
                rejected.append({"hypothesis": h.statement[:200], "reason": "repeats a perturbation already proposed"})
                continue
            seen.add(h.perturbation())
            out.append(h)
        return out

    def _validate(self, raw: Any, model: Optional[ModelRef], info: ModelInfo, source: str) -> Tuple[Optional[Hypothesis], str]:
        """Reflection step: a hypothesis is kept only if its perturbation and readout exist in the model."""
        if not isinstance(raw, dict):
            return None, "not an object"
        statement = neutralise(str(raw.get("statement") or "")).strip()[:400]
        prediction = str(raw.get("prediction") or "").strip().lower()
        if not statement or prediction not in ("increase", "decrease"):
            return None, "missing statement or prediction (increase|decrease)"
        iv = raw.get("intervention") if isinstance(raw.get("intervention"), dict) else {}
        kind = str(iv.get("kind") or "").strip().lower() or None
        if kind is not None and kind not in KINDS:
            return None, f"unknown intervention kind '{kind}'"
        param, readout = iv.get("parameter"), raw.get("readout")
        factor = _num(iv.get("factor"))
        mechanism = neutralise(str(raw.get("mechanism") or "")).strip()[:400]
        if model is None or info.source == "none":
            return Hypothesis(id="", statement=statement, mechanism=mechanism, kind=kind, parameter=str(param)[:120] if param else None,
                              factor=factor, readout=str(readout)[:120] if readout else None, prediction=prediction, source=source,
                              prediction_basis="model proposal"), ""
        if kind is None:
            return None, "no intervention kind"
        resolved = info.resolve(str(param) if param else None)
        if resolved is None:
            return None, f"'{str(param)[:60]}' is not a species or parameter of the model"
        r_out = info.resolve(str(readout) if readout else None)
        if r_out is None or r_out[1] != "species":
            return None, f"readout '{str(readout)[:60]}' is not a model species"
        if factor is None or factor <= 0 or abs(factor - 1.0) < 1e-9:
            factor = DEFAULT_FACTORS[kind]
        factor = min(1e3, max(1e-3, factor))
        if (kind == "overexpression") != (factor > 1.0):
            factor = DEFAULT_FACTORS[kind]
        return Hypothesis(id="", statement=statement, mechanism=mechanism, kind=kind, parameter=resolved[0], factor=factor,
                          readout=r_out[0], prediction=prediction, source=source, prediction_basis="model proposal"), ""

    @staticmethod
    def _evidence_digest(state: AgentState) -> str:
        parts: List[str] = []
        viv = _data(state.tool_results.get("vivarix"))
        if viv.get("final_concentrations"):
            finals = list(viv["final_concentrations"].items())[:10]
            parts.append("Baseline simulation endpoints: " + ", ".join(f"{k}={_num(v)}" for k, v in finals))
        syn = _data(state.tool_results.get("synapta"))
        for a in (syn.get("associations") or [])[:3]:
            if isinstance(a, dict):
                parts.append(f"Association: {a.get('disease_name')} score {a.get('overall_association_score')}")
        lit = _data(state.tool_results.get("lithoscribe"))
        for p in (lit.get("ranked_passages") or [])[:3]:
            if isinstance(p, dict):
                parts.append(f"Passage (PMID {p.get('pmid')}): {str(p.get('passage_text') or '')[:300]}")
        return "\n".join(parts) or "none"

    def _score_evidence(self, state: AgentState, hyps: List[Hypothesis]) -> None:
        texts: Dict[str, str] = {}
        for key in ("synapta", "lithoscribe", "websurfer"):
            data = _data(state.tool_results.get(key))
            if data:
                texts[key] = json.dumps(data, default=str).lower()
        for h in hyps:
            name = str(h.parameter or "").split(").")[-1].strip("()").lower()
            if len(name) < 3:
                continue
            hits = [k for k, t in texts.items() if name in t]
            for k in hits:
                if k not in h.evidence_keys:
                    h.evidence_keys.append(k)
            if h.source != "evidence":
                h.evidence_score = max(h.evidence_score, min(1.0, len(hits) / 3.0))

    # -- experiments ----------------------------------------------------------------------------------------
    @staticmethod
    def _duration(state: AgentState) -> Tuple[float, int]:
        viv = _data(state.tool_results.get("vivarix"))
        duration = _num(viv.get("duration_seconds")) or _num(getattr(state.ctx, "sim_duration", None)) or 60.0
        intervals = int(_num(viv.get("intervals")) or 40)
        return max(1.0, min(5000.0, duration)), max(5, min(500, intervals))

    async def _baseline(self, state: AgentState, model: ModelRef) -> Optional[Dict[str, Any]]:
        duration, intervals = self._duration(state)
        viv_res = state.tool_results.get("vivarix")
        viv = _data(viv_res)
        if viv.get("final_concentrations") and viv.get("model_id") == model.biomodel_id and not viv.get("applied_overrides"):
            return {"final": dict(viv["final_concentrations"]), "key": "vivarix", "duration": duration, "intervals": intervals}
        res = await state.call_tool(
            SIM_TOOL, phase=PHASE, purpose=f"baseline (unperturbed) simulation of {model.label} for hypothesis tests",
            store=False, biomodel_id=model.biomodel_id, duration=duration, intervals=intervals, **model.sim_kwargs,
        )
        key = state.add_result(state.next_key(PHASE, SIM_TOOL), res, phase=PHASE, purpose="hypothesis baseline simulation", render=False)
        data = _data(res)
        if not data.get("final_concentrations"):
            return None
        return {"final": dict(data["final_concentrations"]), "key": key, "duration": duration, "intervals": intervals}

    async def _experiment(self, state: AgentState, model: ModelRef, info: ModelInfo, baseline: Dict[str, Any], h: Hypothesis) -> int:
        """Run one perturbation; returns 1 when a simulation was attempted."""
        base_value = info.baseline_value(str(h.parameter))
        if base_value is None or not math.isfinite(base_value) or base_value <= 0:
            h.note = f"untested: the model value of {h.parameter} is {base_value!r}, so a fold perturbation is undefined"
            return 0
        if h.readout not in baseline["final"]:
            h.note = f"untested: the readout {h.readout} is not in the baseline simulation"
            return 0
        value = float(base_value) * float(h.factor or 1.0)
        res = await state.call_tool(
            SIM_TOOL, phase=PHASE, purpose=f"in-silico test of {h.id}: {h.kind} of {h.parameter} (x{h.factor:g})", store=False,
            biomodel_id=model.biomodel_id, duration=baseline["duration"], intervals=baseline["intervals"],
            parameter_overrides={str(h.parameter): value}, **model.sim_kwargs,
        )
        key = state.add_result(state.next_key(PHASE, SIM_TOOL), res, phase=PHASE, purpose=f"hypothesis {h.id} perturbation", render=False)
        data = _data(res)
        perturbed = _num((data.get("final_concentrations") or {}).get(h.readout))
        base = _num(baseline["final"].get(h.readout))
        if perturbed is None or base is None:
            h.status = "failed"
            h.note = "untested: the perturbed simulation failed (" + str(getattr(res, "error", "") or "no endpoint")[:160] + ")"
            h.result = {"result_key": key}
            return 1
        lfc, eps = log2_fold_change(base, perturbed)
        direction = "increase" if lfc > 0 else "decrease"
        supports = bool(abs(lfc) >= LOG2FC_THRESHOLD and direction == h.prediction)
        h.status = "tested"
        h.result = {
            "baseline": float(f"{base:.6g}"),
            "perturbed": float(f"{perturbed:.6g}"),
            "log2_fold_change": round(lfc, 4),
            "supports": supports,
            "epsilon": float(f"{eps:.3g}"),
            "result_key": key,
            "baseline_key": baseline.get("key"),
            "override": {str(h.parameter): float(f"{value:.6g}")},
        }
        h.evidence_keys = [key] + [k for k in h.evidence_keys if k != key]
        if not supports:
            h.note = (
                f"refuted in silico: |log2FC| {abs(lfc):.2f} is below {LOG2FC_THRESHOLD}" if abs(lfc) < LOG2FC_THRESHOLD
                else f"refuted in silico: the readout moved in the opposite direction ({direction})"
            )
        else:
            h.note = "supported in silico (model prediction, not experimental evidence)"
        return 1

    # -- ranking --------------------------------------------------------------------------------------------
    @staticmethod
    def _det_score(h: Hypothesis) -> float:
        lfc = abs(_num((h.result or {}).get("log2_fold_change")) or 0.0)
        if h.status == "tested":
            if (h.result or {}).get("supports"):
                return 2.0 + min(lfc, 5.0) / 5.0 + 0.05 * h.evidence_score
            return 0.5 * min(lfc, 5.0) / 5.0
        return 1.0 + 0.5 * min(1.0, h.evidence_score)

    async def _judge(self, state: AgentState, question: str, a: Hypothesis, b: Hypothesis, meta: Dict[str, Any]) -> Tuple[float, str, str]:
        """(score for a, judge, reason): pairwise model judge when available, else effect size and evidence."""
        if state.llm.available and meta["llm"] < MAX_JUDGE_CALLS:
            system = (
                "[phase:hypothesis_judge] You are the Ranking agent of an AI co-scientist running an Elo tournament. "
                "Compare hypotheses A and B for the research question: prefer the one better supported by its in-silico "
                "result and the evidence, then mechanistic plausibility and testability. An untested hypothesis has no "
                "result; never treat it as confirmed. "
                + SPOTLIGHT_RULE
                + " Reply with JSON only: {\"winner\": \"A\"|\"B\", \"reason\": str}."
            )
            user = "\n".join(
                [
                    f"Research question: {neutralise(question)[:800]}",
                    "A: " + json.dumps(self._judge_view(a), default=str),
                    "B: " + json.dumps(self._judge_view(b), default=str),
                ]
            )
            try:
                reply = await state.llm.json(system, user, phase=PHASE, retries=0)
                winner = str((reply or {}).get("winner") or "").strip().upper() if isinstance(reply, dict) else ""
                if winner in ("A", "B"):
                    meta["llm"] += 1
                    return (1.0 if winner == "A" else 0.0), "llm", neutralise(str(reply.get("reason") or ""))[:200]
            except (AgentLLMUnavailable, BudgetExhausted, ValueError) as exc:
                logger.info("Hypothesis judge call failed: %s", exc)
        meta["deterministic"] += 1
        sa, sb = self._det_score(a), self._det_score(b)
        if abs(sa - sb) < 1e-9:
            return 0.5, "deterministic", "equal effect size and evidence"
        return (1.0 if sa > sb else 0.0), "deterministic", "larger supported effect / more evidence"

    @staticmethod
    def _judge_view(h: Hypothesis) -> Dict[str, Any]:
        view = h.to_artifact()
        return {k: view[k] for k in ("statement", "mechanism", "intervention", "readout", "prediction", "status", "result", "note")}

    async def _match(self, state: AgentState, question: str, a: Hypothesis, b: Hypothesis, meta: Dict[str, Any]) -> None:
        score, judge, reason = await self._judge(state, question, a, b, meta)
        a.elo, b.elo = elo_update(a.elo, b.elo, score)
        meta["matches"].append(
            {"a": a.id, "b": b.id, "winner": a.id if score == 1.0 else b.id if score == 0.0 else "draw", "judge": judge, "reason": reason}
        )

    async def _tournament(self, state: AgentState, question: str, hyps: List[Hypothesis], meta: Dict[str, Any]) -> None:
        for i in range(len(hyps)):
            for j in range(i + 1, len(hyps)):
                await self._match(state, question, hyps[i], hyps[j], meta)

    # -- evolution ------------------------------------------------------------------------------------------
    async def _evolve(
        self, state: AgentState, question: str, top: Hypothesis, hyps: List[Hypothesis], info: ModelInfo,
        tested: set, meta: Dict[str, Any],
    ) -> Optional[Hypothesis]:
        model = model_in_play(state.ctx, state.tool_results)
        if top.kind is None or top.parameter is None or top.source == "evidence":
            return None
        excluded = {h.perturbation() for h in hyps} | set(tested)
        child: Optional[Hypothesis] = None
        if state.llm.available:
            system = (
                "[phase:hypothesis_evolve] You are the Evolution agent of an AI co-scientist. Refine the top-ranked "
                "hypothesis once in light of its in-silico result. Reply with JSON only: {\"hypothesis\": {\"statement\", "
                "\"mechanism\", \"intervention\": {\"kind\", \"parameter\", \"factor\"}, \"readout\", \"prediction\"}}. "
                "Do not repeat a perturbation from the exclusion list. "
                + GROUNDED_STATEMENT_RULE
            )
            user = "\n".join(
                [
                    f"Research question: {neutralise(question)[:800]}",
                    "Top hypothesis: " + json.dumps(self._judge_view(top), default=str),
                    "Exclusion list: " + json.dumps(sorted(list(p) for p in excluded)),
                ]
            )
            try:
                reply = await state.llm.json(system, user, phase=PHASE, retries=0)
                raw = reply.get("hypothesis") if isinstance(reply, dict) else None
                child, _reason = self._validate(raw, model, info, source="evolved")
            except (AgentLLMUnavailable, BudgetExhausted, ValueError) as exc:
                logger.info("Hypothesis evolution call failed: %s", exc)
        if child is None or child.perturbation() in excluded:
            child = self._deterministic_evolution(top)
        if child is None or child.perturbation() in excluded:
            return None
        child.id = f"H{len(hyps) + 1}"
        child.parent = top.id
        child.source = "evolved"
        return child

    @staticmethod
    def _deterministic_evolution(top: Hypothesis) -> Optional[Hypothesis]:
        result = top.result or {}
        lfc = _num(result.get("log2_fold_change"))
        factor = float(top.factor or DEFAULT_FACTORS.get(str(top.kind), 0.1))
        kind, prediction = str(top.kind), top.prediction
        if top.status == "tested" and result.get("supports"):
            factor, why = math.sqrt(factor), "dose-response check: does a partial perturbation still move the readout?"
        elif top.status == "tested" and lfc is not None and abs(lfc) >= LOG2FC_THRESHOLD:
            kind = "overexpression" if kind in ("knockdown", "inhibition") else "knockdown" if top.kind == "overexpression" else kind
            factor = DEFAULT_FACTORS[kind]
            prediction = "decrease" if lfc > 0 else "increase"
            why = "revised direction: the opposite perturbation should reverse the measured effect"
        elif top.status == "tested":
            factor = min(1e3, max(1e-3, factor * factor))
            why = "stronger perturbation: is the readout insensitive or was the perturbation too weak?"
        else:
            return None
        return Hypothesis(
            id="",
            statement=f"{kind.capitalize()} of {top.parameter} (x{factor:.3g}) will {prediction} the {top.readout} endpoint.",
            mechanism=f"Refinement of {top.id}: {why}",
            kind=kind, parameter=top.parameter, factor=round(factor, 6), readout=top.readout, prediction=prediction,
            source="evolved", prediction_basis=why,
        )

    # -- output ---------------------------------------------------------------------------------------------
    def _publish(self, state: AgentState, artifact: Dict[str, Any], ranked: List[Hypothesis], model: Optional[ModelRef]) -> None:
        from ..tools.registry import ToolResult

        summary = {k: v for k, v in artifact.items() if k not in ("matches", "rejected")}
        state.add_result(
            "agent.hypothesis.summary",
            ToolResult(success=True, tool_name="hypothesis_experiments", data=summary, metadata={"adapter": "AGENT"}),
            phase=PHASE,
            purpose="ranked hypotheses and in-silico experiment results",
            render=False,
        )

        def attr(v: Any) -> str:
            return escape("" if v is None else str(v), quote=True)

        lines = [
            f'<hypothesis_experiments model="{attr(model.label if model else "none")}" method="elo-tournament" '
            f'threshold_log2fc="{LOG2FC_THRESHOLD}" epsilon_rule="1e-9 x max(|baseline|, |perturbed|)" '
            f'experiments_run="{artifact.get("experiments_run", 0)}" result_key="agent.hypothesis.summary" '
            f'note="in-silico results are model predictions, not experimental evidence; untested hypotheses have no result">'
        ]
        for h in ranked:
            r = h.result or {}
            fields = {
                "id": h.id, "rank": h.rank, "elo": round(h.elo, 1), "status": h.status,
                "intervention": h.kind, "parameter": h.parameter, "factor": h.factor, "readout": h.readout,
                "prediction": h.prediction,
            }
            if h.status == "tested":
                fields.update(
                    baseline=r.get("baseline"), perturbed=r.get("perturbed"), log2_fold_change=r.get("log2_fold_change"),
                    supports="true" if r.get("supports") else "false", result_key=r.get("result_key"),
                )
            rendered = " ".join(f'{k}="{attr(v)}"' for k, v in fields.items() if v is not None)
            body = escape(h.statement, quote=False) + (f" [{escape(h.note, quote=False)}]" if h.note else "")
            lines.append(f"<hypothesis {rendered}>{body}</hypothesis>")
        lines.append("</hypothesis_experiments>")
        state.evidence_sections.append("\n".join(lines))
        state.brief_notes.append(
            "Hypotheses: present the ranked hypotheses from <hypothesis_experiments>. For a tested one, give the "
            "baseline and perturbed endpoint and the log2 fold-change exactly as listed and call it an in-silico model "
            "prediction; say 'untested' for any hypothesis without a result."
        )


register_phase(HypothesisPhase())
