"""Harness extensions: run options, golden facts, hard gates, run metrics, summaries, baselines, repeats, HTTP
transport (httpx.MockTransport) and the golden/audit suites. Offline: fakes only, no network or keys."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from phronesis.evals import baseline, datasets, report, scoring
from phronesis.evals.cli import main as cli_main
from phronesis.evals.runner import Evaluator, RunConfig, aggregate_repeats, load_results, observe
from phronesis.evals.schema import AnswerExpect, CaseResult, Check, EvalCase, Fact

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------------

GOOD_ANSWER = (
    "Sotorasib (NDA 214665, approved May 28, 2021) improved PFS in CodeBreaK 200: HR 0·66 (95% CI 0.51–0.86) "
    "(NCT04303780; PMID: 36764316). Adagrasib (NDA 216340, approved 2022-12-12) in KRYSTAL-12: HR 0.58 (0.45 to 0.76) "
    "(NCT04685135). KRAS is Tclin."
)


def payload(response: str = GOOD_ANSWER, **over: Any) -> Dict[str, Any]:
    """An orchestrator result as to_dict() / POST /api/chat returns it."""
    base = {
        "thread_id": "t", "response": response, "framework_used": "agentic-plan-execute", "selected_agents": ["Lithoscribe"],
        "thoughts": [{"id": "plan", "status": "done"}, {"id": "openfda", "status": "error"}, {"id": "x", "status": "done"}],
        "verification_report": {
            "passed": True, "verification_score": 0.9, "defects": [], "verified_identifiers": ["FDA_APPLICATION:NDA214665"],
            "claims": [{"status": "supported"}, {"status": "supported"}, {"status": "supported"}, {"status": "unsupported"},
                       {"status": "not_checked"}],
        },
        "citations": {"entries": {"PMID:36764316": {"title": "CodeBreaK 200", "journal": "Lancet", "year": "2023"}}, "aliases": {}},
        "run_manifest": {"tool_calls": [
            {"tool_name": "lithoscribe_clinical_trials_search", "success": True, "canonical_ids": {"nct_ids": "NCT04303780,NCT04685135"},
             "output_digest": {}},
            {"tool_name": "openfda_drug_safety", "success": True, "canonical_ids": {"application_numbers": "NDA216340"}, "output_digest": {}},
            {"tool_name": "chembl_drug_profile", "success": False, "canonical_ids": {"chembl_ids": "CHEMBL999"}, "output_digest": {}},
        ]},
        "execution_artifacts": {
            "agent_run": {"budget": {"used": {"tool_calls": 3, "llm_calls": 12, "tokens_estimated": 15000}}},
            # Model-written: an id that appears only here is not evidence.
            "claim_confidence": {"claims": [{"text": "PMID: 11111111"}]},
        },
        "query_context": {"scope": "research"},
        "task_status": {"task_status": "completed"},
    }
    base.update(over)
    return base


class FakeMemory:
    def __init__(self) -> None:
        self.cleared: List[str] = []

    def clear_thread_memory(self, thread_id: str, user_id: Any = None) -> None:
        self.cleared.append(thread_id)


class FakeResult:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.data)


class FakeOrchestrator:
    """Returns the queued payloads in turn (the last one repeats); records every call's keyword arguments."""

    def __init__(self, *payloads: Dict[str, Any], error: Exception | None = None) -> None:
        self.payloads = list(payloads) or [payload()]
        self.memory, self.calls, self.error = FakeMemory(), [], error

    async def execute_inquiry(self, query: str, **kw: Any) -> Any:
        self.calls.append((query, kw))
        if self.error is not None:
            raise self.error
        data = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return FakeResult(data)


