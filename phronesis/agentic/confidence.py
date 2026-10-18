"""
Per-claim evidence-support score (feature 9), stage finalize, artifact "claim_confidence".

What the number is (AGT-001). By default this is a HEURISTIC evidence-support score, not a calibrated
probability: the weights are hand-set priors (DEFAULT_WEIGHTS) and no labelled set of real pipeline claims has
been used to fit or check them. The artifact says so (`calibration.status` "heuristic", `calibration.calibrated`
false, `label` "Heuristic evidence-support score", method prefix "heuristic-"). It is reported as calibrated only
when the calibration file was produced by scripts/calibrate_confidence.py from a real, human-labelled claim set
of at least MIN_LABELLED_CLAIMS claims and reports an out-of-sample (held-out) ECE (`status: "fitted"`).

Claims come from the verifier report (state.verification.claims), else from the answer's sentences
(FActScore-style sentence-level atomic claims, Min et al., EMNLP 2023, arXiv:2305.14251). Each claim gets a
feature vector x and

    score = sigmoid((w . x + b) / T)

with documented default weights (DEFAULT_WEIGHTS, below; hand-set priors, not fitted), then optional temperature
scaling (Guo et al., ICML 2017, arXiv:1706.04599). T comes from PHRONESIS_CONFIDENCE_T, else from the JSON
file at PHRONESIS_CONFIDENCE_CALIBRATION (else the packaged phronesis/config/confidence_calibration.json), else 1.0.

Features
- nli_entailment, nli_contradiction: from state.bag["nli_scores"] (feature 8: SciFact labels, Wadden et al.
  arXiv:2004.14974; chunk-max scoring after SummaC arXiv:2111.09525 and MiniCheck arXiv:2404.10774). With a
  local cross-encoder, sentences the verifier did not judge are scored here against cited + BM25 passages.
- sources: log(1 + number of independent supporting source keys) (NLI-entailing passages plus the verifier's
  supporting evidence key).
- grade: best evidence-grade weight of the supporting passages (lithoscribe evidence grading policy weight;
  curated tool records 0.5, web 0.1, ungraded passages 0.25).
- conflict: the claim matches a "contradicts" row of state.artifacts["evidence_conflicts"] (feature 7).
- defect: the verifier marked the claim contradicted/unsupported or reported a defect on it.
- verifier_supported: the deterministic audit found support (term and figure overlap, so a weak signal alone).
- exact_match: the claim names an identifier (PMID/NCT/DOI) of its supporting source and every figure in the claim
  appears in that source's text.
- hedge: the sentence is hedged (may, might, suggests, ...).

Bands: high >= 0.75, medium >= 0.45, else low. "High" needs strong support: an NLI entailment at or above the
entailment threshold, or an exact identifier+figure match; without either a claim is capped just below the high band
(term-overlap support alone stays medium). When a back-off threshold tau is configured, claims with score <= tau are
marked low (the back-off set). It is called a split-conformal threshold (Mohri & Hashimoto, ICML 2024,
arXiv:2402.10978; Quach et al., ICLR 2024, arXiv:2306.10193) and tagged "+conformal-backoff" only when it was
computed per answer on a real labelled calibration split and its coverage was checked on held-out answers
(AGT-002); any other threshold is tagged "+threshold-backoff(unvalidated)", and a threshold below the medium band,
which cannot change any band, is not advertised at all. answer_confidence is the claim-type-weighted mean.

Optional stated confidence (PHRONESIS_CONFIDENCE_ELICIT=auto|off, default auto): when the agent model is
available and the budget allows, one batched "[phase:confidence]" call asks for each claim's P(True)
(Kadavath et al. 2022, arXiv:2207.05221; verbalised confidence after Xiong et al., ICLR 2024,
arXiv:2306.13063), blended in at weight 0.2.

Future work: semantic entropy (Farquhar et al., Nature 2024, doi:10.1038/s41586-024-07421-0) clusters N
sampled answers by bidirectional entailment (the feature-8 NLI model could do the clustering); it needs N
extra samples per question, so it is not run here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .budget import BudgetExhausted
from .calibration_stats import logistic
from .config import env_flag
from .guardrails import SPOTLIGHT_RULE, neutralise, spotlight
from .llm import AgentLLMUnavailable
from ..phronesis.agents.verifier import truncate_words
from .nli import claim_key, collect_documents, find_scores, score_texts, thresholds  # noqa: F401 - claim_key re-exported
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import AgentState

logger = logging.getLogger(__name__)

MAX_CLAIMS = 50
HIGH_BAND = 0.75
MEDIUM_BAND = 0.45
STATED_WEIGHT = 0.2

# A calibration file counts as calibrated only when it was fitted on at least this many real, human-labelled
# claims (from at least MIN_LABELLED_ANSWERS answers) and reports held-out metrics on at least MIN_HELDOUT_CLAIMS
# of them. scripts/calibrate_confidence.py enforces the same minimums.
MIN_LABELLED_CLAIMS = 300
MIN_LABELLED_ANSWERS = 60
MIN_HELDOUT_CLAIMS = 90
HEURISTIC_LABEL = "Heuristic evidence-support score"
CALIBRATED_LABEL = "Calibrated claim confidence"

# Default weights (log-odds per unit of feature). Hand-set priors so that a claim with no evidence signal sits
# just below 0.5, a claim the deterministic audit grounds in one graded source lands in the medium band (~0.65-0.7),
# one that also matches its source's identifier and figures exactly near 0.85, an NLI-entailed claim from several
# sources above 0.9, and a contradicted or defective claim below 0.2. Override them with
# "weights" in the calibration file after fitting on labelled claims.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "bias": -0.5,
    "nli_entailment": 3.0,  # applied to (entailment - 0.5); 0 when no NLI score
    "nli_contradiction": -3.0,
    "sources": 0.8,  # applied to log(1 + n)
    "grade": 1.0,  # best grade weight in [0, 1]
    "conflict": -1.5,
    "defect": -2.0,
    "verifier_supported": 0.4,
    "exact_match": 1.0,
    "hedge": -0.5,
}
# Below the high band: the most a claim without strong support (NLI entailment or an exact match) can score.
WEAK_SUPPORT_CAP = 0.74

CLAIM_TYPE_WEIGHTS: Dict[str, float] = {
    "clinical": 1.5,
    "mechanistic": 1.2,
    "computational": 1.0,
    "absence": 0.6,
    "statement": 0.8,
}

_HEDGE_RE = re.compile(
    r"\b(?:may|might|could|possibly|possible|perhaps|likely|unlikely|suggests?|suggested|suggesting|appears?|"
    r"seems?|potential(?:ly)?|preliminary|putative|presumably|hypothes\w*|uncertain|unclear|speculative|"
    r"tentative(?:ly)?|limited evidence)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "were", "was", "from", "have", "has", "been", "are", "its", "not"}

_TOOL_GRADE = 0.5
_WEB_GRADE = 0.1
_UNGRADED = 0.25


# Calibration settings ---------------------------------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def fitted_on_real_labels(data: Dict[str, Any]) -> bool:
    """True when a calibration file documents a fit on a real labelled claim set with held-out evaluation."""
    if not isinstance(data, dict) or data.get("status") != "fitted":
        return False
    prov = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    heldout = ((data.get("evaluation") or {}).get("heldout") or {}) if isinstance(data.get("evaluation"), dict) else {}
    n_claims = _num(prov.get("n_claims")) or 0
    n_answers = _num(prov.get("n_answers")) or 0
    n_heldout = _num(heldout.get("n_claims")) or 0
    return (
        bool(prov.get("labelled_set"))
        and not prov.get("synthetic", False)
        and n_claims >= MIN_LABELLED_CLAIMS
        and n_answers >= MIN_LABELLED_ANSWERS
        and n_heldout >= MIN_HELDOUT_CLAIMS
        and _num(heldout.get("ece")) is not None
    )


def conformal_validated(data: Dict[str, Any]) -> bool:
    """True when the file's back-off threshold was computed per answer and its held-out coverage was checked."""
    if not fitted_on_real_labels(data):
        return False
    conformal = data.get("conformal") if isinstance(data.get("conformal"), dict) else {}
    return bool(conformal.get("grouped")) and bool(conformal.get("heldout_coverage_ok"))


