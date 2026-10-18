"""Tool retrieval (F18): recall@k on a labelled query set over the real default registry (ToolRet-style
evaluation, arXiv:2503.01763), plus the retrieve_tools contract other packages rely on."""

from __future__ import annotations

import socket

import pytest

from phronesis.agentic.tool_retrieval import (
    expand_query,
    rank_tools,
    recall_at_k,
    retrieve_tools,
    tokenize,
    tool_card,
    tool_document,
)
from tests.fakes import build_recording_registry

# (query, the tool a planner needs for it). Cases whose tool is not registered in this checkout are dropped,
# so tools other packages add later do not break the measurement.
LABELLED = [
    ("What diseases is KRAS associated with?", "synapta_opentargets_associations"),
    ("Find the Ensembl identifier for the gene BRAF", "synapta_opentargets_search"),
    ("Which KEGG pathways include EGFR?", "synapta_kegg_pathways"),
    ("Which signaling pathways is TP53 a member of?", "synapta_kegg_pathways"),
    ("Simulate the MAPK cascade model for 200 seconds", "vivarix_simulate_time_course"),
    ("Run a time course of BIOMD0000000010", "vivarix_simulate_time_course"),
    ("What are the steady-state concentrations of the model?", "vivarix_compute_steady_state"),
    ("How sensitive is ERK activation to the V1 parameter? scan it from 0.1 to 10", "vivarix_parameter_scan"),
    ("Cluster my single-cell RNA-seq dataset and find marker genes", "lumicell_analyze_single_cell"),
    ("Find recent papers on KRAS G12C inhibitor resistance", "lithoscribe_literature_synthesis"),
    ("What does the published literature say about SHP2 in pancreatic cancer?", "lithoscribe_literature_synthesis"),
    ("Are there recruiting clinical trials of sotorasib in lung cancer?", "lithoscribe_clinical_trials_search"),
    ("List phase 3 trials for osimertinib", "lithoscribe_clinical_trials_search"),
    ("Search the web for news about adagrasib approval", "websurfer_search"),
    ("Resolve the trade name Lumakras to its ChEMBL molecule", "chembl_resolve_drugs"),
    ("What is the mechanism of action and IC50 of trametinib against MEK1?", "chembl_drug_profile"),
    ("What are the side effects and boxed warnings of vemurafenib?", "openfda_drug_safety"),
    ("Adverse event reports for imatinib in FAERS", "openfda_drug_safety"),
    ("Which targets are most associated with idiopathic pulmonary fibrosis and are they druggable?", "opentargets_disease_targets"),
    ("Could metformin be repurposed for Alzheimer disease? Find mechanism paths in the knowledge graph", "primekg_investigate"),
    ("Neighbors of TNF in PrimeKG", "primekg_query"),
    ("Is BRCA1 c.68_69delAG pathogenic in ClinVar?", "synapta_variant_intelligence"),
    ("What is the clinical significance of the EGFR T790M mutation?", "synapta_variant_intelligence"),
    ("Normalize these gene and drug names to HGNC symbols and identifiers", "synapta_resolve_entities"),
]


@pytest.fixture(scope="module")
def default_registry():
    """The real default registry (builtin + extension tools), built with outbound connections blocked."""
    from phronesis.tools.registry import BiomedicalToolRegistry, register_default_biomedical_tools

    real_connect = socket.socket.connect

    def blocked(self, *args, **kwargs):
        raise OSError("network disabled in tool-retrieval tests")

    socket.socket.connect = blocked  # type: ignore[method-assign]
    try:
        reg = BiomedicalToolRegistry()
        register_default_biomedical_tools(reg)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"default registry could not be built offline: {exc}")
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
    return reg


def _cases(reg):
    names = {e.name for e in reg.list_tools()}
    return [(q, t) for q, t in LABELLED if t in names]


def test_recall_at_k_on_default_registry(default_registry):
    cases = _cases(default_registry)
    assert len(cases) >= 12  # the builtin tools alone cover more than half of the set
    assert recall_at_k(default_registry, cases, k=1) >= 0.85
    assert recall_at_k(default_registry, cases, k=3) >= 0.95
    assert recall_at_k(default_registry, cases, k=5) == 1.0


def test_every_registered_tool_is_retrievable_by_its_own_description(default_registry):
    tools = default_registry.list_tools()
    cases = [((e.description or e.name)[:200], e.name) for e in tools]
    assert recall_at_k(default_registry, cases, k=3) >= 0.9


def test_recording_registry_recall():
    reg, _ = build_recording_registry()
    cases = [(q, t) for q, t in LABELLED if reg.get_entry(t) is not None]
    assert recall_at_k(reg, cases, k=3) == 1.0


def test_retrieve_tools_contract():
    reg, _ = build_recording_registry()
    names = [e.name for e in retrieve_tools(reg, "simulate the model", k=2, always=["lithoscribe_literature_synthesis"])]
    assert names[0] == "lithoscribe_literature_synthesis" and len(names) == 2 and names[1].startswith("vivarix_")
    assert all(e.domain == "synapta" for e in retrieve_tools(reg, "pathways", k=5, domains=["synapta"]))
    assert "synapta_kegg_pathways" not in [e.name for e in retrieve_tools(reg, "kegg pathways", k=9, exclude=["synapta_kegg_pathways"])]
    assert len(retrieve_tools(reg, "anything", k=0)) == len(reg.list_tools())
    assert retrieve_tools(reg, "x", domains=["nope"]) == []
    assert retrieve_tools(reg, "always registered", k=3, always=["not_a_tool"])  # unknown always-names are ignored
    ranked = rank_tools(reg, "parameter scan sensitivity")
    assert ranked[0][0].name == "vivarix_parameter_scan" and ranked[0][1] >= ranked[-1][1]


def test_query_expansion_and_tokenizer():
    assert tokenize("What are the KRAS_G12C papers?") == ["kras", "g12c", "papers"]
    terms, weights = expand_query("side effects of the drug")
    assert weights["side"] == 1.0 and weights["safety"] == 0.5 and "chembl" in weights
    assert len(terms) == len(weights)


def test_tool_card_and_document_are_schema_accurate():
    reg, _ = build_recording_registry()
    card = tool_card(reg.get_entry("synapta_opentargets_associations"))
    assert card["args"]["ensembl_id"]["required"] is True
    assert card["args"]["size"] == {"type": "int", "required": False, "default": 10}
    doc = tool_document(reg.get_entry("vivarix_parameter_scan"))
    assert "parameter_name" in doc and "sensitivity" in doc
