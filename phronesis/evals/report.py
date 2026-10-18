"""
Summaries, reports, run comparison and the CI gate.

summary.json is the machine-readable result of a run: for each level the case pass rate (over cases that had
something to score), pass rates by suite, category, framework and check, and the level's metrics (retrieval hit
rate, recall and MRR; answer verifier pass rate, fact recall, faithfulness, hard-gate failures, model/tool calls,
tokens, benchmark accuracy, judge scores, latency percentiles).
"""

from __future__ import annotations

import html
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import CaseResult
from .scoring import GATES


def _rate(passed: int, n: int) -> Optional[float]:
    return round(passed / n, 3) if n else None


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 2)


def _mean(values: Iterable[Any]) -> Optional[float]:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(statistics.mean(vals), 3) if vals else None


def summarize(results: Sequence[CaseResult]) -> Dict[str, Any]:
    levels: Dict[str, Any] = {}
    by_level: Dict[str, List[CaseResult]] = defaultdict(list)
    for r in results:
        by_level[r.level].append(r)
    for level, rs in by_level.items():
        scored = [r for r in rs if r.skipped is None]
        groups: Dict[str, Dict[str, List[CaseResult]]] = {"by_suite": defaultdict(list), "by_category": defaultdict(list),
                                                          "by_framework": defaultdict(list)}
        for r in scored:
            groups["by_suite"][r.suite].append(r)
            groups["by_category"][r.category].append(r)
            framework = r.observed.get("framework_requested") or r.observed.get("framework")
            if framework:
                groups["by_framework"][str(framework)].append(r)
        checks: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for r in scored:
            for c in r.checks:
                checks[c.name][1] += 1
                checks[c.name][0] += int(c.passed)
        entry: Dict[str, Any] = {
            "n": len(rs),
            "scored": len(scored),
            "skipped": len(rs) - len(scored),
            "errors": sum(1 for r in rs if r.error),
            "flaky": sum(1 for r in scored if r.metrics.get("flaky")),
            "passed": sum(1 for r in scored if r.passed),
            "pass_rate": _rate(sum(1 for r in scored if r.passed), len(scored)),
            **{g: {k: {"n": len(v), "passed": sum(x.passed for x in v), "pass_rate": _rate(sum(x.passed for x in v), len(v))}
                   for k, v in sorted(d.items())} for g, d in groups.items()},
            "checks": {k: {"n": n, "passed": p, "pass_rate": _rate(p, n)} for k, (p, n) in sorted(checks.items())},
            "latency_p50_s": _pct([r.latency_s for r in scored if r.latency_s is not None], 0.5),
            "latency_p90_s": _pct([r.latency_s for r in scored if r.latency_s is not None], 0.9),
            "latency_p95_s": _pct([r.latency_s for r in scored if r.latency_s is not None], 0.95),
        }
        for name, members in groups["by_framework"].items():
            ms = [x.metrics for x in members]
            entry["by_framework"][name].update({
                "fact_recall_mean": _mean(m.get("fact_recall") for m in ms),
                "faithfulness_mean": _mean(m.get("faithfulness") for m in ms),
                "latency_p50_s": _pct([x.latency_s for x in members if x.latency_s is not None], 0.5),
                "latency_p95_s": _pct([x.latency_s for x in members if x.latency_s is not None], 0.95),
                "gate_failures": sum(gate_failures(members).values()),
            })
        metrics = [r.metrics for r in scored]
        if level == "retrieval":
            with_gold = [m for m in metrics if "hit" in m]
            entry["retrieval"] = {
                "cases_with_gold": len(with_gold),
                "hit_rate": _rate(sum(bool(m["hit"]) for m in with_gold), len(with_gold)),
                "recall_mean": _mean(m.get("recall") for m in with_gold),
                "mrr": _mean(m.get("rr") for m in with_gold),
                "llm_plan_share": _rate(sum(m.get("plan_source") == "llm" for m in metrics), len(metrics)),
            }
        if level == "answer":
            labelled = [m for m in metrics if "correct" in m]
            answered = [m for m in labelled if m.get("answered")]
            judged = [m["judge"] for m in metrics if isinstance(m.get("judge"), dict)]
            entry["answer"] = {
                "verified_rate": _rate(sum(m.get("verified") is True for m in metrics), sum(m.get("verified") is not None for m in metrics)),
                "abstained_rate": _rate(sum(bool(m.get("abstained")) for m in metrics), len(metrics)),
                "gold_cited_rate": _rate(sum(m.get("gold_cited") is True for m in metrics), sum(m.get("gold_cited") is not None for m in metrics)),
                "retrieval_hit_rate": _rate(sum(m.get("retrieval_hit") is True for m in metrics), sum("retrieval_hit" in m for m in metrics)),
                "label_accuracy": _rate(sum(m["correct"] for m in labelled), len(labelled)),
                "label_precision": _rate(sum(m["correct"] for m in answered), len(answered)),
                "label_coverage": _rate(len(answered), len(labelled)),
                "factoid_accuracy": _rate(sum(m.get("factoid_correct") is True for m in metrics), sum("factoid_correct" in m for m in metrics)),
                "list_recall_mean": _mean(m.get("list_recall") for m in metrics),
                "judge": {d: _mean(j.get(d) for j in judged) for d in ("overall", "correctness", "grounding", "completeness", "calibration", "clarity")} if judged else None,
                "words_mean": _mean(m.get("length_words") for m in metrics),
                "fact_recall_mean": _mean(m.get("fact_recall") for m in metrics),
                "faithfulness_mean": _mean(m.get("faithfulness") for m in metrics),
                "steps_failed_mean": _mean(m.get("steps_failed") for m in metrics),
                "llm_calls_mean": _mean(m.get("llm_calls") for m in metrics),
                "tool_calls_mean": _mean(m.get("tool_calls") for m in metrics),
                "tokens_est_mean": _mean(m.get("tokens_est") for m in metrics),
                "gate_pass_rate": _rate(sum(1 for r in scored if _gates_ok(r)), sum(1 for r in scored if _gated(r))),
                "gate_failures": gate_failures(scored),
            }
        levels[level] = entry
    return {"levels": levels, "n_results": len(results)}