def kras_case(**over: Any) -> EvalCase:
    data: Dict[str, Any] = {
        "id": "g1", "suite": "golden", "query": "Compare sotorasib and adagrasib (CodeBreaK 200, KRYSTAL-12).",
        "framework": "agentic-plan-execute", "agent_options": {"budget": {"max_llm_calls": 30}}, "dataset_ids": ["ds_1"],
        "project_id": "prj_1",
        "answer": {"verified": None, "facts": [
            {"id": "nda-s", "pattern": r"NDA\s*214665"},
            {"id": "date-s", "date": "2021-05-28"},
            {"id": "cb200", "value": 0.66, "tolerance": 0.01, "ci": [0.51, 0.86], "context": "CodeBreaK|sotorasib"},
            {"id": "k12", "value": 0.58, "tolerance": 0.01, "ci": [0.45, 0.76], "context": "KRYSTAL"},
            {"id": "opt", "pattern": "Kd of 1 nM", "required": False},
        ]},
    }
    data.update(over)
    return EvalCase.model_validate(data)


def run(ev: Evaluator, cases: List[EvalCase]) -> List[CaseResult]:
    return asyncio.run(ev.run(cases))


# ---------------------------------------------------------------------------------------------------------------
# 1. Run options
# ---------------------------------------------------------------------------------------------------------------

def test_case_run_options_reach_execute_inquiry_and_cli_framework_overrides():
    orch = FakeOrchestrator()
    [res] = run(Evaluator(RunConfig(levels=["answer"]), orchestrator=orch, run_id="t"), [kras_case()])
    _, kw = orch.calls[0]
    assert kw["framework"] == "agentic-plan-execute" and kw["agent_options"] == {"budget": {"max_llm_calls": 30}}
    assert kw["dataset_ids"] == ["ds_1"] and kw["project_id"] == "prj_1"
    assert res.observed["framework_requested"] == "agentic-plan-execute"
    orch2 = FakeOrchestrator()
    run(Evaluator(RunConfig(levels=["answer"], framework="multi-agent-debate"), orchestrator=orch2), [kras_case()])
    assert orch2.calls[0][1]["framework"] == "multi-agent-debate"
    # A case without options passes none (the orchestrator's defaults apply).
    orch3 = FakeOrchestrator()
    run(Evaluator(RunConfig(levels=["answer"]), orchestrator=orch3), [EvalCase(id="p", suite="s", query="q", answer={"verified": None})])
    assert not {"framework", "agent_options", "dataset_ids", "project_id"} & set(orch3.calls[0][1])


def test_cli_framework_flag_reaches_run_config(monkeypatch, tmp_path: Path):
    seen: Dict[str, Any] = {}
    orig = Evaluator.__init__

    def spy(self: Evaluator, config: RunConfig, **kw: Any) -> None:
        seen["cfg"] = config
        orig(self, config, orchestrator=FakeOrchestrator(), **kw)

    monkeypatch.setattr(Evaluator, "__init__", spy)
    suite = tmp_path / "s.jsonl"
    suite.write_text(kras_case().model_dump_json() + "\n")
    rc = cli_main(["run", "--suite", str(suite), "--level", "answer", "--framework", "multi-agent-swarm", "--out", str(tmp_path / "runs")])
    assert rc == 0 and seen["cfg"].framework == "multi-agent-swarm"
    run_dir = next((tmp_path / "runs").iterdir())
    [r] = load_results(run_dir / "results.jsonl")
    assert r.observed["framework_requested"] == "multi-agent-swarm"


def test_per_case_timeout_overrides_the_run_timeout():
    class Slow(FakeOrchestrator):
        async def execute_inquiry(self, query: str, **kw: Any) -> Any:
            await asyncio.sleep(0.5)
            return FakeResult(payload())

    case = kras_case(timeout_s=0.05)
    [res] = run(Evaluator(RunConfig(levels=["answer"], timeout_s=60), orchestrator=Slow()), [case])
    assert res.error and "timed out after 0s" in res.error


# ---------------------------------------------------------------------------------------------------------------
# 2. Golden facts
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "HR 0.30 (95% CI 0.23-0.41)", "HR 0.3 (95% CI 0.23–0.41)", "hazard ratio 0·30; 95% CI, 0.23 to 0.41",
    "| SOLO-1 | 0.30 | 0.23, 0.41 |", "HR = 0.30 [0.23—0.41]",
])
def test_numeric_fact_tolerates_formatting(text):
    fact = Fact(id="solo1", value=0.30, tolerance=0.01, ci=[0.23, 0.41])
    assert scoring.match_fact(fact, scoring._fact_text(text)) is not None


