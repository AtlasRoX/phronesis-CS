"""
Calibration statistics for per-claim confidence (feature 9). Pure numpy/scipy, no model.

- Temperature scaling (Guo et al., "On Calibration of Modern Neural Networks", ICML 2017, arXiv:1706.04599):
  one scalar T fitted on a held-out set by minimising the negative log-likelihood of sigmoid(z/T) (binary) or
  softmax(z/T) (multi-class). T > 1 softens over-confident scores, T < 1 sharpens them.
- Expected calibration error with equal-width bins and the matching reliability table (also Guo et al.).
- Brier score (mean squared error of a probability against the 0/1 outcome).
- Split-conformal back-off threshold for sub-claims (Mohri & Hashimoto, "Language Models with Conformal
  Factuality Guarantees", ICML 2024, arXiv:2402.10978; the conformal-LM framing is from Quach et al.,
  ICLR 2024, arXiv:2306.10193): for each calibration answer the non-conformity score is the highest score of
  any incorrect claim, i.e. the threshold that would have removed every incorrect claim. The
  ceil((n+1)(1-alpha))/n empirical quantile of those scores is the threshold; keeping only claims scored
  strictly above it leaves an answer whose kept claims are all correct with probability >= 1 - alpha
  (exchangeability assumed). The guarantee needs per-answer units (pass nested lists) drawn from the same
  distribution as the deployed answers: a flat list treats each claim as its own unit, which breaks exchangeability
  for claims of the same answer, and a synthetic or hand-written set gives no guarantee at all (AGT-002). Check a
  threshold with conformal_coverage on held-out answers (split_groups keeps answers whole).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

_EPS = 1e-12
_T_MIN, _T_MAX = 0.05, 20.0


def logistic(x: Any) -> Any:
    """Numerically stable sigmoid for scalars or arrays."""
    arr = np.asarray(x, dtype=float)
    out = np.where(arr >= 0, 1.0 / (1.0 + np.exp(-np.abs(arr))), np.exp(-np.abs(arr)) / (1.0 + np.exp(-np.abs(arr))))
    return float(out) if out.ndim == 0 else out


def logit(p: Any) -> Any:
    arr = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    out = np.log(arr) - np.log1p(-arr)
    return float(out) if out.ndim == 0 else out


def _as_binary_logits(scores: Any) -> np.ndarray:
    """1-D input: probabilities in [0, 1] are mapped to logits; anything else is taken as logits already."""
    arr = np.asarray(scores, dtype=float).ravel()
    if arr.size and np.all(arr >= 0.0) and np.all(arr <= 1.0):
        return np.asarray(logit(arr), dtype=float).ravel()
    return arr


def _nll(z: np.ndarray, y: np.ndarray, temperature: float) -> float:
    t = max(temperature, _EPS)
    if z.ndim == 1:
        s = z / t
        # binary cross-entropy from logits: log(1 + e^s) - y*s, computed stably
        return float(np.mean(np.logaddexp(0.0, s) - y * s))
    s = z / t
    s = s - s.max(axis=1, keepdims=True)
    log_probs = s - np.log(np.exp(s).sum(axis=1, keepdims=True))
    return float(-np.mean(log_probs[np.arange(len(y)), y.astype(int)]))


def fit_temperature(logits_or_scores: Any, labels: Sequence[Any]) -> float:
    """
    The temperature T that minimises NLL on (logits_or_scores, labels) (Guo et al. 2017).

    - 1-D input: binary. Values in [0, 1] are read as probabilities and converted to logits; labels are 0/1.
    - 2-D input (n, k): multi-class logits; labels are class indices.
    T is searched on log-scale in [0.05, 20] with a bounded scalar minimiser (scipy), falling back to a
    golden-section search when scipy is unavailable. Fewer than two examples return 1.0 (no change).
    """
    y = np.asarray(labels, dtype=float).ravel()
    raw = np.asarray(logits_or_scores, dtype=float)
    z = raw if raw.ndim == 2 else _as_binary_logits(raw)
    if len(y) < 2 or len(z) != len(y):
        return 1.0

    def objective(log_t: float) -> float:
        return _nll(z, y, math.exp(log_t))

    lo, hi = math.log(_T_MIN), math.log(_T_MAX)
    try:
        from scipy.optimize import minimize_scalar

        res = minimize_scalar(objective, bounds=(lo, hi), method="bounded", options={"xatol": 1e-5})
        best = float(res.x)
    except Exception:  # noqa: BLE001 - scipy missing or failed: golden-section search
        gr = (math.sqrt(5) - 1) / 2
        a, b = lo, hi
        c, d = b - gr * (b - a), a + gr * (b - a)
        for _ in range(80):
            if objective(c) < objective(d):
                b = d
            else:
                a = c
            c, d = b - gr * (b - a), a + gr * (b - a)
        best = (a + b) / 2
    return float(min(_T_MAX, max(_T_MIN, math.exp(best))))


def apply_temperature(logits: Any, temperature: float) -> Any:
    """sigmoid(z / T) for binary logits."""
    return logistic(np.asarray(logits, dtype=float) / max(float(temperature), _EPS))


def reliability_table(scores: Sequence[float], labels: Sequence[Any], bins: int = 10) -> List[Dict[str, Any]]:
    """Equal-width bins over [0, 1]: count, mean confidence and observed accuracy per non-empty bin."""
    s = np.clip(np.asarray(scores, dtype=float).ravel(), 0.0, 1.0)
    y = np.asarray(labels, dtype=float).ravel()
    edges = np.linspace(0.0, 1.0, bins + 1)
    # bin i holds (edges[i], edges[i+1]]; score 0 goes to the first bin
    idx = np.clip(np.searchsorted(edges, s, side="left") - 1, 0, bins - 1)
    rows: List[Dict[str, Any]] = []
    for i in range(bins):
        mask = idx == i
        n = int(mask.sum())
        if n == 0:
            continue
        rows.append(
            {
                "bin_lo": round(float(edges[i]), 4),
                "bin_hi": round(float(edges[i + 1]), 4),
                "count": n,
                "mean_confidence": float(s[mask].mean()),
                "accuracy": float(y[mask].mean()),
            }
        )
    return rows


def ece(scores: Sequence[float], labels: Sequence[Any], bins: int = 10) -> float:
    """Expected calibration error: sum over bins of (n_b / N) * |accuracy_b - confidence_b|."""
    total = len(np.asarray(scores).ravel())
    if total == 0:
        return 0.0
    return float(
        sum(r["count"] / total * abs(r["accuracy"] - r["mean_confidence"]) for r in reliability_table(scores, labels, bins))
    )


def brier(scores: Sequence[float], labels: Sequence[Any]) -> float:
    s = np.asarray(scores, dtype=float).ravel()
    y = np.asarray(labels, dtype=float).ravel()
    return float(np.mean((s - y) ** 2)) if len(s) else 0.0


def _group_nonconformity(scores: Sequence[float], labels: Sequence[Any]) -> float:
    """Highest score of an incorrect claim in one answer (-inf when every claim is correct)."""
    bad = [float(s) for s, y in zip(scores, labels) if not bool(y)]
    return max(bad) if bad else float("-inf")


def conformal_threshold(cal_scores: Any, cal_labels: Any, alpha: float = 0.1) -> float:
    """
    Split-conformal back-off threshold (Mohri & Hashimoto 2024).

    cal_scores/cal_labels are either flat sequences (every claim is its own calibration unit) or sequences of
    per-answer sequences (one unit per answer, the paper's setting). Returns tau: keep claims with
    score > tau. tau = +inf when the calibration set is too small for the requested alpha (abstain from
    everything); -inf when no calibration claim was incorrect.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    scores = list(cal_scores)
    labels = list(cal_labels)
    if len(scores) != len(labels):
        raise ValueError("cal_scores and cal_labels differ in length")
    grouped = bool(scores) and isinstance(scores[0], (list, tuple, np.ndarray))
    if grouped:
        r = np.array([_group_nonconformity(s, y) for s, y in zip(scores, labels)], dtype=float)
    else:
        r = np.array([float(s) if not bool(y) else float("-inf") for s, y in zip(scores, labels)], dtype=float)
    n = len(r)
    if n == 0:
        return float("inf")
    k = math.ceil((n + 1) * (1.0 - alpha))
    if k > n:
        return float("inf")
    return float(np.sort(r)[k - 1])


def conformal_coverage(test_scores: Sequence[Sequence[float]], test_labels: Sequence[Sequence[Any]], tau: float) -> float:
    """Empirical coverage of a back-off threshold on held-out answers: the fraction of answers whose kept claims
    (score > tau) are all correct. The split-conformal guarantee is coverage >= 1 - alpha in expectation."""
    groups = list(zip(test_scores, test_labels))
    if not groups:
        return float("nan")
    ok = sum(1 for s, y in groups if _group_nonconformity(s, y) <= tau)
    return ok / len(groups)


def coverage_tolerance(alpha: float, n_answers: int, z: float = 1.645) -> float:
    """One-sided sampling tolerance for checking held-out coverage against 1 - alpha (normal approximation to the
    binomial, 95 %): coverage below 1 - alpha - tolerance is evidence the guarantee does not hold."""
    if n_answers <= 0:
        return float("inf")
    return z * math.sqrt(alpha * (1.0 - alpha) / n_answers)


def split_groups(group_ids: Sequence[Any], fractions: Sequence[float], seed: int = 0) -> List[List[int]]:
    """Row indices split by group (answer), so every claim of one answer lands in the same split: per-answer
    exchangeability is what the conformal guarantee assumes, and a claim-level split would leak an answer's
    claims across calibration and test. Deterministic for a seed; fractions are normalised."""
    order: Dict[Any, List[int]] = {}
    for i, g in enumerate(group_ids):
        order.setdefault(g, []).append(i)
    groups = sorted(order, key=str)
    rng = np.random.default_rng(seed)
    perm = [groups[i] for i in rng.permutation(len(groups))]
    total = float(sum(fractions)) or 1.0
    bounds, acc = [], 0.0
    for f in fractions:
        acc += f / total
        bounds.append(int(round(acc * len(perm))))
    out: List[List[int]] = []
    start = 0
    for end in bounds:
        out.append([i for g in perm[start:end] for i in order[g]])
        start = end
    return out


def fit_logistic(X: Any, y: Sequence[Any], l2: float = 1.0, iterations: int = 100) -> Dict[str, Any]:
    """L2-regularised logistic regression by Newton's method (IRLS); the intercept is not penalised.

    Returns {"coef": array of len(X[0]), "intercept": float}. Pure numpy."""
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float).ravel()
    n, d = Xa.shape
    A = np.hstack([np.ones((n, 1)), Xa])
    w = np.zeros(d + 1)
    penalty = np.full(d + 1, float(l2))
    penalty[0] = 0.0
    for _ in range(iterations):
        p = np.asarray(logistic(A @ w), dtype=float)
        grad = A.T @ (p - ya) + penalty * w
        W = p * (1 - p) + 1e-9
        H = (A * W[:, None]).T @ A + np.diag(penalty + 1e-9)
        step = np.linalg.solve(H, grad)
        w = w - step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return {"coef": w[1:], "intercept": float(w[0])}


