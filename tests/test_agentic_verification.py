"""Features 8 and 9: NLI claim verification (judge for the verifier) and calibrated per-claim confidence.

Offline: the cross-encoder loader is monkeypatched with a fake predictor returning fixed logits, and the model
judge is a ScriptedLLM routed by the "[phase:nli]" marker. No model is downloaded.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest

from phronesis.agentic import calibration_stats as cs
from phronesis.agentic import confidence as conf
from phronesis.agentic import nli
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.config import AGENTIC
from phronesis.agentic.control import RunControl
from phronesis.agentic.llm import AgentLLM
from phronesis.agentic.state import AgentState
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import ToolResult
from tests.agentic_fakes import ScriptedLLM, collect_events, make_agentic_orchestrator

# Logits in the cross-encoder/nli-deberta-v3-base order: 0=contradiction, 1=entailment, 2=neutral.
ENTAIL = [-2.0, 4.0, -1.0]
NEUTRAL = [-1.0, -2.0, 4.0]
CONTRA = [5.0, -2.0, -1.0]

# The recording registry's literature passage (tests/fakes.py) and claims about it.
PASSAGE = "KRAS signaling in pancreatic cancer."
MECH_SUPPORTED = "KRAS signaling drives pancreatic tumour growth (PMID: 123456)."
MECH_UNSUPPORTED = "BRAF amplification mediates acquired resistance to vemurafenib (PMID: 123456)."
CLINICAL_UNSUPPORTED = "In the phase 3 trial, sotorasib improved overall survival to 12.5 months."
CONTRA_PASSAGE = "Sotorasib did not improve overall survival in KRAS G12C lung cancer patients."
CLINICAL_CONTRADICTED = "Sotorasib improved overall survival in KRAS G12C lung cancer (PMID: 222222)."


@pytest.fixture(autouse=True)
def _fresh_nli_cache(monkeypatch):
    nli.reset_cache()
    for var in ("PHRONESIS_NLI", "PHRONESIS_NLI_MODEL", "PHRONESIS_CONFIDENCE_T", "PHRONESIS_CONFIDENCE_CALIBRATION",
                "PHRONESIS_NLI_MAX_LLM_CALLS", "PHRONESIS_NLI_LLM_BATCH", "PHRONESIS_CONFIDENCE_ELICIT"):
        monkeypatch.delenv(var, raising=False)
    yield
    nli.reset_cache()


class FakePredictor:
    """Fixed logits by cue word in the premise (the evidence chunk); records every (premise, hypothesis) pair."""

    def __init__(self, rules: List[Tuple[str, List[float]]], default: List[float] = NEUTRAL) -> None:
        self.rules = rules
        self.default = default
        self.pairs: List[Tuple[str, str]] = []

    def __call__(self, pairs: List[Tuple[str, str]]) -> np.ndarray:
        self.pairs.extend(pairs)
        out = []
        for premise, _hyp in pairs:
            row = self.default
            for cue, logits in self.rules:
                if cue in premise.lower():
                    row = logits
                    break
            out.append(row)
        return np.array(out, dtype=float)


def patch_loader(monkeypatch, predictor: FakePredictor, id2label: Dict[Any, Any] = None) -> List[str]:
    loaded: List[str] = []

    def fake_load(mid: str):
        loaded.append(mid)
        return predictor, dict(id2label or {0: "contradiction", 1: "entailment", 2: "neutral"})

    monkeypatch.setattr(nli, "_load_predictor", fake_load)
    return loaded


def lit_result(passages: List[Dict[str, Any]]) -> ToolResult:
    return ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": passages})


def claim_by_text(report, fragment: str):
    return next(c for c in report.claims if fragment.lower() in c.claim_text.lower())


async def _noop(*_a, **_k) -> None:
    return None


def make_state(tool_results: Dict[str, Any], scripted: ScriptedLLM = None, budget: RunBudget = None, draft: str = "") -> AgentState:
    budget = budget or RunBudget()
    factory = (lambda **_k: scripted) if scripted is not None else None
    return AgentState(
        query="What does the literature say about KRAS signaling in pancreatic cancer?",
        clean_query="KRAS signaling in pancreatic cancer",
        ctx=None,
        registry=None,
        user_id="u1",
        thread_id="t1",
        model_name="fake",
        api_key=None,
        framework=AGENTIC,
        budget=budget,
        control=RunControl("u1", interactive=False),
        llm=AgentLLM(factory, "fake", None, budget),
        tool_results=tool_results,
        emit=_noop,
        send=_noop,
        draft=draft,
    )


# Label mapping, chunking, scoring -----------------------------------------------------------------------------
def test_label_mapping_hard_coded_and_from_config():
    assert nli.label_mapping(None) == {0: "contradiction", 1: "entailment", 2: "neutral"}
    assert nli.label_mapping({0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"}) == nli.DEFAULT_LABELS
    assert nli.label_mapping({"0": "ENTAILMENT", "1": "NEUTRAL", "2": "CONTRADICTION"}) == {
        0: "entailment", 1: "neutral", 2: "contradiction"
    }
    # A config whose order differs is honoured when scoring.
    enc = nli.CrossEncoderNLI(lambda pairs: np.array([[4.0, -1.0, -2.0]] * len(pairs)), {0: "entailment", 1: "neutral", 2: "contradiction"})
    s = enc.score("claim", ["premise"])[0]
    assert s["entailment"] > 0.9 and s["contradiction"] < 0.05


def test_chunking_takes_max_entailment_over_chunks():
    filler = " ".join(f"Sentence {i} reports an unrelated assay of cell viability in culture." for i in range(40))
    passage = filler + " Notably, KRAS G12D drives ERK activation in pancreatic ductal cells. " + filler
    chunks = nli.chunk_passage(passage, max_words=60)
    assert len(chunks) > 3 and all(len(c.split()) <= 60 for c in chunks)
    pred = FakePredictor([("erk activation", ENTAIL)])
    enc = nli.CrossEncoderNLI(pred, None, batch_size=4, max_words=60)
    s = enc.score("KRAS G12D activates ERK.", [passage, filler])
    assert s[0]["entailment"] > 0.95  # the one supporting chunk wins (SummaC-ZS / MiniCheck max)
    assert s[0]["chunks"] == len(chunks)
    assert s[1]["entailment"] < 0.05 and s[1]["neutral"] > 0.9
    assert all(h == "KRAS G12D activates ERK." for _p, h in pred.pairs)  # premise = evidence, hypothesis = claim


def test_status_thresholds_and_scifact_mapping():
    assert nli.status_from_scores(0.9, 0.0, 0.1) == "supported"
    assert nli.status_from_scores(0.02, 0.95, 0.03) == "contradicted"
    assert nli.status_from_scores(0.05, 0.1, 0.85) == "unsupported"
    assert nli.status_from_scores(0.3, 0.1, 0.6) is None  # uncertain band: abstain
    assert nli.scifact_to_scores("SUPPORTS", 0.9)["entailment"] == 0.9
    assert nli.scifact_to_scores("refutes", 0.95)["contradiction"] == 0.95
    assert nli.scifact_to_scores("NOINFO", 0.9)["neutral"] == 0.9
    assert nli.scifact_to_scores("maybe") is None


def test_cross_encoder_loads_local_only_and_never_in_auto_without_cache(monkeypatch):
    pred = FakePredictor([])
    loaded = patch_loader(monkeypatch, pred)
    monkeypatch.setattr(nli, "_model_available", lambda mid: False)
    assert nli.get_cross_encoder(require_cached=True) is None and not loaded  # auto: not cached -> not even tried
    enc = nli.get_cross_encoder(require_cached=False)
    assert enc is not None and loaded == [nli.DEFAULT_MODEL_ID]
    assert nli.get_cross_encoder(require_cached=False) is enc and len(loaded) == 1  # loaded once per process

    nli.reset_cache()

    def broken(mid):
        raise OSError("not in the local cache (local_files_only=True)")

    monkeypatch.setattr(nli, "_load_predictor", broken)
    assert nli.get_cross_encoder(require_cached=False) is None


# Judge contract with the verifier -------------------------------------------------------------------------------
def _tool_results() -> Dict[str, ToolResult]:
    return {
        "lithoscribe": lit_result(
            [
                {"pmid": "123456", "passage_text": PASSAGE},
                {"pmid": "222222", "passage_text": CONTRA_PASSAGE},
            ]
        )
    }


def _verify(response: str, judge=None):
    return ClinicalVerifier().verify_response(
        query="KRAS signaling in pancreatic cancer and sotorasib survival",
        response_markdown=response,
        tool_results=_tool_results(),
        entailment_judge=judge,
    )


def test_judge_downgrades_but_never_upgrades():
    response = " ".join([MECH_SUPPORTED, MECH_UNSUPPORTED, CLINICAL_UNSUPPORTED, CLINICAL_CONTRADICTED])
    base = _verify(response)
    assert claim_by_text(base, "drives pancreatic").status == "supported"
    assert claim_by_text(base, "BRAF amplification").status == "unsupported"
    assert claim_by_text(base, "phase 3 trial").status == "unsupported"
    assert claim_by_text(base, "G12C lung").status == "contradicted"

    # A judge that says "unsupported"/"contradicted" downgrades.
    pred = FakePredictor([("kras signaling in pancreatic", NEUTRAL)], default=CONTRA)
    judge = nli.build_judge(nli.BACKEND_CROSS_ENCODER, encoder=nli.CrossEncoderNLI(pred))
    down = _verify(response, judge)
    sup = claim_by_text(down, "drives pancreatic")
    assert sup.status == "unsupported" and sup.reason.startswith("semantic check: NLI cross-encoder")
    # MECH_UNSUPPORTED's cited passage is neutral too -> stays unsupported, never upgraded
    assert claim_by_text(down, "BRAF amplification").status == "unsupported"

    contra_pred = FakePredictor([], default=CONTRA)
    contra_judge = nli.build_judge(nli.BACKEND_CROSS_ENCODER, encoder=nli.CrossEncoderNLI(contra_pred))
    worse = _verify(MECH_UNSUPPORTED, contra_judge)
    assert claim_by_text(worse, "BRAF amplification").status == "contradicted"  # unsupported mechanistic -> contradicted
    assert not worse.passed

    # A judge that always says "supported" cannot rescue a deterministic clinical failure or a contradiction.
    asked: List[str] = []

    def yes(claim, passages):
        asked.append(claim)
        return {"status": "supported", "reason": "always yes"}

    up = _verify(response, yes)
    assert claim_by_text(up, "phase 3 trial").status == "unsupported"
    assert claim_by_text(up, "G12C lung").status == "contradicted"
    assert claim_by_text(up, "drives pancreatic").status == "supported"
    assert not any("phase 3" in a or "G12C" in a for a in asked)
    assert up.passed == base.passed is False


def test_judge_abstains_and_never_raises():
    enc = nli.CrossEncoderNLI(FakePredictor([], default=[0.0, 0.3, 0.0]))  # ~ uniform: uncertain band
    judge = nli.build_judge(nli.BACKEND_CROSS_ENCODER, encoder=enc)
    assert judge("KRAS drives growth", [{"source_key": "PMID:1", "passage": "kras text"}]) is None
    assert judge("KRAS drives growth", []) is None

    def boom(pairs):
        raise RuntimeError("model crashed")

    bad = nli.build_judge(nli.BACKEND_CROSS_ENCODER, encoder=nli.CrossEncoderNLI(boom))
    assert bad("claim", [{"source_key": "PMID:1", "passage": "text"}]) is None
    assert _verify(MECH_SUPPORTED, bad).claims[0].status == "supported"
    assert nli.build_judge(nli.BACKEND_NONE) is None
    assert nli.build_judge(nli.BACKEND_CROSS_ENCODER, encoder=None) is None


# Model judge: precompute, cache and call cap -------------------------------------------------------------------
def _scifact_responder(label: str, calls: List[str]):
    def respond(system: str, user: str) -> str:
        calls.append(user)
        labels = []
        for item in re.findall(r'<item id="(\d+)">(.*?)</item>', user, re.S):
            n_passages = len(re.findall(r"<passage n=", item[1]))
            labels.extend({"item": int(item[0]), "passage": m, "label": label, "confidence": 0.95} for m in range(1, n_passages + 1))
        return json.dumps({"labels": labels})

    return respond


def test_llm_precompute_fills_cache_and_judge_serves_it(monkeypatch):
    monkeypatch.setenv("PHRONESIS_NLI", "llm")
    calls: List[str] = []
    llm = ScriptedLLM({"nli": _scifact_responder("REFUTES", calls)})
    state = make_state(_tool_results(), llm, draft=MECH_SUPPORTED)
    store = nli.install(state)
    assert store.backend == nli.BACKEND_LLM
    judge = state.bag["entailment_judge"]
    assert state.bag["nli_judge_fn"] is judge and callable(state.bag["nli_score_fn"])
    # Before precompute the model judge abstains (the verifier is synchronous and cannot await the model).
    assert judge(MECH_SUPPORTED, [{"source_key": "PMID:123456", "passage": PASSAGE.lower()}]) is None

    stats = asyncio.run(nli.precompute(state))
    assert stats["requests"] == 1 and stats["llm_calls"] == 1
    assert llm.phases() == ["nli"] and "[phase:nli]" in llm.calls[0][1] and "<untrusted_data" in calls[0]
    scores = nli.find_scores(state.bag["nli_scores"], MECH_SUPPORTED)
    assert scores["contradiction"] == 0.95 and scores["backend"] == "llm" and scores["best_source"] == "PMID:123456"

    report = ClinicalVerifier().verify_response(
        query=state.query, response_markdown=MECH_SUPPORTED, tool_results=state.tool_results, entailment_judge=judge
    )
    c = claim_by_text(report, "drives pancreatic")
    assert c.status == "contradicted" and "NLI llm" in c.reason

    asyncio.run(nli.precompute(state))  # same draft again: everything cached, no new model call
    assert len(llm.calls) == 1 and store.llm_calls == 1


def test_llm_judge_call_cap_and_budget(monkeypatch):
    monkeypatch.setenv("PHRONESIS_NLI", "llm")
    monkeypatch.setenv("PHRONESIS_NLI_LLM_BATCH", "1")
    monkeypatch.setenv("PHRONESIS_NLI_MAX_LLM_CALLS", "3")
    genes = ["BRAF", "EGFR", "MYC", "TP53", "PIK3CA", "SMAD4"]
    passages = [{"pmid": str(100000 + i), "passage_text": f"{g} signaling in pancreatic cancer."} for i, g in enumerate(genes)]
    draft = " ".join(f"{g} signaling drives pancreatic tumour growth (PMID: {100000 + i})." for i, g in enumerate(genes))
    calls: List[str] = []
    llm = ScriptedLLM({"nli": _scifact_responder("SUPPORTS", calls)})
    state = make_state({"lit": lit_result(passages)}, llm, draft=draft)
    nli.install(state)
    stats = asyncio.run(nli.precompute(state))
    assert stats["requests"] == 6 and len(calls) == 3  # env cap
    asyncio.run(nli.precompute(state))
    assert len(calls) == 3  # the cap holds across draft rounds

    monkeypatch.setenv("PHRONESIS_NLI_MAX_LLM_CALLS", "12")
    calls2: List[str] = []
    llm2 = ScriptedLLM({"nli": _scifact_responder("SUPPORTS", calls2)})
    tight = make_state({"lit": lit_result(passages)}, llm2, budget=RunBudget(max_llm_calls=4), draft=draft)
    nli.install(tight)
    asyncio.run(nli.precompute(tight))
    assert len(calls2) == 2  # 4 model calls in the run budget minus the 2 reserved for rewrite and confidence
    assert tight.budget.llm_calls == 2


def test_resolve_backend_order(monkeypatch):
    pred = FakePredictor([])
    patch_loader(monkeypatch, pred)
    llm = ScriptedLLM({})
    monkeypatch.setattr(nli, "_model_available", lambda mid: True)
    assert nli.resolve_backend(make_state({}, llm))[0] == nli.BACKEND_CROSS_ENCODER  # auto, cached model
    nli.reset_cache()
    monkeypatch.setattr(nli, "_model_available", lambda mid: False)
    assert nli.resolve_backend(make_state({}, llm))[0] == nli.BACKEND_LLM  # auto, no model -> agent model
    assert nli.resolve_backend(make_state({}, None))[0] == nli.BACKEND_NONE  # auto, nothing
    monkeypatch.setenv("PHRONESIS_NLI", "off")
    state = make_state({}, llm)
    assert nli.resolve_backend(state)[0] == nli.BACKEND_NONE
    monkeypatch.setenv("PHRONESIS_NLI", "llm")
    assert nli.resolve_backend(make_state({}, llm))[0] == nli.BACKEND_LLM
    none_state = make_state({}, None)
    nli.install(none_state)
    assert "entailment_judge" not in none_state.bag and none_state.bag["nli_backend"] == "none"


# Calibration statistics -----------------------------------------------------------------------------------------
def test_fit_temperature_recovers_known_temperature():
    rng = np.random.default_rng(7)
    z = rng.normal(0.0, 2.0, 20000)
    y = (rng.random(z.size) < cs.logistic(z)).astype(int)
    for t_true in (2.5, 0.5):
        t_hat = cs.fit_temperature(z * t_true, y)  # over/under-confident logits
        assert abs(t_hat - t_true) / t_true < 0.08
    assert cs.ece(cs.apply_temperature(z * 2.5, cs.fit_temperature(z * 2.5, y)), y) < cs.ece(cs.logistic(z * 2.5), y)
    # multi-class logits
    logits = rng.normal(0, 1.5, (8000, 3))
    p = np.exp(logits) / np.exp(logits).sum(1, keepdims=True)
    labels = np.array([rng.choice(3, p=row) for row in p])
    assert abs(cs.fit_temperature(logits * 3.0, labels) - 3.0) < 0.3
    assert cs.fit_temperature([0.2], [1]) == 1.0


def test_ece_reliability_and_brier():
    scores = [0.9] * 10 + [0.1] * 10
    labels = [1] * 5 + [0] * 5 + [0] * 10
    assert math.isclose(cs.ece(scores, labels), 0.5 * 0.4 + 0.5 * 0.1)
    table = cs.reliability_table(scores, labels)
    assert [r["count"] for r in table] == [10, 10] and table[1]["accuracy"] == 0.5
    assert cs.ece([0.0, 1.0], [0, 1]) == 0.0
    assert math.isclose(cs.brier([1.0, 0.0, 0.5], [1, 0, 1]), 0.25 / 3)
    rng = np.random.default_rng(3)
    s = rng.random(50000)
    assert cs.ece(s, (rng.random(s.size) < s).astype(int)) < 0.02  # calibrated scores


def test_conformal_threshold_coverage():
    rng = np.random.default_rng(11)

    def answers(n):
        groups_s, groups_y = [], []
        for _ in range(n):
            k = rng.integers(3, 9)
            y = rng.random(k) < 0.7
            s = np.clip(rng.normal(np.where(y, 0.7, 0.4), 0.15), 0, 1)
            groups_s.append(list(s))
            groups_y.append(list(y.astype(int)))
        return groups_s, groups_y

    alpha = 0.1
    cal_s, cal_y = answers(500)
    tau = cs.conformal_threshold(cal_s, cal_y, alpha)
    test_s, test_y = answers(3000)
    ok = [all(y for s, y in zip(gs, gy) if s > tau) for gs, gy in zip(test_s, test_y)]
    assert np.mean(ok) >= 1 - alpha - 0.02  # every kept claim correct in >= 1 - alpha of answers
    kept = np.mean([s > tau for gs in test_s for s in gs])
    assert 0.0 < kept < 1.0
    # flat per-claim form, tiny-sample abstention, and no incorrect claims
    flat_tau = cs.conformal_threshold([0.9, 0.2, 0.8, 0.3, 0.7], [1, 0, 1, 0, 1], 0.2)
    assert flat_tau == 0.3  # k = ceil(6 * 0.8) = 5th smallest of [-inf, -inf, -inf, 0.2, 0.3]
    assert cs.conformal_threshold([0.9, 0.2, 0.8, 0.3, 0.7], [1, 0, 1, 0, 1], 0.5) == float("-inf")
    assert cs.conformal_threshold([0.5], [0], 0.1) == float("inf")
    assert cs.conformal_threshold([0.9, 0.8, 0.7], [1, 1, 1], 0.5) == float("-inf")


# Confidence ----------------------------------------------------------------------------------------------------
def test_confidence_weights_and_calibration_file(tmp_path, monkeypatch):
    strong = {"nli_entailment": 0.95, "nli_contradiction": 0.01, "n_sources": 3, "best_grade": 0.9, "verifier_status": "supported"}
    weak = {"nli_entailment": 0.05, "nli_contradiction": 0.9, "n_sources": 0, "defect": True, "verifier_status": "contradicted"}
    plain: Dict[str, Any] = {}
    assert conf.score(strong) > 0.9 and conf.band(conf.score(strong)) == "high"
    assert conf.score(weak) < 0.1 and conf.band(conf.score(weak)) == "low"
    assert 0.3 < conf.score(plain) < 0.5
    assert conf.band(0.6) == "medium" and conf.band(0.6, conformal_threshold=0.65) == "low"
    assert conf.score({**strong, "hedged": True}) < conf.score(strong)

    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"temperature": 2.0, "conformal_threshold": 0.6, "weights": {"bias": 0.5}}), encoding="utf-8")
    monkeypatch.setenv("PHRONESIS_CONFIDENCE_CALIBRATION", str(path))
    cal = conf.load_calibration()
    assert cal["temperature"] == 2.0 and cal["conformal_threshold"] == 0.6 and cal["weights"]["bias"] == 0.5
    monkeypatch.setenv("PHRONESIS_CONFIDENCE_T", "1.5")
    assert conf.load_calibration()["temperature"] == 1.5


def test_confidence_phase_uses_nli_conflicts_and_stated_blend(monkeypatch):
    pred = FakePredictor([("kras signaling in pancreatic", ENTAIL)], default=NEUTRAL)
    patch_loader(monkeypatch, pred)
    monkeypatch.setenv("PHRONESIS_NLI", "model")
    stated: List[str] = []

    def p_true(system: str, user: str) -> str:
        stated.append(user)
        ids = re.findall(r'<claim id="([^"]+)">', user)
        return json.dumps({"claims": [{"id": i, "p_true": 1.0} for i in ids]})

    draft = (
        "KRAS signaling drives the growth of pancreatic tumours in these models. "
        "MYC amplification may possibly explain resistance in a subset of colorectal tumours."
    )
    state = make_state(_tool_results(), ScriptedLLM({"confidence": p_true}), draft=draft)
    nli.install(state)
    state.artifacts["evidence_conflicts"] = {
        "conflicts": [{"claim": "MYC amplification explains resistance in colorectal tumours", "label": "contradicts", "likert": 9}]
    }
    art = asyncio.run(conf.compute_claim_confidence(state))
    assert art["nli_backend"] == "cross-encoder" and "stated-p-true" in art["method"] and stated
    first, second = art["claims"]
    assert first["nli_label"] == "entailment" and first["features"]["n_sources"] >= 1
    assert second["features"]["conflict"] and second["features"]["hedged"]
    assert first["confidence"] > second["confidence"]
    assert first["features"]["stated_p_true"] == 1.0
    assert 0.0 <= art["answer_confidence"] <= 1.0 and art["calibration"]["stated_weight"] == 0.2
    json.dumps(art)  # JSON-safe


# End to end through the orchestrator ---------------------------------------------------------------------------
QUESTION = "What does the literature say about KRAS signaling in pancreatic cancer?"
SYNTH = f"## Answer\n\n{MECH_SUPPORTED} KRAS is the most frequently mutated oncogene in pancreatic cancer."


def _run(monkeypatch, tmp_path, responses=None, thread="v1"):
    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path, responses=responses, default_synthesis=SYNTH)
    res, events = asyncio.run(collect_events(orch, query=QUESTION, thread_id=thread, framework=AGENTIC))
    return res, events, calls, llm


def _phases(res) -> List[str]:
    return [p["phase"] for p in res.execution_artifacts["agent_run"]["phases"]]


def test_end_to_end_cross_encoder_judge_and_confidence_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_NLI", "model")
    pred = FakePredictor([], default=NEUTRAL)  # the passage never entails the claim
    loaded = patch_loader(monkeypatch, pred)
    res, _events, calls, _llm = _run(monkeypatch, tmp_path)
    assert any(name == "lithoscribe_literature_synthesis" for name, _ in calls)
    assert loaded and pred.pairs
    # The judge installed at augment is what the verifier used after the draft loop.
    claim = next(c for c in res.verification_report["claims"] if "drives pancreatic" in c["claim_text"])
    assert claim["status"] == "unsupported" and claim["reason"].startswith("semantic check: NLI cross-encoder")
    phases = _phases(res)
    assert {"nli", "nli_precompute", "confidence"} <= set(phases)
    assert phases.index("nli") < phases.index("nli_precompute") < phases.index("confidence")

    art = res.execution_artifacts["claim_confidence"]
    assert set(art) >= {"answer_confidence", "method", "calibration", "claims"}
    assert 0.0 <= art["answer_confidence"] <= 1.0
    assert set(art["calibration"]) >= {"temperature", "conformal_threshold"}
    assert 1 <= len(art["claims"]) <= 50
    row = art["claims"][0]
    assert set(row) >= {"claim_id", "text", "confidence", "band", "features", "nli_label"}
    assert row["band"] in ("high", "medium", "low") and row["nli_label"] == "neutral"
    assert row["features"]["verifier_status"] == "unsupported" and row["features"]["defect"]
    json.dumps(art)


def test_end_to_end_model_judge_precomputed_before_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_NLI", "llm")
    nli_calls: List[str] = []
    res, _events, _calls, llm = _run(monkeypatch, tmp_path, responses={"nli": _scifact_responder("REFUTES", nli_calls)}, thread="v2")
    assert nli_calls and len(nli_calls) <= nli.MAX_LLM_CALLS
    # the model call happened in the after_draft precompute, before the (synchronous) verification used the cache
    phases = llm.phases()
    assert phases.index("nli") > phases.index("synthesis")
    # WS-3.5: a claim still contradicted after correction is removed deterministically (and noted under
    # "Evidence limitations"), so the NLI verdict is checked on the removal step instead of the final report.
    removal = next(t for t in res.thoughts if t.get("id") == "claim_removal")
    removed = removal["extra"]["removed"]
    assert any("drives pancreatic" in (d["claim"] or "") and "NLI llm" in d["description"] for d in removed)
    assert "drives pancreatic tumour growth (PMID" not in res.response.split("Evidence limitations")[0]
    assert res.execution_artifacts["claim_confidence"]["nli_backend"] == "llm"


def test_backend_off_leaves_verification_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_NLI", "off")
    pred = FakePredictor([], default=CONTRA)
    loaded = patch_loader(monkeypatch, pred)
    res, _events, _calls, llm = _run(monkeypatch, tmp_path, thread="v3")
    assert not loaded and "nli" not in llm.phases()
    claims = res.verification_report["claims"]
    claim = next(c for c in claims if "drives pancreatic" in c["claim_text"])
    assert claim["status"] == "supported" and "semantic check" not in claim["reason"]
    assert not any("semantic check" in c["reason"] for c in claims)
    assert "nli" not in _phases(res) and "nli_precompute" not in _phases(res)
    # confidence still runs on deterministic features
    art = res.execution_artifacts["claim_confidence"]
    assert art["nli_backend"] == "none" and art["claims"][0]["nli_label"] is None