def _gated(r: CaseResult) -> bool:
    return any(c.name in GATES for c in r.checks)


def _gates_ok(r: CaseResult) -> bool:
    return _gated(r) and all(c.passed for c in r.checks if c.name in GATES)


def gate_failures(results: Iterable[CaseResult]) -> Dict[str, int]:
    """Hard-gate failures by gate (answer.citations_in_evidence, answer.no_internal_leak, answer.no_crash)."""
    out = {g: 0 for g in GATES}
    for r in results:
        for c in r.checks:
            if c.name in GATES and not c.passed:
                out[c.name] += 1
    return out


# ---------------------------------------------------------------------------------------------------------------
# Markdown and HTML
# ---------------------------------------------------------------------------------------------------------------

def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and 0 <= v <= 1:
        return f"{v:.0%}"
    return str(v)


def _failed_checks(r: CaseResult) -> List[str]:
    return [f"{c.name}: {c.detail}" for c in r.checks if not c.passed and c.required]


def markdown(meta: Dict[str, Any], summary: Dict[str, Any], results: Sequence[CaseResult]) -> str:
    lines = [f"# Phronesis evaluation {meta.get('run_id', '')}", ""]
    lines.append(f"Suites **{', '.join(meta.get('suites', []))}** · {meta.get('n_cases')} cases · model `{meta.get('model')}` · "
                 f"code `{meta.get('source_fingerprint')}` · started {meta.get('started_at')}")
    if meta.get("duration_s") is not None:
        lines[-1] += f" · {meta['duration_s']:.0f}s"
    lines += ["", "| Level | Pass rate | Passed | Scored | Skipped | Errors | Flaky | p50 latency | p90 latency | p95 latency |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for level, e in summary["levels"].items():
        lines.append(f"| {level} | **{_fmt(e['pass_rate'])}** | {e['passed']} | {e['scored']} | {e['skipped']} | {e['errors']} | {e.get('flaky', 0)} | "
                     f"{_fmt(e['latency_p50_s'])}s | {_fmt(e['latency_p90_s'])}s | {_fmt(e.get('latency_p95_s'))}s |")
    for level, e in summary["levels"].items():
        lines += ["", f"## {level.capitalize()}", ""]
        for key in ("retrieval", "answer"):
            if e.get(key):
                parts = []
                for k, v in e[key].items():
                    if k == "gate_failures" and isinstance(v, dict):
                        failed = {g: n for g, n in v.items() if n}
                        parts.append("hard gates **all passed**" if not failed else
                                     "hard-gate failures " + ", ".join(f"**{g.split('.', 1)[-1]} {n}**" for g, n in failed.items()))
                    elif isinstance(v, dict):
                        parts.append(f"judge {', '.join(f'{d} {x}' for d, x in v.items() if x is not None)}")
                    elif v is not None:
                        parts.append(f"{k.replace('_', ' ')} **{_fmt(v)}**")
                lines += [" · ".join(parts), ""]
        if e.get("by_framework"):
            lines += ["| Framework | Pass rate | n | Fact recall | Faithfulness | p50 latency | p95 latency | Gate failures |",
                      "|---|---|---|---|---|---|---|---|"]
            lines += [f"| {k} | {_fmt(v['pass_rate'])} | {v['n']} | {_fmt(v.get('fact_recall_mean'))} | {_fmt(v.get('faithfulness_mean'))} | "
                      f"{_fmt(v.get('latency_p50_s'))}s | {_fmt(v.get('latency_p95_s'))}s | {v.get('gate_failures', 0)} |"
                      for k, v in e["by_framework"].items()]
            lines.append("")
        lines += ["| Category | Pass rate | n |", "|---|---|---|"]
        lines += [f"| {k} | {_fmt(v['pass_rate'])} | {v['n']} |" for k, v in e["by_category"].items()]
        lines += ["", "| Check | Pass rate | n |", "|---|---|---|"]
        lines += [f"| `{k}` | {_fmt(v['pass_rate'])} | {v['n']} |" for k, v in e["checks"].items()]
        failing = [r for r in results if r.level == level and not r.passed and r.skipped is None]
        if failing:
            lines += ["", f"### Failing cases ({len(failing)})", ""]
            for r in sorted(failing, key=lambda r: r.case_id):
                reason = r.error or "; ".join(_failed_checks(r))
                lines.append(f"- **{r.case_id}** ({r.category}): {reason}")
    return "\n".join(lines) + "\n"


_HTML_STYLE = """
:root{--bg:#fff;--fg:#1d2433;--muted:#5b6475;--line:#e3e6ec;--ok:#137a3f;--bad:#b42318;--card:#f7f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e7eaf0;--muted:#9aa3b2;--line:#2a2f3a;--ok:#4ade80;--bad:#f87171;--card:#1a1e27}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.muted{color:var(--muted)}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0;padding:6px 10px}
summary{cursor:pointer}pre{white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,monospace}
input{padding:6px 8px;width:100%;max-width:360px;margin:8px 0;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px}
"""


def html_report(meta: Dict[str, Any], summary: Dict[str, Any], results: Sequence[CaseResult]) -> str:
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(level)}</td><td class='{'ok' if (e['pass_rate'] or 0) >= 0.8 else 'bad'}'>{_fmt(e['pass_rate'])}</td>"
        f"<td>{e['passed']}/{e['scored']}</td><td>{e['skipped']}</td><td>{e['errors']}</td><td>{_fmt(e['latency_p50_s'])}s</td></tr>"
        for level, e in summary["levels"].items()
    )
    cards = []
    for r in sorted(results, key=lambda r: (r.level, r.passed, r.case_id)):
        status = "skip" if r.skipped else ("PASS" if r.passed else "FAIL")
        cls = "muted" if r.skipped else ("ok" if r.passed else "bad")
        checks = "".join(
            f"<li class='{'ok' if c.passed else ('bad' if c.required else 'muted')}'>{esc(c.name)}: {esc(c.detail)}"
            f"{'' if c.required else ' (informational)'}</li>" for c in r.checks
        )
        obs = dict(r.observed)
        answer = obs.pop("response", None)
        body = f"<p class='muted'>{esc(r.query)}</p><ul>{checks}</ul>"
        if r.error:
            body += f"<p class='bad'>{esc(r.error)}</p>"
        if answer:
            body += f"<details><summary>Answer</summary><pre>{esc(answer)}</pre></details>"
        body += f"<details><summary>Observed and metrics</summary><pre>{esc(json.dumps({'metrics': r.metrics, **obs}, indent=1, default=str)[:20000])}</pre></details>"
        cards.append(f"<details class='case' data-text='{esc((r.case_id + ' ' + r.category + ' ' + status).lower())}'>"
                     f"<summary><span class='{cls}'>{status}</span> · {esc(r.level)} · <b>{esc(r.case_id)}</b> "
                     f"<span class='muted'>{esc(r.category)} · {r.latency_s or 0:.1f}s</span></summary>{body}</details>")
    script = ("document.getElementById('f').addEventListener('input',e=>{const q=e.target.value.toLowerCase();"
              "document.querySelectorAll('details.case').forEach(d=>d.style.display=d.dataset.text.includes(q)?'':'none')})")
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Phronesis evaluation</title><style>{_HTML_STYLE}</style></head><body>"
        f"<h1>Phronesis evaluation {esc(str(meta.get('run_id', '')))}</h1>"
        f"<p class='muted'>{esc(', '.join(meta.get('suites', [])))} · {meta.get('n_cases')} cases · {esc(str(meta.get('model')))} · "
        f"code {esc(str(meta.get('source_fingerprint')))} · {esc(str(meta.get('started_at')))}</p>"
        f"<table><tr><th>Level</th><th>Pass rate</th><th>Passed</th><th>Skipped</th><th>Errors</th><th>p50</th></tr>{rows}</table>"
        f"<pre>{esc(json.dumps({k: {m: v.get(m) for m in ('retrieval', 'answer') if v.get(m)} for k, v in summary['levels'].items()}, indent=1))}</pre>"
        f"<input id='f' placeholder='Filter: case id, category, fail, pass'>{''.join(cards)}<script>{script}</script></body></html>"
    )


