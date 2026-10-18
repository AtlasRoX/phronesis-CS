"""
Command line for the Phronesis evaluation harness.

  python -m phronesis.evals list
  python -m phronesis.evals run --suite clinical,behavior --level plan            # offline, seconds, no keys
  python -m phronesis.evals run --suite clinical --level retrieval                # live EuropePMC + planner model
  python -m phronesis.evals run --suite clinical --level answer --judge            # full answers, rubric judge
  python -m phronesis.evals run --suite pubmedqa --level answer --limit 50 --seed 7
  python -m phronesis.evals run --suite bioasq --bioasq-file BioASQ-training12b.json --level retrieval
  python -m phronesis.evals run ... --fail-under "answer.pass_rate=0.8,retrieval.hit_rate=0.7"
  python -m phronesis.evals run --suite golden --level answer --framework multi-agent-supervisor   # override every case
  python -m phronesis.evals run --suite golden --level answer --via http://127.0.0.1:8000          # through POST /api/chat
  python -m phronesis.evals run --suite golden --level answer --ids golden-kras --repeat 3          # mean/SD, flaky flag
  python -m phronesis.evals run --suite golden --level answer --ids golden-kras --record            # live; writes a cassette
  python -m phronesis.evals run --suite golden --level answer --replay                              # offline, from cassettes
  python -m phronesis.evals baseline save eval_runs/<run> --name golden     # eval_runs/baselines/golden.json
  python -m phronesis.evals baseline check eval_runs/<run> --name golden --delta 0.03
  python -m phronesis.evals report eval_runs/<run>          # rebuild report.md / report.html
  python -m phronesis.evals rescore eval_runs/<run>         # re-score stored answers after editing cases/scorers
  python -m phronesis.evals compare eval_runs/<base> eval_runs/<new>
  python -m phronesis.evals fetch pubmedqa litqa2

Every run writes eval_runs/<timestamp>_<suites>_<levels>/ with meta.json, results.jsonl, summary.json, report.md
and report.html. Exit code: 0 ok, 1 below a --fail-under threshold (or a baseline regression), 2 usage error
(or no baseline to check against).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import baseline, datasets, report
from .runner import Evaluator, RunConfig, load_results
from .schema import LEVELS, CaseResult


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(".env")
    except ImportError:
        pass


def _split(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _progress(res: CaseResult, i: int, total: int) -> None:
    status = "skip" if res.skipped else ("PASS" if res.passed else ("ERROR" if res.error else "FAIL"))
    why = ""
    if status in ("FAIL", "ERROR"):
        failed = [c.name for c in res.checks if not c.passed and c.required]
        why = f"  {res.error or ', '.join(failed)}"
    print(f"[{i:>3}/{total}] {status:<5} {res.level:<9} {res.case_id:<40} {res.latency_s or 0:6.1f}s{why}", file=sys.stderr, flush=True)


def cmd_list(_: argparse.Namespace) -> int:
    print("Built-in suites:")
    for name in datasets.builtin_suites():
        cases = datasets.load_suite(name)
        per_level = {lvl: sum(1 for c in cases if getattr(c, lvl) is not None) for lvl in LEVELS}
        cats = sorted({c.category for c in cases})
        live = " (live only)" if datasets.is_live_suite(cases) else ""
        frameworks = sorted({c.framework for c in cases if c.framework})
        fw = f"  frameworks: {', '.join(frameworks)}" if frameworks else ""
        print(f"  {name:<10} {len(cases):>3} cases{live}  levels {per_level}  categories: {', '.join(cats)}{fw}")
    print("External benchmarks (downloaded on first use):")
    print("  pubmedqa   1,000 expert-labelled yes/no/maybe questions (PQA-L)")
    print("  litqa2     199 multiple-choice literature questions (LAB-Bench)")
    print("  bioasq     BioASQ task B; pass --bioasq-file (registration required)")
    print("Any .jsonl file of cases can be passed as a suite.")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    for name in args.names:
        cases = datasets.load_suite(name, refresh=True, bioasq_file=args.bioasq_file)
        print(f"{name}: {len(cases)} cases cached in {datasets.cache_dir()}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _load_env()
    levels = _split(args.level) or ["plan"]
    bad = [lvl for lvl in levels if lvl not in LEVELS]
    if bad:
        print(f"Unknown level(s) {bad}; choose from {LEVELS}", file=sys.stderr)
        return 2
    suites = _split(args.suite) or ["clinical", "behavior"]
    cases = []
    for name in suites:
        cases += datasets.load_suite(name, bioasq_file=args.bioasq_file, refresh=args.refresh)
    cases = datasets.select(cases, ids=_split(args.ids), categories=_split(args.category), tags=_split(args.tags),
                            limit=args.limit, seed=args.seed)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    if args.record and args.replay:
        print("--record and --replay are exclusive", file=sys.stderr)
        return 2
    cassette = "record" if args.record else ("replay" if args.replay else None)
    if args.replay_lenient and not args.replay:
        print("--replay-lenient needs --replay", file=sys.stderr)
        return 2
    if cassette and args.via:
        print(f"--{cassette} runs answers in-process; it cannot be combined with --via", file=sys.stderr)
        return 2
    if cassette and "answer" not in levels:
        print(f"--{cassette} applies to the answer level; add --level answer", file=sys.stderr)
        return 2
    if args.record and args.repeat > 1:
        print("--record keeps one recording per case; drop --repeat (replay with --repeat N checks determinism)", file=sys.stderr)
        return 2
    if cassette and args.resume:
        print(f"--{cassette} cannot be combined with --resume", file=sys.stderr)
        return 2

    config = RunConfig(
        levels=levels, model_name=args.model, live=args.live,
        concurrency=args.concurrency, timeout_s=args.timeout, judge=args.judge, judge_model=args.judge_model,
        judge_min=args.judge_min, grade_with_model=not args.no_model_grading,
        web_search=False if args.no_web else None, retries=args.retries, answer_retries=args.answer_retries,
        framework=args.framework, via=args.via, repeat=max(1, args.repeat), flaky_sd=args.flaky_sd,
        cassette=cassette, cassette_dir=args.cassette_dir, replay_lenient=args.replay_lenient,
    )
    if args.via and "answer" not in levels:
        print("--via only applies to the answer level; plan and retrieval still run in-process", file=sys.stderr)
    if args.resume:
        run_dir = Path(args.resume)
        old_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        evaluator = Evaluator(config, run_id=old_meta.get("run_id"))
        done = {(r.case_id, r.level) for r in load_results(run_dir / "results.jsonl") if not r.error}
        # Errored results are retried: drop them so each (case, level) appears once.
        kept = [r for r in load_results(run_dir / "results.jsonl") if not r.error]
        (run_dir / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in kept), encoding="utf-8")
        meta = old_meta
    else:
        evaluator = Evaluator(config)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_dir = Path(args.out) / f"{stamp}_{'+'.join(Path(s).stem for s in suites)}_{'+'.join(levels)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        done = set()
        meta = evaluator.meta(suites, len(cases))
        meta["bioasq_file"] = args.bioasq_file
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    print(f"Running {len(cases)} cases at {', '.join(levels)} -> {run_dir}", file=sys.stderr)
    t0 = time.perf_counter()
    async def go() -> None:
        try:
            await evaluator.run(cases, results_path=run_dir / "results.jsonl", done=done, on_result=_progress)
        finally:
            await evaluator.aclose()

    asyncio.run(go())
    meta["duration_s"] = round(time.perf_counter() - t0, 1) + float(meta.get("duration_s") or 0)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    summary = report.write_reports(run_dir, meta, load_results(run_dir / "results.jsonl"))

    print(file=sys.stderr)
    for level, e in summary["levels"].items():
        extra = e.get("retrieval") or e.get("answer") or {}
        bits = ", ".join(f"{k} {v:.0%}" if isinstance(v, float) and v <= 1 else f"{k} {v}"
                         for k, v in extra.items() if v is not None and not isinstance(v, dict))
        rate = e["pass_rate"]
        flaky = f", {e['flaky']} flaky" if e.get("flaky") else ""
        print(f"{level:<9} pass {rate:.0%} ({e['passed']}/{e['scored']}{flaky})" if rate is not None else f"{level:<9} nothing scored",
              f"· {bits}" if bits else "", file=sys.stderr)
    print(f"\nReport: {run_dir / 'report.html'}", file=sys.stderr)

    failures = report.gate({"levels": summary["levels"]}, report.parse_thresholds(args.fail_under))
    if cassette == "replay":
        # A replay must reproduce its recording: a call the cassette does not hold fails the run.
        results = load_results(run_dir / "results.jsonl")
        missed = [r.case_id for r in results if any(c.name == "answer.cassette" and not c.passed for c in r.checks)]
        replayed = sum(1 for r in results if r.level == "answer" and not r.skipped)
        print(f"Replayed {replayed} case(s) from cassettes; {len(results) - replayed} without a cassette skipped", file=sys.stderr)
        if missed:
            failures.append(f"cassette replay: {len(missed)} case(s) made unrecorded calls ({', '.join(missed)}); "
                            "re-record them with --record if the change is intended")
    for f in failures:
        print(f"GATE FAILED  {f}", file=sys.stderr)
    return 1 if failures else 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-apply current expectations and scorers to a run's stored observations (the system is not called)."""
    _load_env()
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    cases = {}
    for name in meta.get("suites", []):
        for c in datasets.load_suite(name, bioasq_file=args.bioasq_file or meta.get("bioasq_file")):
            cases[c.id] = c
    old = load_results(run_dir / "results.jsonl")
    config = RunConfig(levels=list(meta.get("config", {}).get("levels", LEVELS)), judge_min=args.judge_min,
                       judge_model=args.judge_model, grade_with_model=not args.no_model_grading)
    evaluator = Evaluator(config, run_id=meta.get("run_id"))

    async def go() -> list:
        out = []
        for r in old:
            case = cases.get(r.case_id)
            out.append(await evaluator.rescore(case, r, rejudge=args.rejudge) if case is not None else r)
        return out

    new = asyncio.run(go())
    (run_dir / "results.prev.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in old), encoding="utf-8")
    (run_dir / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in new), encoding="utf-8")
    meta["rescored_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    summary = report.write_reports(run_dir, meta, new)
    changed = sum(1 for a, b in zip(old, new) if a.passed != b.passed)
    for level, e in summary["levels"].items():
        print(f"{level:<9} pass {e['pass_rate']:.0%} ({e['passed']}/{e['scored']})" if e["pass_rate"] is not None else f"{level:<9} nothing scored")
    print(f"{changed} case(s) changed outcome; previous results kept in results.prev.jsonl. Report: {run_dir / 'report.html'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    report.write_reports(run_dir, meta, load_results(run_dir / "results.jsonl"))
    print(run_dir / "report.html")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """baseline save <run_dir> | baseline check <run_dir>: per-case snapshots and a paired regression check."""
    directory = Path(args.dir) if args.dir else None
    if args.action == "save":
        out = baseline.save(Path(args.run_dir), name=args.name or "baseline", directory=directory, path=args.baseline)
        print(f"Baseline saved: {out}")
        return 0
    path = baseline.baseline_path(args.name, directory=directory, path=args.baseline)
    if not path.exists():
        print(f"No baseline at {path}; save one with: baseline save <run_dir> --name {args.name or 'baseline'}", file=sys.stderr)
        return 2
    base = json.loads(path.read_text(encoding="utf-8"))
    result = baseline.check(base, Path(args.run_dir), delta=args.delta, metrics=_split(args.metrics) or baseline.CHECK_METRICS,
                            seed=args.seed, n_boot=args.n_boot)
    text = baseline.check_markdown(result)
    print(text)
    (Path(args.run_dir) / "baseline_check.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    (Path(args.run_dir) / "baseline_check.md").write_text(text, encoding="utf-8")
    for f in result["failures"]:
        print(f"REGRESSION  {f}", file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_compare(args: argparse.Namespace) -> int:
    text = report.compare(Path(args.base), Path(args.new))
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m phronesis.evals", description="Evaluate Phronesis at the plan, retrieval and answer levels.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list suites").set_defaults(fn=cmd_list)

    f = sub.add_parser("fetch", help="download external benchmarks into the cache")
    f.add_argument("names", nargs="+", choices=list(datasets.EXTERNAL))
    f.add_argument("--bioasq-file")
    f.set_defaults(fn=cmd_fetch)

    r = sub.add_parser("run", help="run cases")
    r.add_argument("--suite", help="comma list: built-in names, pubmedqa, litqa2, bioasq, or .jsonl paths (default clinical,behavior)")
    r.add_argument("--level", help="comma list of plan, retrieval, answer (default plan)")
    r.add_argument("--ids", help="comma list of case ids or id prefixes")
    r.add_argument("--category", help="comma list of categories")
    r.add_argument("--tags", help="comma list of tags")
    r.add_argument("--limit", type=int, help="at most this many cases")
    r.add_argument("--seed", type=int, help="sample --limit cases at random with this seed")
    r.add_argument("--live", action="store_true", help="plan level: use the live entity resolver and planner model")
    r.add_argument("--model", help="model for the orchestrator and planner")
    r.add_argument("--concurrency", type=int, default=2, help="answer-level cases at once (default 2)")
    r.add_argument("--timeout", type=float, default=420.0, help="seconds per case and level (default 420)")
    r.add_argument("--judge", action="store_true", help="score answers with the rubric judge")
    r.add_argument("--judge-model", help="model for the judge and label grader")
    r.add_argument("--judge-min", type=float, help="fail answers whose judge overall score is below this")
    r.add_argument("--no-model-grading", action="store_true", help="grade benchmark labels by rule only")
    r.add_argument("--no-web", action="store_true", help="disable web search for every case")
    r.add_argument("--retries", type=int, default=1, help="re-run failed live plan/retrieval cases (default 1); passes on retry count as flaky")
    r.add_argument("--answer-retries", type=int, default=0, help="re-run failed answers (default 0)")
    r.add_argument("--framework", help="orchestration framework for every case (overrides each case's framework)")
    r.add_argument("--via", help="base URL of a running API (http://127.0.0.1:8000): answer cases POST /api/chat; "
                                 "a bearer token is read from PHRONESIS_EVAL_TOKEN")
    r.add_argument("--repeat", type=int, default=1, help="runs per case: mean/SD per metric and a flaky flag (default 1)")
    r.add_argument("--flaky-sd", type=float, default=0.15, help="with --repeat: SD of fact_recall/faithfulness above which a case is flaky")
    r.add_argument("--record", action="store_true", help="answer level, in-process: run live and record every tool and model "
                                                          "call to a cassette per case (overwrites an existing one)")
    r.add_argument("--replay", action="store_true", help="answer level, in-process: replay cassettes offline (no keys, no "
                                                          "network); a call that was not recorded fails the case; cases "
                                                          "without a cassette are skipped")
    r.add_argument("--replay-lenient", action="store_true", help="with --replay: answer a missed call with the nearest "
                                                                  "recording and report it instead of failing")
    r.add_argument("--cassette-dir", help="cassette directory (default phronesis/evals/cassettes)")
    r.add_argument("--bioasq-file", help="BioASQ task B JSON")
    r.add_argument("--refresh", action="store_true", help="re-download external benchmarks")
    r.add_argument("--out", default="eval_runs", help="directory for runs (default eval_runs)")
    r.add_argument("--resume", help="continue an interrupted run directory")
    r.add_argument("--fail-under", help='thresholds, e.g. "plan.pass_rate=1,answer.pass_rate=0.8,retrieval.hit_rate=0.7"')
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("rescore", help="re-score a run's stored observations with the current cases and scorers")
    rs.add_argument("run_dir")
    rs.add_argument("--rejudge", action="store_true", help="ask the judge and label grader again")
    rs.add_argument("--judge-model")
    rs.add_argument("--judge-min", type=float)
    rs.add_argument("--no-model-grading", action="store_true")
    rs.add_argument("--bioasq-file")
    rs.set_defaults(fn=cmd_rescore)

    rep = sub.add_parser("report", help="rebuild a run's reports")
    rep.add_argument("run_dir")
    rep.set_defaults(fn=cmd_report)

    b = sub.add_parser("baseline", help="save a run as a per-case baseline, or check a run against one")
    b.add_argument("action", choices=["save", "check"])
    b.add_argument("run_dir")
    b.add_argument("--name", help="baseline name (default baseline): <dir>/<name>.json")
    b.add_argument("--dir", help=f"baseline directory (default {baseline.DEFAULT_DIR})")
    b.add_argument("--baseline", help="explicit baseline file (instead of --name/--dir), e.g. a committed one")
    b.add_argument("--delta", type=float, default=0.03, help="tolerated drop: fail when a metric's CI upper bound is below -delta")
    b.add_argument("--metrics", help=f"comma list (default {','.join(baseline.CHECK_METRICS)})")
    b.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    b.add_argument("--n-boot", type=int, default=2000, help="bootstrap resamples (default 2000)")
    b.set_defaults(fn=cmd_baseline)

    c = sub.add_parser("compare", help="compare two runs (metric deltas, regressions, fixes)")
    c.add_argument("base")
    c.add_argument("new")
    c.add_argument("--out", help="also write the comparison to this file")
    c.set_defaults(fn=cmd_compare)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