def test_numeric_fact_rejects_wrong_values_and_respects_context():
    fact = Fact(id="hr", value=0.30, tolerance=0.01, ci=[0.23, 0.41], context="SOLO")
    assert scoring.match_fact(fact, scoring._fact_text("SOLO-1: HR 0.33 (0.25-0.45)")) is None  # outside tolerance
    assert scoring.match_fact(fact, scoring._fact_text("SOLO-1: HR 0.30 (0.20-0.50)")) is None  # wrong interval
    assert scoring.match_fact(fact, scoring._fact_text("PRIMA: HR 0.30 (0.23-0.41)")) is None  # wrong context
    assert scoring.match_fact(Fact(id="n", value=-0.5), scoring._fact_text("change −0.5 units")) is not None  # unicode minus
    assert scoring.match_fact(Fact(id="n", value=0.5), scoring._fact_text("change −0.5 units")) is None
    assert scoring.match_fact(Fact(id="k", value=1063), "1,063 reports") is not None
    assert scoring.match_fact(Fact(id="g", value=12), "KRAS G12C") is None  # glued to a word


@pytest.mark.parametrize("text", ["2014-12-19", "December 19, 2014", "Dec. 19, 2014", "19 December 2014", "12/19/2014", "Dec 19th, 2014"])
def test_date_fact_formats(text):
    assert scoring.match_fact(Fact(id="d", date="2014-12-19"), text) is not None
    assert scoring.match_fact(Fact(id="d", date="2014-12-18"), text) is None


def test_fact_recall_missing_list_and_min_recall():
    facts = [Fact(id="a", pattern="PARP1"), Fact(id="b", date="2017-03-27"), Fact(id="c", pattern="zzz", required=False),
             Fact(id="d", any_of=[Fact(id="d1", pattern="KEYNOTE-716"), Fact(id="d2", pattern="NCT03553836")])]
    checks, m = scoring.fact_checks(facts, "PARP1 inhibitors; trial NCT03553836.")
    assert m["fact_recall"] == 0.667 and m["facts_missing"] == ["b"] and m["facts_optional_missing"] == ["c"]
    assert [c.required for c in checks] == [True, True, False, True]
    checks, m = scoring.fact_checks(facts, "PARP1 inhibitors; trial NCT03553836.", min_recall=0.6)
    assert all(not c.required for c in checks if c.name == "answer.fact")
    assert [c.passed for c in checks if c.name == "answer.fact_recall"] == [True]
    with pytest.raises(ValueError, match="exactly one"):
        Fact(id="x", pattern="a", value=1.0)
    with pytest.raises(ValueError, match="invalid regex"):
        EvalCase(id="x", suite="s", query="q", answer={"facts": [{"id": "f", "pattern": "(unclosed"}]})


def test_answer_level_scores_golden_facts():
    [res] = run(Evaluator(RunConfig(levels=["answer"]), orchestrator=FakeOrchestrator()), [kras_case()])
    assert res.passed, [c for c in res.checks if not c.passed and c.required]
    assert res.metrics["fact_recall"] == 1.0 and res.metrics["facts_missing"] == []
    bad = payload("Sotorasib: HR 0.70 (0.51-0.86), NDA 214665.")
    [res] = run(Evaluator(RunConfig(levels=["answer"]), orchestrator=FakeOrchestrator(bad)), [kras_case()])
    assert not res.passed and res.metrics["fact_recall"] == 0.25
    assert set(res.metrics["facts_missing"]) == {"date-s", "cb200", "k12"}


# ---------------------------------------------------------------------------------------------------------------
# 3. Hard gates
# ---------------------------------------------------------------------------------------------------------------

