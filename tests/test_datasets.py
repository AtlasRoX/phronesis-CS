"""
Offline tests for dataset/model uploads (feature 8): the per-user store, the structural validators, the
API, GEO verification (httpx.MockTransport over captured live responses), orchestrator routing by dataset
id, the verifier and evidence block, and the Vivarix units fix.

Inputs are real data only: tests/fixtures/test_atlas.h5ad from the repository, BIOMD0000000010 as downloaded from
BioModels, and NCBI E-utilities responses captured live (tests/fixtures/datasets/). The hostile files
(external links, DTDs, comp references) are structural probes, not biomedical data.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from pathlib import Path
from typing import Any, List

import h5py
import httpx
import pytest

from phronesis.agents.prompts import build_evidence_block
from phronesis.agents.query_context import QueryContext
from phronesis.datasets import geo
from phronesis.datasets.attach import resolve_attachments
from phronesis.datasets.models import DatasetProvenanceIn
from phronesis.datasets.paths import is_allowed_typed_path
from phronesis.datasets.provenance import postprocess_lumicell_result, postprocess_vivarix_result
from phronesis.datasets.store import DatasetNotFound, DatasetStoreError, get_dataset_store, reset_dataset_store
from phronesis.extensions import hooks, load_extensions
from phronesis.extensions import uploads as uploads_ext
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import ToolResult
from tests.fakes import called, make_offline_orchestrator

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "datasets"
ATLAS = REPO / "tests" / "fixtures" / "test_atlas.h5ad"
SBML = FIXTURES / "BIOMD0000000010_url.xml"
ID_RE = re.compile(r"^ds_[0-9a-f]{12}$")

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4" level="2" version="4"><model id="m">&lol3;</model></sbml>"""
HTML_ROOT = b"<html><body><script>alert(1)</script></body></html>"
COMP_EXTERNAL = b"""<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version1/core" level="3" version="1"
      xmlns:comp="http://www.sbml.org/sbml/level3/version1/comp/version1" comp:required="true">
  <model id="outer"/>
  <comp:listOfExternalModelDefinitions>
    <comp:externalModelDefinition comp:id="ext" comp:source="file:///C:/Windows/win.ini"/>
  </comp:listOfExternalModelDefinitions>
</sbml>"""


def _prov(**kw: Any) -> DatasetProvenanceIn:
    return DatasetProvenanceIn(**{"source": "in_house", "attests_no_direct_identifiers": True, **kw})


