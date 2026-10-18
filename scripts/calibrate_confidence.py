"""
Fit the claim-confidence calibration file (phronesis/config/confidence_calibration.json) from REAL labelled claims.

Usage:
    python scripts/calibrate_confidence.py --labelled claims.jsonl [--out PATH] [--alpha 0.1] [--fit-weights]
                                           [--seed 0] [--heldout-frac 0.3]
    python scripts/calibrate_confidence.py --write-heuristic [--out PATH]

Input (--labelled): JSON Lines, one pipeline claim per line, human-labelled:
    {"answer_id": "<id of the answer the claim came from>", "label": 0 | 1,
     "features": {<the claim_features() dict of phronesis.agentic.confidence for that claim>},
     "labeller": "<who judged it>", "synthetic": false}
Rows marked "synthetic": true are rejected: a calibration fitted on invented examples calibrates nothing (AGT-001).

What it does (AGT-001 / AGT-002):
1. Refuses to fit unless the set has at least confidence.MIN_LABELLED_CLAIMS claims from at least
   confidence.MIN_LABELLED_ANSWERS answers, with both labels present.
2. Splits by ANSWER (claims of one answer stay together) into a calibration split and a held-out split
   (plus a training split with --fit-weights).
3. With --fit-weights, fits the feature weights by L2-regularised logistic regression on the training split;
   otherwise keeps the hand-set DEFAULT_WEIGHTS.
4. Fits the temperature T on the calibration split (Guo et al. 2017) and the split-conformal back-off threshold
   tau per answer on the same split (Mohri & Hashimoto 2024).
5. Evaluates on the held-out split only: ECE and Brier before and after T, and the empirical per-answer
   coverage of tau, checked against 1 - alpha with a one-sided sampling tolerance.
6. Writes the file with status "fitted", the provenance of the labelled set and the held-out metrics. The
   back-off threshold is marked validated only when held-out coverage passes the check; confidence.py reports the
   score as calibrated only for such a file.

--write-heuristic writes the honest default asset instead (hand-set weights, T = 1, no back-off threshold,
status "heuristic"), which is what ships while no real labelled claim set exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phronesis.agentic import calibration_stats as cs  # noqa: E402
from phronesis.agentic import confidence as conf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("calibrate_confidence")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "phronesis" / "config"
CALIBRATION_OUTPUT = CONFIG_DIR / "confidence_calibration.json"
FEATURE_NAMES = [k for k in conf.DEFAULT_WEIGHTS if k != "bias"]


class CalibrationDataError(ValueError):
    """The labelled set cannot support a calibration (too small, synthetic, one class, malformed)."""


def heuristic_asset() -> Dict[str, Any]:
    """The default asset: honest about being uncalibrated."""
    return {
        "version": "2.0.0",
        "status": "heuristic",
        "label": conf.HEURISTIC_LABEL,
        "temperature": 1.0,
        "conformal_threshold": None,
        "weights": dict(conf.DEFAULT_WEIGHTS),
        "provenance": {
            "labelled_set": None,
            "synthetic": False,
            "n_claims": 0,
            "n_answers": 0,
            "note": (
                "Hand-set prior weights (confidence.DEFAULT_WEIGHTS); nothing is fitted and no back-off threshold is "
                "set, because no real labelled set of pipeline claims exists yet. The score is a heuristic "
                "evidence-support score, not a calibrated probability. An earlier asset (T=0.85, tau=0.1968) was fitted "
                "in-sample on 25 hand-written synthetic claims and was withdrawn (audit AGT-001/AGT-002). Run "
                "scripts/calibrate_confidence.py --labelled <claims.jsonl> to fit on real labelled claims."
            ),
        },
    }


def load_labelled(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise CalibrationDataError(f"line {n}: not JSON ({exc})") from exc
            rows.append(obj)
    return rows


def validate_rows(rows: Sequence[Dict[str, Any]]) -> None:
    """Raise CalibrationDataError unless the rows can support a real calibration."""
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise CalibrationDataError(f"row {i}: not an object")
        if r.get("synthetic"):
            raise CalibrationDataError(f"row {i}: synthetic examples are not accepted; label real pipeline claims")
        if r.get("label") not in (0, 1, True, False):
            raise CalibrationDataError(f"row {i}: label must be 0 or 1")
        if not isinstance(r.get("features"), dict):
            raise CalibrationDataError(f"row {i}: features must be the claim_features() dict")
        if not str(r.get("answer_id") or "").strip():
            raise CalibrationDataError(f"row {i}: answer_id is required (the conformal split is per answer)")
    n_claims = len(rows)
    n_answers = len({str(r["answer_id"]) for r in rows})
    labels = {int(bool(r["label"])) for r in rows}
    if n_claims < conf.MIN_LABELLED_CLAIMS:
        raise CalibrationDataError(f"{n_claims} labelled claims; at least {conf.MIN_LABELLED_CLAIMS} are required")
    if n_answers < conf.MIN_LABELLED_ANSWERS:
        raise CalibrationDataError(f"{n_answers} answers; at least {conf.MIN_LABELLED_ANSWERS} are required")
    if labels != {0, 1}:
        raise CalibrationDataError("both correct (1) and incorrect (0) claims are required")


def _matrix(rows: Sequence[Dict[str, Any]]) -> np.ndarray:
    return np.array([[conf.feature_vector(r["features"])[k] for k in FEATURE_NAMES] for r in rows], dtype=float)


def _grouped(rows: Sequence[Dict[str, Any]], scores: Sequence[float]) -> tuple:
    by: Dict[str, List[int]] = {}
    for i, r in enumerate(rows):
        by.setdefault(str(r["answer_id"]), []).append(i)
    keys = sorted(by)
    return [[float(scores[i]) for i in by[k]] for k in keys], [[int(bool(rows[i]["label"])) for i in by[k]] for k in keys]


def run_calibration(
    rows: Sequence[Dict[str, Any]],
    *,
    alpha: float = 0.10,
    fit_weights: bool = False,
    seed: int = 0,
    heldout_frac: float = 0.3,
    labelled_set: str = "",
    labelled_sha256: str = "",
) -> Dict[str, Any]:
    """Fit on the calibration (and training) splits and evaluate on held-out answers. Pure: writes nothing."""
    validate_rows(rows)
    if not 0.1 <= heldout_frac <= 0.5:
        raise CalibrationDataError("heldout_frac must be in [0.1, 0.5]")
    answers = [str(r["answer_id"]) for r in rows]
    if fit_weights:
        rest = 1.0 - heldout_frac
        train_idx, cal_idx, test_idx = cs.split_groups(answers, [rest * 0.55, rest * 0.45, heldout_frac], seed=seed)
    else:
        train_idx = []
        cal_idx, test_idx = cs.split_groups(answers, [1.0 - heldout_frac, heldout_frac], seed=seed)
    cal_rows = [rows[i] for i in cal_idx]
    test_rows = [rows[i] for i in test_idx]
    if len(test_rows) < conf.MIN_HELDOUT_CLAIMS:
        raise CalibrationDataError(f"held-out split has {len(test_rows)} claims; at least {conf.MIN_HELDOUT_CLAIMS} are required")
    if len({int(bool(r["label"])) for r in cal_rows}) < 2:
        raise CalibrationDataError("the calibration split has only one label; add more labelled answers")

    weights = dict(conf.DEFAULT_WEIGHTS)
    if fit_weights:
        train_rows = [rows[i] for i in train_idx]
        if len({int(bool(r["label"])) for r in train_rows}) < 2:
            raise CalibrationDataError("the training split has only one label; add more labelled answers")
        fit = cs.fit_logistic(_matrix(train_rows), [int(bool(r["label"])) for r in train_rows], l2=1.0)
        weights = {"bias": round(fit["intercept"], 4), **{k: round(float(c), 4) for k, c in zip(FEATURE_NAMES, fit["coef"])}}

    cal_logits = np.array([conf.raw_logit(r["features"], weights) for r in cal_rows], dtype=float)
    cal_y = np.array([int(bool(r["label"])) for r in cal_rows], dtype=float)
    t = float(cs.fit_temperature(cal_logits, cal_y))

    def deployed(rs: Sequence[Dict[str, Any]], temperature: float) -> np.ndarray:
        # The deployed score: logistic(w.x / T), capped below "high" without strong support.
        return np.array([conf.score(r["features"], temperature=temperature, weights=weights) for r in rs], dtype=float)

    cal_scores = deployed(cal_rows, t)
    g_scores, g_labels = _grouped(cal_rows, cal_scores)
    tau = cs.conformal_threshold(g_scores, g_labels, alpha=alpha)
    tau_finite = math.isfinite(tau)

    test_y = np.array([int(bool(r["label"])) for r in test_rows], dtype=float)
    test_uncal = deployed(test_rows, 1.0)
    test_cal = deployed(test_rows, t)
    tg_scores, tg_labels = _grouped(test_rows, test_cal)
    coverage = cs.conformal_coverage(tg_scores, tg_labels, tau) if tau_finite else float("nan")
    tolerance = cs.coverage_tolerance(alpha, len(tg_scores))
    coverage_ok = bool(tau_finite and coverage >= 1.0 - alpha - tolerance)

    heldout = {
        "n_claims": len(test_rows),
        "n_answers": len(tg_scores),
        "ece": round(cs.ece(test_cal, test_y), 4),
        "brier": round(cs.brier(test_cal, test_y), 4),
        "ece_temperature_1": round(cs.ece(test_uncal, test_y), 4),
        "brier_temperature_1": round(cs.brier(test_uncal, test_y), 4),
        "reliability": cs.reliability_table(test_cal, test_y, bins=10),
    }
    return {
        "version": "2.0.0",
        "status": "fitted",
        "label": conf.CALIBRATED_LABEL,
        "temperature": round(t, 4),
        # A threshold is shipped only when its held-out coverage passed; otherwise no back-off is configured.
        "conformal_threshold": round(float(tau), 4) if coverage_ok else None,
        "weights": weights,
        "provenance": {
            "labelled_set": labelled_set or "in-memory",
            "sha256": labelled_sha256,
            "synthetic": False,
            "n_claims": len(rows),
            "n_answers": len(set(answers)),
            "weights": "fitted (L2 logistic regression, training split)" if fit_weights else "hand-set DEFAULT_WEIGHTS",
            "split": {"seed": seed, "heldout_frac": heldout_frac, "unit": "answer",
                      "n_train_claims": len(train_idx), "n_calibration_claims": len(cal_rows), "n_heldout_claims": len(test_rows)},
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "evaluation": {
            "calibration_split": {"n_claims": len(cal_rows), "ece_in_sample": round(cs.ece(cal_scores, cal_y), 4)},
            "heldout": heldout,
        },
        "conformal": {
            "alpha": alpha,
            "grouped": True,
            "threshold": round(float(tau), 4) if tau_finite else None,
            "n_calibration_answers": len(g_scores),
            "heldout_coverage": round(float(coverage), 4) if tau_finite else None,
            "heldout_coverage_tolerance": round(float(tolerance), 4),
            "heldout_coverage_ok": coverage_ok,
        },
    }


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write(asset: Dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(asset, fh, indent=2)
        fh.write("\n")
    logger.info("Wrote %s", out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--labelled", help="JSONL of real, human-labelled pipeline claims")
    parser.add_argument("--out", default=str(CALIBRATION_OUTPUT))
    parser.add_argument("--alpha", type=float, default=0.10)
    parser.add_argument("--fit-weights", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--heldout-frac", type=float, default=0.3)
    parser.add_argument("--write-heuristic", action="store_true", help="write the uncalibrated default asset")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.write_heuristic:
        write(heuristic_asset(), out)
        return 0
    if not args.labelled:
        parser.error("--labelled is required (or --write-heuristic): calibration needs a real labelled claim set")
    try:
        asset = run_calibration(
            load_labelled(args.labelled), alpha=args.alpha, fit_weights=args.fit_weights, seed=args.seed,
            heldout_frac=args.heldout_frac, labelled_set=os.path.basename(args.labelled), labelled_sha256=_sha256(args.labelled),
        )
    except CalibrationDataError as exc:
        logger.error("Calibration refused: %s", exc)
        return 2
    write(asset, out)
    print(json.dumps({"heldout": {k: v for k, v in asset["evaluation"]["heldout"].items() if k != "reliability"},
                      "conformal": asset["conformal"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
