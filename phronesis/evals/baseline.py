"""
Regression control: per-case metric snapshots of a run (baselines) and a paired check of a new run against one.

  phronesis-eval baseline save eval_runs/<run> --name golden          # eval_runs/baselines/golden.json
  phronesis-eval baseline check eval_runs/<run> --name golden --delta 0.03

A snapshot keeps, for every scored (case, level): pass, fact_recall, faithfulness, latency, and each hard gate's
outcome. The check pairs the cases both runs scored and, per metric, bootstraps a 95% CI of the mean per-case
delta (new - baseline; fixed seed, pure Python). It fails when a CI's upper bound is below -delta (a drop that is
both real and larger than the tolerance) or when a hard gate that held in the baseline now fails.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .runner import load_results
from .schema import CaseResult
from .scoring import GATES

DEFAULT_DIR = Path("eval_runs") / "baselines"
CHECK_METRICS = ("fact_recall", "faithfulness", "pass")
SNAPSHOT_VERSION = 1


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def case_snapshot(r: CaseResult) -> Dict[str, Any]:
    return {
        "suite": r.suite,
        "passed": bool(r.passed),
        "pass": 1.0 if r.passed else 0.0,
        "fact_recall": _num(r.metrics.get("fact_recall")),
        "faithfulness": _num(r.metrics.get("faithfulness")),
        "latency_s": _num(r.latency_s),
        "error": r.error,
        "flaky": bool(r.metrics.get("flaky")),
        "gates": {c.name: bool(c.passed) for c in r.checks if c.name in GATES},
        "failed_checks": sorted({c.name for c in r.checks if c.required and not c.passed}),
    }


def snapshot(run_dir: Path, *, name: str) -> Dict[str, Any]:
    """The baseline document for a finished run directory."""
    run_dir = Path(run_dir)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    results = [r for r in load_results(run_dir / "results.jsonl") if r.skipped is None]
    if not results:
        raise ValueError(f"{run_dir}: no scored results to snapshot")
    return {
        "version": SNAPSHOT_VERSION,
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_run": str(run_dir),
        "meta": {k: meta.get(k) for k in ("run_id", "suites", "model", "source_fingerprint", "started_at")},
        "cases": {f"{r.case_id}::{r.level}": case_snapshot(r) for r in sorted(results, key=lambda r: (r.case_id, r.level))},
    }


def baseline_path(name: Optional[str] = None, *, directory: Optional[Path] = None, path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    return Path(directory or DEFAULT_DIR) / f"{name or 'baseline'}.json"


def save(run_dir: Path, *, name: str = "baseline", directory: Optional[Path] = None, path: Optional[str] = None) -> Path:
    doc = snapshot(run_dir, name=name)
    out = baseline_path(name, directory=directory, path=path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=False), encoding="utf-8")
    return out


def bootstrap_ci(deltas: Sequence[float], *, n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> Tuple[float, float, float]:
    """(mean, low, high): percentile bootstrap CI of the mean, deterministic for a seed. One delta gives [d, d]."""
    vals = [float(d) for d in deltas]
    if not vals:
        raise ValueError("no deltas")
    mean = statistics.fmean(vals)
    if len(vals) == 1 or len(set(vals)) == 1:
        return mean, mean, mean
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(statistics.fmean(rng.choices(vals, k=n)) for _ in range(n_boot))
    lo = means[max(0, int((alpha / 2) * n_boot))]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot) - 1)]
    return mean, lo, hi


def check(
    base: Dict[str, Any],
    run_dir: Path,
    *,
    delta: float = 0.03,
    metrics: Sequence[str] = CHECK_METRICS,
    seed: int = 0,
    n_boot: int = 2000,
) -> Dict[str, Any]:
    """Paired comparison of a run against a baseline; result["ok"] is False on a significant drop or a new gate failure."""
    new = snapshot(run_dir, name="current")["cases"]
    old = base.get("cases") or {}
    shared = sorted(set(old) & set(new))
    rows: List[Dict[str, Any]] = []
    failures: List[str] = []
    for metric in metrics:
        pairs = [(k, old[k].get(metric), new[k].get(metric)) for k in shared]
        deltas = [(k, float(b) - float(a)) for k, a, b in pairs if a is not None and b is not None]
        if not deltas:
            rows.append({"metric": metric, "n": 0})
            continue
        mean, lo, hi = bootstrap_ci([d for _, d in deltas], n_boot=n_boot, seed=seed)
        by_case = dict(deltas)
        worse = sorted((k for k, d in deltas if d < 0), key=by_case.__getitem__)
        row = {"metric": metric, "n": len(deltas), "base_mean": round(statistics.fmean(float(a) for _, a, b in pairs if a is not None and b is not None), 4),
               "new_mean": round(statistics.fmean(float(b) for _, a, b in pairs if a is not None and b is not None), 4),
               "delta_mean": round(mean, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4), "worse_cases": worse[:10],
               "regressed": hi < -delta}
        rows.append(row)
        if row["regressed"]:
            failures.append(f"{metric}: mean delta {mean:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}] is below -{delta}")
    new_gate_failures: List[Dict[str, str]] = []
    for k in shared:
        before = old[k].get("gates") or {}
        for gate, ok in (new[k].get("gates") or {}).items():
            if not ok and before.get(gate) is not False:
                new_gate_failures.append({"case": k, "gate": gate})
    for f in new_gate_failures:
        failures.append(f"hard gate {f['gate']} newly fails on {f['case']}")
    return {
        "ok": not failures,
        "baseline": base.get("name"),
        "baseline_run": (base.get("meta") or {}).get("run_id"),
        "delta": delta,
        "shared_cases": len(shared),
        "only_in_baseline": sorted(set(old) - set(new)),
        "only_in_run": sorted(set(new) - set(old)),
        "metrics": rows,
        "new_gate_failures": new_gate_failures,
        "newly_failing_cases": [k for k in shared if old[k].get("passed") and not new[k].get("passed")],
        "failures": failures,
    }


def check_markdown(result: Dict[str, Any]) -> str:
    lines = [f"# Baseline check against `{result.get('baseline')}` (run {result.get('baseline_run')})", "",
             f"{result['shared_cases']} shared cases · tolerance {result['delta']} · "
             + ("**OK**" if result["ok"] else "**REGRESSION**"), "",
             "| Metric | n | Base | New | Mean Δ | 95% CI | Regressed |", "|---|---|---|---|---|---|---|"]
    for r in result["metrics"]:
        if not r.get("n"):
            lines.append(f"| {r['metric']} | 0 | – | – | – | – | – |")
            continue
        lines.append(f"| {r['metric']} | {r['n']} | {r['base_mean']:.3f} | {r['new_mean']:.3f} | {r['delta_mean']:+.3f} | "
                     f"[{r['ci_low']:+.3f}, {r['ci_high']:+.3f}] | {'yes' if r['regressed'] else 'no'} |")
    if result["new_gate_failures"]:
        lines += ["", "## Newly failing hard gates", ""]
        lines += [f"- {f['case']}: `{f['gate']}`" for f in result["new_gate_failures"]]
    if result["newly_failing_cases"]:
        lines += ["", "## Cases that passed in the baseline and fail now", ""]
        lines += [f"- {k}" for k in result["newly_failing_cases"]]
    for key, title in (("only_in_baseline", "Not in this run"), ("only_in_run", "New cases (no baseline)")):
        if result[key]:
            lines += ["", f"{title}: {', '.join(result[key][:20])}"]
    return "\n".join(lines) + "\n"


__all__ = ["CHECK_METRICS", "DEFAULT_DIR", "baseline_path", "bootstrap_ci", "check", "check_markdown", "save", "snapshot"]
