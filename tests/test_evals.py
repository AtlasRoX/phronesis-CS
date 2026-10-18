"""The evaluation harness: case data, scorers, runner (with fakes), reports, gate and CLI. No network or keys."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from phronesis.evals import datasets, report, scoring
from phronesis.evals.cli import main as cli_main
from phronesis.evals.runner import Evaluator, RunConfig, load_results
from phronesis.evals.schema import AnswerExpect, CaseResult, Check, EvalCase, RetrievalExpect


# ---------------------------------------------------------------------------------------------------------------
# Case data
# ---------------------------------------------------------------------------------------------------------------

def test_builtin_suites_load_and_cover_every_level():
    suites = {name: datasets.load_suite(name) for name in datasets.builtin_suites()}
    assert {"clinical", "behavior"} <= set(suites)
    clinical = suites["clinical"]
    assert len(clinical) >= 20 and all(c.answer and c.reference for c in clinical)
    assert sum(1 for c in clinical if c.retrieval) >= 19
    all_ids = [c.id for cases in suites.values() for c in cases]
    assert len(all_ids) == len(set(all_ids))


def test_cases_reject_broken_patterns():
    with pytest.raises(ValueError, match="control character"):
        EvalCase(id="x", suite="s", query="q", answer={"must_mention": ["\x08evoke"]})
    with pytest.raises(ValueError, match="invalid regex"):
        EvalCase(id="x", suite="s", query="q", retrieval={"gold_title_patterns": ["(unclosed"]})


def test_sources_have_no_stray_control_characters():
    """A lost backslash turns a regex's \\b into a backspace, which silently never matches."""
    root = Path(__file__).resolve().parents[1]
    bad = [str(p.relative_to(root)) for d in ("phronesis", "tests") for p in (root / d).rglob("*.py")
           if any(ch in p.read_text(encoding="utf-8", errors="ignore") for ch in ("\x08", "\x0b", "\x0c"))]
    assert not bad, bad


def test_select_filters_and_samples():
    cases = datasets.load_suite("clinical")
    assert [c.id for c in datasets.select(cases, ids=["clin-sepsis"])] == ["clin-sepsis-criteria"]
    assert {c.category for c in datasets.select(cases, categories=["treatment"])} == {"treatment"}
    a = datasets.select(cases, limit=5, seed=3)
    assert len(a) == 5 and [c.id for c in a] == [c.id for c in datasets.select(cases, limit=5, seed=3)]


def test_external_loaders_read_cached_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PHRONESIS_EVAL_CACHE", str(tmp_path))
    (tmp_path / "pubmedqa_ori_pqal.json").write_text(json.dumps({
        "123": {"QUESTION": "Does X help?", "final_decision": "yes", "LONG_ANSWER": "It does.", "YEAR": "2010"}}))
    (tmp_path / "litqa2_rows.json").write_text(json.dumps([{
        "id": "q1", "question": "Which?", "ideal": "A", "distractors": "['B', 'C']",
        "sources": "['https://doi.org/10.1/abc']", "key-passage": "A is right."}]))
    pq = datasets.load_suite("pubmedqa")[0]
    assert (pq.id, pq.answer.label, pq.retrieval.gold_pmids) == ("pubmedqa-123", "yes", ["123"])
    lq = datasets.load_suite("litqa2")[0]
    assert lq.answer.options[:3] == ["A", "B", "C"] and lq.answer.options[-1].startswith("Insufficient")
    assert lq.retrieval.gold_dois == ["10.1/abc"]
    bio = tmp_path / "bioasq.json"
    bio.write_text(json.dumps({"questions": [
        {"id": "b1", "type": "factoid", "body": "Which gene?", "documents": ["http://www.ncbi.nlm.nih.gov/pubmed/555"],
         "exact_answer": [["TP53", "p53"]], "ideal_answer": ["TP53."]},
        {"id": "b2", "type": "yesno", "body": "Is it?", "documents": [], "exact_answer": "no", "ideal_answer": "No."}]}))
    b = datasets.load_suite("bioasq", bioasq_file=str(bio))
    assert b[0].retrieval.gold_pmids == ["555"] and b[0].answer.exact == [["TP53", "p53"]]
    assert b[1].retrieval is None and b[1].answer.label == "no"
    with pytest.raises(ValueError, match="bioasq-file"):
        datasets.load_suite("bioasq")


# ---------------------------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------------------------