def test_extract_ids_canonicalizes():
    ids = scoring.extract_ids("PMIDs: 123, 456 and PMID 7; NCT 04303780, chembl4535757, NDA #214665, BLA125554, nda 999999")
    assert ids == ["PMID:123", "PMID:456", "PMID:7", "NCT04303780", "CHEMBL4535757", "NDA214665", "BLA125554"]


def test_citations_gate_uses_run_evidence_only():
    obs = observe(payload(), 1.0, query="q")
    ev = set(obs["evidence_ids"])
    assert {"NCT04303780", "NCT04685135", "NDA216340", "NDA214665", "PMID:36764316"} <= ev
    assert "CHEMBL999" not in ev  # a failed tool call is not evidence
    assert "PMID:11111111" not in ev  # model-written artifact
    gate = {c.name: c for c in scoring.gate_checks(obs, query="q")}
    assert gate["answer.citations_in_evidence"].passed
    obs["response"] += " See PMID: 11111111 and CHEMBL999."
    gate = {c.name: c for c in scoring.gate_checks(obs, query="q")}
    assert not gate["answer.citations_in_evidence"].passed and "PMID:11111111" in gate["answer.citations_in_evidence"].detail
    # An id the question itself names is not a hallucination.
    gate = {c.name: c for c in scoring.gate_checks(obs, query="What about PMID 11111111 and CHEMBL999?")}
    assert gate["answer.citations_in_evidence"].passed


def test_leak_gate_and_skip_gates():
    assert scoring.internal_leaks("Per Lithoscribe and the clinical_trials source [CLINICAL_CLAIM], see agent.gaps.9-12.") == [
        "Lithoscribe", "clinical_trials", "[CLINICAL_CLAIM]", "agent.gaps.9-12"]
    assert scoring.internal_leaks("lithoscribe returned total_reports = 5") == ["lithoscribe", "total_reports"]
    assert scoring.internal_leaks("See https://x.org/clinical_trials and `tool_status`; PMID [12] and [Ref 3].") == []
    obs = observe(payload(GOOD_ANSWER + " (Synapta)"), 1.0)
    assert not {c.name: c for c in scoring.gate_checks(obs)}["answer.no_internal_leak"].passed
    assert "answer.no_internal_leak" not in {c.name for c in scoring.gate_checks(obs, skip=["answer.no_internal_leak"])}
    intro = observe(payload("I am Phronesis: Synapta, Vivarix ...", framework_used="conversational-direct"), 0.1)
    assert "answer.no_internal_leak" not in {c.name for c in scoring.gate_checks(intro)}  # the introduction names them


def test_no_crash_gate_on_empty_answer_and_exception():
    obs = observe(payload(""), 1.0)
    assert not {c.name: c for c in scoring.gate_checks(obs)}["answer.no_crash"].passed
    [res] = run(Evaluator(RunConfig(levels=["answer"]), orchestrator=FakeOrchestrator(error=RuntimeError("boom"))), [kras_case()])
    assert not res.passed and "boom" in res.error
    assert [(c.name, c.passed) for c in res.checks] == [("answer.no_crash", False)]
    assert report.gate_failures([res])["answer.no_crash"] == 1


# ---------------------------------------------------------------------------------------------------------------
# 4. Metrics, budgets and summaries
# ---------------------------------------------------------------------------------------------------------------

def test_run_metrics_and_budgets():
    obs = observe(payload(), 12.5)
    m = scoring.run_metrics(obs)
    assert m["faithfulness"] == 0.75 and m["claims_checked"] == 4  # not_checked is excluded
    assert (m["steps_failed"], m["llm_calls"], m["tool_calls"], m["tokens_est"], m["latency_s"]) == (1, 12, 3, 15000, 12.5)
    exp = AnswerExpect(verified=None, max_tokens=10000, max_llm_calls=20, max_tool_calls=3, min_faithfulness=0.8)
    checks = {c.name: c for c in scoring.answer_checks(exp, obs)[0]}
    assert not checks["answer.budget_tokens"].passed and checks["answer.budget_llm_calls"].passed
    assert checks["answer.budget_tool_calls"].passed and not checks["answer.faithfulness"].passed
    # Classic runs record no model calls: the budget is reported, not failed.
    classic = observe(payload(execution_artifacts={}), 1.0)
    c = {c.name: c for c in scoring.answer_checks(AnswerExpect(verified=None, max_llm_calls=5), classic)[0]}["answer.budget_llm_calls"]
    assert c.passed and not c.required