def load_calibration() -> Dict[str, Any]:
    """{"temperature", "conformal_threshold", "weights", "source", "status", "calibrated", "conformal_validated"}
    from env / the calibration file / defaults.

    status: "heuristic" (hand-set weights, nothing fitted), "fitted" (fitted on a real labelled set with held-out
    evaluation: the only status with calibrated=True) or "unverified" (a file that sets values without that
    provenance, or an operator-set PHRONESIS_CONFIDENCE_T)."""
    out: Dict[str, Any] = {
        "temperature": 1.0,
        "conformal_threshold": None,
        "weights": dict(DEFAULT_WEIGHTS),
        "source": "default",
        "status": "heuristic",
        "calibrated": False,
        "conformal_validated": False,
    }
    path = (os.getenv("PHRONESIS_CONFIDENCE_CALIBRATION", "") or "").strip()
    is_explicit = bool(path)
    if not path:
        default_file = Path(__file__).resolve().parent.parent / "config" / "confidence_calibration.json"
        if default_file.is_file():
            path = str(default_file)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                t = data.get("temperature")
                if isinstance(t, (int, float)) and math.isfinite(t) and t > 0:
                    out["temperature"] = float(t)
                tau = data.get("conformal_threshold")
                if isinstance(tau, (int, float)) and math.isfinite(tau):
                    out["conformal_threshold"] = float(tau)
                if isinstance(data.get("weights"), dict):
                    for k, v in data["weights"].items():
                        if k in DEFAULT_WEIGHTS and isinstance(v, (int, float)) and math.isfinite(v):
                            out["weights"][k] = float(v)
                out["source"] = "file" if is_explicit else "package_default"
                if fitted_on_real_labels(data):
                    out["status"], out["calibrated"] = "fitted", True
                    out["conformal_validated"] = conformal_validated(data) and out["conformal_threshold"] is not None
                elif data.get("status") == "heuristic" and out["temperature"] == 1.0 and out["conformal_threshold"] is None:
                    out["status"] = "heuristic"
                else:
                    out["status"] = "unverified"
        except (OSError, ValueError) as exc:
            logger.warning("Confidence calibration file %s unusable: %s", path, exc)
    env_t = (os.getenv("PHRONESIS_CONFIDENCE_T", "") or "").strip()
    if env_t:
        try:
            t = float(env_t)
            if math.isfinite(t) and t > 0:
                out["temperature"] = min(20.0, max(0.05, t))
                out["source"] = "env" if out["source"] == "default" else out["source"] + "+env"
                # An operator-set temperature was not fitted with the file's evaluation: not calibrated.
                out["status"], out["calibrated"], out["conformal_validated"] = "unverified", False, False
        except ValueError:
            pass
    return out