def test_retrieval_scores_rank_recall_and_title_patterns():
    exp = RetrievalExpect(gold_pmids=["2", "9"], k=3)
    checks, m = scoring.retrieval_scores(exp, ["1", "2", "2", "3", "9"])
    assert checks[0].passed and m["first_gold_rank"] == 2 and m["rr"] == 0.5 and m["recall"] == 0.5  # 9 is rank 4 > k
    checks, m = scoring.retrieval_scores(RetrievalExpect(gold_pmids=["7"], gold_title_patterns=[r"\bevoke\b"]),
                                         ["1", "2"], titles=["Other", "Revisiting the evoke trials"])
    assert checks[0].passed and m["first_gold_rank"] == 2 and m["recall"] == 0.0
    checks, _ = scoring.retrieval_scores(RetrievalExpect(gold_pmids=["7"], required=False), ["1"])
    assert not checks[0].passed and not checks[0].required
    assert scoring.retrieval_scores(RetrievalExpect(), ["1"]) == ([], {"retrieved": 1})


def test_abstention_and_refusal():
    assert scoring.abstained("No peer-reviewed evidence was found for ZX999.")
    assert scoring.abstained("ZX999 appears to be hypothetical.")
    assert not scoring.abstained("Pertuzumab plus trastuzumab and docetaxel is standard.")
    assert scoring.opens_with_refusal("No relevant evidence was retrieved. Nothing more can be said.")
    assert not scoring.opens_with_refusal("Use vancomycin. Evidence limitations: no guideline was retrieved.")


def test_answer_checks():
    exp = AnswerExpect(must_mention=["SOFA", "lactate"], must_not_mention=[r"cured 100\s?%"], agents_include=["Lithoscribe"],
                       agents_exclude=["Vivarix"], framework="authoritative-react-compiler", cite_gold=True, min_citations=1)
    obs = {"response": "SOFA rise of 2 (PMID: 26903338); lactate > 2.", "agents": ["Lithoscribe"], "verified": True,
           "framework": "authoritative-react-compiler", "citation_keys": ["PMID:26903338"]}
    checks, m = scoring.answer_checks(exp, obs, ["26903338"])
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert m["gold_cited"] and m["unresolved_citations"] == 0
    bad = {**obs, "response": "Compound X cured 100% of patients (PMID: 1).", "agents": ["Vivarix"], "verified": False}
    failed = {c.name for c in scoring.answer_checks(exp, bad, ["26903338"])[0] if not c.passed}
    assert {"answer.mentions", "answer.avoids", "answer.agent_ran", "answer.agent_skipped", "answer.verified", "answer.cites_gold"} <= failed


def test_label_heuristics_and_exact_answers():
    assert scoring.heuristic_yesno("Yes. The trial showed benefit.") == "yes"
    assert scoring.heuristic_yesno("**No** - it does not.") == "no"
    assert scoring.heuristic_yesno("No retrieved evidence directly addresses this.") is None  # not a verdict
    assert scoring.heuristic_yesno("The evidence is mixed and inconclusive.") is None  # left to the model grader
    assert scoring.heuristic_yesno("Patients with X ...") is None
    opts = ["ciprofloxacin", "meropenem", "Insufficient information to answer the question"]
    assert scoring.heuristic_choice("It evolved resistance to ciprofloxacin only.", opts) == "ciprofloxacin"
    assert scoring.heuristic_choice("No evidence was found.", opts) == opts[2]
    checks, m = scoring.label_checks(AnswerExpect(label="yes"), "Yes")
    assert checks[0].passed and m["answered"]
    _, m = scoring.label_checks(AnswerExpect(label="A"), opts[2])
    assert not m["correct"] and not m["answered"]
    c, m = scoring.exact_checks(AnswerExpect(answer_type="list", exact=[["TP53", "p53"], ["KRAS"], ["EGFR"]]), "KRAS and p53")
    assert c[0].passed and m["list_recall"] == 0.667
    c, _ = scoring.exact_checks(AnswerExpect(answer_type="factoid", exact=[["BRCA1"]]), "It is BRCA2.")
    assert not c[0].passed


# ---------------------------------------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------------------------------------

def test_offline_plan_level_passes_every_builtin_case():
    """A routing regression test: every plan expectation that holds offline must pass."""
    cases = [c for name in ("clinical", "behavior") for c in datasets.load_suite(name) if c.plan]
    results = asyncio.run(Evaluator(RunConfig(levels=["plan"])).run(cases))
    failures = [(r.case_id, [c.detail for c in r.checks if not c.passed]) for r in results if not r.passed and not r.skipped]
    assert not failures, failures