def test_summary_has_framework_breakdown_p95_means_and_gate_failures():
    cases = [kras_case(id=f"c{i}", framework=fw) for i, fw in enumerate(["agentic-plan-execute", "multi-agent-debate", "multi-agent-debate"])]
    orch = FakeOrchestrator(payload(), payload(GOOD_ANSWER + " (Vivarix)"), payload("nothing useful"))
    results = run(Evaluator(RunConfig(levels=["answer"], concurrency=1), orchestrator=orch), cases)
    e = report.summarize(results)["levels"]["answer"]
    assert set(e["by_framework"]) == {"agentic-plan-execute", "multi-agent-debate"}
    assert e["by_framework"]["multi-agent-debate"]["n"] == 2 and "fact_recall_mean" in e["by_framework"]["multi-agent-debate"]
    assert e["latency_p95_s"] is not None
    a = e["answer"]
    assert a["faithfulness_mean"] == 0.75 and a["fact_recall_mean"] is not None and a["llm_calls_mean"] == 12
    assert a["gate_failures"]["answer.no_internal_leak"] == 1 and a["gate_pass_rate"] == pytest.approx(0.667, abs=0.001)
    md = report.markdown({"run_id": "x", "suites": ["golden"]}, report.summarize(results), results)
    assert "| Framework |" in md and "hard-gate failures" in md and "p95 latency" in md


# ---------------------------------------------------------------------------------------------------------------
# 5. Built-in golden and audit suites
# ---------------------------------------------------------------------------------------------------------------

def test_golden_suite_is_live_with_frameworks_and_facts():
    cases = datasets.load_suite("golden")
    assert len(cases) == 7 and datasets.is_live_suite(cases)
    assert {c.framework for c in cases} == {"agentic-plan-execute", "multi-agent-supervisor", "multi-agent-debate",
                                           "authoritative-react-compiler", "multi-agent-swarm"}
    for c in cases:
        assert c.answer and c.answer.facts and c.enable_web_search and c.plan is None and c.timeout_s
        assert any(f.required for f in c.answer.facts)
    by_id = {c.id: c for c in cases}
    kras = {f.id: f for f in by_id["golden-kras-g12c-nsclc"].answer.facts}
    assert kras["codebreak200-pfs-hr"].ci == [0.51, 0.86] and kras["sotorasib-approval-date"].date == "2021-05-28"
    # Facts hold on a correct answer written several ways (registry figures 0.663/0.509-0.864 included).
    text = ("Sotorasib NDA 214665 was approved May 28, 2021; adagrasib NDA216340 on 12 December 2022. CodeBreaK 200: HR 0.663 "
            "(95% CI 0.509–0.864). KRYSTAL-12: HR 0.58 (95% CI 0.45-0.76). Pharos: Tclin.")
    _, m = scoring.fact_checks(by_id["golden-kras-g12c-nsclc"].answer.facts, text)
    assert m["fact_recall"] == 1.0, m
    brca = by_id["golden-brca2-synthetic-lethality"].answer
    assert any("BRCA2" in p for p in brca.must_not_mention)


def test_audit_suite_converts_the_script_cases_with_assertions():
    cases = {c.id: c for c in datasets.load_suite("audit")}
    assert len(cases) >= 22 and datasets.is_live_suite(cases.values())
    assert all(not c.enable_web_search and c.answer is not None for c in cases.values())
    greet = cases["audit-01-greeting"].answer
    assert greet.max_tool_calls == 0 and greet.framework == "conversational-direct"
    assert cases["audit-07-memory-followup"].setup == ["Investigate KRAS G12D resistance mechanisms in PDAC."]
    inj = cases["audit-21-prompt-injection"].answer
    assert inj.abstain and any("API_KEY" in p for p in inj.must_not_mention)
    checks, _ = scoring.answer_checks(inj, {"response": "Yes, compound X cured 100%. NVIDIA_API_KEY=nvapi-abcdefghijklmnopqrstu", "evidence_ids": []})
    assert {c.name for c in checks if not c.passed} >= {"answer.avoids", "answer.abstains"}
    all_ids = [c.id for n in datasets.builtin_suites() for c in datasets.load_suite(n)]
    assert len(all_ids) == len(set(all_ids))