def band(confidence: float, conformal_threshold: Optional[float] = None) -> str:
    if conformal_threshold is not None and confidence <= conformal_threshold:
        return "low"
    if confidence >= HIGH_BAND:
        return "high"
    if confidence >= MEDIUM_BAND:
        return "medium"
    return "low"


def backoff_effective(tau: Optional[float]) -> bool:
    """A back-off threshold changes a band only when it reaches the medium band (scores <= tau below it are low anyway)."""
    return tau is not None and tau >= MEDIUM_BAND


def method_tag(cal: Dict[str, Any], stated: bool = False) -> str:
    """The artifact's method string: what was actually applied, and nothing that was not (AGT-001/002)."""
    parts = ["calibrated-logistic-features" if cal.get("calibrated") else "heuristic-logistic-features"]
    t = cal.get("temperature")
    if isinstance(t, (int, float)) and abs(float(t) - 1.0) > 1e-9:
        parts.append("temperature(fitted)" if cal.get("calibrated") else "temperature(unfitted)")
    if stated:
        parts.append(f"stated-p-true({STATED_WEIGHT})")
    tau = cal.get("conformal_threshold")
    if backoff_effective(tau):
        parts.append("conformal-backoff" if cal.get("conformal_validated") else "threshold-backoff(unvalidated)")
    return "+".join(parts)