class _FakeMemory:
    def __init__(self) -> None:
        self.cleared: List[str] = []

    def clear_thread_memory(self, thread_id: str, user_id: Any = None) -> None:
        self.cleared.append(thread_id)


class _FakeOrchestrator:
    def __init__(self, response: str) -> None:
        self.response, self.memory, self.calls = response, _FakeMemory(), []

    async def execute_inquiry(self, query: str, **kw: Any) -> Any:
        self.calls.append((query, kw))
        return SimpleNamespace(
            response=self.response, framework_used="authoritative-react-compiler", selected_agents=["Lithoscribe"],
            verification_report={"passed": True, "verification_score": 1.0, "defects": []},
            citations={"entries": {"PMID:26903338": {"title": "Sepsis-3", "journal": "JAMA", "year": "2016"}}},
            run_manifest={"tool_calls": [{"tool_name": "lithoscribe_literature_synthesis", "output_digest": {"pmids": ["26903338"]}}]},
            query_context={"scope": "research"},
        )


class _FakeGrader:
    """Answers the label extractor and the judge with fixed JSON."""

    def __init__(self, choice: str) -> None:
        self.choice = choice

    async def ainvoke(self, messages: Any) -> Any:
        if "fixed options" in messages[0].content:
            return SimpleNamespace(content=json.dumps({"choice": self.choice}))
        return SimpleNamespace(content='{"correctness": 5, "grounding": 4, "completeness": 4, "calibration": 4, "clarity": 5, "overall": 4.4, "rationale": "ok"}')


def test_answer_level_runs_setup_isolates_threads_and_grades():
    orch = _FakeOrchestrator("The answer: SOFA increase of 2 points (PMID: 26903338) with lactate above 2.")
    case = EvalCase(id="c1", suite="s", query="Sepsis criteria?", setup=["earlier turn"],
                    retrieval={"gold_pmids": ["26903338"]}, reference="Sepsis-3.",
                    answer={"must_mention": ["SOFA"], "cite_gold": True, "label": "maybe", "options": ["yes", "no", "maybe"],
                            "answer_type": "yesno"})
    ev = Evaluator(RunConfig(levels=["answer"], judge=True, judge_min=4.0), orchestrator=orch,
                   llm_factory=lambda **kw: _FakeGrader("maybe"), run_id="t")
    [res] = asyncio.run(ev.run([case]))
    assert [q for q, _ in orch.calls] == ["earlier turn", "Sepsis criteria?"]
    assert orch.calls[1][1]["recall_memory"] is True and orch.memory.cleared == ["eval-t-c1"]
    assert res.passed, [c for c in res.checks if not c.passed]
    assert res.metrics["predicted"] == "maybe" and res.metrics["judge"]["overall"] == 4.4 and res.metrics["retrieval_hit"]


def test_rescore_applies_new_expectations_without_calling_the_system():
    orch = _FakeOrchestrator("SOFA rises (PMID: 26903338).")
    case = EvalCase(id="r1", suite="s", query="q", answer={"must_mention": ["SOFA"]})
    ev = Evaluator(RunConfig(levels=["answer"]), orchestrator=orch, llm_factory=lambda **kw: _FakeGrader("x"), run_id="r")
    [old] = asyncio.run(ev.run([case]))
    assert old.passed and len(orch.calls) == 1
    stricter = case.model_copy(update={"answer": AnswerExpect(must_mention=["SOFA", "lactate"])})
    new = asyncio.run(ev.rescore(stricter, old))
    assert not new.passed and len(orch.calls) == 1  # re-scored from stored observations only
    retrieval = CaseResult(case_id="r2", suite="s", category="c", level="retrieval", query="q",
                           observed={"pmids": ["1", "2"], "dois": ["", ""], "titles": ["A", "Revisiting the evoke trials"]})
    re_case = EvalCase(id="r2", suite="s", query="q", retrieval={"gold_pmids": ["9"], "gold_title_patterns": [r"\bevoke\b"]})
    assert asyncio.run(ev.rescore(re_case, retrieval)).passed