def _chunks(data: bytes, size: int = 4096) -> List[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def _atlas_csv(transpose: bool = False) -> bytes:
    import anndata as ad
    import numpy as np
    import pandas as pd

    adata = ad.read_h5ad(ATLAS)
    df = pd.DataFrame(np.asarray(adata.X), index=adata.obs_names, columns=adata.var_names)
    return (df.T if transpose else df).to_csv().encode("utf-8")


@pytest.fixture
def upload_root(monkeypatch, tmp_path):
    root = tmp_path / "uploads"
    monkeypatch.setenv("PHRONESIS_UPLOAD_ROOT", str(root))
    for name in ("PHRONESIS_UPLOAD_MAX_MB_H5AD", "PHRONESIS_UPLOAD_MAX_MB_CSV", "PHRONESIS_UPLOAD_MAX_MB_SBML",
                 "PHRONESIS_UPLOAD_MAX_TOTAL_MB", "PHRONESIS_UPLOAD_MAX_DATASETS", "PHRONESIS_DATASET_ROOTS"):
        monkeypatch.delenv(name, raising=False)
    reset_dataset_store()
    geo.clear_cache()
    yield root
    reset_dataset_store()
    geo.clear_cache()


@pytest.fixture
def store(upload_root):
    return get_dataset_store()


@pytest.fixture
def upload_hooks():
    """UPLOADS hooks registered regardless of what earlier tests did to the global registries."""
    load_extensions()  # load every feature outside the isolation so their global registrations persist
    with hooks.isolated_hooks():
        uploads_ext.register_hooks()
        yield


def _geo_handler(calls: List[httpx.Request]):
    esearch_ok = json.loads((FIXTURES / "geo_esearch_GSE132465.json").read_text(encoding="utf-8"))
    esearch_nf = json.loads((FIXTURES / "geo_esearch_GSE999999999.json").read_text(encoding="utf-8"))
    esummary = json.loads((FIXTURES / "geo_esummary_200132465.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("esearch.fcgi"):
            term = request.url.params.get("term", "")
            return httpx.Response(200, json=esearch_ok if term.startswith("GSE132465[ACCN]") else esearch_nf)
        if request.url.path.endswith("esummary.fcgi") and request.url.params.get("id") == "200132465":
            return httpx.Response(200, json=esummary)
        return httpx.Response(404)

    return handler


@pytest.fixture
def geo_mock(monkeypatch):
    calls: List[httpx.Request] = []
    monkeypatch.setattr(geo, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(_geo_handler(calls))))
    return calls


def _no_part_files(root: Path) -> bool:
    return not list(root.rglob("*.part"))


# =============================================================================
# 1-5. h5ad validation and the store
# =============================================================================

@pytest.mark.asyncio
async def test_valid_h5ad_is_accepted(store, upload_root):
    data = ATLAS.read_bytes()
    rec = await store.create_from_stream("alice", _chunks(data), "test_atlas.h5ad", _prov())
    assert ID_RE.match(rec.id)
    assert rec.sha256 == hashlib.sha256(data).hexdigest()
    assert rec.size_bytes == len(data)
    assert (rec.validation.n_obs, rec.validation.n_vars) == (40, 10)
    assert rec.validation.obs_columns == ["donor_id"]
    ds_dir = store.user_dir("alice") / rec.id
    assert (ds_dir / "manifest.json").exists() and (ds_dir / "original.h5ad").exists()
    public = json.dumps(rec.public_provenance()) + json.dumps(rec.to_api())
    assert str(upload_root) not in public and "original.h5ad" not in public
    assert _no_part_files(upload_root)


@pytest.mark.asyncio
async def test_truncated_h5ad_is_rejected(store, upload_root):
    data = ATLAS.read_bytes()
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [data[: len(data) // 2]], "half.h5ad", _prov())
    assert (err.value.status_code, err.value.reason_code) == (422, "hdf5_unreadable")
    assert store.list("alice") == [] and _no_part_files(upload_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["external", "soft"])
async def test_h5ad_with_links_is_rejected(store, tmp_path, link):
    crafted = tmp_path / "crafted.h5ad"
    shutil.copy(ATLAS, crafted)
    with h5py.File(crafted, "a") as f:
        f["uns"]["leak"] = h5py.ExternalLink("other.h5ad", "/X") if link == "external" else h5py.SoftLink("/X")
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [crafted.read_bytes()], "crafted.h5ad", _prov())
    assert (err.value.status_code, err.value.reason_code) == (422, "hdf5_external_link")


@pytest.mark.asyncio
async def test_non_anndata_hdf5_is_rejected(store, tmp_path):
    plain = tmp_path / "plain.h5"
    with h5py.File(ATLAS, "r") as src, h5py.File(plain, "w") as dst:
        dst.create_dataset("matrix", data=src["X"][()])
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [plain.read_bytes()], "plain.h5ad", _prov())
    assert err.value.reason_code == "not_anndata"
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [SBML.read_bytes()], "model.h5ad", _prov())
    assert err.value.reason_code == "not_hdf5"


@pytest.mark.asyncio
async def test_size_cap_while_streaming_leaves_no_part_file(store, upload_root, monkeypatch):
    monkeypatch.setenv("PHRONESIS_UPLOAD_MAX_MB_H5AD", "0.01")  # ~10 KB; the atlas is ~21 KB
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", _chunks(ATLAS.read_bytes()), "test_atlas.h5ad", _prov())
    assert (err.value.status_code, err.value.reason_code) == (413, "too_large")
    assert _no_part_files(upload_root)
    assert not [p for p in store.user_dir("alice").iterdir()]


@pytest.mark.asyncio
async def test_startup_sweep_removes_orphaned_uploads(store, upload_root):
    import os
    import time

    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "keep.h5ad", _prov())
    udir = store.user_dir("alice")
    orphan = udir / "ds_0123456789ab"
    orphan.mkdir()
    (orphan / "upload.part").write_bytes(ATLAS.read_bytes()[:100])
    stray = udir / rec.id / "upload.part"
    stray.write_bytes(b"x")
    old = time.time() - 7200
    for p in (orphan / "upload.part", orphan, stray, udir / rec.id):
        os.utime(p, (old, old))
    reset_dataset_store()
    fresh = get_dataset_store()
    assert not orphan.exists() and not stray.exists()
    assert [r.id for r in fresh.list("alice")] == [rec.id]


@pytest.mark.asyncio
@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows refuses to move a directory holding an open file")
async def test_delete_while_file_is_open_is_a_conflict(store):
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "busy.h5ad", _prov())
    analysis_path, _ = store.file_paths("alice", rec)
    with open(analysis_path, "rb"):
        with pytest.raises(DatasetStoreError) as err:
            store.delete("alice", rec.id)
    assert (err.value.status_code, err.value.reason_code) == (409, "in_use")
    assert store.get("alice", rec.id).id == rec.id
    store.delete("alice", rec.id)
    assert store.list("alice") == []


@pytest.mark.asyncio
async def test_total_quota_and_dataset_count(store, monkeypatch):
    monkeypatch.setenv("PHRONESIS_UPLOAD_MAX_DATASETS", "1")
    await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov())
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [ATLAS.read_bytes()], "b.h5ad", _prov())
    assert (err.value.status_code, err.value.reason_code) == (507, "quota_exceeded")
    monkeypatch.setenv("PHRONESIS_UPLOAD_MAX_DATASETS", "50")
    monkeypatch.setenv("PHRONESIS_UPLOAD_MAX_TOTAL_MB", "0.03")
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", _chunks(ATLAS.read_bytes()), "c.h5ad", _prov())
    assert err.value.status_code == 507


# =============================================================================
# 6. CSV/TSV conversion and a real Lumicell run
# =============================================================================

@pytest.mark.asyncio
async def test_csv_converts_to_h5ad_and_lumicell_runs(store):
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    rec = await store.create_from_stream("alice", [_atlas_csv()], "atlas.csv", _prov())
    assert rec.kind.value == "table" and rec.validation.format == "csv->anndata"
    assert (rec.validation.n_obs, rec.validation.n_vars) == (40, 10)
    analysis_path, model_path = store.file_paths("alice", rec)
    assert model_path is None and analysis_path.endswith("data.h5ad")
    assert rec.derived_sha256 == hashlib.sha256(Path(analysis_path).read_bytes()).hexdigest()

    res = LumicellAnalysisEngine().execute_single_cell_pipeline(dataset_path=analysis_path, dataset_name=rec.display_name)
    assert res.success, res.error
    assert res.data["n_cells_initial"] == 40 and res.data["dataset_name"] == "atlas.csv"

    tsv = _atlas_csv(transpose=True).decode().replace(",", "\t").encode()
    rec2 = await store.create_from_stream("alice", [tsv], "genes_by_cells.tsv", _prov(table_orientation="genes_by_cells"))
    assert (rec2.validation.n_obs, rec2.validation.n_vars) == (40, 10)