def feature_vector(features: Dict[str, Any]) -> Dict[str, float]:
    """The numeric x the weights apply to (names match DEFAULT_WEIGHTS, without bias)."""
    e = features.get("nli_entailment")
    c = features.get("nli_contradiction")
    return {
        "nli_entailment": (float(e) - 0.5) if e is not None else 0.0,
        "nli_contradiction": float(c) if c is not None else 0.0,
        "sources": math.log1p(max(0, int(features.get("n_sources") or 0))),
        "grade": float(features.get("best_grade") or 0.0),
        "conflict": 1.0 if features.get("conflict") else 0.0,
        "defect": 1.0 if features.get("defect") else 0.0,
        "verifier_supported": 1.0 if features.get("verifier_status") == "supported" else 0.0,
        "exact_match": 1.0 if features.get("exact_match") else 0.0,
        "hedge": 1.0 if features.get("hedged") else 0.0,
    }


def strong_support(features: Dict[str, Any]) -> bool:
    """NLI entailment at or above the entailment threshold, or an exact identifier+figure match."""
    e = features.get("nli_entailment")
    if e is not None and float(e) >= thresholds()[0]:
        return True
    return bool(features.get("exact_match"))


def cap_confidence(confidence: float, features: Dict[str, Any]) -> float:
    """Keep a claim without strong support out of the high band."""
    return confidence if strong_support(features) else min(confidence, WEAK_SUPPORT_CAP)


