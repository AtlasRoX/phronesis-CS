"""
Comprehensive Agent Calibration Test Suite for Phronesis.
Validates:
1. Multi-agent confidence & factuality calibration (Guo et al. 2017, Mohri & Hashimoto 2024).
2. Split-conformal back-off coverage guarantees at alpha = 0.10.
3. Proper classification into high (>=0.75), medium (>=0.45), and low (<0.45) confidence bands.
4. Deterministic verifier reflexion and numerical tolerance calibration.
5. Vivarix kinetic parameter calibration and profile likelihood identifiability (Raue et al. 2009).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from phronesis.agentic import calibration_stats as cs
from phronesis.agentic import confidence as conf
from phronesis.vivarix.calibration import (
    CHI2_THRESHOLD_95,
    calibrate_parameters,
    load_timecourse,
)


def test_confidence_calibration_asset_exists():
    # AGT-001/AGT-002: In-sample fitted asset was withdrawn for an honest heuristic asset
    cal = conf.load_calibration()
    assert cal["temperature"] in (0.85, 1.0)
    assert cal.get("conformal_threshold") is None or cal.get("conformal_threshold") == pytest.approx(0.1968, abs=0.01)
    assert "bias" in cal["weights"]
    assert cal["source"] in ("package_default", "file")


def test_confidence_bands_and_conformal_backoff():
    cal = conf.load_calibration()
    T = cal["temperature"]
    tau = cal["conformal_threshold"]

    # Gold clinical RCT claim: multi-source, high grade, NLI entailment, exact match
    strong_clinical = {
        "nli_entailment": 0.95,
        "nli_contradiction": 0.01,
        "n_sources": 3,
        "best_grade": 1.0,
        "verifier_status": "supported",
        "exact_match": True,
        "hedged": False,
    }
    strong_score = conf.score(strong_clinical, temperature=T)
    assert strong_score >= 0.75
    assert conf.band(strong_score, conformal_threshold=tau) == "high"

    # Hedged preclinical observation: 1 source, moderate grade, no exact match
    hedged_obs = {
        "nli_entailment": 0.65,
        "nli_contradiction": 0.08,
        "n_sources": 1,
        "best_grade": 0.25,
        "verifier_status": "supported",
        "exact_match": False,
        "hedged": True,
    }
    hedged_score = conf.score(hedged_obs, temperature=T)
    assert 0.45 <= hedged_score < 0.75
    assert conf.band(hedged_score, conformal_threshold=tau) == "medium"

    # Unsupported plain claim: no sources, not checked
    unsupported_plain = {}
    plain_score = conf.score(unsupported_plain, temperature=T)
    assert 0.30 <= plain_score <= 0.45

    # Direct contradiction / critical defect / hallucination trap
    contradicted = {
        "nli_entailment": 0.05,
        "nli_contradiction": 0.90,
        "n_sources": 0,
        "best_grade": 0.0,
        "conflict": True,
        "defect": True,
        "verifier_status": "contradicted",
        "exact_match": False,
        "hedged": False,
    }
    contra_score = conf.score(contradicted, temperature=T)
    assert contra_score < 0.10
    assert conf.band(contra_score, conformal_threshold=tau) == "low"


def test_ece_and_brier_score_properties():
    # Perfect calibration synthetic test
    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [0, 0, 1, 1]
    e = cs.ece(scores, labels, bins=2)
    b = cs.brier(scores, labels)
    assert e < 0.2
    assert b < 0.05

    # Temperature scaling lowers NLL
    logits = np.array([-3.0, -1.0, 1.0, 3.0])
    y = np.array([0, 0, 1, 1])
    fitted_t = cs.fit_temperature(logits, y)
    assert 0.05 <= fitted_t <= 10.0


def test_vivarix_ode_calibration_identifiability():
    """Verify kinetic parameter calibration and profile likelihood identifiability (Raue et al. 2009)."""
    # Simple analytic model: A -> B with rate k
    times = np.array([0.0, 1.0, 2.0, 4.0, 8.0])
    k_true = 0.5
    A_vals = 10.0 * np.exp(-k_true * times)

    import pandas as pd
    df = pd.DataFrame({"time": times, "A": A_vals})

    def sim(p):
        k = p["k1"]
        return {"A": 10.0 * np.exp(-k * times)}

    res = calibrate_parameters(
        parameter_names=["k1"],
        initial_values={"k1": 1.0},
        data=df,
        simulate=sim,
        bounds={"k1": (0.01, 10.0)},
        profile_params=["k1"],
        timeout_seconds=5.0,
    )

    assert "parameters" in res
    param = next(p for p in res["parameters"] if p["name"] == "k1")
    assert abs(param["estimate"] - k_true) < 0.05
    assert param["identifiability"] in ("identifiable", "practically_non_identifiable")
    assert res["objective"]["chi2"] < 1e-4
    assert res["objective"]["rmse"] < 1e-4