@pytest.mark.asyncio
async def test_csv_with_non_numeric_column_is_rejected(store):
    import pandas as pd

    df = pd.read_csv(io.BytesIO(_atlas_csv()), index_col=0)
    df["donor"] = "donor_1"
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [df.to_csv().encode()], "mixed.csv", _prov())
    assert (err.value.status_code, err.value.reason_code) == (422, "non_numeric_values")


# =============================================================================
# 7. SBML
# =============================================================================

@pytest.mark.asyncio
async def test_sbml_valid_model(store):
    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels"))
    v = rec.validation
    assert rec.kind.value == "sbml" and v.format == "sbml-l2v4"
    assert v.sbml_model_id == "BIOMD0000000010"
    assert v.n_species == 8 and v.n_reaction_parameters == 22
    assert v.units["time_unit"] == "s" and v.units["quantity_unit"] == "nmol"
    assert rec.dims() == "8 species"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,reason",
    [
        (BILLION_LAUGHS, "sbml_dtd_forbidden"),
        (HTML_ROOT, "not_sbml"),
        (COMP_EXTERNAL, "sbml_external_reference"),
        (b"this is not xml <<<", "xml_unparseable"),
    ],
)
async def test_hostile_sbml_is_rejected_before_copasi(store, monkeypatch, payload, reason):
    import basico

    def must_not_load(*_a, **_k):
        raise AssertionError("COPASI must not see a rejected document")

    monkeypatch.setattr(basico, "load_model_from_string", must_not_load)
    with pytest.raises(DatasetStoreError) as err:
        await store.create_from_stream("alice", [payload], "model.sbml", _prov())
    assert (err.value.status_code, err.value.reason_code) == (422, reason)


# =============================================================================
# 8. Isolation, hashed user dirs and hostile names
# =============================================================================

@pytest.mark.asyncio
async def test_per_user_isolation_and_safe_names(store, upload_root):
    rec = await store.create_from_stream("alice:lab-1", [ATLAS.read_bytes()], "../../evil.h5ad", _prov())
    udir = store.user_dir("alice:lab-1")
    assert re.fullmatch(r"[0-9a-f]{16}", udir.name)
    assert all(":" not in str(p.relative_to(upload_root)) for p in upload_root.rglob("*"))
    assert rec.display_name == "evil.h5ad"

    found, errors = store.resolve_for_user("bob", [rec.id])
    assert found == [] and errors == [f"Attached dataset {rec.id} not found"]
    _, unknown = store.resolve_for_user("bob", ["ds_0123456789ab"])
    assert unknown == ["Attached dataset ds_0123456789ab not found"]
    with pytest.raises(DatasetNotFound):
        store.get("bob", rec.id)
    with pytest.raises(DatasetNotFound):
        store.get("alice:lab-1", "../" + rec.id)

    for name, expected in [("C:\\x\\y.h5ad", "y.h5ad"), ("a\x00b.h5ad", "ab.h5ad"), ("<script>.h5ad", "_script_.h5ad")]:
        r = await store.create_from_stream("alice:lab-1", [ATLAS.read_bytes()], name, _prov())
        assert r.display_name == expected
    for path in upload_root.rglob("*"):
        if path.is_file():
            assert path.parent.parent == udir and ID_RE.match(path.parent.name)


@pytest.mark.asyncio
async def test_resolve_attachments_builds_refs_and_typed_mentions(store):
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "atlas.h5ad", _prov())
    res = await resolve_attachments("alice", [], f"Cluster {rec.id} please")
    assert [a.id for a in res.attachments] == [rec.id] and res.errors == []
    ref = res.attachments[0]
    assert ref.kind == "h5ad" and ref.analysis_path and ref.dims == "40 cells × 10 genes"
    assert "path" not in json.dumps(ref.to_public()).lower()
    foreign = await resolve_attachments("bob", [rec.id], "")
    assert foreign.attachments == [] and foreign.errors == [f"Attached dataset {rec.id} not found"]


@pytest.mark.asyncio
async def test_resolve_attachments_without_store_creates_nothing(upload_root):
    res = await resolve_attachments("alice", ["ds_0123456789ab"], "")
    assert res.errors == ["Attached dataset ds_0123456789ab not found"]
    assert not upload_root.exists()


# =============================================================================
# 9-12. API
# =============================================================================

@pytest.fixture
def client(monkeypatch, upload_root):
    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    monkeypatch.delenv("PHRONESIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PHRONESIS_JWT_SECRET", raising=False)
    monkeypatch.setenv("PHRONESIS_API_TOKENS", "alice-token:alice,bob-token:bob")
    return TestClient(backend_main.app)


ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
PROV_JSON = json.dumps({"source": "in_house", "attests_no_direct_identifiers": True})