def write_reports(run_dir: Path, meta: Dict[str, Any], results: Sequence[CaseResult]) -> Dict[str, Any]:
    summary = summarize(results)
    (run_dir / "summary.json").write_text(json.dumps({"meta": meta, **summary}, indent=1), encoding="utf-8")
    (run_dir / "report.md").write_text(markdown(meta, summary, results), encoding="utf-8")
    (run_dir / "report.html").write_text(html_report(meta, summary, results), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------------------------------------------
# Comparison and gate
# ---------------------------------------------------------------------------------------------------------------

def _lookup(summary: Dict[str, Any], path: str) -> Optional[float]:
    """'answer.pass_rate', 'retrieval.retrieval.hit_rate' or the short form 'retrieval.hit_rate'."""
    parts = path.split(".")
    node: Any = summary.get("levels", {})
    for i, p in enumerate(parts):
        if isinstance(node, dict) and p in node:
            node = node[p]
        elif i == 1 and isinstance(node, dict) and isinstance(node.get(parts[0]), dict) and p in node[parts[0]]:
            node = node[parts[0]][p]
        else:
            return None
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def parse_thresholds(spec: Optional[str]) -> List[Tuple[str, float]]:
    out = []
    for part in re.split(r"[,\s]+", spec or ""):
        if part:
            key, _, value = part.partition("=")
            out.append((key.strip(), float(value)))
    return out


def gate(summary: Dict[str, Any], thresholds: Sequence[Tuple[str, float]]) -> List[str]:
    """Failures as messages; an empty list means the run passes every threshold."""
    failures = []
    for key, minimum in thresholds:
        value = _lookup(summary, key)
        if value is None:
            failures.append(f"{key}: not measured in this run")
        elif value < minimum:
            failures.append(f"{key}: {value:.3f} < {minimum:.3f}")
    return failures


_COMPARE_KEYS = ("pass_rate", "retrieval.hit_rate", "retrieval.recall_mean", "retrieval.mrr", "answer.verified_rate",
                 "answer.label_accuracy", "answer.gold_cited_rate", "answer.judge.overall", "answer.fact_recall_mean",
                 "answer.faithfulness_mean", "answer.gate_pass_rate", "answer.llm_calls_mean", "answer.tokens_est_mean",
                 "latency_p50_s", "latency_p95_s")


def compare(base_dir: Path, new_dir: Path) -> str:
    from .runner import load_results

    base = json.loads((base_dir / "summary.json").read_text(encoding="utf-8"))
    new = json.loads((new_dir / "summary.json").read_text(encoding="utf-8"))
    lines = [f"# Comparison: {base['meta'].get('run_id')} → {new['meta'].get('run_id')}", "",
             f"code `{base['meta'].get('source_fingerprint')}` → `{new['meta'].get('source_fingerprint')}`", "",
             "| Level | Metric | Base | New | Δ |", "|---|---|---|---|---|"]
    for level in sorted(set(base["levels"]) | set(new["levels"])):
        for key in _COMPARE_KEYS:
            a, b = _lookup(base, f"{level}.{key}"), _lookup(new, f"{level}.{key}")
            if a is None and b is None:
                continue
            delta = "" if a is None or b is None else f"{b - a:+.3f}"
            lines.append(f"| {level} | {key} | {_fmt(a)} | {_fmt(b)} | {delta} |")
    old = {(r.case_id, r.level): r for r in load_results(base_dir / "results.jsonl") if r.skipped is None}
    cur = {(r.case_id, r.level): r for r in load_results(new_dir / "results.jsonl") if r.skipped is None}
    regressions = sorted(k for k in old.keys() & cur.keys() if old[k].passed and not cur[k].passed)
    fixes = sorted(k for k in old.keys() & cur.keys() if not old[k].passed and cur[k].passed)
    lines += ["", f"## Regressions ({len(regressions)})", ""]
    lines += [f"- **{cid}** ({lvl}): {cur[(cid, lvl)].error or '; '.join(_failed_checks(cur[(cid, lvl)]))}" for cid, lvl in regressions] or ["None."]
    lines += ["", f"## Fixed ({len(fixes)})", ""]
    lines += [f"- {cid} ({lvl})" for cid, lvl in fixes] or ["None."]
    return "\n".join(lines) + "\n"


__all__ = ["compare", "gate", "gate_failures", "html_report", "markdown", "parse_thresholds", "summarize", "write_reports"]