def raw_logit(features: Dict[str, Any], weights: Optional[Dict[str, float]] = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    x = feature_vector(features)
    return float(w.get("bias", 0.0) + sum(w.get(k, 0.0) * v for k, v in x.items()))


def score(features: Dict[str, Any], temperature: float = 1.0, weights: Optional[Dict[str, float]] = None) -> float:
    return cap_confidence(float(logistic(raw_logit(features, weights) / max(temperature, 1e-6))), features)


# Claims and features ----------------------------------------------------------------------------------------
def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _tokens(text: str) -> set:
    return {w for w in _WORD_RE.findall(str(text or "").lower()) if w not in _STOP}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def gather_claims(state: AgentState) -> List[Dict[str, Any]]:
    """[{claim_id, text, claim_type, status, evidence_keys}] from the verifier report, else answer sentences."""
    out: List[Dict[str, Any]] = []
    for c in list(_get(state.verification, "claims", None) or [])[:MAX_CLAIMS]:
        text = str(_get(c, "claim_text", "") or "").strip()
        if not text or _get(c, "claim_type", "") == "operational":
            continue  # a statement about this run's tools, not a scientific claim with an evidence grade
        evidence = list(_get(c, "evidence", None) or [])
        keys = [str(_get(e, "source_key", "")) for e in evidence if _get(e, "source_key", "")]
        out.append(
            {
                "claim_id": str(_get(c, "claim_id", "") or f"c{len(out) + 1}"),
                "text": text,
                "claim_type": str(_get(c, "claim_type", "") or "statement"),
                "status": str(_get(c, "status", "") or "not_checked"),
                "evidence_keys": keys,
                "evidence_passages": {
                    str(_get(e, "source_key", "")): str(_get(e, "passage", "") or "") for e in evidence if _get(e, "source_key", "")
                },
            }
        )
    if out:
        return out
    from .nli import answer_sentences

    for i, s in enumerate(answer_sentences(state.draft or "", limit=MAX_CLAIMS), 1):
        out.append({"claim_id": f"s{i}", "text": s, "claim_type": "statement", "status": "not_checked", "evidence_keys": []})
    return out


def _grade_weight(doc: Dict[str, Any]) -> float:
    w = doc.get("grade_weight")
    if isinstance(w, (int, float)):
        return float(w)
    label = doc.get("grade_label")
    if label == "web":
        return _WEB_GRADE
    if label:
        try:
            from ..lithoscribe.evidence_grading import STUDY_TYPE_LABELS, STUDY_TYPE_POLICY

            for st, (lab, _s, _p) in STUDY_TYPE_LABELS.items():
                if str(lab).lower() == str(label).lower():
                    return float(STUDY_TYPE_POLICY[st][1])
        except Exception:  # noqa: BLE001
            pass
    return _UNGRADED


def source_grades(tool_results: Dict[str, Any]) -> Dict[str, float]:
    grades: Dict[str, float] = {}
    for d in collect_documents(tool_results):
        grades[d["source_key"]] = max(grades.get(d["source_key"], 0.0), _grade_weight(d))
    return grades


def _key_grade(key: str, grades: Dict[str, float]) -> float:
    if key in grades:
        return grades[key]
    if key.startswith("tool:"):
        return _TOOL_GRADE
    if key.startswith("web:"):
        return _WEB_GRADE
    return _UNGRADED


def _conflict_rows(state: AgentState) -> List[Dict[str, Any]]:
    """Contradicting counter-evidence rows from a model or NLI verdict. Rows rated by cue words only
    (classifier "heuristic") are left out (AGT-008): a lexical match is a possible conflict to check, not a
    verified contradiction, so it must not lower a claim's score as one (the escalation trigger excludes them too)."""
    art = state.artifacts.get("evidence_conflicts") if isinstance(state.artifacts, dict) else None
    rows = (art or {}).get("conflicts") if isinstance(art, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("classifier") == "heuristic":
            continue
        likert = r.get("likert")
        if r.get("label") == "contradicts" or (isinstance(likert, (int, float)) and likert >= 8):
            out.append(r)
    return out


_ID_IN_CLAIM_RE = re.compile(r"\bPMID:?\s*(\d{1,9})\b|\b(NCT\d{8})\b|\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)
_DOC_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\d])")


def exact_match(text: str, source_key: str, doc_text: str) -> bool:
    """The claim names its supporting source's identifier (PMID/NCT/DOI, as the source key or in the source text)
    and every figure it states appears in that source's text."""
    if not source_key or not doc_text:
        return False
    from ..phronesis.agents.verifier import bare_numbers

    low_doc = doc_text.lower()
    named = False
    for pmid, nct, doi in _ID_IN_CLAIM_RE.findall(text or ""):
        if pmid and (source_key == f"PMID:{pmid}" or re.search(rf"pmid\W{{0,4}}{pmid}(?!\d)", low_doc)):
            named = True
        elif nct and (nct.lower() in source_key.lower() or nct.lower() in low_doc):
            named = True
        elif doi and (doi.rstrip(".,;)").lower() in source_key.lower() or doi.rstrip(".,;)").lower() in low_doc):
            named = True
    if not named:
        return False
    stripped = re.sub(r"\b(?:grade|phase|stage|cycle)\s*[0-9IVX]+(?:\s*/\s*[0-9IVX]+)?\b", " ", text or "", flags=re.IGNORECASE)
    figures = bare_numbers(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", stripped))
    if not figures:
        return False
    doc_numbers = {float(m.group(1)) for m in _DOC_NUM_RE.finditer(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", low_doc))}
    def _matches_any(f_val: float) -> bool:
        if f_val in doc_numbers:
            return True
        num_str = f"{f_val:g}"
        d = len(num_str.split(".")[1]) if "." in num_str else 0
        return any(
            round(dn, d) == f_val or abs(dn - f_val) <= 0.55 * (10 ** -d) or (0.0 < dn <= 1.0 and round(dn * 100, d) == f_val)
            for dn in doc_numbers
        )
    return all(_matches_any(f) for f in figures)


def _defect_claims(state: AgentState) -> List[str]:
    return [str(_get(d, "claim", "") or "") for d in (_get(state.verification, "defects", None) or []) if _get(d, "claim", "")]


def claim_features(
    claim: Dict[str, Any],
    nli: Optional[Dict[str, Any]],
    grades: Dict[str, float],
    conflicts: List[Dict[str, Any]],
    defect_claims: List[str],
    docs_by_key: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    t_entail, _, _ = thresholds()
    text = claim["text"]
    status = claim.get("status") or "not_checked"
    supporting: List[str] = []
    if nli:
        supporting.extend(str(s) for s in nli.get("supporting_sources") or [])
        if not supporting and nli.get("best_source") and float(nli.get("entailment") or 0.0) >= t_entail:
            supporting.append(str(nli["best_source"]))
    if status == "supported":
        supporting.extend(claim.get("evidence_keys") or [])
    supporting = list(dict.fromkeys(k for k in supporting if k))
    low = text.lower()[:200]
    defect = status in ("contradicted", "unsupported") or any(
        low and (low in d.lower() or d.lower()[:200] in text.lower() or _overlap(text, d) >= 0.6) for d in defect_claims
    )
    conflict = any(_overlap(text, str(r.get("claim") or "")) >= 0.4 for r in conflicts) or any(
        r.get("source_key") and r.get("source_key") in supporting and _overlap(text, str(r.get("claim") or "")) >= 0.2 for r in conflicts
    )
    exact = False
    if status == "supported" and not defect:
        passages = claim.get("evidence_passages") or {}
        for key in claim.get("evidence_keys") or []:
            doc = " ".join(t for t in ((docs_by_key or {}).get(key, ""), passages.get(key, "")) if t)
            if exact_match(text, key, doc):
                exact = True
                break
    return {
        "nli_entailment": round(float(nli["entailment"]), 4) if nli else None,
        "nli_contradiction": round(float(nli["contradiction"]), 4) if nli else None,
        "nli_backend": nli.get("backend") if nli else None,
        "n_sources": len(supporting),
        "supporting_sources": supporting[:8],
        "best_grade": round(max((_key_grade(k, grades) for k in supporting), default=0.0), 3),
        "conflict": bool(conflict),
        "defect": bool(defect),
        "verifier_status": status,
        "exact_match": exact,
        "hedged": bool(_HEDGE_RE.search(text)),
    }


# Stated confidence (P(True)) ---------------------------------------------------------------------------------
CONFIDENCE_SYSTEM_PROMPT = (
    "[phase:confidence] You estimate how likely each numbered claim from a research answer is to be true, "
    "given the evidence excerpt shown with it (P(True), as in Kadavath et al. 2022). Give a calibrated "
    "probability between 0 and 1: 0.5 means you cannot tell, values near 1 only when the excerpt clearly states "
    f"the claim. {SPOTLIGHT_RULE} "
    'Reply with JSON only: {"claims": [{"id": "<claim id>", "p_true": 0.0-1.0}]}'
)


def _elicit_allowed(state: AgentState) -> bool:
    if env_flag("PHRONESIS_CONFIDENCE_ELICIT", "auto") == "off":
        return False
    b = state.budget
    if b.exhausted() or b.llm_calls + 1 > b.max_llm_calls:
        return False
    try:
        return bool(state.llm.available)
    except Exception:  # noqa: BLE001
        return False


async def stated_confidence(state: AgentState, rows: List[Dict[str, Any]], excerpts: Dict[str, str]) -> Dict[str, float]:
    """One batched model call: claim id -> stated P(True). Empty on any failure."""
    lines = []
    for r in rows:
        ex = excerpts.get(r["claim_id"], "")
        lines.append(
            f'<claim id="{r["claim_id"]}">{neutralise(r["text"])[:400]}</claim>'
            + (f"\n{spotlight(ex, 'evidence excerpt', 400)}" if ex else "\n(no evidence excerpt)")
        )
    user = "Claims:\n\n" + "\n\n".join(lines)
    try:
        data = await state.llm.json(CONFIDENCE_SYSTEM_PROMPT, user, phase="confidence", retries=0)
    except (BudgetExhausted, AgentLLMUnavailable, ValueError) as exc:
        logger.info("Stated confidence unavailable: %s", exc)
        return {}
    items = data.get("claims") if isinstance(data, dict) else data if isinstance(data, list) else None
    out: Dict[str, float] = {}
    ids = {r["claim_id"] for r in rows}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("id") or it.get("claim_id") or "")
        try:
            p = float(it.get("p_true", it.get("confidence")))
        except (TypeError, ValueError):
            continue
        if cid in ids and math.isfinite(p):
            out[cid] = min(1.0, max(0.0, p))
    return out


# Phase -------------------------------------------------------------------------------------------------------
def _summarise(
    rows: List[Dict[str, Any]], stated: Dict[str, float], cal: Dict[str, Any], method: str, nli_backend: str
) -> Dict[str, Any]:
    """The claim_confidence artifact from scored rows, blending in stated P(True) where present."""
    tau = cal["conformal_threshold"]
    t_entail, _, _ = thresholds()
    total_w = 0.0
    acc = 0.0
    supported = 0
    backed_off = 0
    out_rows: List[Dict[str, Any]] = []
    for base in rows:
        r = {**base, "features": dict(base["features"])}
        conf = r["confidence"]
        if r["claim_id"] in stated:
            r["features"]["stated_p_true"] = round(stated[r["claim_id"]], 4)
            conf = (1 - STATED_WEIGHT) * conf + STATED_WEIGHT * stated[r["claim_id"]]
        conf = cap_confidence(conf, r["features"])
        r["confidence"] = round(min(1.0, max(0.0, conf)), 4)
        r["band"] = band(r["confidence"], tau)
        r["backed_off"] = r["band"] == "low" and band(r["confidence"]) != "low"
        backed_off += int(r["backed_off"])
        w = CLAIM_TYPE_WEIGHTS.get(r["claim_type"], CLAIM_TYPE_WEIGHTS["statement"])
        total_w += w
        acc += w * r["confidence"]
        f = r["features"]
        if f["verifier_status"] == "supported" or (f["nli_entailment"] is not None and f["nli_entailment"] >= t_entail):
            supported += 1
        out_rows.append(r)
    calibrated = bool(cal.get("calibrated"))
    validated = bool(cal.get("conformal_validated"))
    return {
        "answer_confidence": round(acc / total_w, 4) if total_w else None,
        "method": method,
        "label": CALIBRATED_LABEL if calibrated else HEURISTIC_LABEL,
        "calibration": {
            "temperature": cal["temperature"],
            # Reported as a conformal threshold only when it was validated as one (AGT-002); see "backoff".
            "conformal_threshold": tau if validated else None,
            "source": cal["source"],
            "status": cal.get("status", "heuristic"),
            "calibrated": calibrated,
            "label": CALIBRATED_LABEL if calibrated else HEURISTIC_LABEL,
            "note": (
                "Fitted on a real labelled claim set; see the calibration file for its held-out ECE."
                if calibrated else
                "Hand-set evidence weights, not fitted to labelled outcomes: a ranking of evidence support, not a "
                "probability that the claim is true."
            ),
            "stated_weight": STATED_WEIGHT if stated else 0.0,
            "bands": {"high": HIGH_BAND, "medium": MEDIUM_BAND},
            "backoff": {
                "threshold": tau,
                "validated": validated,
                "effective": backoff_effective(tau),
                "claims_backed_off": backed_off,
            },
        },
        "nli_backend": nli_backend,
        "supported_fraction": round(supported / len(out_rows), 4) if out_rows else None,
        "claims": out_rows,
    }


async def compute_claim_confidence(
    state: AgentState, publish: Optional[Callable[[Dict[str, Any]], None]] = None
) -> Dict[str, Any]:
    """The claim_confidence artifact. `publish(artifact)` receives the deterministic-feature artifact before the
    optional stated-confidence model call, so a failed or slow call still leaves an artifact."""
    cal = load_calibration()
    T, tau, weights = cal["temperature"], cal["conformal_threshold"], cal["weights"]
    claims = gather_claims(state)
    nli_scores: Dict[str, Any] = state.bag.get("nli_scores") or {}
    if claims and state.bag.get("_nli_store") is not None:
        try:
            await score_texts(state, [c["text"] for c in claims])  # cross-encoder fills sentences not judged yet
        except Exception as exc:  # noqa: BLE001
            logger.warning("NLI scoring for confidence failed: %s", exc)
        nli_scores = state.bag.get("nli_scores") or nli_scores
    grades = source_grades(state.tool_results)
    conflicts = _conflict_rows(state)
    defects = _defect_claims(state)
    docs_by_key = {d["source_key"]: d["text"] for d in collect_documents(state.tool_results)}

    rows: List[Dict[str, Any]] = []
    excerpts: Dict[str, str] = {}
    for c in claims[:MAX_CLAIMS]:
        nli = find_scores(nli_scores, c["text"])
        feats = claim_features(c, nli, grades, conflicts, defects, docs_by_key)
        z = raw_logit(feats, weights)
        conf = float(logistic(z / max(T, 1e-6)))
        rows.append(
            {
                "claim_id": c["claim_id"],
                "text": truncate_words(c["text"], 240),  # display text; nli_scores keys stay claim_key()
                "claim_type": c["claim_type"],
                "confidence": conf,
                "features": {**feats, "logit": round(z, 4)},
                "nli_label": nli.get("label") if nli else None,
            }
        )
        src = (feats["supporting_sources"] or c.get("evidence_keys") or [None])[0]
        if src and src in docs_by_key:
            excerpts[c["claim_id"]] = docs_by_key[src][:400]

    method = method_tag(cal)
    nli_backend = state.bag.get("nli_backend") or "none"
    artifact = _summarise(rows, {}, cal, method, nli_backend)
    if publish is not None:
        publish(artifact)
    if rows and _elicit_allowed(state):
        try:
            stated = await stated_confidence(state, rows, excerpts)
        except (BudgetExhausted, AgentLLMUnavailable) as exc:
            logger.info("Stated confidence unavailable: %s", exc)
            stated = {}
        if stated:
            method = method_tag(cal, stated=True)
            artifact = _summarise(rows, stated, cal, method, nli_backend)
            if publish is not None:
                publish(artifact)
    return artifact


class ConfidencePhase(AgentPhase):
    """Feature 9: per-claim evidence-support score after verification (calibrated only with a fitted file)."""

    key = "confidence"
    stage = "finalize"
    order = 10

    def applies(self, state: AgentState) -> bool:
        return bool((state.draft or "").strip()) or state.verification is not None

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        def publish(art: Dict[str, Any]) -> None:
            state.artifacts["claim_confidence"] = art

        # The deterministic artifact is stored before the optional model call, so a model failure still leaves it.
        artifact = await compute_claim_confidence(state, publish=publish)
        state.artifacts["claim_confidence"] = artifact
        claims = artifact["claims"]
        if claims:
            counts = {b: sum(1 for c in claims if c["band"] == b) for b in ("high", "medium", "low")}
            ac = artifact["answer_confidence"]
            what = "Answer confidence" if artifact["calibration"].get("calibrated") else "Evidence-support score"
            await state.step(
                "agent_confidence",
                f"{what} {ac:.2f} ({band(ac)})" if ac is not None else what,
                f"{len(claims)} claim(s): {counts['high']} high, {counts['medium']} medium, {counts['low']} low",
                agent="Verifier",
            )
        return None


register_phase(ConfidencePhase())

__all__ = [
    "CLAIM_TYPE_WEIGHTS",
    "DEFAULT_WEIGHTS",
    "ConfidencePhase",
    "backoff_effective",
    "band",
    "cap_confidence",
    "claim_features",
    "compute_claim_confidence",
    "feature_vector",
    "gather_claims",
    "fitted_on_real_labels",
    "load_calibration",
    "method_tag",
    "raw_logit",
    "score",
    "strong_support",
]