def _post(client, headers, name="test_atlas.h5ad", data=None, provenance=PROV_JSON):
    files = {"file": (name, data if data is not None else ATLAS.read_bytes(), "application/octet-stream")}
    return client.post("/api/datasets", headers=headers, files=files, data={"provenance": provenance})


def test_api_rejects_unauthenticated_before_parsing(client, monkeypatch):
    from starlette.requests import Request

    def never(*_a, **_k):
        raise AssertionError("the body must not be parsed before authentication")

    monkeypatch.setattr(Request, "form", never)
    assert _post(client, {}).status_code == 401
    assert _post(client, {"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/datasets").status_code == 401


def test_api_length_size_type_and_provenance_errors(client, monkeypatch):
    def body():
        yield b"--x\r\n"

    r = client.post("/api/datasets", headers={**ALICE, "Content-Type": "multipart/form-data; boundary=x"}, content=body())
    assert r.status_code == 411 and r.json()["reason_code"] == "length_required"

    r = _post(client, ALICE, name="tool.exe", data=b"MZ\x90\x00")
    assert r.status_code == 415 and r.json()["reason_code"] == "unsupported_type"

    r = _post(client, ALICE, provenance="{not json")
    assert r.status_code == 422 and r.json()["reason_code"] == "invalid_provenance"
    r = _post(client, ALICE, provenance=json.dumps({"source": "in_house", "attests_no_direct_identifiers": False}))
    assert r.status_code == 422 and "attest" in r.json()["detail"]
    r = _post(client, ALICE, provenance=json.dumps({"source": "geo", "geo_accession": "GSM1", "attests_no_direct_identifiers": True}))
    assert r.status_code == 422

    r = _post(client, ALICE, data=ATLAS.read_bytes()[:5000])
    assert r.status_code == 422 and r.json()["reason_code"] == "hdf5_unreadable"

    for name in ("PHRONESIS_UPLOAD_MAX_MB_H5AD", "PHRONESIS_UPLOAD_MAX_MB_CSV", "PHRONESIS_UPLOAD_MAX_MB_SBML"):
        monkeypatch.setenv(name, "0.001")
    r = _post(client, ALICE, data=ATLAS.read_bytes() * 5)  # ~100 KB > 1 KB cap + 64 KB multipart allowance
    assert r.status_code == 413 and r.json()["reason_code"] == "too_large"


def test_api_upload_list_and_cross_user_404(client):
    r = _post(client, ALICE)
    assert r.status_code == 201, r.text
    body = r.json()
    ds_id = body["id"]
    assert ID_RE.match(ds_id) and body["dims"] == "40 cells × 10 genes" and body["declared_by"] == "uploader"
    assert "path" not in json.dumps(body).lower()

    assert [d["id"] for d in client.get("/api/datasets", headers=ALICE).json()["datasets"]] == [ds_id]
    assert client.get("/api/datasets", headers=BOB).json() == {"datasets": [], "count": 0}
    assert client.get(f"/api/datasets/{ds_id}", headers=BOB).status_code == 404
    assert client.delete(f"/api/datasets/{ds_id}", headers=BOB).status_code == 404
    assert client.get("/api/datasets/not-an-id", headers=ALICE).status_code == 404
    assert client.get(f"/api/datasets/{ds_id}", headers=ALICE).status_code == 200

    assert client.delete(f"/api/datasets/{ds_id}", headers=ALICE).status_code == 200
    assert client.get(f"/api/datasets/{ds_id}", headers=ALICE).status_code == 404


def test_api_patch_provenance_keeps_history(client):
    ds_id = _post(client, ALICE).json()["id"]
    r = client.patch(
        f"/api/datasets/{ds_id}/provenance",
        headers=ALICE,
        json={"source": "collaborator", "consent_status": "irb_approved", "irb_protocol_id": "IRB-2024/17", "attests_no_direct_identifiers": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provenance"]["consent_status"] == "irb_approved"
    assert len(body["provenance_history"]) == 1
    assert body["provenance_history"][0]["previous"]["provenance"]["source"] == "in_house"
    assert client.patch(f"/api/datasets/{ds_id}/provenance", headers=BOB, json={"source": "other", "attests_no_direct_identifiers": True}).status_code == 404
    bad = client.patch(f"/api/datasets/{ds_id}/provenance", headers=ALICE, json={"source": "other"})
    assert bad.status_code == 422


# =============================================================================
# 13-15. GEO verification
# =============================================================================

@pytest.mark.asyncio
async def test_geo_verified(geo_mock):
    out = await geo.lookup_geo("GSE132465", "human")
    assert out.status == "verified"
    assert out.pubmedids == ["32451460"] and out.taxon == "Homo sapiens" and out.n_samples == 33
    assert out.bioproject == "PRJNA548146" and out.samples[0] == "GSM3868448"
    assert out.organism_matches is True
    assert "gse[ETYP]" in geo_mock[0].url.params["term"]
    n_calls = len(geo_mock)
    await geo.lookup_geo("GSE132465")
    assert len(geo_mock) == n_calls  # cached per accession


@pytest.mark.asyncio
async def test_geo_not_found(geo_mock):
    out = await geo.lookup_geo("GSE999999999")
    assert out.status == "not_found" and out.pubmedids == []
    assert (await geo.lookup_geo("not-an-accession")).status == "not_found"
    assert len(geo_mock) == 1  # the malformed accession never reaches NCBI


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["429", "timeout"])
async def test_geo_lookup_failure_never_blocks_upload(store, monkeypatch, failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(429, json={"error": "API rate limit exceeded"})

    monkeypatch.setattr(geo, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await geo.lookup_geo("GSE132465")).status == "lookup_failed"
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov(source="geo", geo_accession="GSE132465"))
    assert rec.geo_verification.status == "lookup_failed" and rec.public_provenance()["geo_verified"] is False


def test_api_geo_lookup_endpoint(client, geo_mock):
    r = client.post("/api/datasets/geo-lookup", headers=ALICE, json={"accession": "GSE132465"})
    assert r.status_code == 200 and r.json()["status"] == "verified" and r.json()["pubmedids"] == ["32451460"]
    assert client.post("/api/datasets/geo-lookup", headers=ALICE, json={"accession": "x; rm"}).status_code == 422
    assert client.post("/api/datasets/geo-lookup", json={"accession": "GSE132465"}).status_code == 401


# =============================================================================
# 16-20. Orchestrator routing
# =============================================================================

@pytest.mark.asyncio
async def test_orchestrator_routes_h5ad_attachment_to_lumicell(store, upload_root, upload_hooks, monkeypatch, tmp_path):
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "test_atlas.h5ad", _prov(consent_status="public_deidentified"))
    analysis_path, _ = store.file_paths("alice", rec)
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry("Cluster this dataset", thread_id="t-ds", enable_web_search=False, user_id="alice", dataset_ids=[rec.id])

    lum_calls = called(calls, "lumicell_analyze_single_cell")
    assert len(lum_calls) == 1
    assert lum_calls[0]["dataset_path"] == analysis_path and lum_calls[0]["dataset_name"] == "test_atlas.h5ad"
    art = res.execution_artifacts
    assert art["datasets"][0]["id"] == rec.id
    assert art["lumicell"]["dataset_path"] == f"upload://{rec.id}/test_atlas.h5ad"
    assert art["lumicell"]["dataset_provenance"]["declared_by"] == "uploader"
    step = next(t for t in res.thoughts if t["id"] == "attachments")
    assert step["status"] == "done" and "test_atlas.h5ad (40 cells × 10 genes)" in step["title"]
    lum_step = next(t for t in res.thoughts if t["id"] == "lumicell" and t["status"] == "done")
    assert "provenance declared by uploader" in lum_step["text"]
    serialized = json.dumps(res.to_dict(), default=str)
    assert str(upload_root) not in serialized and str(upload_root).replace("\\", "\\\\") not in serialized


@pytest.mark.asyncio
async def test_real_lumicell_run_on_upload_redacts_server_paths(store, upload_root, upload_hooks, monkeypatch, tmp_path):
    from phronesis.lumicell.engine import LumicellAnalysisEngine
    from phronesis.tools.registry import LumicellPipelineInput

    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "test_atlas.h5ad", _prov())
    real = {"lumicell_analyze_single_cell": (LumicellPipelineInput, lambda kw: LumicellAnalysisEngine().execute_single_cell_pipeline(**kw))}
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=real)
    res = await orch.execute_inquiry("Cluster this dataset", thread_id="t-real", enable_web_search=False, user_id="alice", dataset_ids=[rec.id])
    lum = res.execution_artifacts["lumicell"]
    assert lum["n_cells_initial"] == 40 and lum["dataset_name"] == "test_atlas.h5ad"
    assert lum["dataset_path"] == f"upload://{rec.id}/test_atlas.h5ad"
    assert lum["dataset_provenance"]["file_path"] == "test_atlas.h5ad" and lum["dataset_provenance"]["sha256"] == rec.sha256
    serialized = json.dumps(res.to_dict(), default=str)
    assert str(upload_root) not in serialized and str(upload_root).replace("\\", "\\\\") not in serialized