def test_audit_script_prints_deprecation_and_exits():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_evaluation_runner.py")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "deprecated" in out.stderr and "--suite audit" in out.stderr


def test_cli_list_marks_live_suites(capsys):
    assert cli_main(["list"]) == 0
    out = capsys.readouterr().out
    assert "golden" in out and "audit" in out and "(live only)" in out and "multi-agent-debate" in out


# ---------------------------------------------------------------------------------------------------------------
# 6. Baselines and repeats
# ---------------------------------------------------------------------------------------------------------------

def _write_run(d: Path, rows: List[CaseResult]) -> Path:
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in rows))
    (d / "meta.json").write_text(json.dumps({"run_id": d.name, "suites": ["golden"]}))
    return d


def _res(cid: str, passed: bool, recall: float, faith: float = 1.0, leak_ok: bool = True) -> CaseResult:
    return CaseResult(case_id=cid, suite="golden", category="c", level="answer", query="q", latency_s=10.0,
                      checks=[Check(name="x", passed=passed), Check(name="answer.no_internal_leak", passed=leak_ok)],
                      metrics={"fact_recall": recall, "faithfulness": faith}).finalize()


def test_bootstrap_is_deterministic():
    d = [-0.1, 0.0, -0.2, 0.05, -0.3]
    assert baseline.bootstrap_ci(d, seed=3) == baseline.bootstrap_ci(d, seed=3)
    mean, lo, hi = baseline.bootstrap_ci(d)
    assert lo <= mean <= hi and baseline.bootstrap_ci([0.2]) == (0.2, 0.2, 0.2)


def test_baseline_save_and_check_cli(tmp_path: Path, capsys):
    cases = [f"c{i}" for i in range(6)]
    base = _write_run(tmp_path / "base", [_res(c, True, 1.0) for c in cases])
    same = _write_run(tmp_path / "same", [_res(c, True, 1.0 if i % 2 else 0.98) for i, c in enumerate(cases)])
    worse = _write_run(tmp_path / "worse", [_res(c, i == 0, 0.5) for i, c in enumerate(cases)])
    leak = _write_run(tmp_path / "leak", [_res(c, True, 1.0, leak_ok=(c != "c3")) for c in cases])
    bdir = tmp_path / "baselines"
    assert cli_main(["baseline", "check", str(same), "--name", "golden", "--dir", str(bdir)]) == 2  # nothing saved yet
    assert cli_main(["baseline", "save", str(base), "--name", "golden", "--dir", str(bdir)]) == 0
    doc = json.loads((bdir / "golden.json").read_text())
    assert doc["cases"]["c0::answer"]["gates"] == {"answer.no_internal_leak": True} and doc["cases"]["c0::answer"]["fact_recall"] == 1.0
    assert cli_main(["baseline", "check", str(same), "--name", "golden", "--dir", str(bdir), "--delta", "0.03"]) == 0
    assert cli_main(["baseline", "check", str(worse), "--name", "golden", "--dir", str(bdir)]) == 1
    result = json.loads((worse / "baseline_check.json").read_text())
    rows = {r["metric"]: r for r in result["metrics"]}
    assert rows["fact_recall"]["regressed"] and rows["pass"]["regressed"] and rows["fact_recall"]["ci_high"] == -0.5
    assert len(result["newly_failing_cases"]) == 5
    assert cli_main(["baseline", "check", str(leak), "--baseline", str(bdir / "golden.json")]) == 1  # gate newly fails
    result = json.loads((leak / "baseline_check.json").read_text())
    assert result["new_gate_failures"] == [{"case": "c3::answer", "gate": "answer.no_internal_leak"}]
    assert "REGRESSION" in capsys.readouterr().out


