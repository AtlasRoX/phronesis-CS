"""
Lumicell Single-Cell Transcriptomics Analysis Engine, inspired by CellAgent (2024).
Executes reproducible AnnData/Scanpy workflows on real, verified single-cell datasets:
- Dataset provenance validation (AnnData .h5ad / .zarr files)
- Input matrix selection (X, a named layer or .raw) and input-scale detection
  (raw counts, library-size normalized, log1p, scaled); unknown or scaled-only input needs user input
- Data inspection & QC (count-based filters only on counts; mt% on counts or normalized values)
- Library-size normalization & log1p only where the input scale requires them
- Highly variable gene selection & PCA dimensionality reduction sized from the selected features
- Neighborhood graph construction & Leiden (or recorded fallback) community clustering
- Cluster marker genes (Wilcoxon rank-sum, Benjamini-Hochberg adjusted, on the log-normalized matrix)
- Putative cluster cell types from hierarchical marker scoring with confidence, runner-up and margin
  (phronesis/lumicell/annotation.py; versioned, hashed marker references)
- Diagnostic reporting with sample/donor metadata, every transformation and visual artifacts
- Strict rejection of ungrounded synthetic data in product pathways
- Dataset path policy (phronesis/lumicell/path_policy.py) and structural/size checks before any file is parsed

QC defaults (all overridable per call and recorded in qc_parameters): cells need >= min(200, 10% of genes) detected
genes (the Scanpy PBMC3k tutorial uses 200); on count data, cells more than 5 MADs from the median of log1p total
counts or log1p detected genes are removed (both tails; Germain et al., Genome Biol 2020;21:226; Heumos et al., Nat Rev
Genet 2023;24:550); genes detected in < 3 cells are removed; mt% < max_pct_mt where mt% is defined. No doublet detection
is run; a matrix that looks unfiltered (empty droplets) is flagged in the warnings.

Runtime: pbmc3k (2,638 cells) takes ~8 s in a warm process. The first run in a process adds ~20 s of numba JIT
compilation (neighbors / UMAP), so the registry timeout allows for that and for larger inputs.
"""

from __future__ import annotations

import base64
import io
import logging
import math
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy as sc
from pydantic import BaseModel, Field
from scipy import sparse

from ..config.settings import get_int
from ..tools.registry import ErrorType, ToolResult
from . import annotation as _annotation
from .path_policy import resolve_dataset_path

logger = logging.getLogger(__name__)

TOOL_NAME = "lumicell_analyze_single_cell"
# log1p_counts: log1p of unnormalized counts (expm1 recovers integers); counts are recovered before QC.
INPUT_SCALES = ("raw_counts", "log1p_counts", "normalized", "log1p", "scaled", "unknown")
MARKER_PADJ_MAX = 0.05
MAX_PCS = 50
DEFAULT_MAX_CELLS = 50_000
DEFAULT_UMAP_MAX_CELLS = 20_000
_SCALE_SAMPLE_ROWS = 2_000
# Gene-symbol columns used when var_names hold Ensembl or other stable ids (CELLxGENE, 10x features).
_SYMBOL_COLUMNS = ("feature_name", "gene_symbols", "gene_symbol", "gene_name", "gene_names", "symbol")

# QC defaults (SCI-011); every value can be overridden per call and is reported in qc_parameters.
DEFAULT_MIN_GENES = 200  # Scanpy PBMC3k tutorial; capped at 10% of the measured genes for small panels
MIN_GENES_FRACTION_CAP = 0.10
DEFAULT_OUTLIER_MADS = 5.0  # MAD outliers on log1p total counts / log1p detected genes (count data only)
MIN_CELLS_FOR_MAD_QC = 100  # fewer cells give unstable median/MAD estimates
DEFAULT_GENE_MIN_CELLS = 3
UNFILTERED_MEDIAN_GENES = 100  # median detected genes per barcode below this suggests empty droplets
UNFILTERED_MIN_BARCODES = 1_000

# Structural limits checked before a dataset is parsed in-process (SEC-010).
MAX_DENSE_ELEMENTS = 2_000_000_000  # cells x genes loaded as a dense matrix
MAX_SPARSE_NNZ = 2_000_000_000  # stored entries loaded from a sparse matrix
MAX_ZARR_FILES = 200_000