@pytest.mark.asyncio
async def test_orchestrator_routes_sbml_attachment_to_vivarix(store, upload_hooks, monkeypatch, tmp_path):
    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels", consent_status="not_human_subjects"))
    _, model_path = store.file_paths("alice", rec)
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)

    def no_search(*_a, **_k):
        raise AssertionError("an uploaded model must not trigger a BioModels search")

    monkeypatch.setattr(orch.vivarix, "search_models", no_search)
    res = await orch.execute_inquiry("Simulate this model for 30 seconds", thread_id="t-sbml", enable_web_search=False, user_id="alice", dataset_ids=[rec.id])
    viv = called(calls, "vivarix_simulate_time_course")
    assert len(viv) == 1
    assert viv[0]["biomodel_id"] == rec.id and viv[0]["sbml_path"] == model_path
    assert viv[0]["model_label"] == "kholodenko.xml" and viv[0]["model_provenance"]["sbml_model_id"] == "BIOMD0000000010"
    assert res.execution_artifacts["vivarix"]["provenance"]["source"] == "user_upload"
    viv_step = next(t for t in res.thoughts if t["id"] == "vivarix" and t["status"] == "done")
    assert viv_step["title"].startswith("Simulated uploaded model")


@pytest.mark.asyncio
async def test_papers_only_suppresses_uploaded_model(store, monkeypatch, tmp_path):
    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels"))
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    await orch.execute_inquiry("Find ERK2 papers only.", thread_id="t-po", enable_web_search=False, user_id="alice", dataset_ids=[rec.id])
    assert not called(calls, "vivarix_simulate_time_course")