def test_repeat_reports_mean_sd_and_flaky():
    good, bad = payload(), payload("Sotorasib NDA 214665 only.")
    orch = FakeOrchestrator(good, bad, good)
    [res] = run(Evaluator(RunConfig(levels=["answer"], repeat=3), orchestrator=orch), [kras_case()])
    assert len(orch.calls) == 3
    rep = res.metrics["repeat"]
    assert rep["n"] == 3 and rep["pass_rate"] == pytest.approx(0.667, abs=0.001)
    assert rep["mean"]["fact_recall"] == pytest.approx(0.75, abs=0.001) and rep["sd"]["fact_recall"] > 0.15
    assert res.metrics["flaky"] and res.passed and res.metrics["fact_recall"] == rep["mean"]["fact_recall"]
    stable = aggregate_repeats([_res("a", True, 1.0), _res("a", True, 1.0)])
    assert not stable.metrics["flaky"] and stable.metrics["repeat"]["sd"]["fact_recall"] == 0.0


# ---------------------------------------------------------------------------------------------------------------
# 7. HTTP transport
# ---------------------------------------------------------------------------------------------------------------

def _mock_api(replies: List[Any]) -> tuple:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        reply = replies[min(sum(r.method == "POST" for r in seen) - 1, len(replies) - 1)]
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def test_via_http_posts_api_chat_and_scores_the_reply():
    client, seen = _mock_api([payload()])
    case = kras_case(setup=["earlier turn"], enable_web_search=True)
    ev = Evaluator(RunConfig(levels=["answer"], via="http://api.test:8000/", model_name="m1"), http_client=client,
                   http_token="tok", run_id="h")
    [res] = run(ev, [case])
    posts = [r for r in seen if r.method == "POST"]
    assert [str(r.url) for r in posts] == ["http://api.test:8000/api/chat"] * 2
    body = json.loads(posts[1].content)
    assert body == {"message": case.query, "thread_id": "eval-h-g1", "enable_web_search": True, "framework": "agentic-plan-execute",
                    "agent_options": {"budget": {"max_llm_calls": 30}}, "dataset_ids": ["ds_1"], "project_id": "prj_1", "model": "m1"}
    assert json.loads(posts[0].content)["message"] == "earlier turn" and posts[0].headers["Authorization"] == "Bearer tok"
    deletes = [r for r in seen if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == ["/api/memory/thread/eval-h-g1"]
    assert res.passed, [c for c in res.checks if not c.passed and c.required]
    assert res.observed["transport"] == "http" and res.observed["http_status"] == 200 and res.metrics["faithfulness"] == 0.75


def test_via_http_errors_fail_the_no_crash_gate():
    client, _ = _mock_api([httpx.Response(500, json={"detail": "Internal error (request abc)"})])
    [res] = run(Evaluator(RunConfig(levels=["answer"], via="http://api.test"), http_client=client), [kras_case()])
    assert not res.passed and "HTTP 500" in res.error and res.checks[0].name == "answer.no_crash"
    client, _ = _mock_api([{"detail": "no response field"}])
    [res] = run(Evaluator(RunConfig(levels=["answer"], via="http://api.test"), http_client=client), [kras_case()])
    assert "no 'response'" in res.error


def test_rescore_keeps_new_metrics_and_gates(tmp_path: Path):
    ev = Evaluator(RunConfig(levels=["answer"]), orchestrator=FakeOrchestrator())
    [old] = run(ev, [kras_case()])
    stricter = kras_case(answer={"verified": None, "facts": [{"id": "x", "pattern": "PARP1"}], "max_llm_calls": 5})
    new = asyncio.run(ev.rescore(stricter, old))
    assert not new.passed and new.metrics["facts_missing"] == ["x"]
    assert {c.name for c in new.checks} >= {"answer.budget_llm_calls", "answer.citations_in_evidence", "answer.no_crash"}
