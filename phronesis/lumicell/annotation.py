"""
Hierarchical, marker-based cluster annotation for Lumicell (audit finding SCI-001).

Level 1 (lineage): every cluster is scored against a small curated lineage anchor panel
(markers/lineage_markers.json) with an ScType-style score (Ianevski et al., Nat Commun 2022;13:1246):

    score(cluster, lineage) = sum_g w_g * z(cluster, g) / sqrt(n_pos)  -  sum_g max(z(cluster, g), 0) / sqrt(n_neg)
                              (positive markers g)                          (negative markers g)

where z(cluster, g) = (mean of g in the cluster - balanced centre of g) / SD of g over all cells. The balanced centre
is the unweighted mean of the per-cluster means, so a majority population is not its own reference. z is clipped to
[-Z_CLIP, Z_CLIP]. w_g is an inverse-document-frequency marker-specificity weight (1 for a gene that marks one
lineage, lower for genes shared by several). A positive marker that is absent from the matrix (not measured, or
removed by gene QC) contributes zero but still counts in n_pos, so missing markers lower a signature's score instead of
being ignored. Negative markers only penalise (their absence earns nothing).

Decision: the best lineage is called when its score is >= MIN_SCORE and at least one of its positive markers is
detected in >= MIN_ANCHOR_DETECTION of the cluster's cells. When the runner-up scores >= AMBIGUITY_RATIO x best, the
call is ambiguous: within one lineage group (e.g. CD4 vs CD8 T cells) the group is reported, otherwise the cluster is
labelled "Ambiguous (A / B)". Clusters below the thresholds are "Unassigned".

Level 2 (subtype): atlas signatures (markers/cell_markers.json) whose names are compatible with the called lineage are
scored the same way (weights from the atlas, absent markers counted as zero). A subtype replaces the lineage label only
when it scores >= MIN_SCORE and leads the next subtype by >= SUBTYPE_MARGIN (relative); otherwise the lineage label
stays and the subtype candidates are reported as putative.

When the tissue is known to be blood / PBMC / bone marrow, only hematopoietic lineages compete.

All labels are putative marker heuristics: they are not a reference-based classification and must be confirmed by
the marker genes (reported per cluster as evidence_genes).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import sparse

MARKER_DIR = Path(__file__).parent / "markers"
LINEAGE_PANEL_FILE = MARKER_DIR / "lineage_markers.json"
ATLAS_FILE = MARKER_DIR / "cell_markers.json"
ATLAS_MANIFEST_FILE = MARKER_DIR / "cell_markers.manifest.json"

METHOD_NAME = "hierarchical marker scoring (ScType-style z-score, lineage anchors, then atlas subtype)"
MIN_SCORE = 0.5
AMBIGUITY_RATIO = 0.8
SUBTYPE_MARGIN = 0.15
MIN_ANCHOR_DETECTION = 0.2
Z_CLIP = 5.0
HIGH_CONFIDENCE_MARGIN = 0.5
UNASSIGNED = "Unassigned"
CAVEAT = (
    "Cell types are putative marker-signature heuristics (curated lineage anchors, then a literature marker atlas); "
    "they are relative to the clusters in this dataset and must be confirmed with the listed marker genes."
)


@dataclass(frozen=True)
class Lineage:
    key: str
    label: str
    group: str
    compartment: str
    positive: Tuple[str, ...]
    negative: Tuple[str, ...]
    atlas_include: "re.Pattern[str]"
    atlas_exclude: "re.Pattern[str]"


@dataclass(frozen=True)
class MarkerReference:
    lineages: Tuple[Lineage, ...]
    blood_pattern: "re.Pattern[str]"
    panel_version: str
    panel_sha256: str
    atlas: Dict[str, Tuple[str, ...]]
    atlas_sha256: Optional[str]
    atlas_manifest: Optional[Dict[str, Any]]


@dataclass
class AnnotationResult:
    labels: Dict[str, str]
    details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    method: Dict[str, Any] = field(default_factory=dict)


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _file_key(path: Path) -> Tuple[str, float, int]:
    try:
        st = path.stat()
        return str(path), st.st_mtime, st.st_size
    except OSError:
        return str(path), 0.0, 0


@lru_cache(maxsize=4)
def _load_reference_cached(panel_key: Tuple[str, float, int], atlas_key: Tuple[str, float, int]) -> MarkerReference:
    panel_path, atlas_path = Path(panel_key[0]), Path(atlas_key[0])
    panel = json.loads(panel_path.read_text(encoding="utf-8"))
    meta = panel.get("_meta", {})
    lineages = tuple(
        Lineage(
            key=key,
            label=str(spec["label"]),
            group=str(spec.get("group") or spec["label"]),
            compartment=str(spec.get("compartment") or "unknown"),
            positive=tuple(dict.fromkeys(str(g).upper() for g in spec.get("positive", []))),
            negative=tuple(dict.fromkeys(str(g).upper() for g in spec.get("negative", []))),
            atlas_include=re.compile(spec.get("atlas_include") or "(?!)"),
            atlas_exclude=re.compile(spec.get("atlas_exclude") or "(?!)"),
        )
        for key, spec in panel["lineages"].items()
    )
    atlas: Dict[str, Tuple[str, ...]] = {}
    atlas_sha: Optional[str] = None
    if atlas_path.exists():
        raw = json.loads(atlas_path.read_text(encoding="utf-8"))
        signatures = raw.get("signatures", raw) if isinstance(raw, dict) else {}
        for name, genes in signatures.items():
            if str(name).startswith("_") or not isinstance(genes, list):
                continue
            clean = tuple(dict.fromkeys(str(g).strip().upper() for g in genes if str(g).strip()))
            if clean:
                atlas[str(name)] = clean
        atlas_sha = _sha256(atlas_path)
    manifest: Optional[Dict[str, Any]] = None
    manifest_path = atlas_path.with_name(atlas_path.stem + ".manifest.json")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
    return MarkerReference(
        lineages=lineages,
        blood_pattern=re.compile(meta.get("blood_tissue_pattern") or r"(?i)\b(blood|pbmcs?|bone marrow)\b"),
        panel_version=str(meta.get("version") or "unversioned"),
        panel_sha256=_sha256(panel_path) or "",
        atlas=atlas,
        atlas_sha256=atlas_sha,
        atlas_manifest=manifest,
    )


def load_reference(panel_path: Path = LINEAGE_PANEL_FILE, atlas_path: Path = ATLAS_FILE) -> MarkerReference:
    """The lineage panel and marker atlas (cached per file modification time)."""
    return _load_reference_cached(_file_key(panel_path), _file_key(atlas_path))


def reference_provenance(ref: MarkerReference) -> Dict[str, Any]:
    manifest = ref.atlas_manifest or {}
    return {
        "lineage_panel": {
            "file": f"phronesis/lumicell/markers/{LINEAGE_PANEL_FILE.name}",
            "version": ref.panel_version,
            "sha256": ref.panel_sha256,
            "n_lineages": len(ref.lineages),
        },
        "atlas": {
            "file": f"phronesis/lumicell/markers/{ATLAS_FILE.name}",
            "sha256": ref.atlas_sha256,
            "n_signatures": len(ref.atlas),
            "build_version": manifest.get("build_version"),
            "manifest_output_sha256": manifest.get("output_sha256"),
            "sources": [
                {"name": s.get("name"), "sha256": s.get("sha256")} for s in manifest.get("sources", []) if isinstance(s, dict)
            ],
            "matches_manifest": (manifest.get("output_sha256") == ref.atlas_sha256) if manifest else None,
        },
    }


def is_blood_tissue(tissue: Optional[str], ref: Optional[MarkerReference] = None) -> bool:
    if not tissue:
        return False
    ref = ref or load_reference()
    return bool(ref.blood_pattern.search(str(tissue)))


def _idf_weights(gene_lists: Sequence[Sequence[str]]) -> Dict[str, float]:
    """1 for a gene in one list, log(N / n_g) / log(N) for a gene in n_g of N lists."""
    counts: Dict[str, int] = {}
    for genes in gene_lists:
        for g in set(genes):
            counts[g] = counts.get(g, 0) + 1
    n = max(len(gene_lists), 2)
    return {g: (1.0 if c <= 1 else max(0.0, math.log(n / c) / math.log(n))) for g, c in counts.items()}


def _atlas_lineages(ref: MarkerReference) -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Tuple[str, ...]] = {}
    for name in ref.atlas:
        out[name] = tuple(
            lin.key for lin in ref.lineages if lin.atlas_include.search(name) and not lin.atlas_exclude.search(name)
        )
    return out


def _cluster_statistics(
    matrix: Any, labels: np.ndarray, clusters: Sequence[Any], columns: List[int]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(cluster means, cluster detection fractions, overall mean, overall SD) for the given matrix columns."""
    sub = matrix[:, columns]
    sub = sub.tocsr().astype(np.float64) if sparse.issparse(sub) else np.asarray(sub, dtype=np.float64)
    masks = np.stack([np.asarray(labels == cl) for cl in clusters]).astype(np.float64)
    indicator = sparse.csr_matrix(masks)
    n_in = np.maximum(masks.sum(axis=1), 1.0)[:, None]
    if sparse.issparse(sub):
        sums = np.asarray((indicator @ sub).toarray())
        detected = sub.copy()
        detected.data = (detected.data > 0).astype(np.float64)
        det = np.asarray((indicator @ detected).toarray())
        mean_all = np.asarray(sub.mean(axis=0)).ravel()
        sq = sub.copy()
        sq.data = sq.data ** 2
        mean_sq = np.asarray(sq.mean(axis=0)).ravel()
    else:
        sums = indicator @ sub
        det = indicator @ (sub > 0).astype(np.float64)
        mean_all = sub.mean(axis=0)
        mean_sq = (sub ** 2).mean(axis=0)
    sd = np.sqrt(np.maximum(mean_sq - mean_all ** 2, 0.0))
    return sums / n_in, det / n_in, mean_all, sd


