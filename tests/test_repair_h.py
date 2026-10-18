"""
Workstream H regressions: Synapta graph extraction, GraphRAG, legacy Vivarix tools.
All offline; Milvus, COPASI and LLM calls are faked.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from phronesis.synapta.tools import graphrag_reasoning as graphrag_module
from phronesis.synapta.tools import milvus_multimodal_subgraph_extraction as tool_module
from phronesis.synapta.utils.extractions import milvus_multimodal_pcst as pcst_module
from phronesis.synapta.utils.extractions.milvus_multimodal_pcst import (
    DynamicLibraryLoader,
    MultimodalPCSTPruning,
    SystemDetector,
)
from phronesis.vivarix.tools import parameter_scan as scan_module
from phronesis.vivarix.tools import steady_state as steady_module

pytestmark = pytest.mark.unit_mock


@pytest.fixture(autouse=True)
def _fresh_caches():
    pcst_module.clear_edge_index_cache()
    graphrag_module.clear_vectorstore_cache()
    yield
    pcst_module.clear_edge_index_cache()
    graphrag_module.clear_vectorstore_cache()


@pytest.fixture(name="cpu_loader")
def cpu_loader_fixture():
    detector = SystemDetector.__new__(SystemDetector)
    detector.os_type, detector.architecture = "linux", "x86_64"
    detector.has_nvidia_gpu = detector.use_gpu = False
    return DynamicLibraryLoader(detector)


def _edges_collection(rows, num_entities, calls):
    """Fake edges collection returning `rows` (any order) for every range query."""

    class FakeEdges:
        def __init__(self, name):
            self.name = name
            self.num_entities = num_entities

        def load(self):
            return None

        def query(self, expr=None, output_fields=None):
            calls.append((expr, tuple(output_fields or ())))
            low = int(expr.split(">=")[1].split("and")[0])
            high = int(expr.split("<")[1])
            return [r for r in rows if low <= r["triplet_index"] < high]

    return FakeEdges


def _cfg(db="primekg", batch=10):
    return SimpleNamespace(milvus_db=SimpleNamespace(database_name=db, query_batch_size=batch))


def test_edge_index_is_placed_by_triplet_index(monkeypatch, cpu_loader):
    # Milvus returned rows out of order: column i must still be triplet i.
    rows = [
        {"triplet_index": 2, "head_index": 20, "tail_index": 21},
        {"triplet_index": 0, "head_index": 0, "tail_index": 1},
        {"triplet_index": 1, "head_index": 10, "tail_index": 11},
    ]
    calls = []
    monkeypatch.setattr(pcst_module, "Collection", _edges_collection(rows, 3, calls))
    edge_index = MultimodalPCSTPruning(loader=cpu_loader).load_edge_index(_cfg())
    assert edge_index.tolist() == [[0, 10, 20], [1, 11, 21]]
    assert "triplet_index" in calls[0][1]


def test_edge_index_missing_rows_fail_loudly(monkeypatch, cpu_loader):
    rows = [{"triplet_index": 0, "head_index": 0, "tail_index": 1}]
    monkeypatch.setattr(pcst_module, "Collection", _edges_collection(rows, 2, []))
    with pytest.raises(ValueError, match="have no row"):
        MultimodalPCSTPruning(loader=cpu_loader).load_edge_index(_cfg())


def test_edge_index_is_cached_per_database(monkeypatch, cpu_loader):
    rows = [{"triplet_index": i, "head_index": i, "tail_index": i + 1} for i in range(4)]
    calls = []
    monkeypatch.setattr(pcst_module, "Collection", _edges_collection(rows, 4, calls))
    pcst = MultimodalPCSTPruning(loader=cpu_loader)
    first = pcst.load_edge_index(_cfg(batch=2))
    n_queries = len(calls)
    second = pcst.load_edge_index(_cfg(batch=2))
    assert len(calls) == n_queries == 2
    assert second is first
    pcst.load_edge_index(_cfg(db="other", batch=2))
    assert len(calls) == 4


def test_milvus_filter_values_are_escaped():
    expr = tool_module.milvus_in_expr("node_name", ['TP53', 'a"] or node_id != "x', "back\\slash"])
    assert expr == 'node_name IN ["TP53","a\\"] or node_id != \\"x","back\\\\slash"]'


@pytest.fixture(name="extraction_tool")
def extraction_tool_fixture(monkeypatch):
    monkeypatch.setattr(SystemDetector, "_detect_nvidia_gpu", lambda self: False)
    return tool_module.MultimodalSubgraphExtractionTool()


def test_reads_every_multimodal_file(monkeypatch, extraction_tool):
    books = {
        "a.xlsx": {"gene-protein": pd.DataFrame({"name": ["TP53"], "node_type": ["gene/protein"]})},
        "b.xlsx": {"drug": pd.DataFrame({"name": ["imatinib"], "node_type": ["drug"]})},
    }
    monkeypatch.setattr(tool_module.pd, "read_excel", lambda path, sheet_name=None: books[path])
    state = {
        "uploaded_files": [
            {"file_type": "multimodal", "file_path": "a.xlsx"},
            {"file_type": "drug_data", "file_path": "x.pdf"},
            {"file_type": "multimodal", "file_path": "b.xlsx"},
        ]
    }
    df = extraction_tool._read_multimodal_files(state)
    assert sorted(df["q_node_name"]) == ["TP53", "imatinib"]
    assert sorted(df["q_node_type"]) == ["drug", "gene_protein"]


def test_selections_are_returned_and_arg_data_is_optional(extraction_tool):
    query_df = pd.DataFrame(
        {
            "node_id": ["TP53_(1)", "user_prompt"],
            "node_type": ["gene/protein", "prompt"],
        }
    )
    selections = tool_module.selections_from_query_df(query_df)
    assert selections == {"gene/protein": ["TP53_(1)"]}

    final_subgraph = {"name": ["Unified Subgraph"], "nodes": [[]], "edges": [[]], "text": ""}
    state = {"dic_source_graph": [{"name": "primekg"}], "topk_nodes": 3, "topk_edges": 3}
    command = extraction_tool._create_extraction_result(
        "call-1", state, final_subgraph, None, selections
    )
    assert command.update["selections"] == selections
    assert command.update["dic_extracted_graph"][0]["name"] == tool_module.DEFAULT_EXTRACTION_NAME
    assert "selections" not in state  # the injected state is not mutated


def test_embedding_dimension_mismatch_is_reported(monkeypatch):
    field = SimpleNamespace(name="desc_emb", params={"dim": 768})
    monkeypatch.setattr(
        tool_module,
        "Collection",
        lambda name: SimpleNamespace(schema=SimpleNamespace(fields=[field])),
    )
    cfg_db = SimpleNamespace(milvus_db=SimpleNamespace(database_name="primekg"))
    with pytest.raises(ValueError, match="1536 dimensions"):
        tool_module.MultimodalSubgraphExtractionTool._check_embedding_dim(cfg_db, [0.0] * 1536)
    tool_module.MultimodalSubgraphExtractionTool._check_embedding_dim(cfg_db, [0.0] * 768)


class _HydraCtx:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _patch_graphrag(monkeypatch, built):
    cfg = SimpleNamespace(
        prompt_graphrag_w_docs="system",
        splitter_chunk_size=100,
        splitter_chunk_overlap=0,
        retriever_search_type="mmr",
        retriever_k=2,
        retriever_fetch_k=2,
        retriever_lambda_mult=0.5,
    )
    monkeypatch.setattr(graphrag_module.hydra, "initialize", _HydraCtx)
    monkeypatch.setattr(
        graphrag_module.hydra,
        "compose",
        lambda **_k: SimpleNamespace(tools=SimpleNamespace(graphrag_reasoning=cfg)),
    )
    monkeypatch.setattr(
        graphrag_module,
        "PyPDFLoader",
        lambda file_path: SimpleNamespace(load=lambda: [file_path]),
    )
    monkeypatch.setattr(
        graphrag_module,
        "RecursiveCharacterTextSplitter",
        lambda **_k: SimpleNamespace(split_documents=lambda docs: docs),
    )

    class Store:
        @classmethod
        def from_documents(cls, documents, embedding):
            built.append(documents)
            return cls()

        def as_retriever(self, **_k):
            return object()

    monkeypatch.setattr(graphrag_module, "InMemoryVectorStore", Store)
    monkeypatch.setattr(graphrag_module, "create_stuff_documents_chain", lambda *a: object())
    monkeypatch.setattr(
        graphrag_module,
        "create_retrieval_chain",
        lambda *a: SimpleNamespace(
            invoke=lambda payload: {"input": "q", "context": ["doc text"], "answer": "The answer"}
        ),
    )


def _graphrag_state():
    return {
        "llm_model": object(),
        "embedding_model": object(),
        "uploaded_files": [{"file_type": "drug_data", "file_path": "drug.pdf"}],
        "dic_extracted_graph": [{"name": "subkg_1", "graph_summary": "summary"}],
    }


def test_graphrag_returns_answer_and_caches_documents(monkeypatch):
    built = []
    _patch_graphrag(monkeypatch, built)
    tool = graphrag_module.GraphRAGReasoningTool()
    state = _graphrag_state()
    for _ in range(2):
        command = tool._run("call-1", state, "Why?", "subkg_1")
        assert command.update["messages"][0].content == "The answer"
    assert len(built) == 1


def test_graphrag_unknown_extraction_name(monkeypatch):
    built = []
    _patch_graphrag(monkeypatch, built)
    command = graphrag_module.GraphRAGReasoningTool()._run(
        "call-1", _graphrag_state(), "Why?", "missing"
    )
    content = command.update["messages"][0].content
    assert "missing" in content and "subkg_1" in content
    assert built == []


@pytest.mark.parametrize("code", [steady_module.STEADY_STATE_NOT_FOUND, steady_module.STEADY_STATE_NEGATIVE])
def test_steady_state_rejects_not_found_and_negative(code):
    with pytest.raises(ValueError):
        steady_module.check_steady_state(code, "copasi")


def test_steady_state_rejects_unstable_point(monkeypatch):
    monkeypatch.setattr(
        steady_module.basico.model_info,
        "get_reduced_jacobian_matrix",
        lambda model=None: pd.DataFrame([[0.5, 0.0], [0.0, -1.0]]),
    )
    with pytest.raises(ValueError, match="unstable"):
        steady_module.check_steady_state(steady_module.STEADY_STATE_FOUND, "copasi")


def test_steady_state_accepts_stable_point(monkeypatch):
    monkeypatch.setattr(
        steady_module.basico.model_info,
        "get_reduced_jacobian_matrix",
        lambda model=None: pd.DataFrame([[-0.5, 0.1], [0.0, -1.0]]),
    )
    eigenvalues = steady_module.check_steady_state(steady_module.STEADY_STATE_FOUND, "copasi")
    assert np.all(eigenvalues.real < 0)


def test_parameter_scan_accepts_display_names_and_scans_copies(monkeypatch):
    monkeypatch.setattr(
        scan_module.basico.model_info,
        "get_parameters",
        lambda model=None: pd.DataFrame(index=["(R1).k1"]),
    )
    monkeypatch.setattr(
        scan_module.basico.model_info,
        "get_species",
        lambda model=None: pd.DataFrame({"display_name": ["Mos{cell}"]}, index=["Mos"]),
    )
    updates = []

    class Model:
        copasi_model = "copasi"

        def __init__(self):
            self.simulation_results = None

        def model_copy(self):
            return Model()

        def update_parameters(self, params):
            updates.append(dict(params))

        def simulate(self, duration, interval):
            self.simulation_results = pd.DataFrame({"Time": [0.0, duration], "Mos": [1.0, 2.0]})

    arg_data = SimpleNamespace(
        parameter_scan_data=SimpleNamespace(
            species_parameter_name="Mos{cell}",
            species_parameter_values=[1.0, 2.0],
            species_names=["Mos"],
        )
    )
    original = Model()
    results = scan_module.run_parameter_scan(original, arg_data, {}, 10.0, 2)
    # Display name resolved to the COPASI species name update_parameters() expects
    assert {"Mos": 1.0} in updates and {"Mos": 2.0} in updates
    assert list(results["Mos"].columns) == ["Time", "Mos{cell}_1.0", "Mos{cell}_2.0"]
    assert original.simulation_results is None


def test_parameter_scan_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(
        scan_module.basico.model_info, "get_parameters", lambda model=None: pd.DataFrame(index=[])
    )
    monkeypatch.setattr(
        scan_module.basico.model_info,
        "get_species",
        lambda model=None: pd.DataFrame({"display_name": ["Mos"]}, index=["Mos"]),
    )
    arg_data = SimpleNamespace(
        parameter_scan_data=SimpleNamespace(
            species_parameter_name="INVALID_PARAM_XYZ",
            species_parameter_values=[1.0],
            species_names=["Mos"],
        )
    )
    with pytest.raises(ValueError, match="INVALID_PARAM_XYZ"):
        scan_module.run_parameter_scan(
            SimpleNamespace(copasi_model="copasi"), arg_data, {}, 10.0, 2
        )
