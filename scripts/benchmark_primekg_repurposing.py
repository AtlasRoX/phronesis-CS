"""
Benchmark the PrimeKG repurposing score: for each disease in a panel, how well does each component (and the
weighted sum) rank that disease's own known indications above every other drug?

    uv run python scripts/benchmark_primekg_repurposing.py            # default panel and weights
    uv run python scripts/benchmark_primekg_repurposing.py --strict   # hold out direct ontology relatives

--strict removes parent/child diseases of the query disease from the similar-disease signal, since subtypes
often carry the same indications by construction; the strict numbers are the honest estimate.

Reference run (PrimeKG 2.1, 20 diseases, 534 known indications, n_random=200), mean AUROC:
    config               default   strict
    target overlap only   0.661    0.661
    proximity only        0.695    0.695
    similar-disease only  0.908    0.751
    equal (1, 1, 1)       0.891    0.787
    default (1, 0.5, 2)   0.919    0.811      <- REPURPOSING_WEIGHTS
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from phronesis.kgintel import analytics as an
from phronesis.kgintel.graph import get_primekg

PANEL = [
    "type 2 diabetes mellitus", "hypertension", "rheumatoid arthritis", "asthma", "Parkinson disease",
    "schizophrenia", "epilepsy", "breast cancer", "psoriasis", "multiple sclerosis", "idiopathic pulmonary fibrosis",
    "Crohn disease", "migraine disorder", "gout", "plasma cell myeloma", "osteoporosis", "major depressive disorder",
    "heart failure", "Alzheimer disease", "prostate cancer",
]
CONFIGS = {
    "target overlap only": {"network_proximity": 0, "similar_disease_indication": 0},
    "proximity only": {"target_overlap": 0, "similar_disease_indication": 0},
    "similar-disease only": {"target_overlap": 0, "network_proximity": 0},
    "equal (1, 1, 1)": {"target_overlap": 1, "network_proximity": 1, "similar_disease_indication": 1},
    "default": {},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true", help="hold out direct ontology relatives")
    parser.add_argument("--n-random", type=int, default=200)
    args = parser.parse_args()

    kg = get_primekg()
    t0 = time.time()
    signals = {}
    for name in PANEL:
        d = kg.resolve_one(name, ["disease"])
        sig = an.repurposing_signals(kg, d, n_random=args.n_random)
        if args.strict:
            rel = set(kg.neighbor_set(d, ["disease_disease"], node_type="disease").tolist())
            for f in sig["drugs"].values():
                f["sims"] = [p for p in f["sims"] if p[0] not in rel]
        signals[name] = (d, sig)
    print(f"signals for {len(PANEL)} diseases in {time.time() - t0:.1f} s ({'strict' if args.strict else 'default'})")

    for label, weights in CONFIGS.items():
        aucs, top100, known = [], 0, 0
        for d, sig in signals.values():
            indicated = set(kg.neighbor_set(d, ["indication"], node_type="drug").tolist())
            scored = sorted(((sum(an.score_repurposing(f, weights).values()), drug) for drug, f in sig["drugs"].items()),
                            reverse=True)
            rank_of = {drug: k + 1 for k, (s, drug) in enumerate(scored) if s > 0}
            cal = an.calibration_stats(rank_of, indicated, sig["pool"])
            if cal["auroc_known_indications"] is not None:
                aucs.append(cal["auroc_known_indications"])
            top100 += cal["in_top_100"]
            known += cal["known_indications"]
        print(f"{label:22} mean AUROC {np.mean(aucs):.3f}  median {np.median(aucs):.3f}  "
              f"known indications in top 100: {top100}/{known}")


if __name__ == "__main__":
    main()