def _env_int(name: str, default: int) -> int:
    """Positive integer setting (CFG-002): registered bounds apply and the value is never below 1.

    0 and negative values previously meant "no cap" or crashed the subsampler; they now clamp to 1."""
    value = get_int(name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


class ClusterMarkerInfo(BaseModel):
    """Cluster marker gene identified by differential expression (cluster vs. rest)."""
    gene: str
    cluster: str
    log_fold_change: float
    score: float
    p_value: float
    p_adj: float
    gene_id: Optional[str] = None  # var_name when it differs from the reported symbol


class LumicellExecutionArtifact(BaseModel):
    """Artifact returned from single-cell transcriptomics pipeline."""
    dataset_name: str
    dataset_path: Optional[str] = None
    dataset_provenance: Dict[str, Any] = Field(default_factory=dict)
    n_cells_initial: int
    n_cells_post_qc: int
    n_genes: int
    n_clusters: int
    donor_distribution: Dict[str, int] = Field(default_factory=dict)
    cluster_cell_counts: Dict[str, int] = Field(default_factory=dict)
    cluster_cell_types: Dict[str, str] = Field(default_factory=dict)
    # Per cluster: label, lineage, lineage_group, status (assigned|ambiguous|unassigned), confidence
    # (high|medium|low), score, runner_up, margin, top_candidates, evidence_genes, subtype, subtype_candidates, reason.
    cluster_annotations: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Annotation method, thresholds, tissue restriction and the marker references' versions and sha256 hashes.
    annotation_method: Dict[str, Any] = Field(default_factory=dict)
    top_markers_by_cluster: Dict[str, List[ClusterMarkerInfo]] = Field(default_factory=dict)
    marker_criteria: Dict[str, Any] = Field(default_factory=dict)
    target_expression_summary: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    target_expression_note: Optional[str] = None
    # measured | removed_by_qc | not_in_dataset (None when no target gene was requested)
    target_gene_status: Optional[str] = None
    input_matrix: str = "X"
    input_scale: str = "unknown"
    scale_basis: str = ""
    transformations: List[str] = Field(default_factory=list)
    subsampling: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    qc_parameters: Dict[str, Any] = Field(default_factory=dict)
    clustering_parameters: Dict[str, Any] = Field(default_factory=dict)
    plot_base64_png: Optional[str] = None
    disclaimer: str = (
        "Statistical Note: Cluster marker genes are computed from the same data used to define the clusters, so "
        "their p-values are optimistic (double dipping). They signify correlative transcriptomic phenotypes and "
        "lineage markers; they do not establish mutation status, pathway activity, causal disease etiology or "
        "therapeutic response without an appropriate design and functional perturbation benchmarks."
    )

    def to_markdown_summary(self) -> str:
        method = str(self.clustering_parameters.get("method") or "leiden").capitalize()
        mt_note = (
            f"Mitochondrial cutoff: <{self.qc_parameters.get('max_pct_mt', 20)}%"
            if self.qc_parameters.get("mt_filter_applied")
            else "no mitochondrial filter applied"
        )
        lines = [
            f"**Dataset**: `{self.dataset_name}` ({self.n_cells_post_qc} cells, {self.n_genes} genes across {self.n_clusters} {method} clusters)",
            f"**Input**: `{self.input_matrix}` as {self.input_scale.replace('_', ' ')} ({self.scale_basis}); "
            f"transformations: {', '.join(self.transformations) if self.transformations else 'none'}",
            f"**QC Filtering**: Filtered {self.n_cells_initial - self.n_cells_post_qc} low-quality cells ({mt_note})",
        ]
        if self.subsampling.get("applied"):
            lines.append(
                f"**Subsampling**: {self.subsampling.get('n_cells_used')} of {self.subsampling.get('n_cells_total')} cells "
                f"(seed {self.subsampling.get('seed')})"
            )
        lines.extend([
            "",
            f"| Cluster | Cell Count | Putative cell type (confidence) | Cluster Markers (Wilcoxon vs. rest, BH padj < {MARKER_PADJ_MAX:g}, log2FC > 0) | Smallest padj |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for cl_id, count in sorted(self.cluster_cell_counts.items()):
            markers = self.top_markers_by_cluster.get(cl_id, [])
            top_genes = ", ".join(f"*{m.gene}*" for m in markers[:4]) if markers else "None"
            best = f"padj = {min(m.p_adj for m in markers):.2e}" if markers else f"no marker with padj < {MARKER_PADJ_MAX:g}"
            note = self.cluster_annotations.get(cl_id) or {}
            cell_type = self.cluster_cell_types.get(cl_id, "")
            if note.get("confidence"):
                cell_type = f"{cell_type} ({note['confidence']})"
            lines.append(f"| **Cluster {cl_id}** | {count} cells | {cell_type or 'n/a'} | {top_genes} | {best} |")
        if self.annotation_method.get("caveat"):
            lines.extend(["", f"*{self.annotation_method['caveat']}*"])

        if self.target_expression_summary and self.target_expression_note:
            lines.extend(["", f"*{self.target_expression_note}*"])
        for warning in self.warnings:
            lines.append(f"- Warning: {warning}")
        lines.extend([
            "",
            f"> [!NOTE]",
            f"> {self.disclaimer}",
        ])
        return "\n".join(lines)


class _NeedsInput(Exception):
    """The dataset cannot be analyzed as given; the user must supply a different matrix or declaration."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _sample_values(matrix: Any, max_rows: int = _SCALE_SAMPLE_ROWS) -> np.ndarray:
    """Values of up to max_rows evenly spaced rows (stored entries only for sparse matrices)."""
    n_rows = matrix.shape[0]
    rows = np.unique(np.linspace(0, n_rows - 1, num=min(n_rows, max_rows)).astype(int)) if n_rows else np.array([], int)
    sub = matrix[rows]
    if sparse.issparse(sub):
        return np.asarray(sub.data, dtype=np.float64)
    return np.asarray(sub, dtype=np.float64).ravel()


def _row_sums(matrix: Any, max_rows: int = _SCALE_SAMPLE_ROWS, transform: Optional[Any] = None) -> np.ndarray:
    n_rows = matrix.shape[0]
    rows = np.unique(np.linspace(0, n_rows - 1, num=min(n_rows, max_rows)).astype(int)) if n_rows else np.array([], int)
    sub = matrix[rows]
    if sparse.issparse(sub):
        sub = sub.copy().astype(np.float64)
        if transform is not None:
            sub.data = transform(sub.data)
        return np.asarray(sub.sum(axis=1)).ravel()
    arr = np.asarray(sub, dtype=np.float64)
    if transform is not None:
        arr = transform(arr)
    return np.asarray(arr.sum(axis=1))


def _nearly_constant(values: np.ndarray) -> bool:
    values = values[values > 0]
    if values.size < 2:
        return False
    return float(np.std(values) / np.mean(values)) < 0.01


def detect_input_scale(matrix: Any, uns: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Classify a cells × genes matrix as raw_counts | normalized | log1p | scaled | unknown, with the basis.

    Raises _NeedsInput for non-finite values. Only the values are inspected, never file names.
    """
    values = _sample_values(matrix)
    if values.size and not np.all(np.isfinite(values)):
        raise _NeedsInput("NON_FINITE_VALUES", "The expression matrix contains NaN or infinite values.")
    if values.size == 0:
        return "unknown", "matrix has no stored values"
    if float(values.min()) < 0:
        return "scaled", "contains negative values (centered/scaled data)"
    if np.allclose(values, np.round(values), atol=1e-6):
        return "raw_counts", "non-negative integer values"
    with np.errstate(over="ignore", invalid="ignore"):
        recovered = np.expm1(values)
        log_of_counts = bool(np.all(np.isfinite(recovered))) and bool(
            np.all(np.abs(recovered - np.round(recovered)) <= 1e-3 * np.maximum(1.0, recovered))
        )
    if log_of_counts:
        return "log1p_counts", "expm1(values) are integers: log1p of unnormalized counts"
    if _nearly_constant(_row_sums(matrix)):
        return "normalized", "non-integer values with constant per-cell totals"
    if uns is not None and "log1p" in uns:
        return "log1p", "uns['log1p'] records a log1p transform"
    if _nearly_constant(_row_sums(matrix, transform=np.expm1)):
        return "log1p", "per-cell totals of expm1(values) are constant"
    return "unknown", "non-integer values that are neither constant-total nor recognisably log1p-normalized"


def _symbol_series(var: pd.DataFrame, var_names: pd.Index) -> Tuple[pd.Series, Optional[str]]:
    """Gene symbols per var: a symbol column when var_names are not symbols (e.g. Ensembl ids)."""
    names = pd.Series(var_names.astype(str), index=var_names)
    looks_like_ids = names.str.match(r"^(ENS[A-Z]*G\d{6,}|\d+)(\.\d+)?$").mean() > 0.5 if len(names) else False
    if looks_like_ids:
        for column in _SYMBOL_COLUMNS:
            if column in var.columns:
                col = var[column].astype(str)
                if col.str.len().gt(0).mean() > 0.5:
                    return pd.Series(col.values, index=var_names), column
    return names, None


class LumicellAnalysisEngine:
    """
    Scanpy/AnnData Analytics Engine for Lumicell.
    Executes single-cell QC, clustering, differential expression, and marker analysis
    on validated real-world single-cell datasets.
    """

    def _detect_matrix_scale(self, adata: ad.AnnData) -> str:
        """Classify matrix scale of an AnnData expression matrix."""
        scale, _ = detect_input_scale(adata.X, getattr(adata, "uns", None))
        return scale

    @staticmethod
    def _annotate_clusters_detailed(
        work: ad.AnnData, symbols: pd.Series, unique_clusters: List[Any], tissue: Optional[str] = None
    ) -> "_annotation.AnnotationResult":
        """Putative cell types per cluster with confidence, runner-up and margin (SCI-001; see annotation.py)."""
        symbol_list = symbols.reindex(work.var_names).astype(str).str.upper().tolist()
        return _annotation.annotate(work.X, symbol_list, np.asarray(work.obs["leiden"]), unique_clusters, tissue=tissue)

    @staticmethod
    def _annotate_clusters(
        work: ad.AnnData, symbols: pd.Series, unique_clusters: List[Any], tissue: Optional[str] = None
    ) -> Dict[str, str]:
        """Cluster id -> putative cell-type label ("Unassigned" / "Ambiguous (...)" when the evidence is weak)."""
        return LumicellAnalysisEngine._annotate_clusters_detailed(work, symbols, unique_clusters, tissue).labels

    @staticmethod
    def _create_umap_plot_base64(adata: ad.AnnData, method: str = "leiden") -> Optional[str]:
        """Generate UMAP scatter plot colored by clusters."""
        try:
            fig, ax = plt.subplots(figsize=(6, 4.5), dpi=120)
            sc.pl.umap(
                adata,
                color="leiden",
                ax=ax,
                show=False,
                frameon=False,
                title=f"Single-Cell UMAP - {method.capitalize()} Clusters",
            )
            buf = io.BytesIO()
            plt.savefig(buf, format="png", bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")
        except Exception as e:
            logger.warning("Could not render UMAP plot: %s", e)
            plt.close("all")
            return None

    @staticmethod
    def _failure(error: str, error_type: ErrorType, t0: float, **metadata: Any) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name=TOOL_NAME,
            error=error,
            error_type=error_type,
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"adapter": "LOCAL_COMPUTE", **metadata},
        )

    @staticmethod
    def _resolve_path(dataset_path: str) -> Path:
        p = Path(dataset_path)
        if p.exists() or p.is_absolute():
            return p
        try:  # a typed relative path is looked up under the configured dataset roots
            from ..datasets.paths import resolve_typed_path

            resolved = resolve_typed_path(dataset_path)
        except Exception:  # noqa: BLE001
            resolved = None
        return Path(resolved) if resolved else p

    @staticmethod
    def _load(p: Path, max_cells: int, seed: int) -> Tuple[ad.AnnData, Dict[str, Any]]:
        """Load the dataset, reading at most max_cells (seeded, without replacement) from large .h5ad files."""
        if p.suffix == ".zarr":
            adata = ad.read_zarr(str(p))
            n_total = adata.n_obs
            if max_cells and n_total > max_cells:
                idx = np.sort(np.random.default_rng(seed).choice(n_total, size=max_cells, replace=False))
                adata = adata[idx].copy()
        else:
            backed = sc.read_h5ad(str(p), backed="r")
            try:
                n_total = backed.n_obs
                if max_cells and n_total > max_cells:
                    idx = np.sort(np.random.default_rng(seed).choice(n_total, size=max_cells, replace=False))
                    adata = backed[idx].to_memory()
                else:
                    adata = backed.to_memory()
            finally:
                try:
                    backed.file.close()
                except Exception:  # noqa: BLE001
                    pass
        applied = bool(max_cells and n_total > max_cells)
        return adata, {
            "applied": applied,
            "n_cells_total": int(n_total),
            "n_cells_used": int(adata.n_obs),
            "seed": seed if applied else None,
            "method": "uniform random cells without replacement" if applied else None,
        }

    @staticmethod
    def _select_matrix(
        adata: ad.AnnData, matrix_layer: Optional[str], input_scale: Optional[str]
    ) -> Tuple[ad.AnnData, str, str, str]:
        """(working AnnData whose X is the chosen matrix, matrix label, scale, scale basis)."""
        uns = dict(adata.uns) if hasattr(adata, "uns") else {}

        def build(label: str) -> ad.AnnData:
            if label == "X":
                return ad.AnnData(X=adata.X.copy(), obs=adata.obs.copy(), var=adata.var.copy(), uns=uns)
            if label == "raw":
                raw = adata.raw
                return ad.AnnData(X=raw.X.copy(), obs=adata.obs.copy(), var=raw.var.copy(), uns=uns)
            return ad.AnnData(X=adata.layers[label].copy(), obs=adata.obs.copy(), var=adata.var.copy(), uns=uns)

        def label_of(name: str) -> str:
            return "X" if name == "X" else ("raw" if name == "raw" else f"layers['{name}']")

        if matrix_layer:
            if matrix_layer == "raw" and adata.raw is None:
                raise _NeedsInput("MATRIX_NOT_FOUND", "The dataset has no .raw matrix to analyze.")
            if matrix_layer not in ("X", "raw") and matrix_layer not in adata.layers:
                raise _NeedsInput("MATRIX_NOT_FOUND", f"The dataset has no layer named '{matrix_layer}'.")
            work = build(matrix_layer)
            detected, basis = detect_input_scale(work.X, uns)
            if input_scale:
                LumicellAnalysisEngine._check_declared(input_scale, detected)
                return work, label_of(matrix_layer), input_scale, f"declared by the user; detected {detected}"
            return work, label_of(matrix_layer), detected, basis

        candidates: List[str] = [name for name in ("counts", "raw_counts") if name in adata.layers]
        candidates.append("X")
        if adata.raw is not None:
            candidates.append("raw")
        detected_by: Dict[str, Tuple[str, str]] = {}
        for name in candidates:
            raw = adata.raw
            assert raw is not None or name != "raw"  # guaranteed by candidates list
            matrix = adata.X if name == "X" else (raw.X if name == "raw" and raw is not None else adata.layers[name])
            detected_by[name] = detect_input_scale(matrix, uns)

        if input_scale:  # a declaration applies to X unless a layer was named
            detected = detected_by["X"][0]
            LumicellAnalysisEngine._check_declared(input_scale, detected)
            return build("X"), "X", input_scale, f"declared by the user; detected {detected}"

        # Prefer counts (full QC), then log-normalized, then library-size normalized values.
        for wanted in ("raw_counts", "log1p_counts", "log1p", "normalized"):
            for name in candidates:
                scale, basis = detected_by[name]
                if scale == wanted:
                    return build(name), label_of(name), scale, basis
        scale, basis = detected_by["X"]
        if scale == "scaled":
            raise _NeedsInput(
                "INPUT_SCALE_UNSUPPORTED",
                "The expression matrix is centered/scaled (negative values) and the file has no raw-count layer or "
                ".raw matrix. Upload raw counts or log-normalized values, or name the layer that holds them.",
            )
        raise _NeedsInput(
            "INPUT_SCALE_UNKNOWN",
            f"The input scale of the expression matrix could not be determined ({basis}). Declare whether it holds "
            "raw counts, library-size normalized values or log1p-normalized values, or name a raw-count layer.",
        )

    @staticmethod
    def _check_declared(declared: str, detected: str) -> None:
        if declared not in INPUT_SCALES or declared == "unknown":
            raise _NeedsInput("INPUT_SCALE_INVALID", f"Unsupported input scale '{declared}'.")
        if declared == "scaled":
            raise _NeedsInput(
                "INPUT_SCALE_UNSUPPORTED",
                "Scaled data cannot be used for QC, normalization or marker testing; provide counts or log-normalized values.",
            )
        if detected == "scaled":
            raise _NeedsInput(
                "INPUT_SCALE_CONFLICT", f"The matrix was declared {declared} but contains negative values."
            )
        if declared == "raw_counts" and detected != "raw_counts":
            raise _NeedsInput(
                "INPUT_SCALE_CONFLICT", f"The matrix was declared raw counts but holds non-integer values ({detected})."
            )

    def execute_single_cell_pipeline(
        self,
        dataset_path: Optional[str] = None,
        target_gene: Optional[str] = None,
        dataset_name: Optional[str] = None,
        max_pct_mt: float = 20.0,
        n_neighbors: Optional[int] = None,
        resolution: float = 0.5,
        matrix_layer: Optional[str] = None,
        input_scale: Optional[str] = None,
        skip_umap: Optional[bool] = None,
        max_cells: Optional[int] = None,
        random_seed: int = 0,
    ) -> ToolResult:
        """
        Run end-to-end QC, normalization, clustering, and differential expression marker analysis.
        Requires a real AnnData file path (.h5ad / .zarr).
        If dataset_path is None or empty, returns an explicit DATASET_REQUIRED error state.
        matrix_layer ("X", "raw" or a layer name) and input_scale (raw_counts | normalized | log1p) override
        automatic selection; an undeterminable or scaled-only input returns VALIDATION_ERROR with
        metadata task_status="needs_input" instead of running.
        """
        t0 = time.perf_counter()

        # Enforce real dataset requirement (No synthetic generation on product path)
        if not dataset_path or not dataset_path.strip():
            return self._failure(
                "Single-cell transcriptomics analysis requires a real dataset or uploaded AnnData file (.h5ad / .zarr) with provenance. No dataset was provided.",
                ErrorType.DATASET_REQUIRED,
                t0,
                status="DATASET_REQUIRED",
                task_status="needs_input",
            )

        p = self._resolve_path(dataset_path.strip())
        if not p.exists():
            return self._failure(
                f"Single-cell dataset file not found at: {dataset_path}",
                ErrorType.NOT_FOUND,
                t0,
                dataset_path=dataset_path,
                task_status="needs_input",
            )

        cell_cap = max_cells if max_cells is not None else _env_int("PHRONESIS_LUMICELL_MAX_CELLS", DEFAULT_MAX_CELLS)
        try:
            # 1. Load real AnnData dataset (large files are subsampled with a recorded seed)
            adata, subsampling = self._load(p, cell_cap, random_seed)

            if adata.n_obs < 5 or adata.n_vars < 2:
                return self._failure(
                    f"Dataset has insufficient cells ({adata.n_obs}) or genes ({adata.n_vars}) for statistical single-cell analysis.",
                    ErrorType.VALIDATION_ERROR,
                    t0,
                )

            n_initial = adata.n_obs
            resolved_ds_name = dataset_name or p.stem

            # Extract provenance metadata if present
            provenance: Dict[str, Any] = {
                "file_path": str(p),
                "file_size_bytes": p.stat().st_size if p.is_file() else None,
                "n_obs_raw": subsampling["n_cells_total"],
                "n_vars_raw": adata.n_vars,
            }
            if hasattr(adata, "uns"):
                for k in ("organism", "tissue", "disease", "technology", "donor_id"):
                    if k in adata.uns:
                        provenance[k] = str(adata.uns[k])

            # 2. Choose the matrix and establish its scale before any transformation
            try:
                work, matrix_label, scale, scale_basis = self._select_matrix(adata, matrix_layer, input_scale)
            except _NeedsInput as exc:
                return self._failure(
                    exc.message,
                    ErrorType.VALIDATION_ERROR,
                    t0,
                    status="NEEDS_INPUT",
                    reason_code=exc.reason_code,
                    task_status="needs_input",
                )
            del adata
            reported_scale = scale
            work.var_names_make_unique()
            transformations: List[str] = []
            warnings: List[str] = []
            if subsampling["applied"]:
                warnings.append(
                    f"analyzed a random subsample of {subsampling['n_cells_used']} of {subsampling['n_cells_total']} cells"
                )

            if scale == "log1p_counts":
                if sparse.issparse(work.X):
                    work.X = work.X.copy()
                    work.X.data = np.round(np.expm1(work.X.data))
                else:
                    work.X = np.round(np.expm1(np.asarray(work.X, dtype=np.float64))).astype(np.float32)
                transformations.append("expm1 + round to recover counts from log1p(counts)")
                scale = "raw_counts"
                scale_basis = f"recovered from log1p counts ({scale_basis})"

            # 3. Quality Control (QC); mt% is meaningful on counts and on library-size normalized values only
            symbols, symbol_column = _symbol_series(work.var, work.var_names)
            if symbol_column:
                transformations.append(f"gene symbols read from var['{symbol_column}']")
            work.var["mt"] = symbols.str.upper().str.startswith("MT-").values
            n_mt = int(work.var["mt"].sum())
            mt_filter_applied = scale in ("raw_counts", "normalized") and n_mt > 0
            if n_mt == 0:
                warnings.append("no mitochondrial (MT-) genes found; the mitochondrial QC filter was not applied")
            elif not mt_filter_applied:
                warnings.append(f"mitochondrial percentage is not defined on {scale} values; the mt filter was not applied")
            sc.pp.calculate_qc_metrics(work, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)

            min_genes = max(1, min(5, work.n_vars - 1))
            sc.pp.filter_cells(work, min_genes=min_genes)
            if mt_filter_applied and "pct_counts_mt" in work.obs.columns:
                work = work[work.obs["pct_counts_mt"] < max_pct_mt, :].copy()
            min_cells = 3 if work.n_obs >= 50 else 1
            n_vars_before = work.n_vars
            sc.pp.filter_genes(work, min_cells=min_cells)
            if work.n_vars < n_vars_before:
                transformations.append(f"removed {n_vars_before - work.n_vars} genes detected in < {min_cells} cells")
            n_post_qc = work.n_obs

            if n_post_qc < 3:
                return self._failure(
                    f"Too few cells ({n_post_qc}) survived QC threshold (pct_counts_mt < {max_pct_mt}%).",
                    ErrorType.VALIDATION_ERROR,
                    t0,
                )
            if work.n_vars < 2:
                return self._failure(
                    f"Too few genes ({work.n_vars}) are expressed after QC for clustering.",
                    ErrorType.VALIDATION_ERROR,
                    t0,
                )

            # 4. Normalization & log transform, only where the input scale requires them
            if scale == "raw_counts":
                work.layers["counts"] = work.X.copy()
                sc.pp.normalize_total(work, target_sum=1e4)
                transformations.append("normalize_total(target_sum=1e4)")
            if scale in ("raw_counts", "normalized"):
                sc.pp.log1p(work)
                transformations.append("log1p")
            else:
                transformations.append("input already log1p-normalized; no normalization or log applied")

            # 5. Dimensionality Reduction & Clustering
            use_hvg = False
            n_hvg = 0
            if work.n_vars > 50:
                try:
                    sc.pp.highly_variable_genes(work, min_mean=0.0125, max_mean=5, min_disp=0.2)
                    n_hvg = int(work.var["highly_variable"].sum()) if "highly_variable" in work.var else 0
                    use_hvg = n_hvg >= 5
                except Exception as exc:  # noqa: BLE001
                    logger.info("HVG selection skipped: %s", exc)
                    use_hvg = False
            n_features = n_hvg if use_hvg else work.n_vars
            # ARPACK needs n_comps strictly below both matrix dimensions.
            n_pcs = max(1, min(MAX_PCS, n_features - 1, n_post_qc - 1))
            sc.tl.pca(
                work,
                svd_solver="arpack",
                n_comps=n_pcs,
                mask_var="highly_variable" if use_hvg else None,
                random_state=random_seed,
            )
            k_neighbors = n_neighbors if n_neighbors is not None else min(15, n_post_qc - 1)
            k_neighbors = max(2, min(k_neighbors, n_post_qc - 1))
            sc.pp.neighbors(work, n_neighbors=k_neighbors, n_pcs=n_pcs, random_state=random_seed)

            method = "leiden"
            try:
                sc.tl.leiden(
                    work, resolution=resolution, key_added="leiden", flavor="igraph", n_iterations=2, random_state=random_seed
                )
                method_detail = "leiden (igraph)"
            except Exception:
                try:
                    sc.tl.leiden(work, resolution=resolution, key_added="leiden", random_state=random_seed)
                    method_detail = "leiden (leidenalg)"
                except Exception:
                    sc.tl.louvain(work, resolution=resolution, key_added="leiden", random_state=random_seed)
                    method = "louvain"
                    method_detail = "louvain (Leiden unavailable)"

            umap_cap = _env_int("PHRONESIS_LUMICELL_UMAP_MAX_CELLS", DEFAULT_UMAP_MAX_CELLS)
            run_umap = not skip_umap if skip_umap is not None else n_post_qc <= umap_cap
            if run_umap:
                sc.tl.umap(work, random_state=random_seed)
            else:
                warnings.append("UMAP embedding skipped")

            # 6. Cluster markers: Wilcoxon rank-sum, cluster vs. rest, on the log-normalized matrix in X
            top_markers_by_cluster: Dict[str, List[ClusterMarkerInfo]] = {}
            unique_clusters = sorted(work.obs["leiden"].unique().tolist(), key=lambda c: (len(str(c)), str(c)))
            cluster_sizes = work.obs["leiden"].value_counts()
            testable = [str(c) for c in unique_clusters if int(cluster_sizes.get(c, 0)) >= 2]
            for cl in unique_clusters:
                top_markers_by_cluster[str(cl)] = []
            if len(unique_clusters) > 1 and testable:
                sc.tl.rank_genes_groups(
                    work,
                    groupby="leiden",
                    groups=testable,
                    reference="rest",
                    method="wilcoxon",
                    corr_method="benjamini-hochberg",
                    use_raw=False,
                    key_added="rank_genes",
                )
                result_genes = work.uns["rank_genes"]
                symbol_of = dict(zip(work.var_names.astype(str), symbols.reindex(work.var_names).astype(str)))
                for cl_str in testable:
                    names = result_genes["names"][cl_str]
                    for idx in range(len(names)):
                        lfc = float(result_genes["logfoldchanges"][cl_str][idx])
                        padj = float(result_genes["pvals_adj"][cl_str][idx])
                        if not (np.isfinite(lfc) and lfc > 0 and padj < MARKER_PADJ_MAX):
                            continue
                        var_name = str(names[idx])
                        symbol = symbol_of.get(var_name, var_name)
                        top_markers_by_cluster[cl_str].append(
                            ClusterMarkerInfo(
                                gene=symbol,
                                gene_id=var_name if symbol != var_name else None,
                                cluster=cl_str,
                                log_fold_change=round(lfc, 3),
                                score=round(float(result_genes["scores"][cl_str][idx]), 3),
                                p_value=float(result_genes["pvals"][cl_str][idx]),
                                p_adj=padj,
                            )
                        )
                        if len(top_markers_by_cluster[cl_str]) >= 5:
                            break
            untested = [str(c) for c in unique_clusters if str(c) not in testable]
            if untested:
                warnings.append(f"clusters with a single cell were not tested for markers: {', '.join(untested)}")

            # 7. Target gene expression per cluster (expression only; not a mutation or activity test)
            target_summary: Dict[str, Dict[str, float]] = {}
            target_note: Optional[str] = None
            if target_gene:
                t_upper = target_gene.strip().upper()
                matches = [v for v, s in zip(work.var_names, symbols.reindex(work.var_names).astype(str)) if s.upper() == t_upper or str(v).upper() == t_upper]
                if matches:
                    gene_key = matches[0]
                    for cl in unique_clusters:
                        sub = work[work.obs["leiden"] == cl, gene_key].X
                        arr = sub.toarray() if hasattr(sub, "toarray") else np.asarray(sub)
                        target_summary[str(cl)] = {
                            "mean_expression": round(float(np.mean(arr)), 3),
                            "percent_expressing_cells": round(float(np.mean(arr > 0) * 100), 1),
                        }
                    target_note = (
                        f"{target_gene.strip()} values are mean log-normalized expression per cluster; they do not test "
                        "mutation status, pathway activity or causal regulation."
                    )
                else:
                    warnings.append(f"target gene {target_gene.strip()} was not found among the dataset's genes")

            # 8. Cluster cell counts & Donor distribution
            cluster_counts = {str(k): int(v) for k, v in cluster_sizes.items()}
            donor_dist: Dict[str, int] = {}
            if "donor_id" in work.obs.columns:
                donor_dist = {str(k): int(v) for k, v in work.obs["donor_id"].value_counts().items()}
            elif "sample" in work.obs.columns:
                donor_dist = {str(k): int(v) for k, v in work.obs["sample"].value_counts().items()}
            else:
                donor_dist = {"unassigned_donor": int(n_post_qc)}

            # 9. Annotate clusters with biological cell types
            cluster_cell_types = self._annotate_clusters(work, symbols, unique_clusters)

            # 10. Render UMAP Plot
            plot_b64 = self._create_umap_plot_base64(work, method) if run_umap else None

            artifact = LumicellExecutionArtifact(
                dataset_name=resolved_ds_name,
                dataset_path=str(p),
                dataset_provenance=provenance,
                n_cells_initial=n_initial,
                n_cells_post_qc=n_post_qc,
                n_genes=work.n_vars,
                n_clusters=len(unique_clusters),
                donor_distribution=donor_dist,
                cluster_cell_counts=cluster_counts,
                cluster_cell_types=cluster_cell_types,
                top_markers_by_cluster=top_markers_by_cluster,
                marker_criteria={
                    "test": "wilcoxon rank-sum, cluster vs. rest",
                    "correction": "benjamini-hochberg",
                    "matrix": "log-normalized X (use_raw=False)",
                    "padj_max": MARKER_PADJ_MAX,
                    "log2fc_min_exclusive": 0.0,
                    "max_per_cluster": 5,
                },
                target_expression_summary=target_summary,
                target_expression_note=target_note,
                input_matrix=matrix_label,
                input_scale=reported_scale,
                scale_basis=scale_basis,
                transformations=transformations,
                subsampling=subsampling,
                warnings=warnings,
                qc_parameters={
                    "max_pct_mt": max_pct_mt,
                    "mt_filter_applied": mt_filter_applied,
                    "n_mt_genes": n_mt,
                    "min_genes": min_genes,
                    "gene_min_cells": min_cells,
                },
                clustering_parameters={
                    "method": method,
                    "method_detail": method_detail,
                    "resolution": resolution,
                    "n_pcs": n_pcs,
                    "pca_features": "highly_variable" if use_hvg else "all",
                    "n_features": n_features,
                    "n_neighbors": k_neighbors,
                    "random_seed": random_seed,
                    "umap": run_umap,
                },
                plot_base64_png=plot_b64,
            )

            elapsed = time.perf_counter() - t0
            return ToolResult(
                success=True,
                tool_name=TOOL_NAME,
                data=artifact.model_dump(),
                canonical_ids={"target_gene": target_gene.upper() if target_gene else ""},
                execution_time_seconds=elapsed,
                metadata={"adapter": "LOCAL_COMPUTE", "library": "Scanpy/AnnData", "dataset_provenance": provenance},
            )

        except _NeedsInput as exc:
            return self._failure(
                exc.message, ErrorType.VALIDATION_ERROR, t0, status="NEEDS_INPUT", reason_code=exc.reason_code, task_status="needs_input"
            )
        except Exception as e:
            elapsed = time.perf_counter() - t0
            logger.error("Lumicell execution failed: %s", e, exc_info=True)
            return ToolResult(
                success=False,
                tool_name=TOOL_NAME,
                error=f"Single-cell analysis failed: {e}",
                error_type=ErrorType.INTERNAL_ERROR,
                canonical_ids={"target_gene": target_gene.upper() if target_gene else ""},
                execution_time_seconds=elapsed,
            )
