"""
Workstream D (single-cell and datasets) regression tests from the audit repair plan
(docs/plans/audit-repair-plan.md; report F13, F14 and §8).

Matrices built here are small artificial fixtures for adversarial unit checks of the pipeline's branching,
not biological data. tests/fixtures/test_atlas.h5ad is the repository's synthetic structural fixture.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from phronesis.datasets import paths as dataset_paths
from phronesis.datasets import validators
from phronesis.datasets.models import DatasetProvenanceIn
from phronesis.datasets.store import DatasetStore
from phronesis.datasets.validators import ValidationFailure, validate_h5ad, validate_table
from phronesis.lumicell.engine import (
    ClusterMarkerInfo,
    LumicellAnalysisEngine,
    LumicellExecutionArtifact,
    detect_input_scale,
)
from phronesis.tools.registry import ErrorType

ATLAS = Path(__file__).parent / "fixtures" / "test_atlas.h5ad"


def _two_group_counts(n_per_group: int = 30, n_noise: int = 8, seed: int = 0) -> ad.AnnData:
    """Integer counts with two groups: GENEA high in group 1, GENEB high in group 2, plus noise and MT genes."""
    rng = np.random.default_rng(seed)
    n = 2 * n_per_group
    noise = rng.poisson(3.0, size=(n, n_noise))
    a = np.concatenate([rng.poisson(40, n_per_group), rng.poisson(1, n_per_group)])
    b = np.concatenate([rng.poisson(1, n_per_group), rng.poisson(40, n_per_group)])
    mt = rng.poisson(1.0, size=(n, 2))
    X = np.column_stack([a, b, noise, mt]).astype(np.float32)
    genes = ["GENEA", "GENEB"] + [f"NOISE{i}" for i in range(n_noise)] + ["MT-CO1", "MT-CO2"]
    obs = pd.DataFrame(index=[f"c{i}" for i in range(n)])
    obs["donor_id"] = ["d1"] * n_per_group + ["d2"] * n_per_group
    return ad.AnnData(X=X, obs=obs, var=pd.DataFrame(index=genes))


def _write(adata: ad.AnnData, tmp_path: Path, name: str = "atlas.h5ad") -> str:
    path = tmp_path / name
    adata.write_h5ad(path)
    return str(path)


def _run(path: str, **kw: Any):
    kw.setdefault("skip_umap", True)
    return LumicellAnalysisEngine().execute_single_cell_pipeline(dataset_path=path, **kw)


# -----------------------------------------------------------------------------
# Missing / truncated input
# -----------------------------------------------------------------------------

def test_missing_anndata_needs_input_without_invented_clusters():
    res = LumicellAnalysisEngine().execute_single_cell_pipeline(target_gene="KRAS")
    assert res.success is False and res.error_type == ErrorType.DATASET_REQUIRED
    assert res.metadata["task_status"] == "needs_input"
    assert res.data is None


def test_truncated_anndata_keeps_structural_rejection(tmp_path):
    full = ATLAS.read_bytes()
    truncated = tmp_path / "truncated.h5ad"
    truncated.write_bytes(full[: len(full) // 2])
    with pytest.raises(ValidationFailure) as err:
        validate_h5ad(truncated)
    assert err.value.reason_code == "hdf5_unreadable"
    res = _run(str(truncated))
    assert res.success is False and res.data is None


# -----------------------------------------------------------------------------
# Input-scale detection and branches
# -----------------------------------------------------------------------------

def test_raw_counts_are_normalized_and_logged_once(tmp_path):
    res = _run(_write(_two_group_counts(), tmp_path), target_gene="GENEA")
    assert res.success, res.error
    data = res.data
    assert data["input_matrix"] == "X" and data["input_scale"] == "raw_counts"
    assert data["transformations"].count("log1p") == 1
    assert any(t.startswith("normalize_total") for t in data["transformations"])
    assert data["qc_parameters"]["mt_filter_applied"] is True
    assert "mutation status" in data["target_expression_note"]


def test_log_normalized_input_is_not_logged_again(tmp_path):
    adata = _two_group_counts()
    import scanpy as sc

    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    data = res.data
    assert data["input_scale"] == "log1p"
    assert "log1p" not in data["transformations"]
    assert not any(t.startswith("normalize_total") for t in data["transformations"])
    # mt% is undefined on log values, so the filter is reported as not applied
    assert data["qc_parameters"]["mt_filter_applied"] is False
    assert any("mitochondrial percentage is not defined" in w for w in data["warnings"])


def test_log1p_of_counts_is_recovered_to_counts(tmp_path):
    adata = _two_group_counts()
    adata.X = np.log1p(adata.X)
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    data = res.data
    assert data["input_scale"] == "log1p_counts"
    assert data["transformations"][0].startswith("expm1")
    assert data["transformations"].count("log1p") == 1


def test_normalized_unlogged_input_is_logged_without_renormalizing(tmp_path):
    adata = _two_group_counts()
    import scanpy as sc

    sc.pp.normalize_total(adata, target_sum=1e4)
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    data = res.data
    assert data["input_scale"] == "normalized"
    assert data["transformations"].count("log1p") == 1
    assert not any(t.startswith("normalize_total") for t in data["transformations"])


def test_scaled_only_matrix_needs_input(tmp_path):
    adata = _two_group_counts()
    X = np.log1p(adata.X)
    adata.X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)
    res = _run(_write(adata, tmp_path))
    assert res.success is False and res.error_type == ErrorType.VALIDATION_ERROR
    assert res.metadata["reason_code"] == "INPUT_SCALE_UNSUPPORTED"
    assert res.metadata["task_status"] == "needs_input"
    assert res.data is None


def test_scaled_x_with_count_raw_uses_raw(tmp_path):
    adata = _two_group_counts()
    adata.raw = adata.copy()
    X = np.log1p(np.asarray(adata.X))
    adata.X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    assert res.data["input_matrix"] == "raw" and res.data["input_scale"] == "raw_counts"


def test_unknown_scale_needs_input(tmp_path):
    rng = np.random.default_rng(1)
    adata = ad.AnnData(X=rng.uniform(0.5, 90.0, size=(30, 12)).astype(np.float32),
                       var=pd.DataFrame(index=[f"G{i}" for i in range(12)]))
    scale, _basis = detect_input_scale(adata.X)
    assert scale == "unknown"
    res = _run(_write(adata, tmp_path))
    assert res.success is False and res.metadata["reason_code"] == "INPUT_SCALE_UNKNOWN"


def test_declared_raw_counts_on_log_data_conflicts(tmp_path):
    adata = _two_group_counts()
    import scanpy as sc

    sc.pp.normalize_total(adata)
    sc.pp.log1p(adata)
    res = _run(_write(adata, tmp_path), input_scale="raw_counts")
    assert res.success is False and res.metadata["reason_code"] == "INPUT_SCALE_CONFLICT"


def test_declared_layer_is_used(tmp_path):
    adata = _two_group_counts()
    adata.layers["umi"] = adata.X.copy()
    adata.X = np.log1p(adata.X) * 0.5 - 1.0  # scaled-looking X the user tells us to ignore
    res = _run(_write(adata, tmp_path), matrix_layer="umi")
    assert res.success, res.error
    assert res.data["input_matrix"] == "layers['umi']" and res.data["input_scale"] == "raw_counts"
    missing = _run(_write(adata, tmp_path, "b.h5ad"), matrix_layer="nope")
    assert missing.success is False and missing.metadata["reason_code"] == "MATRIX_NOT_FOUND"


def test_non_finite_values_are_rejected(tmp_path):
    adata = _two_group_counts()
    X = adata.X.copy()
    X[0, 0] = np.nan
    adata.X = X
    res = _run(_write(adata, tmp_path))
    assert res.success is False and res.metadata["reason_code"] == "NON_FINITE_VALUES"


def test_sparse_counts_are_detected():
    X = sparse.csr_matrix(np.array([[0, 2, 0], [1, 0, 3], [0, 0, 5]], dtype=np.float32))
    assert detect_input_scale(X)[0] == "raw_counts"


# -----------------------------------------------------------------------------
# Markers, PCA bound and gene symbols
# -----------------------------------------------------------------------------

def test_markers_use_x_not_raw(tmp_path):
    adata = _two_group_counts(n_per_group=40)
    raw = adata.copy()
    flat = np.asarray(raw.X).copy()
    flat[:, :2] = 5.0  # in .raw the two group genes carry no signal
    raw.X = flat
    adata.raw = raw
    res = _run(_write(adata, tmp_path), resolution=0.3)
    assert res.success, res.error
    data = res.data
    assert data["input_matrix"] == "X"
    assert data["marker_criteria"]["matrix"].startswith("log-normalized X")
    marker_genes = {m["gene"] for rows in data["top_markers_by_cluster"].values() for m in rows}
    assert {"GENEA", "GENEB"} & marker_genes
    for rows in data["top_markers_by_cluster"].values():
        for m in rows:
            assert m["p_adj"] < 0.05 and m["log_fold_change"] > 0


def test_small_matrix_does_not_fail_on_pca(tmp_path):
    rng = np.random.default_rng(3)
    X = np.column_stack([rng.poisson(5, 8), rng.poisson(9, 8)]).astype(np.float32)
    adata = ad.AnnData(X=X, var=pd.DataFrame(index=["G1", "G2"]))
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    assert res.data["clustering_parameters"]["n_pcs"] == 1


def test_ensembl_var_names_use_symbol_column(tmp_path):
    adata = _two_group_counts()
    symbols = list(adata.var_names)
    adata.var["feature_name"] = symbols
    adata.var_names = [f"ENSG{100000000000 + i:011d}" for i in range(adata.n_vars)]
    res = _run(_write(adata, tmp_path), target_gene="GENEA")
    assert res.success, res.error
    data = res.data
    assert data["qc_parameters"]["n_mt_genes"] == 2
    assert data["target_expression_summary"], "target looked up through the symbol column"
    markers = [m for rows in data["top_markers_by_cluster"].values() for m in rows]
    assert markers and all(m["gene_id"].startswith("ENSG") and not m["gene"].startswith("ENSG") for m in markers)


def test_no_mt_genes_warns(tmp_path):
    adata = _two_group_counts()[:, :-2].copy()
    res = _run(_write(adata, tmp_path))
    assert res.success, res.error
    assert res.data["qc_parameters"]["mt_filter_applied"] is False
    assert any("no mitochondrial" in w for w in res.data["warnings"])


def test_markdown_reports_real_padj_and_cluster_markers():
    tiny = ClusterMarkerInfo(gene="CD3E", cluster="0", log_fold_change=2.5, score=9.1, p_value=1e-300, p_adj=3e-280)
    artifact = LumicellExecutionArtifact(
        dataset_name="x", n_cells_initial=10, n_cells_post_qc=10, n_genes=5, n_clusters=2,
        cluster_cell_counts={"0": 6, "1": 4}, top_markers_by_cluster={"0": [tiny], "1": []},
        clustering_parameters={"method": "louvain"},
    )
    assert artifact.top_markers_by_cluster["0"][0].p_adj == 3e-280  # not rounded to zero
    text = artifact.to_markdown_summary()
    assert "FDR $p < 0.05$" not in text
    assert "padj = 3.00e-280" in text and "no marker with padj < 0.05" in text
    assert "Louvain clusters" in text and "Cluster Markers" in text


# -----------------------------------------------------------------------------
# Large files and UMAP
# -----------------------------------------------------------------------------

def test_large_files_are_subsampled_with_recorded_seed():
    res = LumicellAnalysisEngine().execute_single_cell_pipeline(
        dataset_path=str(ATLAS), max_cells=20, random_seed=7, skip_umap=True, max_pct_mt=80.0
    )
    assert res.success, res.error
    data = res.data
    assert data["n_cells_initial"] == 20
    assert data["subsampling"] == {
        "applied": True, "n_cells_total": 40, "n_cells_used": 20, "seed": 7,
        "method": "uniform random cells without replacement",
    }
    assert data["plot_base64_png"] is None and data["clustering_parameters"]["umap"] is False


def test_umap_runs_by_default_on_small_data():
    res = LumicellAnalysisEngine().execute_single_cell_pipeline(dataset_path=str(ATLAS), max_pct_mt=80.0)
    assert res.success, res.error
    assert res.data["clustering_parameters"]["umap"] is True and res.data["plot_base64_png"]


# -----------------------------------------------------------------------------
# Typed dataset paths
# -----------------------------------------------------------------------------

def test_typed_paths_default_to_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PHRONESIS_DATASET_ROOTS", raising=False)
    monkeypatch.setenv("PHRONESIS_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "atlas.h5ad").write_bytes(ATLAS.read_bytes())
    (tmp_path / "outside.h5ad").write_bytes(ATLAS.read_bytes())
    assert dataset_paths.allowed_dataset_roots() == [os.path.join(str(tmp_path), "data")]
    assert dataset_paths.is_allowed_typed_path("atlas.h5ad")
    assert dataset_paths.resolve_typed_path("atlas.h5ad") == os.path.realpath(tmp_path / "data" / "atlas.h5ad")
    assert not dataset_paths.is_allowed_typed_path(str(tmp_path / "outside.h5ad"))
    assert not dataset_paths.is_allowed_typed_path("../outside.h5ad")
    res = _run("atlas.h5ad", max_pct_mt=80.0)
    assert res.success, res.error


# -----------------------------------------------------------------------------
# Upload store and validators
# -----------------------------------------------------------------------------

def test_table_column_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(validators, "MAX_TABLE_COLUMNS", 5)
    df = pd.DataFrame(np.ones((6, 8)), index=[f"c{i}" for i in range(6)], columns=[f"G{i}" for i in range(8)])
    src = tmp_path / "wide.csv"
    df.to_csv(src)
    with pytest.raises(ValidationFailure) as err:
        validate_table(src, ".csv", None, tmp_path / "out.h5ad")
    assert err.value.reason_code == "matrix_too_large"


def _prov(**kw: Any) -> DatasetProvenanceIn:
    return DatasetProvenanceIn(**{"source": "in_house", "attests_no_direct_identifiers": True, **kw})


def test_declared_matrix_and_scale_reach_public_provenance(tmp_path):
    store = DatasetStore(tmp_path / "uploads")
    rec = asyncio.run(
        store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov(matrix_layer="counts", input_scale="raw_counts"))
    )
    public: Dict[str, Any] = rec.public_provenance()
    assert public["matrix_layer"] == "counts" and public["input_scale"] == "raw_counts"
    with pytest.raises(ValueError):
        _prov(matrix_layer="../etc")


def test_concurrent_provenance_updates_are_all_recorded(tmp_path):
    store = DatasetStore(tmp_path / "uploads")

    async def scenario() -> int:
        rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov())
        await asyncio.gather(*(store.update_provenance("alice", rec.id, _prov(description=f"v{i}")) for i in range(5)))
        return len(store.get("alice", rec.id).provenance_history)

    assert asyncio.run(scenario()) == 5


# -----------------------------------------------------------------------------
# Single Cell Portal study search
# -----------------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def test_scp_search_retries_bounded_and_honours_limit(monkeypatch):
    from phronesis.lumicell.tools.scp_agent import search_studies as mod

    calls = []
    studies = [{"study_source": "SCP", "name": f"S{i}", "study_url": f"u{i}", "gene_count": 1, "cell_count": 2} for i in range(9)]
    responses = [_Resp(503), _Resp(200, {"studies": studies})]

    def fake_get(url, params=None, timeout=None, **kw):
        assert "verify" not in kw  # TLS verification stays on
        calls.append(params)
        return responses.pop(0)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    assert len(mod.fetch_studies("crohn", limit=3)) == 3 and len(calls) == 2

    calls.clear()
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: calls.append(1) or _Resp(404))
    with pytest.raises(RuntimeError):
        mod.fetch_studies("crohn")
    assert len(calls) == 1  # 4xx is not retried

    calls.clear()
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: calls.append(1) or _Resp(500))
    with pytest.raises(RuntimeError):
        mod.fetch_studies("crohn")
    assert len(calls) == mod.MAX_ATTEMPTS


def test_scp_tool_reports_empty_results(monkeypatch):
    from phronesis.lumicell.tools.scp_agent import search_studies as mod

    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp(200, {"studies": []}))
    out = mod.search_studies.func("nothing", tool_call_id="t1", limit=5)
    assert out.update["search_table"] == ""
    assert "No Single Cell Portal studies" in out.update["messages"][0].content
