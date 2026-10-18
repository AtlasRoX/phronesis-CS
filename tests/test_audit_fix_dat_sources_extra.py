"""Regression tests for the sources_extra audit fixes DAT-003, DAT-013, DAT-014 (bioRxiv) and DAT-010 (releases).

All upstreams are mocked with httpx.MockTransport; no network.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Dict

import httpx
import pytest

from phronesis.agents.query_context import QueryContext
from phronesis.sources_extra import all_sources, bindingdb, biorxiv, common, reactome

SOURCES = {s.key: s for s in all_sources()}

# (R)- and (S)-thalidomide, and the unspecified (racemic) structure.
THAL_R = "O=C1CC[C@@H](N2C(=O)c3ccccc3C2=O)C(=O)N1"
THAL_S = "O=C1CC[C@H](N2C(=O)c3ccccc3C2=O)C(=O)N1"
THAL_S_KEKULE = "C1CC(=O)NC(=O)[C@H]1N2C(=O)C3=CC=CC=C3C2=O"
THAL_ANY = "O=C1CCC(N2C(=O)c3ccccc3C2=O)C(=O)N1"
# (Z)-tamoxifen (aromatic, Kekule, and written from the other end) and (E)-tamoxifen.
TAM_Z = r"CC/C(=C(\c1ccccc1)/c1ccc(OCCN(C)C)cc1)/c1ccccc1"
TAM_Z_KEKULE = r"CC/C(=C(\C1=CC=CC=C1)/C2=CC=C(C=C2)OCCN(C)C)/C3=CC=CC=C3"
TAM_Z_REVERSED = r"CN(C)CCOc1ccc(cc1)/C(=C(/CC)\c1ccccc1)/c1ccccc1"
TAM_E = r"CC/C(=C(/C1=CC=CC=C1)\C2=CC=C(C=C2)OCCN(C)C)/C3=CC=CC=C3"
IBUPROFEN = "CC(C)Cc1ccc(cc1)C(C)C(=O)O"
IBUPROFEN_D3 = "[2H]C([2H])([2H])C(C(=O)O)c1ccc(CC(C)C)cc1"
IBUPROFEN_EXPLICIT_H = "[H]C([H])([H])C(C(=O)O)c1ccc(CC(C)C)cc1"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# DAT-003: structure matching
# -----------------------------------------------------------------------------

def test_constitution_key_distinguishes_saturation_and_isotopes():
    key = bindingdb.skeleton_key
    assert key("C1CCCCC1") != key("c1ccccc1")  # cyclohexane is not benzene
    assert key("c1ccccc1") == key("C1=CC=CC=C1")  # aromatic and Kekule forms agree
    assert key(IBUPROFEN) != key(IBUPROFEN_D3)  # a deuterated analogue is a different compound
    assert key(IBUPROFEN) == key(IBUPROFEN_EXPLICIT_H)  # explicit protium is the same compound
    assert key("CC#C") != key("CC=C")  # alkyne vs alkene
    assert key("C[N+](C)(C)C") != key("CC(C)C")  # charge counts
    assert key("[13CH4]") != key("C")


def test_stereo_relation_tetrahedral_known_answers():
    # L-alanine written three ways is one configuration; D-alanine is the other.
    l1, l2, l3, d = "N[C@@H](C)C(=O)O", "C[C@H](N)C(=O)O", "OC(=O)[C@@H](N)C", "N[C@H](C)C(=O)O"
    assert bindingdb.stereo_relation(l1, l2) == "identical"
    assert bindingdb.stereo_relation(l1, l3) == "identical"
    assert bindingdb.stereo_relation(l1, d) == "different"
    # R- and S-thalidomide share their constitution but not their configuration.
    assert bindingdb.skeleton_key(THAL_R) == bindingdb.skeleton_key(THAL_S) == bindingdb.skeleton_key(THAL_ANY)
    assert bindingdb.stereo_relation(THAL_S, THAL_R) == "different"
    assert bindingdb.stereo_relation(THAL_S, THAL_S_KEKULE) == "identical"
    assert bindingdb.stereo_relation(THAL_ANY, THAL_S) == "record_only"
    assert bindingdb.stereo_relation(THAL_S, THAL_ANY) == "drug_only"
    assert bindingdb.stereo_relation(THAL_ANY, THAL_ANY) == "unspecified"


def test_stereo_relation_double_bond_known_answers():
    assert bindingdb.stereo_relation("F/C=C/F", r"F\C=C\F") == "identical"  # both trans
    assert bindingdb.stereo_relation("F/C=C/F", r"C(\F)=C/F") == "identical"  # OpenSMILES equivalence
    assert bindingdb.stereo_relation("F/C=C/F", r"F/C=C\F") == "different"  # trans vs cis
    assert bindingdb.skeleton_key(TAM_Z) == bindingdb.skeleton_key(TAM_E)
    assert bindingdb.stereo_relation(TAM_Z, TAM_Z_KEKULE) == "identical"
    assert bindingdb.stereo_relation(TAM_Z_KEKULE, TAM_Z_REVERSED) == "identical"
    assert bindingdb.stereo_relation(TAM_Z, TAM_E) == "different"


def _row(mid: str, smiles: str, value: str, pmid: str = "") -> Dict[str, Any]:
    return {"query": "Protein cereblon", "monomerid": mid, "smile": smiles, "affinity_type": "Kd",
            "affinity": value, "pmid": pmid, "doi": f"10.1/{mid}"}


@pytest.fixture
def bdb(monkeypatch):
    state: Dict[str, Any] = {"rows": [], "pubchem": {}, "requests": []}

    def handle(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.host == "bindingdb.org":
            return httpx.Response(200, json={"getLindsByUniprotsResponse": {"affinities": state["rows"]}})
        if request.url.host == "pubchem.ncbi.nlm.nih.gov":
            name = request.url.path.split("/compound/name/")[1].split("/")[0].lower()
            props = state["pubchem"].get(name)
            if props is None:
                return httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})
            return httpx.Response(200, json={"PropertyTable": {"Properties": [props]}})
        return httpx.Response(404, json={})

    monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(handle))
    bindingdb._CACHE.clear()
    bindingdb._KEY_CACHE.clear()
    yield state
    bindingdb._CACHE.clear()
    bindingdb._KEY_CACHE.clear()


def test_bindingdb_does_not_attribute_the_other_enantiomer_or_an_isotopologue(bdb):
    bdb["rows"] = [
        _row("100", THAL_R, "5000", "11111111"),  # the other enantiomer: must not be labelled (S)-thalidomide
        _row("101", THAL_S_KEKULE, "250", "22222222"),  # the same enantiomer, written differently
        _row("102", "[2H]C1([2H])CC(N2C(=O)c3ccccc3C2=O)C(=O)NC1=O", "40"),  # deuterated analogue
    ]
    bdb["pubchem"] = {"s-thalidomide": {"CID": 92142, "SMILES": THAL_S, "ConnectivitySMILES": THAL_ANY}}
    res = run(bindingdb.bindingdb_affinities("CRBN", uniprot="Q96SW2", drugs=["S-thalidomide"]))
    assert res.success
    items = res.data["items"]
    matched = [i for i in items if i.get("matched_drug")]
    assert [i["monomer_id"] for i in matched] == ["101"]
    assert matched[0]["ligand"] == "BindingDB monomer 101"  # never relabelled as the drug
    assert matched[0]["stereo_relation"] == "identical" and "stereochemistry" in matched[0]["match_basis"]
    other = next(i for i in items if i["monomer_id"] == "100")
    assert "matched_drug" not in other and other["ligand"] == "BindingDB monomer 100"
    assert other["same_constitution_as"] == "S-thalidomide" and other["stereo_relation"] == "different"
    deut = next(i for i in items if i["monomer_id"] == "102")
    assert "matched_drug" not in deut and "same_constitution_as" not in deut
    status = res.data["drugs"][0]
    assert status["status"] == "matched" and status["n_records"] == 1 and status["n_stereoisomer_records_excluded"] == 1
    xml = SOURCES["bindingdb"].evidence_xml(res)
    assert 'matched_drug="S-thalidomide"' in xml and 'stereo_relation="different"' in xml
    assert "not attributed" in xml and "match_basis" in xml


def test_bindingdb_only_stereoisomer_records_give_a_scope_note(bdb):
    bdb["rows"] = [_row("100", THAL_R, "5000", "11111111")]
    bdb["pubchem"] = {"s-thalidomide": {"CID": 92142, "SMILES": THAL_S}}
    res = run(bindingdb.bindingdb_affinities("CRBN", uniprot="Q96SW2", drugs=["S-thalidomide"]))
    assert res.success and not [i for i in res.data["items"] if i.get("matched_drug")]
    assert res.data["drugs"][0]["status"] == "stereoisomer_only"
    ctx = QueryContext(raw_query="Kd of S-thalidomide for CRBN", clean_query="Kd of S-thalidomide for CRBN")
    notes = " ".join(SOURCES["bindingdb"].scope_notes(ctx, res))
    assert "different or uncomparable stereochemistry" in notes and "Do not state a BindingDB value" in notes


def test_bindingdb_racemic_drug_match_states_that_stereo_was_not_compared(bdb):
    bdb["rows"] = [_row("100", THAL_R, "5000", "11111111")]
    bdb["pubchem"] = {"thalidomide": {"CID": 5426, "SMILES": THAL_ANY}}
    res = run(bindingdb.bindingdb_affinities("CRBN", uniprot="Q96SW2", drugs=["thalidomide"]))
    row = res.data["items"][0]
    assert row["matched_drug"] == "thalidomide" and row["stereo_relation"] == "record_only"
    assert "single stereoisomer" in row["match_basis"] and "not compared" in row["match_basis"]


def test_bindingdb_e_and_z_isomers_are_not_conflated(bdb):
    bdb["rows"] = [_row("200", TAM_E, "12"), _row("201", TAM_Z_REVERSED, "3")]
    bdb["pubchem"] = {"tamoxifen": {"CID": 2733526, "SMILES": TAM_Z_KEKULE}}
    res = run(bindingdb.bindingdb_affinities("ESR1", uniprot="P03372", drugs=["tamoxifen"]))
    matched = [i["monomer_id"] for i in res.data["items"] if i.get("matched_drug")]
    assert matched == ["201"]


# -----------------------------------------------------------------------------
# DAT-013: affinity values are the upstream strings, never rounded or in e-notation
# -----------------------------------------------------------------------------

def test_parse_record_keeps_upstream_values_verbatim():
    for raw in ("31622.78", "1234567", "0.006"):
        rec = bindingdb.parse_record({"monomerid": "1", "smile": "C", "affinity_type": "Ki", "affinity": raw})
        assert rec["value"] == raw and rec["value_text"] == f"{raw} nM"
    rec = bindingdb.parse_record({"monomerid": "1", "smile": "C", "affinity_type": "IC50", "affinity": ">1.5e-05"})
    assert rec["value"] == "0.000015" and rec["value_text"] == ">0.000015 nM"  # exact, positional
    assert bindingdb.plain_decimal("0.100000") == "0.1"
    assert bindingdb._fmt(1000.0) == "1000"


def test_rendered_bindingdb_values_have_no_e_notation(bdb):
    bdb["rows"] = [_row("1", "CCO", "31622.78", "1"), _row("2", "CCN", "1234567", "2")]
    res = run(bindingdb.bindingdb_affinities("CRBN", uniprot="Q96SW2", cutoff_nm=100000))
    xml = SOURCES["bindingdb"].evidence_xml(res)
    assert 'value="31622.78"' in xml and 'value="1234567"' in xml
    assert "e+" not in xml and "31622.8\"" not in xml


# -----------------------------------------------------------------------------
# DAT-014: bioRxiv/medRxiv partial scans are disclosed
# -----------------------------------------------------------------------------

def test_biorxiv_partial_server_is_rendered_and_noted(monkeypatch):
    monkeypatch.setattr(biorxiv, "_today", lambda: date(2026, 10, 16))
    rec = {"doi": "10.1101/2026.10.01.000001", "title": "Cereblon modulators", "abstract": "cereblon binding",
           "date": "2026-10-01", "version": "1", "server": "bioRxiv", "category": "pharmacology"}

    def handle(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")  # /details/<server>/<start>/<end>/<cursor>/json
        server, cursor = parts[2], parts[5]
        if cursor == "0":
            return httpx.Response(200, json={"messages": [{"status": "ok", "total": 100}],
                                             "collection": [rec] if server == "biorxiv" else []})
        if server == "medrxiv":
            return httpx.Response(500, text="error")
        return httpx.Response(200, json={"collection": []})

    monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(handle))
    res = run(biorxiv.recent_preprints(["cereblon"]))
    assert res.success
    med = next(s for s in res.data["sources"] if s["name"] == "medrxiv")
    assert med["status"] == "partial" and med["pages_failed"] == 2 and med["pages_requested"] == 3
    src = SOURCES["preprints"]
    xml = src.evidence_xml(res)
    assert '<server name="medrxiv" status="partial"' in xml and '<server name="biorxiv" status="ok"' in xml
    ctx = QueryContext(raw_query="recent preprints on cereblon", clean_query="recent preprints on cereblon")
    notes = " ".join(src.scope_notes(ctx, res))
    assert "medRxiv returned only some pages" in notes and "may be missing" in notes


# -----------------------------------------------------------------------------
# DAT-010: database release recorded where the API exposes it
# -----------------------------------------------------------------------------

def _reactome_transport(version_response: httpx.Response) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ContentService/data/database/version":
            return version_response
        if request.url.path == "/ContentService/data/mapping/UniProt/Q8NBP7/pathways":
            return httpx.Response(200, json=[{"stId": "R-HSA-8964038", "displayName": "LDL clearance", "isInDisease": False}])
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def test_reactome_release_is_recorded_in_metadata_and_xml(monkeypatch):
    common.clear_release_cache()
    monkeypatch.setattr(common, "TRANSPORT", _reactome_transport(httpx.Response(200, text="97")))
    res = run(reactome.reactome_pathways(uniprot="Q8NBP7"))
    assert res.success and res.metadata["source_release"] == "Reactome release 97"
    assert res.data["reactome_release"] == "97"
    assert 'release="97"' in SOURCES["reactome"].evidence_xml(res)
    common.clear_release_cache()


def test_reactome_release_probe_failure_never_fails_the_tool(monkeypatch):
    common.clear_release_cache()
    monkeypatch.setattr(common, "TRANSPORT", _reactome_transport(httpx.Response(503, text="down")))
    res = run(reactome.reactome_pathways(uniprot="Q8NBP7"))
    assert res.success and res.data["items"][0]["stId"] == "R-HSA-8964038"
    assert "source_release" not in res.metadata and res.data["reactome_release"] is None
    assert "release=" not in SOURCES["reactome"].evidence_xml(res)
    common.clear_release_cache()


def test_pinned_string_release_is_in_metadata(monkeypatch):
    import json
    from pathlib import Path

    from phronesis.sources_extra import string_db

    fix = Path(__file__).parent / "fixtures" / "omics"

    def handle(request: httpx.Request) -> httpx.Response:
        name = "string_interaction_partners_pcsk9.json" if request.url.path.endswith("/interaction_partners")             else "string_network_pcsk9.json"
        return httpx.Response(200, json=json.loads((fix / name).read_text(encoding="utf-8")))

    monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(handle))
    res = run(string_db.string_partners("PCSK9"))
    assert res.success and res.metadata["source_release"] == "STRING 12.5"