def test_failed_live_case_is_retried_and_marked_flaky():
    calls = {"n": 0}

    class Flaky(Evaluator):
        async def run_plan(self, case: EvalCase) -> CaseResult:
            calls["n"] += 1
            r = self._result(case, "plan")
            r.checks = [Check(name="plan.scope", passed=calls["n"] > 1)]
            return r

    case = EvalCase(id="f", suite="s", query="q", plan={"scope": "research"})
    [res] = asyncio.run(Flaky(RunConfig(levels=["plan"], live=True, retries=1)).run([case]))
    assert res.passed and res.metrics["flaky"] and res.metrics["attempts"] == 2
    calls["n"] = -5  # fails twice
    [res] = asyncio.run(Flaky(RunConfig(levels=["plan"], live=True, retries=1)).run([case]))
    assert not res.passed and not res.metrics["flaky"]


def test_errors_and_timeouts_become_results():
    class Broken(Evaluator):
        async def run_plan(self, case: EvalCase) -> CaseResult:
            raise RuntimeError("boom")

    case = EvalCase(id="e", suite="s", query="q", plan={"scope": "research"})
    [res] = asyncio.run(Broken(RunConfig(levels=["plan"])).run([case]))
    assert not res.passed and "boom" in res.error


# ---------------------------------------------------------------------------------------------------------------
# Reports, gate, compare, CLI
# ---------------------------------------------------------------------------------------------------------------

def _result(cid: str, level: str, passed: bool, **metrics: Any) -> CaseResult:
    r = CaseResult(case_id=cid, suite="s", category="cat", level=level, query="q", latency_s=1.0,
                   checks=[Check(name="x", passed=passed)], metrics=metrics)
    return r.finalize()


def test_summary_gate_and_compare(tmp_path: Path):
    base = [_result("a", "retrieval", True, hit=True, recall=1.0, rr=1.0), _result("b", "retrieval", True, hit=True, recall=0.5, rr=0.5)]
    new = [_result("a", "retrieval", True, hit=True, recall=1.0, rr=1.0), _result("b", "retrieval", False, hit=False, recall=0.0, rr=0.0)]
    s = report.summarize(new)
    assert s["levels"]["retrieval"]["pass_rate"] == 0.5 and s["levels"]["retrieval"]["retrieval"]["hit_rate"] == 0.5
    assert report.gate(s, report.parse_thresholds("retrieval.pass_rate=0.5, retrieval.hit_rate=0.5")) == []
    failures = report.gate(s, report.parse_thresholds("retrieval.hit_rate=0.9,answer.pass_rate=0.5"))
    assert len(failures) == 2 and "not measured" in failures[1]
    for name, rows in (("base", base), ("new", new)):
        d = tmp_path / name
        d.mkdir()
        (d / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in rows))
        report.write_reports(d, {"run_id": name, "suites": ["s"], "n_cases": 2}, rows)
        assert (d / "report.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    text = report.compare(tmp_path / "base", tmp_path / "new")
    assert "Regressions (1)" in text and "**b** (retrieval)" in text and "| retrieval | pass_rate | 100% | 50% | -0.500 |" in text


def test_results_file_keeps_the_last_result_per_case(tmp_path: Path):
    path = tmp_path / "results.jsonl"
    path.write_text(_result("a", "plan", False).model_dump_json() + "\n" + _result("a", "plan", True).model_dump_json() + "\n")
    [r] = load_results(path)
    assert r.passed


def test_cli_runs_a_suite_and_applies_the_gate(tmp_path: Path, capsys):
    suite = tmp_path / "mini.jsonl"
    suite.write_text(
        EvalCase(id="m1", suite="mini", query="thanks!", plan={"scope": "conversational", "offline": True}).model_dump_json() + "\n"
        + EvalCase(id="m2", suite="mini", query="What are the diagnostic criteria for sepsis?",
                   plan={"scope": "conversational", "offline": True}).model_dump_json() + "\n"  # wrong on purpose
    )
    out = tmp_path / "runs"
    assert cli_main(["run", "--suite", str(suite), "--level", "plan", "--out", str(out), "--fail-under", "plan.pass_rate=0.5"]) == 0
    assert cli_main(["run", "--suite", str(suite), "--level", "plan", "--out", str(out), "--fail-under", "plan.pass_rate=1"]) == 1
    run_dir = sorted(out.iterdir())[-1]
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["levels"]["plan"]["passed"] == 1 and (run_dir / "report.md").exists()
    assert cli_main(["report", str(run_dir)]) == 0
    assert cli_main(["run", "--suite", "nope", "--out", str(out)]) == 2
    assert cli_main(["list"]) == 0 and "clinical" in capsys.readouterr().out