@pytest.mark.asyncio
async def test_foreign_attachment_is_an_error_step(store, upload_root, monkeypatch, tmp_path):
    rec = await store.create_from_stream("bob", [ATLAS.read_bytes()], "bob.h5ad", _prov())
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry("Cluster this dataset", thread_id="t-foreign", enable_web_search=False, user_id="alice", dataset_ids=[rec.id])
    step = next(t for t in res.thoughts if t["id"] == "attachments")
    assert step["status"] == "error" and step["title"] == f"Attached dataset {rec.id} not found"
    assert not called(calls, "lumicell_analyze_single_cell")
    assert str(upload_root) not in json.dumps(calls, default=str)


@pytest.mark.asyncio
async def test_typed_path_outside_roots_is_ignored(store, upload_root, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere" / "atlas.h5ad"
    outside.parent.mkdir()
    shutil.copy(ATLAS, outside)
    assert not is_allowed_typed_path(str(outside))
    assert is_allowed_typed_path("test_atlas.h5ad")
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov())
    analysis_path, _ = store.file_paths("alice", rec)
    monkeypatch.setenv("PHRONESIS_DATASET_ROOTS", str(upload_root.parent))
    assert not is_allowed_typed_path(analysis_path)  # uploads are reachable only through their id

    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.delenv("PHRONESIS_DATASET_ROOTS")
    await orch.execute_inquiry(f"Analyze the single-cell file {outside}", thread_id="t-typed", enable_web_search=False, user_id="alice")
    assert not called(calls, "lumicell_analyze_single_cell")


# =============================================================================
# 21-22. Verifier and evidence block
# =============================================================================

async def _geo_verified_context(store, monkeypatch) -> tuple:
    calls: List[httpx.Request] = []
    monkeypatch.setattr(geo, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(_geo_handler(calls))))
    rec = await store.create_from_stream(
        "alice", [ATLAS.read_bytes()], "crc_atlas.h5ad", _prov(source="geo", geo_accession="GSE132465", organism="Homo sapiens")
    )
    assert rec.geo_verification.status == "verified"
    resolution = await resolve_attachments("alice", [rec.id], "")
    ctx = QueryContext(raw_query="Cluster this dataset", clean_query="Cluster this dataset", attachments=resolution.attachments)
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    res = LumicellAnalysisEngine().execute_single_cell_pipeline(
        dataset_path=resolution.attachments[0].analysis_path, dataset_name=rec.display_name
    )
    assert res.success, res.error
    return rec, ctx, postprocess_lumicell_result(res, ctx)


@pytest.mark.asyncio
async def test_verifier_geo_and_dataset_identifiers(store, monkeypatch, upload_hooks):
    rec, ctx, lum = await _geo_verified_context(store, monkeypatch)
    assert (lum.canonical_ids.get("declared_source_pmids") == "32451460" or lum.canonical_ids.get("pmids") == "32451460") and lum.canonical_ids["geo_accessions"] == "GSE132465"
    verifier = ClinicalVerifier()
    good = f"The uploader reports that dataset {rec.id} comes from GEO series GSE132465 (PMID 32451460)."
    report = verifier.verify_response("Cluster this dataset", good, {"lumicell": lum})
    assert not [d for d in report.defects if d.category == "IDENTIFIER"], report.defects

    bare = ToolResult(success=True, tool_name="lumicell_analyze_single_cell", data={"n_clusters": lum.data["n_clusters"]})
    report = verifier.verify_response("Cluster this dataset", "This dataset comes from GEO series GSE132465.", {"lumicell": bare})
    assert any(d.category == "IDENTIFIER" and d.severity.value == "CRITICAL" and "GSE132465" in d.description for d in report.defects)
    report = verifier.verify_response("Cluster this dataset", "Results come from dataset ds_0123456789ab.", {"lumicell": lum})
    assert any(d.category == "IDENTIFIER" and "ds_0123456789ab" in d.description for d in report.defects)

    irb = "The uploaded dataset was IRB-approved for this analysis."
    report = verifier.verify_response("Cluster this dataset", irb, {"lumicell": lum}, query_context=ctx)
    assert any(d.category == "PROVENANCE" and d.severity.value == "MAJOR" for d in report.defects)


@pytest.mark.asyncio
async def test_evidence_block_carries_declared_provenance_without_paths(store, upload_root, monkeypatch, upload_hooks):
    rec, ctx, lum = await _geo_verified_context(store, monkeypatch)
    block = build_evidence_block({"lumicell": lum}, "ranking", query_context=ctx)
    tag = re.search(r"<dataset_provenance [^>]*/>", block).group(0)
    assert f'id="{rec.id}"' in tag and 'declared_by="uploader"' in tag and 'geo_verified="true"' in tag
    assert 'geo_accession="GSE132465"' in tag and f'sha256_prefix="{rec.sha256[:12]}"' in tag
    assert str(upload_root) not in block and "original.h5ad" not in block
    assert not re.search(r"[A-Za-z]:\\\\|/tmp/|\\\\Users\\\\", block)
    assert lum.data["dataset_provenance"]["file_path"] == "crc_atlas.h5ad"


# =============================================================================
# 23. Vivarix: uploaded SBML loading and the units fix
# =============================================================================

