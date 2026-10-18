"""
Structure intelligence (feature 12): AlphaFold DB and RCSB PDB clients, the structure_intel EvidenceSource and its
registry tools, and the NVIDIA NIM gating (no key, or no explicit request, means no docking call).

Offline: httpx.MockTransport serves responses trimmed from live calls (AlphaFold DB /api/prediction/P04637, the
RCSB Search API for P04637, RCSB Data API entries 1A1U, 1AIE, 1C26 and 4HHB, and the AF-P04637-F1 model file
header), captured 2026-10-16. The NIM 401 mirrors the unauthenticated live probe in the references.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.extensions import structure as structure_ext
from phronesis.sources import SourceRunState
from phronesis.structintel import clients
from phronesis.structintel.clients import StructureClient, plddt_band, summarize_alphafold, summarize_entry
from phronesis.structintel.source import (
    DOCK_TOOL,
    StructureIntelSource,
    has_structure_intent,
    smiles_in_text,
    wants_docking,
    wants_generation,
)
from phronesis.tools.registry import BiomedicalToolRegistry
from tests.fakes import make_offline_orchestrator

# Trimmed from GET https://alphafold.ebi.ac.uk/api/prediction/P04637 (first and second of 9 models).
AF_P04637 = [
    {
        "toolUsed": "AlphaFold Monomer v2.0 pipeline", "providerId": "GDM", "entityType": "protein", "isUniProt": True,
        "modelEntityId": "AF-P04637-F1", "modelCreatedDate": "2025-08-01T00:00:00Z", "globalMetricValue": 75.06,
        "fractionPlddtVeryLow": 0.298, "fractionPlddtLow": 0.104, "fractionPlddtConfident": 0.071,
        "fractionPlddtVeryHigh": 0.527, "latestVersion": 6, "allVersions": [1, 2, 3, 4, 5, 6], "sequenceStart": 1,
        "sequenceEnd": 393, "gene": "TP53", "uniprotAccession": "P04637", "uniprotId": "P53_HUMAN",
        "uniprotDescription": "Cellular tumor antigen p53", "taxId": 9606, "organismScientificName": "Homo sapiens",
        "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-P04637-F1-model_v6.cif",
        "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-P04637-F1-model_v6.pdb",
        "paeImageUrl": "https://alphafold.ebi.ac.uk/files/AF-P04637-F1-predicted_aligned_error_v6.png",
        "entryId": "AF-P04637-F1", "uniprotStart": 1, "uniprotEnd": 393,
    },
    {
        "toolUsed": "AlphaFold Monomer v2.0 pipeline", "providerId": "GDM", "entityType": "protein", "isUniProt": True,
        "modelEntityId": "AF-P04637-9-F1", "modelCreatedDate": "2025-08-01T00:00:00Z", "globalMetricValue": 82.94,
        "fractionPlddtVeryLow": 0.201, "fractionPlddtLow": 0.051, "fractionPlddtConfident": 0.033,
        "fractionPlddtVeryHigh": 0.715, "latestVersion": 6, "allVersions": [5, 6],
    },
]
# POST https://search.rcsb.org/rcsbsearch/v2/query (P04637, rows 3).
RCSB_SEARCH_P04637 = {
    "query_id": "3f22cc5c-5755-4ce9-9d1d-fe65b480c4e0", "result_type": "entry", "total_count": 323,
    "result_set": [{"identifier": "1A1U", "score": 1.0}, {"identifier": "1AIE", "score": 1.0}, {"identifier": "1C26", "score": 1.0}],
}
# GET https://data.rcsb.org/rest/v1/core/entry/{id}, trimmed to the fields the client reads.
RCSB_ENTRIES: Dict[str, Dict[str, Any]] = {
    "1A1U": {
        "rcsb_id": "1A1U",
        "struct": {"title": "SOLUTION STRUCTURE DETERMINATION OF A P53 MUTANT DIMERIZATION DOMAIN, NMR, MINIMIZED AVERAGE STRUCTURE"},
        "exptl": [{"method": "SOLUTION NMR"}],
        "rcsb_entry_info": {"experimental_method": "NMR", "polymer_entity_count": 1},
        "rcsb_accession_info": {"initial_release_date": "1998-04-08T00:00:00.000+00:00"},
    },
    "1AIE": {
        "rcsb_id": "1AIE",
        "struct": {"title": "P53 TETRAMERIZATION DOMAIN CRYSTAL STRUCTURE"},
        "exptl": [{"crystals_number": 1, "method": "X-RAY DIFFRACTION"}],
        "rcsb_entry_info": {"experimental_method": "X-ray", "polymer_entity_count": 1, "resolution_combined": [1.5]},
        "rcsb_accession_info": {"initial_release_date": "1997-06-16T00:00:00.000+00:00"},
    },
    "1C26": {
        "rcsb_id": "1C26",
        "struct": {"title": "CRYSTAL STRUCTURE OF P53 TETRAMERIZATION DOMAIN"},
        "exptl": [{"crystals_number": 1, "method": "X-RAY DIFFRACTION"}],
        "rcsb_entry_info": {"experimental_method": "X-ray", "polymer_entity_count": 1, "resolution_combined": [1.7]},
        "rcsb_accession_info": {"initial_release_date": "1999-07-27T00:00:00.000+00:00"},
    },
}
RCSB_4HHB = {
    "rcsb_id": "4HHB",
    "struct": {"title": "THE CRYSTAL STRUCTURE OF HUMAN DEOXYHAEMOGLOBIN AT 1.74 ANGSTROMS RESOLUTION"},
    "exptl": [{"method": "X-RAY DIFFRACTION"}],
    "rcsb_entry_info": {"experimental_method": "X-ray", "polymer_entity_count": 2, "resolution_combined": [1.74],
                        "nonpolymer_bound_components": ["HEM"]},
    "rcsb_accession_info": {"deposit_date": "1984-03-07T00:00:00.000+00:00", "initial_release_date": "1984-07-17T00:00:00.000+00:00"},
}
AF_P04637_PDB_HEAD = (
    "HEADER                                            01-AUG-25                     \n"
    "TITLE     ALPHAFOLD MONOMER V2.0 PREDICTION FOR CELLULAR TUMOR ANTIGEN P53      \n"
    "TITLE    2 (P04637)                                                             \n"
    "COMPND    MOL_ID: 1;                                                            \n"
)
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


class Upstream:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "alphafold.ebi.ac.uk" and path == "/api/prediction/P04637":
            return httpx.Response(200, json=AF_P04637)
        if host == "alphafold.ebi.ac.uk" and path.startswith("/api/prediction/"):
            return httpx.Response(404, json={"detail": "not found"})
        if host == "alphafold.ebi.ac.uk" and path == "/files/AF-P04637-F1-model_v6.pdb":
            return httpx.Response(200, text=AF_P04637_PDB_HEAD)
        if host == "search.rcsb.org":
            body = json.loads(request.content)
            accession = body["query"]["nodes"][0]["parameters"]["value"]
            return httpx.Response(200, json=RCSB_SEARCH_P04637) if accession == "P04637" else httpx.Response(204)
        if host == "data.rcsb.org":
            pid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=RCSB_ENTRIES[pid]) if pid in RCSB_ENTRIES else httpx.Response(404)
        if host == "health.api.nvidia.com":
            return httpx.Response(401, json={"detail": "authorization missing"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def to(self, host: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    transport = httpx.MockTransport(up)
    monkeypatch.setattr(clients, "_client_factory", lambda: StructureClient(transport=transport))
    monkeypatch.setenv("PHRONESIS_TOOL_CACHE", "off")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    return up


def _ctx(query: str, uniprot: str = "P04637", symbol: str = "TP53") -> QueryContext:
    gene = ResolvedEntity(type="gene", mention=symbol, label=symbol, ids={"hgnc_id": "HGNC:11998", "uniprot_id": uniprot},
                          source="HGNC", validated=True)
    return QueryContext(raw_query=query, clean_query=query, entities=[gene], primary_gene=gene, primary_target=symbol)


def _registry() -> BiomedicalToolRegistry:
    reg = BiomedicalToolRegistry()
    structure_ext.register_tools(reg)
    return reg


def _run_source(ctx: QueryContext, reg: BiomedicalToolRegistry):
    return asyncio.run(StructureIntelSource().run(ctx, reg, SourceRunState(reg, ctx)))


# =============================================================================
# Parsing and intent


def test_plddt_bands_follow_alphafold():
    assert plddt_band(95.0) == "very high"
    assert plddt_band(90.0) == "confident" and plddt_band(75.06) == "confident" and plddt_band(70.0) == "confident"
    assert plddt_band(69.9) == "low" and plddt_band(50.0) == "low"
    assert plddt_band(49.9) == "very low"
    assert plddt_band(None) is None


def test_alphafold_summary_picks_the_canonical_model():
    s = summarize_alphafold(AF_P04637, "P04637")
    assert s["model_id"] == "AF-P04637-F1" and s["version"] == 6
    assert s["mean_plddt"] == 75.06 and s["plddt_band"] == "confident"
    assert s["url"] == "https://alphafold.ebi.ac.uk/entry/P04637" and s["pdb_url"].endswith("model_v6.pdb")
    assert s["plddt_fractions"]["very_high"] == 0.527 and s["n_models"] == 2
    # Older API responses only carry entryId.
    legacy = summarize_alphafold([{"entryId": "AF-P04637-F1", "globalMetricValue": 75.06}], "P04637")
    assert legacy["model_id"] == "AF-P04637-F1"


def test_rcsb_entry_summary_4hhb():
    s = summarize_entry(RCSB_4HHB, "4hhb")
    assert s["pdb_id"] == "4HHB" and s["method"] == "X-RAY DIFFRACTION"
    assert s["resolution"] == 1.74 and s["ligands"] == ["HEM"]
    assert s["release_date"] == "1984-07-17" and s["url"] == "https://www.rcsb.org/structure/4HHB"
    nmr = summarize_entry(RCSB_ENTRIES["1A1U"], "1A1U")
    assert nmr["resolution"] is None and nmr["method"] == "SOLUTION NMR" and nmr["ligands"] == []


def test_structure_intent_and_explicit_requests():
    assert has_structure_intent("Show the AlphaFold structure of TP53")
    assert has_structure_intent("Which ligands bind the KRAS binding pocket?")
    assert has_structure_intent("What is the pLDDT of the model?")
    assert not has_structure_intent("KRAS shows a 2-fold change in expression")
    assert not has_structure_intent("log2 fold change of EGFR in tumours")
    assert not has_structure_intent("What diseases is KRAS associated with?")
    assert wants_docking("Dock aspirin into PTGS2") and not wants_docking("Show the PDB structures")
    assert wants_generation("Generate analogs of this compound") and wants_generation("generate 10 new molecules")
    assert not wants_generation("Show the AlphaFold model")
    assert smiles_in_text(f"Dock {ASPIRIN} into the TP53 structure") == ASPIRIN
    assert smiles_in_text("Show the structure of P04637 and KRAS G12D") is None


# =============================================================================
# Clients


def test_clients_parse_live_shapes_and_build_the_search_query(upstream):
    client = clients.get_client()
    af = asyncio.run(client.alphafold_prediction("P04637"))
    assert af["model_id"] == "AF-P04637-F1"
    assert asyncio.run(client.alphafold_prediction("Q99999")) is None  # 404
    found = asyncio.run(client.pdb_search("P04637", rows=3))
    assert found == {"total_count": 323, "pdb_ids": ["1A1U", "1AIE", "1C26"]}
    body = json.loads(upstream.to("search.rcsb.org")[0].content)
    term = body["query"]["nodes"][0]["parameters"]
    assert term["attribute"] == "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession"
    assert term["operator"] == "exact_match" and term["value"] == "P04637"
    assert body["return_type"] == "entry" and body["request_options"]["paginate"]["rows"] == 3
    assert asyncio.run(client.pdb_search("Q99999")) == {"total_count": 0, "pdb_ids": []}  # 204 No Content
    assert "Phronesis" in upstream.requests[0].headers["user-agent"]


# =============================================================================
# EvidenceSource


def test_source_routing():
    src = StructureIntelSource()
    assert src.should_run(_ctx("Show the AlphaFold structure and PDB entries of TP53")).run
    assert not src.should_run(_ctx("What diseases is TP53 associated with?")).run
    no_target = QueryContext(raw_query="Show me a protein structure", clean_query="Show me a protein structure")
    assert not src.should_run(no_target).run
    typed = QueryContext(raw_query="AlphaFold model for P04637", clean_query="AlphaFold model for P04637")
    assert src.should_run(typed).run
    papers_only = _ctx("Show the crystal structures of TP53")
    papers_only.source_allowlist = {"lithoscribe"}
    assert not src.should_run(papers_only).run


def test_source_run_evidence_ids_citations_and_no_docking_without_key(upstream):
    reg = _registry()
    assert reg.get_entry(DOCK_TOOL) is None  # NIM tools only exist with NVIDIA_API_KEY
    ctx = _ctx(f"Dock {ASPIRIN} into the TP53 structure and report the AlphaFold pLDDT")
    res = _run_source(ctx, reg)
    assert res.success and res.canonical_ids == {"uniprot_id": "P04637"}
    assert not upstream.to("health.api.nvidia.com")
    data = res.data
    assert data["docking"] is None and any("not configured" in n for n in data["notes"])
    assert [e["pdb_id"] for e in data["pdb_entries"]] == ["1A1U", "1AIE", "1C26"] and data["pdb_total_count"] == 323

    src = StructureIntelSource()
    xml = src.evidence_xml(res)
    assert '<alphafold_model model_id="AF-P04637-F1"' in xml and 'mean_plddt="75.06"' in xml and 'plddt_band="confident"' in xml
    assert 'pdb_id="1AIE"' in xml and 'resolution_angstrom="1.5"' in xml and "computational prediction" in xml
    ids = src.allowed_identifiers(res)
    assert ids["pdb"] == {"1A1U", "1AIE", "1C26"} and ids["alphafold"] == {"AF-P04637-F1"}
    cites = src.citations(res)
    assert {c.type for c in cites} == {"url"} and cites[0].url == "https://alphafold.ebi.ac.uk/entry/P04637"
    key, art = src.artifact(res)
    assert key == "structure_intel" and art["uniprot"] == "P04637" and art["gene"] == "TP53"
    assert art["alphafold"]["model_id"] == "AF-P04637-F1" and art["alphafold"]["plddt_band"] == "confident"
    assert art["pdb_entries"][1] == {
        "pdb_id": "1AIE", "title": "P53 TETRAMERIZATION DOMAIN CRYSTAL STRUCTURE", "method": "X-RAY DIFFRACTION",
        "resolution": 1.5, "ligands": [], "url": "https://www.rcsb.org/structure/1AIE", "release_date": "1997-06-16",
    }
    assert art["docking"] is None and art["generated"] is None
    json.dumps(art)  # JSON-safe
    notes = src.scope_notes(ctx, res)
    assert any("predictions, not experimental" in n for n in notes)
    assert any("No docking was run" in n for n in notes)


def test_evidence_is_escaped(upstream):
    res = _run_source(_ctx("Show the TP53 crystal structure"), _registry())
    res.data["pdb_entries"][0]["title"] = '<script>alert("x")</script> & co'
    xml = StructureIntelSource().evidence_xml(res)
    assert "<script>" not in xml and "&lt;script&gt;" in xml and "&amp; co" in xml


def test_docking_runs_only_with_key_and_explicit_request(upstream, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-not-a-real-key")
    reg = _registry()
    assert reg.get_entry(DOCK_TOOL) is not None
    # Structure question without a docking request: no NIM call even with a key.
    res = _run_source(_ctx(f"Show the AlphaFold structure of TP53 for {ASPIRIN}"), reg)
    assert res.success and not upstream.to("health.api.nvidia.com")
    # Explicit docking request: one DiffDock call with the documented fields; the 401 is reported, not invented.
    res = _run_source(_ctx(f"Dock {ASPIRIN} into the TP53 structure"), reg)
    nim = upstream.to("health.api.nvidia.com")
    assert len(nim) == 1 and nim[0].url.path == "/v1/biology/mit/diffdock"
    assert nim[0].headers["authorization"] == "Bearer nvapi-test-not-a-real-key"
    body = json.loads(nim[0].content)
    assert set(body) == {"protein", "ligand", "ligand_file_type", "num_poses"}
    assert body["ligand"] == ASPIRIN and body["ligand_file_type"] == "txt" and body["protein"].startswith("HEADER")
    assert res.success and res.data["docking"]["status"] == "error" and "401" in res.data["docking"]["error"]


def test_unresolvable_target_is_skipped_with_a_scope_note(upstream):
    ctx = QueryContext(raw_query="Show the KRAS crystal structure", clean_query="Show the KRAS crystal structure",
                       primary_target="KRAS")
    src = StructureIntelSource()
    assert src.should_run(ctx).run  # a target is named; the accession is resolved at run time
    res = _run_source(ctx, _registry())
    assert not res.success and res.error_type.value == "NOT_FOUND"
    assert any("No structure evidence was retrieved" in n for n in src.scope_notes(ctx, res))
    # The only request is the UniProt REST fallback for the symbol (the offline upstream answers it with no entry);
    # no AlphaFold or PDB call is made without an accession.
    assert [r.url.host for r in upstream.requests] == ["rest.uniprot.org"]


def test_orchestrator_adds_structure_artifact_and_evidence(upstream, monkeypatch, tmp_path):
    orch, _calls = make_offline_orchestrator(monkeypatch, tmp_path)
    structure_ext.register_tools(orch_module.get_tool_registry())
    res = asyncio.run(
        orch.execute_inquiry("Show the AlphaFold structure and PDB entries for UniProt P04637", thread_id="t-st1",
                             enable_web_search=False)
    )
    art = res.execution_artifacts["structure_intel"]
    assert art["uniprot"] == "P04637" and art["alphafold"]["mean_plddt"] == 75.06
    assert [e["pdb_id"] for e in art["pdb_entries"]] == ["1A1U", "1AIE", "1C26"]
    assert upstream.to("alphafold.ebi.ac.uk") and not upstream.to("health.api.nvidia.com")


def test_binding_prediction_wording_routes_to_structure_and_docking():
    from phronesis.structintel.source import has_structure_intent, wants_docking

    q = ("Design a pipeline to identify FDA-approved drugs that could be repurposed for KRAS-mutant pancreatic cancer, "
         "incorporating clinical-trial status, in-silico binding predictions, and real-world EHR safety data.")
    assert has_structure_intent(q) and wants_docking(q)
    assert has_structure_intent("run virtual screening against EGFR") and wants_docking("run virtual screening against EGFR")
    assert not has_structure_intent("What diseases is KRAS associated with?")