def calibration_summary(scores: Sequence[float], labels: Sequence[Any], bins: int = 10) -> Dict[str, Any]:
    """ECE, Brier score and the reliability table in one JSON-safe dict."""
    return {
        "n": len(np.asarray(scores).ravel()),
        "ece": ece(scores, labels, bins),
        "brier": brier(scores, labels),
        "reliability": reliability_table(scores, labels, bins),
    }


def fit_calibration(
    raw_logits: Sequence[float], labels: Sequence[Any], alpha: Optional[float] = 0.1
) -> Dict[str, Any]:
    """Fit T on binary logits and (optionally) a conformal threshold on the calibrated scores: the JSON shape
    PHRONESIS_CONFIDENCE_CALIBRATION reads ({"temperature", "conformal_threshold"})."""
    t = fit_temperature(np.asarray(raw_logits, dtype=float), labels)
    calibrated = apply_temperature(np.asarray(raw_logits, dtype=float), t)
    tau = conformal_threshold(list(np.atleast_1d(calibrated)), list(labels), alpha) if alpha else None
    return {
        "temperature": t,
        "conformal_threshold": tau if tau is not None and math.isfinite(tau) else None,
        "ece_after": ece(np.atleast_1d(calibrated), labels),
    }


__all__ = [
    "apply_temperature",
    "brier",
    "calibration_summary",
    "conformal_coverage",
    "conformal_threshold",
    "coverage_tolerance",
    "ece",
    "fit_calibration",
    "fit_logistic",
    "fit_temperature",
    "logistic",
    "logit",
    "reliability_table",
    "split_groups",
]