@pytest.mark.asyncio
async def test_vivarix_uploaded_sbml_and_units(store, monkeypatch):
    from phronesis.agentic.sandbox import bound_scope
    from phronesis.vivarix import engine as engine_module

    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels"))
    _, model_path = store.file_paths("alice", rec)
    eng = engine_module.VivarixSimulationEngine()
    kwargs = dict(biomodel_id=rec.id, duration=20.0, intervals=10, sbml_path=model_path, model_label=rec.display_name,
                  model_provenance=rec.public_provenance())
    with bound_scope("alice", [rec.id]):
        res = eng.simulate_time_course(**kwargs)
        assert res.success, res.error
        # get_model_units() reports 'time_unit'/'quantity_unit'; the engine used to read 'time'/'quantity' and fall back to nM.
        assert res.data["units"]["time"] == "s" and res.data["units"]["quantity"] == "nmol"
        assert res.data["model_name"] == "kholodenko.xml" and res.data["provenance"]["source"] == "user_upload"
        assert res.data["model_id"] == rec.id and "Erk2-PP" in res.data["species_list"]

        real_units = engine_module.basico.model_info.get_model_units
        monkeypatch.setattr(
            engine_module.basico.model_info,
            "get_model_units",
            lambda model=None: {**real_units(model=model), "time_unit": "min", "quantity_unit": "mmol"},
        )
        sim_data = eng.simulate_time_course(**kwargs).data
        assert sim_data["units"]["time"] == "min" and sim_data["units"]["quantity"] == "mmol"

        outside = eng.simulate_time_course(biomodel_id=rec.id, sbml_path=str(SBML))
        assert not outside.success and "upload store" in outside.error and str(SBML) not in outside.error

    ctx = QueryContext(raw_query="q", clean_query="q", attachments=(await resolve_attachments("alice", [rec.id], "")).attachments)
    post = postprocess_vivarix_result(res, ctx)
    assert post.data["model_source"] == "user_upload" and post.data["provenance"]["declared_source"] == "biomodels"
    load_extensions()
    with hooks.isolated_hooks():
        uploads_ext.register_hooks()
        block = build_evidence_block({"vivarix": post}, "r")
    assert 'model_source="user_upload"' in block and 'sbml_model_id="BIOMD0000000010"' in block


# =============================================================================
# Review regressions
# =============================================================================

@pytest.mark.asyncio
async def test_parameter_scan_never_reports_a_missing_species(store):
    from phronesis.agentic.sandbox import bound_scope
    from phronesis.vivarix.engine import VivarixSimulationEngine

    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels"))
    _, model_path = store.file_paths("alice", rec)
    common = dict(biomodel_id=rec.id, parameter_name="Erk2", start_val=100.0, end_val=200.0, num_steps=2, sbml_path=model_path)
    with bound_scope("alice", [rec.id]):
        missing = VivarixSimulationEngine().run_parameter_scan(target_species="NOT_A_SPECIES", **common)
        assert not missing.success and "not found" in missing.error  # previously every point reported a 0.0 response
        ok = VivarixSimulationEngine().run_parameter_scan(target_species="Erk2-PP", **common)
        assert ok.success and len(ok.data["scan_results"]) == 2


def _attachment_ctx(ref, query: str = "q") -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query, attachments=[ref])


@pytest.mark.asyncio
async def test_declared_provenance_keeps_file_values_and_redacts_paths(store, upload_root):
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov(organism="Mus musculus"))
    ref = (await resolve_attachments("alice", [rec.id], "")).attachments[0]
    ctx = _attachment_ctx(ref)
    res = ToolResult(
        success=True,
        tool_name="lumicell_analyze_single_cell",
        data={"dataset_path": ref.analysis_path, "dataset_provenance": {"file_path": ref.analysis_path, "organism": "Homo sapiens", "tissue": "colon"}},
    )
    prov = postprocess_lumicell_result(res, ctx).data["dataset_provenance"]
    assert prov["organism"] == "Mus musculus" and prov["file_organism"] == "Homo sapiens"  # declared vs read from uns
    assert prov["tissue"] == "colon" and prov["declared_by"] == "uploader"

    rec2 = await store.create_from_stream("alice", [ATLAS.read_bytes()], "b.h5ad", _prov())
    ref2 = (await resolve_attachments("alice", [rec2.id], "")).attachments[0]
    res2 = ToolResult(success=True, tool_name="lumicell_analyze_single_cell", data={"dataset_provenance": {"organism": "Homo sapiens"}})
    assert postprocess_lumicell_result(res2, _attachment_ctx(ref2)).data["dataset_provenance"]["organism"] == "Homo sapiens"

    folder = str(Path(ref.analysis_path).parent)
    failed = ToolResult(success=False, tool_name="lumicell_analyze_single_cell", error=f"Unable to open (dir = '{folder}', root {upload_root.resolve()})")
    err = postprocess_lumicell_result(failed, ctx).error
    assert folder not in err and str(upload_root.resolve()) not in err


@pytest.mark.asyncio
async def test_consent_check_ignores_negations_and_cited_studies(store, upload_hooks):
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov())
    ctx = QueryContext(raw_query="q", clean_query="q", attachments=(await resolve_attachments("alice", [rec.id], "")).attachments)

    class V:
        query_context = ctx
        hedge_pattern = None
        sentences = [
            "The uploaded dataset is not IRB-approved according to the declared consent status.",
            "In the cited cohort, patients gave informed consent (PMID 32451460).",
        ]

    assert uploads_ext.consent_claims_check(V()) == []
    V.sentences = ["The uploaded dataset was collected with informed consent."]
    assert [d["category"] for d in uploads_ext.consent_claims_check(V())] == ["PROVENANCE"]


