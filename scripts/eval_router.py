"""
Evaluate the Auto router on the routing benchmark (tests/fixtures/routing/benchmark.json).

Reports strict accuracy (the gold architecture), acceptable accuracy (gold or a listed alternative), accuracy per
question kind, the confusion matrix and the misses.

Usage:
    python scripts/eval_router.py                       # deterministic signals only (offline)
    python scripts/eval_router.py --semantic            # add the model's semantic features (needs a model key)
    python scripts/eval_router.py --semantic --samples 3 --model gpt-4o
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROUTING_DIR = ROOT / "tests" / "fixtures" / "routing"


def load_benchmark(name: str = "benchmark") -> Dict[str, Any]:
    return json.loads((ROUTING_DIR / f"{name}.json").read_text(encoding="utf-8"))


def cache_semantic_features(path: Path) -> None:
    """Reuse the model's semantic features per question across runs (re-tuning the scores costs no model calls)."""
    from phronesis.agentic import router

    cache: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    real = router.semantic_features

    async def cached(query: str, *args: Any, **kwargs: Any) -> Any:
        if query not in cache:
            feats, kind, valid = await real(query, *args, **kwargs)
            cache[query] = [feats, kind, valid]
            path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        feats, kind, valid = cache[query]
        return feats, kind, valid

    router.semantic_features = cached


async def route_all(cases: List[Dict[str, Any]], semantic: bool, model: str) -> List[str]:
    from phronesis.agentic import router
    from phronesis.agents import query_planner as qp

    factory = None
    if semantic:
        from phronesis.utils.llm.llm_factory import get_llm

        factory = get_llm
    out: List[str] = []
    for case in cases:
        ctx = await qp.build_query_context(
            query=case["q"], clean_query=case["q"], registry=None, enable_web_search=False, attachments=[],
            attachment_errors=[], user_id="eval", thread_id="eval", model_name=model, api_key=None,
        )
        decision = await router.choose_framework(case["q"], ctx, llm_factory=factory, model_name=model)
        out.append(decision.framework)
    return out


def score(bench: Dict[str, Any], routed: List[str]) -> Dict[str, Any]:
    labels = bench["labels"]
    names = {v: k for k, v in labels.items()}
    strict = acceptable = 0
    per_kind: Dict[str, Counter] = defaultdict(Counter)
    confusion: Dict[str, Counter] = defaultdict(Counter)
    misses: List[Dict[str, str]] = []
    for case, fw in zip(bench["cases"], routed):
        got = names.get(fw, fw)
        ok_strict = got == case["gold"]
        ok_accept = ok_strict or got in case.get("also", [])
        strict += ok_strict
        acceptable += ok_accept
        per_kind[case.get("kind", "other")]["n"] += 1
        per_kind[case.get("kind", "other")]["ok"] += ok_accept
        confusion[case["gold"]][got] += 1
        if not ok_strict:
            misses.append({"q": case["q"], "gold": case["gold"], "got": got, "acceptable": ok_accept})
    n = len(routed)
    return {
        "n": n,
        "strict_accuracy": round(strict / n, 3) if n else 0.0,
        "acceptable_accuracy": round(acceptable / n, 3) if n else 0.0,
        "per_kind": {k: round(v["ok"] / v["n"], 2) for k, v in sorted(per_kind.items())},
        "confusion": {g: dict(c) for g, c in confusion.items()},
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--semantic", action="store_true", help="add the model's semantic features")
    parser.add_argument("--samples", type=int, default=1, help="semantic feature samples per question (1-5)")
    parser.add_argument("--model", default=None, help="model id (default: AIAGENTS4PHARMA_LLM_MODEL)")
    parser.add_argument("--cache", type=Path, help="JSON file caching the model's semantic features per question")
    parser.add_argument("--set", default="benchmark", choices=("benchmark", "holdout"), help="question set to route")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    os.environ.setdefault("PHRONESIS_PLANNER", "off")
    os.environ["PHRONESIS_ROUTER_LEARNING"] = "off"  # measure the router, not this machine's history
    os.environ["PHRONESIS_ROUTER_SAMPLES"] = str(max(1, min(5, args.samples)))
    if not args.semantic:
        os.environ["PHRONESIS_ROUTER_LLM"] = "off"
    if args.semantic:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")  # the model key the app uses
    args.model = args.model or os.getenv("AIAGENTS4PHARMA_LLM_MODEL") or "gpt-4o-mini"
    if args.cache:
        cache_semantic_features(args.cache)
    bench = load_benchmark(args.set)
    report = score(bench, asyncio.run(route_all(bench["cases"], args.semantic, args.model)))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    mode = f"semantic x{args.samples} ({args.model})" if args.semantic else "deterministic"
    print(f"Auto router, {mode}, {args.set}: {report['n']} questions")
    print(f"  strict accuracy     {report['strict_accuracy']:.1%}")
    print(f"  acceptable accuracy {report['acceptable_accuracy']:.1%}")
    print("  per kind: " + ", ".join(f"{k} {v:.0%}" for k, v in report["per_kind"].items()))
    for m in report["misses"]:
        tag = "ALT " if m["acceptable"] else "MISS"
        print(f"  {tag} gold={m['gold']:<12} got={m['got']:<12} {m['q'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
