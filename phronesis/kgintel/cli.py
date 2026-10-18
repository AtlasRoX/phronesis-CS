"""
phronesis-primekg: download, build and query the local PrimeKG store.

    phronesis-primekg build                       download (resumable) + compile into the Parquet store
    phronesis-primekg stats                       node / edge counts and provenance
    phronesis-primekg query drug_profile drug=metformin
    phronesis-primekg query repurposing disease="Parkinson disease" limit=10
    phronesis-primekg query phenotype_diagnosis present=Seizure,Ataxia,Microcephaly
    phronesis-primekg ask "Which drugs could be repurposed for idiopathic pulmonary fibrosis?"

Set PHRONESIS_PRIMEKG_DIR to choose the data directory (default ~/.cache/phronesis/primekg).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List

LIST_ARGS = {"present", "absent", "drugs", "diseases", "genes", "collections", "node_types"}
INT_ARGS = {"limit", "max_hops"}
BOOL_ARGS = {"include_drug_interactions", "include_experimental"}


def _parse_kv(pairs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"argument {p!r} must be key=value")
        k, v = p.split("=", 1)
        if k in LIST_ARGS:
            out[k] = [s.strip() for s in v.split(",") if s.strip()]
        elif k in INT_ARGS:
            out[k] = int(v)
        elif k in BOOL_ARGS:
            out[k] = v.lower() in ("1", "true", "yes")
        else:
            out[k] = v
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phronesis-primekg", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="download and compile the store")
    b.add_argument("--no-download", action="store_true", help="use raw files already in raw/")
    sub.add_parser("download", help="download the raw Dataverse files only")
    sub.add_parser("stats", help="store statistics")
    q = sub.add_parser("query", help="run one operation")
    q.add_argument("operation")
    q.add_argument("args", nargs="*", help="key=value (lists comma-separated)")
    a = sub.add_parser("ask", help="plan and run analyses for a free-text question")
    a.add_argument("question")
    for p in (q, a):
        p.add_argument("--markdown", action="store_true", help="print the fallback summary instead of JSON")
    ns = parser.parse_args(argv)

    from . import service
    from .graph import PrimeKGError, get_primekg
    from .store import build_store, download_raw

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        if ns.cmd == "build":
            manifest = build_store(download=not ns.no_download, log=log)
            print(json.dumps(manifest["counts"], indent=2))
            return 0
        if ns.cmd == "download":
            download_raw(log=log)
            return 0
        t0 = time.time()
        kg = get_primekg()
        log(f"PrimeKG loaded in {time.time() - t0:.1f} s")
        if ns.cmd == "stats":
            print(json.dumps(service.stats(kg=kg), indent=2, default=str))
            return 0
        if ns.cmd == "query":
            t1 = time.time()
            out = service.run(ns.operation, kg=kg, **_parse_kv(ns.args))
            log(f"{ns.operation} in {time.time() - t1:.2f} s")
            if ns.markdown:
                from .render import fallback_markdown
                print(fallback_markdown({"analyses": [out], "provenance": out.get("provenance")}))
            else:
                print(json.dumps(out, indent=2, default=str))
            return 0
        if ns.cmd == "ask":
            from phronesis.extensions.primekg import investigate

            t1 = time.time()
            res = investigate(ns.question)
            log(f"investigation in {time.time() - t1:.2f} s")
            if not res.success:
                print(res.error, file=sys.stderr)
                return 1
            if ns.markdown:
                from .render import fallback_markdown
                print(fallback_markdown(res.data or {}))
            else:
                print(json.dumps(res.data, indent=2, default=str))
            return 0
    except PrimeKGError as exc:
        print(f"error ({exc.kind}): {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
