"""
Audit fixes on the legacy Synapta path (hermetic: no network, no Milvus).

KG-015 / DEP-001: pickles are unpickled only after their SHA-256 matches a pinned value.
KG-016: PCST edge arrays, real node names, deterministic OLS text, Reactome body validation, no runtime pip
installs, vectorised multimodal query matching.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import joblib
import networkx as nx
import numpy as np
import pandas as pd
import pytest
import torch
from torch_geometric.data import Data

from phronesis.synapta.datasets import biobridge_primekg as biobridge_module
from phronesis.synapta.datasets import starkqa_primekg as starkqa_module
from phronesis.synapta.utils import integrity
from phronesis.synapta.utils.integrity import IntegrityError, load_local_pickle, load_verified_pickle, verify_sha256
from phronesis.synapta.utils.kg_utils import display_node_keys

pytestmark = pytest.mark.unit_mock


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(name="no_unpickle")
def no_unpickle_fixture(monkeypatch):
    """joblib.load must never be reached for a rejected file."""

    def boom(*_a, **_k):
        raise AssertionError("joblib.load called on an unverified file")

    monkeypatch.setattr(joblib, "load", boom)


# -----------------------------------------------------------------------------
# KG-015 / DEP-001: integrity helper
# -----------------------------------------------------------------------------

def test_tampered_pickle_is_rejected_before_unpickling(tmp_path, no_unpickle):
    good = {"node": "info"}
    path = tmp_path / "node_info.pkl"
    joblib.dump(good, path)
    pinned = _sha(path.read_bytes())
    path.write_bytes(path.read_bytes() + b"tampered")  # upstream replaced / truncated / altered
    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        load_verified_pickle(path, pinned)


def test_downloaded_pickle_without_a_pinned_hash_is_refused(tmp_path, no_unpickle):
    path = tmp_path / "x.pkl"
    path.write_bytes(b"anything")
    with pytest.raises(IntegrityError, match="no pinned SHA-256"):
        load_verified_pickle(path, None)
    with pytest.raises(IntegrityError, match="invalid SHA-256"):
        verify_sha256(path, "not-a-hash")


def test_verified_pickle_loads(tmp_path):
    path = tmp_path / "ok.pkl"
    joblib.dump({"a": 1}, path)
    assert load_verified_pickle(path, "sha256:" + _sha(path.read_bytes()).upper()) == {"a": 1}


def test_local_pickle_policy(tmp_path, monkeypatch):
    monkeypatch.delenv(integrity.TRUST_LOCAL_PICKLES_ENV, raising=False)
    path = tmp_path / "graph.pkl"
    joblib.dump([1, 2, 3], path)
    digest = _sha(path.read_bytes())

    # Neither a hash nor the operator flag: refused.
    with pytest.raises(IntegrityError, match="refusing to unpickle"):
        load_local_pickle(path)
    # Expected hash from the source-graph config.
    assert load_local_pickle(path, digest) == [1, 2, 3]
    # A sidecar next to the file.
    Path(str(path) + ".sha256").write_text(f"{digest}  graph.pkl\n", encoding="utf-8")
    assert load_local_pickle(path) == [1, 2, 3]
    # A wrong sidecar hash is a mismatch, not a fallback to trust.
    Path(str(path) + ".sha256").write_text("0" * 64, encoding="utf-8")
    monkeypatch.setenv(integrity.TRUST_LOCAL_PICKLES_ENV, "1")
    with pytest.raises(IntegrityError, match="mismatch"):
        load_local_pickle(path)
    Path(str(path) + ".sha256").unlink()
    # Explicit operator trust for files they built themselves.
    assert load_local_pickle(path) == [1, 2, 3]


def test_subgraph_tool_rejects_a_graph_whose_pinned_hash_differs(tmp_path, monkeypatch, no_unpickle):
    from phronesis.synapta.tools import subgraph_extraction as tool_module

    monkeypatch.delenv(integrity.TRUST_LOCAL_PICKLES_ENV, raising=False)
    pyg, text = tmp_path / "pyg.pkl", tmp_path / "text.pkl"
    pyg.write_bytes(b"x")
    text.write_bytes(b"y")

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(tool_module, "hydra", SimpleNamespace(
        initialize=lambda **_k: Ctx(),
        compose=lambda **_k: SimpleNamespace(tools=SimpleNamespace(subgraph_extraction=None))))
    state = {"dic_source_graph": [{"name": "g", "kg_pyg_path": str(pyg), "kg_text_path": str(text),
                                   "kg_pyg_sha256": "1" * 64}],
             "uploaded_files": []}
    with pytest.raises(IntegrityError):
        tool_module.SubgraphExtractionTool()._run(tool_call_id="t", state=state, prompt="p")


# -----------------------------------------------------------------------------
# KG-015: BioBridge and StarkQA loaders
# -----------------------------------------------------------------------------

def _biobridge(tmp_path, pins=None):
    inst = biobridge_module.BioBridgePrimeKG(primekg_dir=str(tmp_path / "primekg") + "/",
                                             local_dir=str(tmp_path / "bb") + "/", embedding_sha256=pins)
    inst.preselected_node_types = ["protein", "drug"]
    return inst


def _fake_download(payloads):
    def download(remote_url, local_dir, local_filename):
        del remote_url
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / local_filename).write_bytes(payloads[local_filename])
    return download


def _pickle_bytes(tmp_path, obj, name):
    p = tmp_path / name
    joblib.dump(obj, p)
    return p.read_bytes()


def test_biobridge_refuses_unpinned_or_tampered_embeddings(tmp_path, monkeypatch, no_unpickle):
    protein = _pickle_bytes(tmp_path, {"node_index": [0], "embedding": [[1.0, 2.0]]}, "p.pkl")
    drug = _pickle_bytes(tmp_path, {"node_index": [5], "embedding": [[3.0]]}, "d.pkl")
    payloads = {"protein.pkl": protein, "drug.pkl": drug}

    unpinned = _biobridge(tmp_path / "a")
    monkeypatch.setattr(unpinned, "_download_file", _fake_download(payloads))
    with pytest.raises(IntegrityError, match="no pinned SHA-256"):
        unpinned._build_node_embeddings()

    # The audited bytes were pinned; what arrives from the mutable branch differs.
    tampered = _biobridge(tmp_path / "b", pins={"protein.pkl": _sha(protein), "drug.pkl": _sha(drug)})
    monkeypatch.setattr(tampered, "_download_file",
                        _fake_download({"protein.pkl": protein + b"-injected", "drug.pkl": drug}))
    with pytest.raises(IntegrityError, match="mismatch"):
        tampered._build_node_embeddings()


def test_biobridge_pinned_embeddings_load_and_cache_without_pickle(tmp_path, monkeypatch):
    protein = _pickle_bytes(tmp_path, {"node_index": [0, 1], "embedding": np.array([[1.0, 2.0], [3.0, 4.0]])}, "p.pkl")
    drug = _pickle_bytes(tmp_path, {"node_index": [5], "embedding": [[3.5]]}, "d.pkl")
    inst = _biobridge(tmp_path, pins={"protein.pkl": _sha(protein), "drug.pkl": _sha(drug)})
    monkeypatch.setattr(inst, "_download_file", _fake_download({"protein.pkl": protein, "drug.pkl": drug}))
    emb = inst._build_node_embeddings()
    assert emb == {0: [1.0, 2.0], 1: [3.0, 4.0], 5: [3.5]}
    cache = Path(inst.local_dir) / "embeddings" / biobridge_module.EMBEDDING_CACHE
    assert cache.is_file() and not (Path(inst.local_dir) / "embeddings" / "embedding_dict.pkl").exists()

    # The cached second load reads the .npz (allow_pickle=False) and never unpickles.
    monkeypatch.setattr(joblib, "load", lambda *_a, **_k: pytest.fail("cache must not be unpickled"))
    assert inst._build_node_embeddings() == emb


def test_biobridge_urls_use_the_configured_ref(tmp_path, monkeypatch):
    inst = biobridge_module.BioBridgePrimeKG(primekg_dir=str(tmp_path / "pk") + "/", local_dir=str(tmp_path / "bb") + "/",
                                             ref="abc123")
    seen = []

    def record(remote_url, local_dir, local_filename):
        seen.append(remote_url)
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / local_filename).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(inst, "_download_file", record)
    inst._load_data_config()
    assert seen == ["https://raw.githubusercontent.com/RyanWangZf/BioBridge/abc123/data/BindData/data_config.json"]
    assert "refs/heads" not in Path(biobridge_module.__file__).read_text(encoding="utf-8")


def test_starkqa_rejects_tampered_node_info_and_pins_revision(tmp_path, monkeypatch, no_unpickle):
    base = tmp_path / "stark"
    (base / "qa/prime/stark_qa").mkdir(parents=True)
    pd.DataFrame({"id": [1]}).to_csv(base / "qa/prime/stark_qa/stark_qa.csv", index=False)
    (base / "qa/prime/split").mkdir(parents=True)
    for split in ["train", "val", "test", "test-0.1"]:
        (base / f"qa/prime/split/{split}.index").write_text("1\n")
    (base / "skb/prime/processed").mkdir(parents=True)
    node_info = base / "skb/prime/processed/node_info.pkl"
    node_info.write_bytes(b"crafted pickle")

    inst = starkqa_module.StarkQAPrimeKG(local_dir=str(base) + "/", node_info_sha256=_sha(b"the audited file"))
    with pytest.raises(IntegrityError, match="mismatch"):
        inst._load_stark_repo()
    assert starkqa_module.StarkQAPrimeKG(local_dir=str(base) + "/").node_info_sha256 is None  # nothing trusted
    with pytest.raises(IntegrityError, match="no pinned"):
        starkqa_module.StarkQAPrimeKG(local_dir=str(base) + "/")._load_stark_repo()

    src = Path(starkqa_module.__file__).read_text(encoding="utf-8")
    calls = src.count("hf_hub_download(") + src.count("list_repo_files(")
    assert calls == src.count("revision=self.revision") == 2  # every Hub call names the pinned revision


# -----------------------------------------------------------------------------
# KG-016: vendored extraction code
# -----------------------------------------------------------------------------

def _graph() -> Data:
    return Data(
        x=torch.tensor([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]),
        node_id=["G1", "D1", "G2"], node_name=["KRAS", "pancreatic cancer", "KRAS"],
        node_type=["gene/protein", "disease", "gene/protein"],
        desc_x=np.array([[0.2, 0.8], [0.3, 0.7], [0.1, 0.1]]),
        enriched_node=["a", "b", "c"],
        edge_index=torch.tensor([[0, 1], [1, 2]]),
        edge_attr=torch.tensor([[1.0, 0.0], [0.5, 0.5]]),
        edge_type=["rel1", "rel2"], enriched_edge=["e1", "e2"], num_nodes=3,
    )


@pytest.mark.parametrize("which", ["pcst", "multimodal"])
def test_pcst_without_virtual_vertices_returns_an_integer_array(which):
    from phronesis.synapta.utils.extractions.multimodal_pcst import MultimodalPCSTPruning
    from phronesis.synapta.utils.extractions.pcst import PCSTPruning

    pruner = PCSTPruning(topk=1, topk_e=1) if which == "pcst" else MultimodalPCSTPruning(topk=1, topk_e=1)
    graph = _graph()
    out = pruner.get_subgraph_nodes_edges(graph, np.array([0, 1]), {"edges": np.array([0]), "num_prior_edges": 1},
                                          {"edges": {0: 1}, "nodes": {}})
    assert isinstance(out["edges"], np.ndarray) and out["edges"].dtype.kind == "i"
    assert out["edges"].tolist() == [1]  # the caller's .tolist() no longer raises AttributeError
    assert sorted(out["nodes"].tolist()) == [0, 1, 2]
    empty = pruner.get_subgraph_nodes_edges(graph, np.array([0]), {"edges": np.array([], dtype=int), "num_prior_edges": 0},
                                            {"edges": {}, "nodes": {}})
    assert isinstance(empty["edges"], np.ndarray) and empty["edges"].tolist() == []


def test_milvus_pcst_without_virtual_vertices_returns_an_array():
    from phronesis.synapta.utils.extractions.milvus_multimodal_pcst import (
        DynamicLibraryLoader,
        MultimodalPCSTPruning,
        SystemDetector,
    )

    detector = SystemDetector.__new__(SystemDetector)
    detector.os_type, detector.architecture = "linux", "x86_64"
    detector.has_nvidia_gpu = detector.use_gpu = False
    pruner = MultimodalPCSTPruning(loader=DynamicLibraryLoader(detector))
    out = pruner.get_subgraph_nodes_edges(
        3, np.array([0, 1]),
        {"edges": np.array([0]), "num_prior_edges": 1, "edge_index": np.array([[0, 1], [1, 2]])},
        {"edges": {0: 1}, "nodes": {}},
    )
    assert isinstance(out["edges"], np.ndarray) and out["edges"].tolist() == [1]


def test_display_node_keys_use_names_and_disambiguate():
    assert display_node_keys(["G1", "D1", "G2"], ["KRAS", "pancreatic cancer", "KRAS"]) == [
        "KRAS (G1)", "pancreatic cancer", "KRAS (G2)"]
    assert display_node_keys(["X"], [None]) == ["unnamed (X)"]


def test_extracted_subgraph_nodes_carry_names_not_ids():
    from phronesis.synapta.tools.subgraph_extraction import SubgraphExtractionTool

    graph = _graph()
    text = {"nodes": pd.DataFrame({"n": ["a", "b", "c"]}), "edges": pd.DataFrame({"e": ["x", "y"]})}
    out = SubgraphExtractionTool().prepare_final_subgraph({"nodes": np.array([0, 1, 2]), "edges": np.array([0, 1])},
                                                          graph, text)
    assert out["graph_pyg"].node_name == ["KRAS", "pancreatic cancer", "KRAS"]
    g: nx.DiGraph = out["graph_nx"]
    assert set(g.nodes) == {"KRAS (G1)", "pancreatic cancer", "KRAS (G2)"}
    assert g.has_edge("KRAS (G1)", "pancreatic cancer") and g.nodes["pancreatic cancer"]["node_id"] == "D1"


def test_multimodal_query_matching_without_cross_join():
    from phronesis.synapta.tools import multimodal_subgraph_extraction as mm

    graph = _graph()
    graph.x = graph.x.tolist()  # the multimodal graph stores per-node feature lists
    state = {"uploaded_files": [{"file_type": "multimodal", "file_path": "q.xlsx"}]}
    sheets = {"gene-protein": pd.DataFrame({"name": ["kras"]}), "disease": pd.DataFrame({"name": ["Cancer"]})}
    orig = mm.pd.read_excel
    mm.pd.read_excel = lambda *_a, **_k: sheets
    try:
        q = mm.MultimodalSubgraphExtractionTool()._prepare_query_modalities([[0.1, 0.2]], state, graph)
    finally:
        mm.pd.read_excel = orig
    assert q["node_id"].tolist() == ["G1", "G2", "D1", "user_prompt"]
    assert state["selections"] == {"disease": ["D1"], "gene/protein": ["G1", "G2"]}


def _hydra_stub(module, attr, cfg):
    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    names = attr.split(".")
    tree = cfg
    for n in reversed(names):
        tree = SimpleNamespace(**{n: tree})
    return SimpleNamespace(initialize=lambda **_k: Ctx(), compose=lambda **_k: tree)


def test_ols_description_order_is_deterministic(monkeypatch):
    from phronesis.synapta.utils.enrichments import ols_terms as ols_module

    cfg = SimpleNamespace(base_url="https://ols.invalid/terms", timeout=1)
    monkeypatch.setattr(ols_module, "hydra", _hydra_stub(ols_module, "utils.enrichments.ols_terms", cfg))
    body = ('{"_embedded": {"terms": [{"description": ["zeta", "alpha"], "synonyms": ["mu", "alpha"], '
            '"label": "beta"}, {"description": ["zeta"], "label": "omega"}]}}')
    monkeypatch.setattr(ols_module.requests, "get", lambda *_a, **_k: SimpleNamespace(text=body))
    out = ols_module.EnrichmentWithOLS().enrich_documents(["CL_0000057"])
    # First-seen order, duplicates removed: identical text (and embedding) in every process.
    assert out == ["zeta\nalpha\nmu\nbeta\nomega"]


def test_reactome_malformed_body_is_a_clear_error(monkeypatch):
    from phronesis.synapta.utils.enrichments import reactome_pathways as reactome_module

    cfg = SimpleNamespace(base_url="https://reactome.invalid/", timeout=1)
    monkeypatch.setattr(reactome_module, "hydra",
                        _hydra_stub(reactome_module, "utils.enrichments.reactome_pathways", cfg))
    monkeypatch.setattr(reactome_module.requests, "get",
                        lambda url, **_k: SimpleNamespace(ok=True, text="R-HSA-1\tGood summary\n" if "R-HSA-1/" in url
                                                          else "<html>maintenance</html>"))
    enricher = reactome_module.EnrichmentWithReactome()
    assert enricher.enrich_documents(["R-HSA-1"]) == ["Good summary"]
    with pytest.raises(ValueError, match="unexpected Reactome /summation response for 'R-HSA-2'"):
        enricher.enrich_documents(["R-HSA-2"])


def test_milvus_data_dump_never_pip_installs(monkeypatch):
    import importlib.util

    from phronesis.synapta import milvus_data_dump as dump

    monkeypatch.setattr(dump.subprocess, "run", lambda *a, **k: pytest.fail(f"subprocess.run called: {a}"))
    det = dump.SystemDetector.__new__(dump.SystemDetector)
    det.os_type, det.architecture, det.has_nvidia_gpu, det.use_gpu = "linux", "x86_64", True, True
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name in ("cudf", "dask_cudf", "pymilvus") else real(name, *a))
    with pytest.raises(ImportError, match="pymilvus"):
        det.install_packages()
    assert det.use_gpu is False  # missing GPU packages fall back to CPU instead of installing them
    assert not any("pip install" in p for p in det.get_required_packages())