@pytest.mark.asyncio
async def test_scope_note_names_attachments_that_were_not_analyzed(store):
    a = await store.create_from_stream("alice", [ATLAS.read_bytes()], "first.h5ad", _prov())
    b = await store.create_from_stream("alice", [ATLAS.read_bytes()], "second.h5ad", _prov())
    ctx = QueryContext(raw_query="q", clean_query="q", attachments=(await resolve_attachments("alice", [a.id, b.id], "")).attachments)
    notes = uploads_ext.upload_scope_notes(ctx, {})
    assert any("not analyzed" in n and b.id in n and a.id not in n for n in notes)


def test_geo_accession_is_normalized():
    assert _prov(geo_accession=" gse132465 ").geo_accession == "GSE132465"


# =============================================================================
# Final-review regressions: declared identifiers, NCBI key, parallel quota
# =============================================================================

def _geo_429(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={"error": "API rate limit exceeded"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,accession", [("not_found", "GSE999999999"), ("lookup_failed", "GSE132465")])
async def test_unverified_geo_accession_is_a_critical_identifier(store, monkeypatch, upload_hooks, status, accession):
    handler = _geo_handler([]) if status == "not_found" else _geo_429
    monkeypatch.setattr(geo, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rec = await store.create_from_stream("alice", [ATLAS.read_bytes()], "a.h5ad", _prov(source="geo", geo_accession=accession))
    assert rec.geo_verification.status == status
    ctx = _attachment_ctx((await resolve_attachments("alice", [rec.id], "")).attachments[0], "Cluster this dataset")
    lum = postprocess_lumicell_result(
        ToolResult(success=True, tool_name="lumicell_analyze_single_cell", data={"dataset_provenance": {}}), ctx
    )
    assert lum.data["dataset_provenance"]["geo_accession"] == accession  # still shown to the client as declared
    assert "geo_accessions" not in lum.canonical_ids and "pmids" not in lum.canonical_ids
    assert accession not in ctx.observed_identifiers()["geo"]
    assert accession not in json.dumps(hooks.apply_serialization_filters("lumicell", lum.data))

    from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine

    assert accession not in ClinicalMemoryEngine.collect_identifiers(lum.data, "lumicell")["geo"]
    tag = re.search(r"<dataset_provenance [^>]*/>", build_evidence_block({"lumicell": lum}, "r", query_context=ctx)).group(0)
    assert accession not in tag and f'geo_status="{status}"' in tag and 'geo_verified="false"' in tag

    answer = f"This dataset comes from GEO series {accession}."
    report = ClinicalVerifier().verify_response(
        "Cluster this dataset", answer, {"lumicell": lum}, context_identifiers=ctx.observed_identifiers(), query_context=ctx
    )
    assert any(d.category == "IDENTIFIER" and d.severity.value == "CRITICAL" and accession in d.description for d in report.defects)
    assert f"GEO:{accession}" not in report.verified_identifiers


@pytest.mark.asyncio
async def test_uploaded_sbml_model_id_is_not_a_biomodels_citation(store, upload_hooks):
    rec = await store.create_from_stream("alice", [SBML.read_bytes()], "kholodenko.xml", _prov(source="biomodels"))
    ctx = _attachment_ctx((await resolve_attachments("alice", [rec.id], "")).attachments[0])
    assert ctx.attachments[0].sbml_model_id == "BIOMD0000000010"
    assert ctx.observed_identifiers()["biomodels"] == set()
    viv = postprocess_vivarix_result(
        ToolResult(success=True, tool_name="vivarix_simulate_time_course", data={"model_id": rec.id, "provenance": {}}), ctx
    )
    assert viv.data["provenance"]["sbml_model_id"] == "BIOMD0000000010"
    assert "BIOMD0000000010" not in json.dumps(hooks.apply_serialization_filters("vivarix", viv.data))
    report = ClinicalVerifier().verify_response(
        "q", "The uploaded model is BioModels entry BIOMD0000000010.", {"vivarix": viv},
        context_identifiers=ctx.observed_identifiers(), query_context=ctx,
    )
    assert any(d.category == "IDENTIFIER" and d.claim == "BIOMD0000000010" for d in report.defects)


@pytest.mark.asyncio
async def test_geo_lookup_sends_ncbi_api_key(geo_mock, monkeypatch):
    geo.clear_cache()
    monkeypatch.setenv("NCBI_API_KEY", "placeholder-key")
    assert (await geo.lookup_geo("GSE132465")).status == "verified"
    assert geo_mock and all(r.url.params.get("api_key") == "placeholder-key" for r in geo_mock)
    geo.clear_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_env,limit", [("PHRONESIS_UPLOAD_MAX_DATASETS", "1"), ("PHRONESIS_UPLOAD_MAX_TOTAL_MB", "0.03")])
async def test_parallel_uploads_cannot_exceed_user_limits(store, monkeypatch, limit_env, limit):
    import asyncio

    monkeypatch.setenv(limit_env, limit)
    gate = asyncio.Event()

    async def held_open():
        await gate.wait()  # every upload has passed the early quota checks before any of them commits
        yield ATLAS.read_bytes()

    tasks = [asyncio.create_task(store.create_from_stream("alice", held_open(), f"{i}.h5ad", _prov())) for i in range(3)]
    for _ in range(10):
        await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 2 and all(isinstance(f, DatasetStoreError) and f.status_code == 507 for f in failures)
    assert len(store.list("alice")) == 1 and _no_part_files(store.root)