def _score(
    z_row: np.ndarray, col_of: Dict[str, int], positive: Sequence[str], negative: Sequence[str], weights: Dict[str, float]
) -> float:
    if not positive:
        return float("-inf")
    pos = sum(weights.get(g, 1.0) * float(z_row[col_of[g]]) for g in positive if g in col_of)
    score = pos / math.sqrt(len(positive))
    if negative:
        neg = sum(max(float(z_row[col_of[g]]), 0.0) for g in negative if g in col_of)
        score -= neg / math.sqrt(len(negative))
    return score


def annotate(
    matrix: Any,
    symbols: Sequence[str],
    labels: Sequence[Any],
    clusters: Sequence[Any],
    tissue: Optional[str] = None,
    ref: Optional[MarkerReference] = None,
) -> AnnotationResult:
    """Annotate clusters of a log-normalised cells x genes matrix (see the module docstring)."""
    ref = ref or load_reference()
    labels_arr = np.asarray(labels).astype(str)
    cluster_ids = [str(c) for c in clusters]
    blood = is_blood_tissue(tissue, ref)
    lineages = [lin for lin in ref.lineages if not blood or lin.compartment == "hematopoietic"]
    method: Dict[str, Any] = {
        "method": METHOD_NAME,
        "reference": "Ianevski A et al. Nat Commun 2022;13:1246 (ScType scoring); lineage anchors listed in the panel file",
        "tissue": tissue,
        "tissue_restriction": "hematopoietic lineages only (blood/PBMC/bone marrow)" if blood else None,
        "min_score": MIN_SCORE,
        "ambiguity_ratio": AMBIGUITY_RATIO,
        "subtype_margin": SUBTYPE_MARGIN,
        "min_anchor_detection": MIN_ANCHOR_DETECTION,
        "z_clip": Z_CLIP,
        "caveat": CAVEAT,
        **reference_provenance(ref),
    }

    # Symbol -> first matrix column with that symbol (upper case).
    first_col: Dict[str, int] = {}
    for j, s in enumerate(symbols):
        first_col.setdefault(str(s).upper(), j)

    atlas_lineage = _atlas_lineages(ref)
    wanted = {g for lin in lineages for g in (*lin.positive, *lin.negative)}
    wanted |= {g for genes in ref.atlas.values() for g in genes}
    present = sorted(g for g in wanted if g in first_col)

    if len(cluster_ids) < 2 or not present:
        reason = (
            "relative marker scoring needs at least two clusters" if len(cluster_ids) < 2
            else "none of the lineage or atlas marker genes are in the matrix"
        )
        details = {
            cl: {"label": UNASSIGNED, "status": "unassigned", "confidence": "low", "lineage": None, "reason": reason}
            for cl in cluster_ids
        }
        method["note"] = reason
        return AnnotationResult(labels={cl: UNASSIGNED for cl in cluster_ids}, details=details, method=method)

    col_of = {g: i for i, g in enumerate(present)}
    means, detection, _mean_all, sd = _cluster_statistics(matrix, labels_arr, cluster_ids, [first_col[g] for g in present])
    centre = means.mean(axis=0)  # balanced: every cluster counts once
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sd > 0, (means - centre) / np.where(sd > 0, sd, 1.0), 0.0)
    z = np.clip(z, -Z_CLIP, Z_CLIP)

    panel_weights = _idf_weights([lin.positive for lin in ref.lineages])
    atlas_weights = _idf_weights(list(ref.atlas.values()))
    by_key = {lin.key: lin for lin in lineages}

    out_labels: Dict[str, str] = {}
    details: Dict[str, Dict[str, Any]] = {}
    for i, cl in enumerate(cluster_ids):
        scores = sorted(
            ((_score(z[i], col_of, lin.positive, lin.negative, panel_weights), lin.key) for lin in lineages),
            key=lambda t: (-t[0], t[1]),
        )
        best_score, best_key = scores[0]
        best = by_key[best_key]
        runner_score, runner_key = scores[1] if len(scores) > 1 else (float("-inf"), None)
        runner = by_key.get(runner_key) if runner_key else None
        anchors = [
            {"gene": g, "z": round(float(z[i, col_of[g]]), 3), "pct_detected": round(float(detection[i, col_of[g]]) * 100, 1)}
            for g in best.positive
            if g in col_of
        ]
        anchors.sort(key=lambda a: -a["z"])
        max_detection = max((a["pct_detected"] for a in anchors), default=0.0) / 100.0
        margin = (best_score - max(runner_score, 0.0)) / best_score if best_score > 0 else 0.0
        info: Dict[str, Any] = {
            "lineage": best.label,
            "lineage_group": best.group,
            "score": round(float(best_score), 3),
            "runner_up": {"lineage": runner.label, "score": round(float(runner_score), 3)} if runner else None,
            "margin": round(float(margin), 3),
            "top_candidates": [{"lineage": by_key[k].label, "score": round(float(s), 3)} for s, k in scores[:3]],
            "evidence_genes": anchors[:5],
            "n_markers": len(best.positive),
            "n_markers_measured": len(anchors),
            "subtype": None,
            "subtype_candidates": [],
        }

        if best_score < MIN_SCORE or max_detection < MIN_ANCHOR_DETECTION:
            info.update(
                label=UNASSIGNED,
                status="unassigned",
                confidence="low",
                reason=(
                    f"best lineage {best.label} scores {best_score:.2f} (< {MIN_SCORE})" if best_score < MIN_SCORE
                    else f"no {best.label} anchor marker is detected in >= {MIN_ANCHOR_DETECTION:.0%} of the cluster's cells"
                ),
            )
            details[cl] = info
            out_labels[cl] = UNASSIGNED
            continue

        if runner is not None and runner_score > 0 and runner_score >= AMBIGUITY_RATIO * best_score:
            if runner.group == best.group:
                label = best.group
                info.update(
                    label=label,
                    status="assigned",
                    level="group",
                    confidence="medium",
                    reason=f"{best.label} and {runner.label} score within {1 - AMBIGUITY_RATIO:.0%}; reported at group level",
                )
            else:
                label = f"Ambiguous ({best.label} / {runner.label})"
                info.update(
                    label=label,
                    status="ambiguous",
                    confidence="low",
                    reason=f"runner-up {runner.label} scores >= {AMBIGUITY_RATIO:.0%} of {best.label}",
                )
            details[cl] = info
            out_labels[cl] = label
            continue

        label = best.label
        info.update(
            status="assigned",
            level="lineage",
            confidence="high" if margin >= HIGH_CONFIDENCE_MARGIN else "medium",
            reason=f"{best.label} leads the runner-up by {margin:.0%}",
        )

        # Level 2: subtype among atlas signatures compatible with the lineage.
        allow_unclassified = best.compartment != "hematopoietic"
        sub_scores: List[Tuple[float, str, int, int]] = []
        for name, genes in ref.atlas.items():
            lin_keys = atlas_lineage.get(name, ())
            if best.key not in lin_keys and not (allow_unclassified and not lin_keys):
                continue
            s = _score(z[i], col_of, genes, (), atlas_weights)
            sub_scores.append((s, name, len(genes), sum(1 for g in genes if g in col_of)))
        sub_scores.sort(key=lambda t: (-t[0], t[1]))
        info["subtype_candidates"] = [
            {"cell_type": n, "score": round(float(s), 3), "n_markers": k, "n_markers_measured": m}
            for s, n, k, m in sub_scores[:3]
        ]
        if sub_scores:
            s1 = sub_scores[0][0]
            s2 = sub_scores[1][0] if len(sub_scores) > 1 else 0.0
            if s1 >= MIN_SCORE and (s1 - max(s2, 0.0)) / s1 >= SUBTYPE_MARGIN:
                info["subtype"] = sub_scores[0][1]
                label = sub_scores[0][1]
        info["label"] = label
        details[cl] = info
        out_labels[cl] = label
    return AnnotationResult(labels=out_labels, details=details, method=method)
