"""
EntityResolver against httpx.MockTransport replaying trimmed real responses from tests/fixtures/entity_resolution
(OpenTargets mapIds, HGNC fetch, NCBI dbSNP esummary, NCBI Variation, ChEMBL). No network.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from phronesis.agents import query_planner as qp
from phronesis.utils.biocontext.entity_resolver import EntityResolver
from phronesis.utils.http_limits import TTLCache

FIX = Path(__file__).parent / "fixtures" / "entity_resolution"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _ot_mappings() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for name in ("mapids_noise.json", "ot_mapids_sample.json", "mapids_noise_extra.json"):
        for m in _load(name)["data"]["mapIds"]["mappings"]:
            if m["hits"]:
                out[m["term"]] = m["hits"]
    out.update(_load("opentargets_mapids_recorded.json")["mappings"])
    return out


class Upstream:
    """Routes requests to recorded responses; unrecorded requests are listed in .unmatched and fail."""

    def __init__(self, fail: Optional[Dict[str, int]] = None, slow: Optional[Dict[str, float]] = None) -> None:
        self.ot = _ot_mappings()
        self.hgnc = _load("hgnc_fetch_recorded.json")
        self.snp = _load("eutils_snp_recorded.json")
        self.variation = _load("ncbi_variation_recorded.json")
        self.chembl = _load("chembl_synonym_recorded.json")
        self.fail = fail or {}
        self.slow = slow or {}
        self.calls: List[str] = []
        self.unmatched: List[str] = []

    @staticmethod
    def _source(url: httpx.URL) -> str:
        if "opentargets" in url.host:
            return "opentargets"
        if "genenames" in url.host:
            return "hgnc"
        if "eutils" in url.host:
            return "dbsnp"
        if "/variation/" in url.path:
            return "ncbi_variation"
        if "chembl" in url.path:
            return "chembl"
        return "unknown"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        source = self._source(request.url)
        self.calls.append(source)
        if source in self.slow:
            await asyncio.sleep(self.slow[source])
        if source in self.fail:
            return httpx.Response(self.fail[source], json={"error": "unavailable"})
        if source == "opentargets":
            terms = json.loads(request.content)["variables"]["t"]
            mappings = [{"term": t, "hits": self.ot.get(t, [])} for t in terms]
            return httpx.Response(200, json={"data": {"mapIds": {"total": len(terms), "mappings": mappings}}})
        if source == "hgnc":
            _, _, field, term = request.url.path.split("/", 3)
            key = f"{field}/{urllib.parse.unquote(term)}"
            if key in self.hgnc:
                return httpx.Response(200, json=self.hgnc[key])
        if source == "dbsnp":
            return httpx.Response(200, json=self.snp)
        if source == "ncbi_variation":
            hgvs = urllib.parse.unquote(request.url.path.split("/hgvs/", 1)[1].rsplit("/contextuals", 1)[0])
            if hgvs in self.variation:
                rec = self.variation[hgvs]
                return httpx.Response(rec["status"], json=rec["body"])
        if source == "chembl":
            code = request.url.params.get("molecule_synonyms__molecule_synonym__iexact")
            if code in self.chembl:
                return httpx.Response(200, json=self.chembl[code])
        self.unmatched.append(str(request.url))
        return httpx.Response(404, json={"error": "no fixture"})


def _resolver(up: Upstream, **kw: Any) -> EntityResolver:
    return EntityResolver(transport=httpx.MockTransport(up), cache=TTLCache(maxsize=256, ttl_s=3600), **kw)


async def _resolve_question(res: EntityResolver, query: str) -> Dict[str, Any]:
    c = qp.extract_candidates(query)
    return await res.resolve(query, c.terms, c.gene_tokens, c.drug_codes, c.rsids, c.transcripts_hgvs)


def _by_mention(out: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e["mention"]: e for e in out["entities"]}


SHP2_Q = "Is SHP2 inhibition with TNO155 synergistic with sotorasib in KRAS G12C NSCLC?"


@pytest.mark.asyncio
async def test_shp2_question_resolves_genes_drugs_and_disease():
    up = Upstream()
    out = await _resolve_question(_resolver(up), SHP2_Q)
    ents = _by_mention(out)
    assert out["source_status"] == {"opentargets": "ok", "hgnc": "ok"}
    shp2 = ents["SHP2"]
    assert (shp2["label"], shp2["matched_on"], shp2["source"], shp2["validated"]) == ("PTPN11", "alias_symbol", "HGNC", True)
    assert shp2["ids"] == {"hgnc_id": "HGNC:9644", "ensembl_id": "ENSG00000179295", "uniprot_id": "Q06124"}
    assert "SHP2" in shp2["aliases"] and "NS1" in shp2["aliases"]
    assert ents["KRAS"]["ids"]["hgnc_id"] == "HGNC:6407" and ents["KRAS"]["matched_on"] == "symbol"
    assert ents["TNO155"]["label"] == "BATOPROTAFIB" and ents["TNO155"]["ids"] == {"chembl_id": "CHEMBL4650521"}
    assert ents["sotorasib"]["ids"] == {"chembl_id": "CHEMBL4535757"}
    assert ents["NSCLC"]["ids"] == {"ontology_id": "MONDO_0005233"} and ents["NSCLC"]["type"] == "disease"
    assert SHP2_Q[slice(*shp2["span"])] == "SHP2"
    # HGNC symbol and alias were fetched in parallel; prev_symbol was not needed. ChEMBL is never called here.
    assert "chembl" not in up.calls and not up.unmatched
    assert set(out["timings_ms"]) >= {"opentargets", "hgnc", "total"}


@pytest.mark.asyncio
async def test_mapids_noise_is_rejected():
    up = Upstream()
    query = "Is pembrolizumab or Keytruda with T-DXd useful in AML and lung adenocarcinoma of 2 patients? We met to Find data on lung."
    terms = ["Is", "pembrolizumab", "Keytruda", "T-DXd", "in", "of", "to", "2", "AML", "lung", "lung adenocarcinoma", "adenocarcinoma", "Find", "MET"]
    out = await _resolver(up).resolve(query, terms=terms)
    got = {(e["type"], e["mention"], e["label"]) for e in out["entities"]}
    # Keytruda resolves to the same PEMBROLIZUMAB record, so only the first mention is kept.
    assert got == {
        ("drug", "pembrolizumab", "PEMBROLIZUMAB"),
        ("drug", "T-DXd", "TRASTUZUMAB DERUXTECAN"),
        ("disease", "AML", "acute myeloid leukemia"),
        ("disease", "lung adenocarcinoma", "lung adenocarcinoma"),
    }
    pembro = _by_mention(out)["pembrolizumab"]
    assert pembro["ids"] == {"chembl_id": "CHEMBL3137343"}  # the exact name, not NIVOLUMAB or TORIPALIMAB
    assert _by_mention(out)["lung adenocarcinoma"]["ids"] == {"ontology_id": "MONDO_0005061"}
    trade = await _resolver(up).resolve("Is the response to Keytruda durable?", terms=["Keytruda"])
    assert [(e["label"], e["matched_on"]) for e in trade["entities"]] == [("PEMBROLIZUMAB", "synonym")]
    labels = {e["label"] for e in out["entities"]}
    assert not labels & {"CD44", "SPI1", "TDO2", "IRON SUCROSE", "DCSTAMP", "METHADONE", "AMLODIPINE", "NIVOLUMAB", "lung neoplasm", "2"}


@pytest.mark.asyncio
async def test_gene_family_is_ambiguous_and_upper_case_met_is_a_gene():
    up = Upstream()
    out = await _resolver(up).resolve("PI3K and MET amplification", terms=["PI3K", "MET"])
    ents = _by_mention(out)
    assert ents["PI3K"]["ambiguous_with"] == ["PIK3CD", "PIK3CG", "PIK3CA", "PIK3CB"] and not ents["PI3K"]["validated"]
    assert ents["MET"]["label"] == "MET" and ents["MET"]["ids"] == {"ensembl_id": "ENSG00000105976"}
    assert all(e["type"] == "gene" for e in out["entities"])


@pytest.mark.asyncio
async def test_hgnc_prev_symbol_fallback_and_ambiguous_alias():
    up = Upstream()
    out = await _resolver(up).resolve("Role of MLL and CAP in leukemia", gene_tokens=["MLL", "CAP"])
    ents = _by_mention(out)
    assert ents["MLL"]["label"] == "KMT2A" and ents["MLL"]["matched_on"] == "prev_symbol" and ents["MLL"]["validated"]
    assert ents["CAP"]["ambiguous_with"][:3] == ["HACD1", "CAP1", "SERPINB6"] and not ents["CAP"]["validated"]
    hgnc_paths = [c for c in up.calls if c == "hgnc"]
    # MLL: symbol + alias + prev; CAP: symbol + alias (alias had docs, so prev_symbol is skipped).
    assert len(hgnc_paths) == 5 and not up.unmatched


@pytest.mark.asyncio
async def test_dbsnp_error_document_is_not_validated():
    up = Upstream()
    q = "What is the clinical relevance of rs113488022 and rs99999999999 in melanoma treated with dabrafenib?"
    out = await _resolve_question(_resolver(up), q)
    ents = _by_mention(out)
    good, bad = ents["rs113488022"], ents["rs99999999999"]
    assert (good["validated"], good["gene"], good["ids"], good["source"]) == (True, "BRAF", {"rsid": "rs113488022"}, "dbSNP")
    assert bad["validated"] is False and bad["ids"] == {} and bad["gene"] is None
    assert ents["dabrafenib"]["ids"] == {"chembl_id": "CHEMBL2028663"} and ents["melanoma"]["ids"] == {"ontology_id": "MONDO_0005105"}


@pytest.mark.asyncio
async def test_hgvs_transcript_maps_to_gene_and_ncbi_validity():
    up = Upstream()
    q = "Is NM_004985.5:c.35G>A or NM_004985.5:c.35C>A pathogenic in KRAS?"
    out = await _resolve_question(_resolver(up), q)
    ents = _by_mention(out)
    valid, invalid = ents["NM_004985.5:c.35G>A"], ents["NM_004985.5:c.35C>A"]
    assert (valid["gene"], valid["validated"], valid["matched_on"]) == ("KRAS", True, "mane_select")
    assert (invalid["gene"], invalid["validated"]) == ("KRAS", False)
    assert "NM" not in ents  # pieces of the HGVS string are never looked up as names
    assert not up.unmatched


@pytest.mark.asyncio
async def test_chembl_only_for_codes_opentargets_missed():
    up = Upstream()
    out = await _resolve_question(_resolver(up), "Does BI-3406 combined with AMG510 work in AML?")
    ents = _by_mention(out)
    assert ents["AMG510"]["label"] == "SOTORASIB" and ents["AMG510"]["source"] == "OpenTargets"
    assert ents["BI-3406"]["ids"] == {"chembl_id": "CHEMBL4519023"} and ents["BI-3406"]["source"] == "ChEMBL"
    assert ents["BI-3406"]["label"] == "BI-3406"  # pref_name is null in ChEMBL
    assert up.calls.count("chembl") == 1 and not up.unmatched

    up2 = Upstream()
    await _resolve_question(_resolver(up2), SHP2_Q)
    assert "chembl" not in up2.calls  # TNO155 is known to OpenTargets


@pytest.mark.asyncio
async def test_one_failing_source_gives_partial_results():
    up = Upstream(fail={"hgnc": 503})
    out = await _resolve_question(_resolver(up), SHP2_Q)
    assert out["source_status"] == {"opentargets": "ok", "hgnc": "error"}
    ents = _by_mention(out)
    # OpenTargets alone still names the genes (without HGNC ids), and the drugs and disease are kept.
    assert ents["SHP2"]["label"] == "PTPN11" and ents["SHP2"]["ids"] == {"ensembl_id": "ENSG00000179295"}
    assert ents["sotorasib"]["ids"]["chembl_id"] == "CHEMBL4535757" and ents["NSCLC"]["validated"]


@pytest.mark.asyncio
async def test_graphql_errors_count_as_failure():
    class Errors(Upstream):
        async def __call__(self, request):
            if self._source(request.url) == "opentargets":
                self.calls.append("opentargets")
                return httpx.Response(200, json={"errors": [{"message": "bad query"}], "data": None})
            return await super().__call__(request)

    up = Errors()
    out = await _resolve_question(_resolver(up), "Does BI-3406 combined with AMG510 work in AML?")
    assert out["source_status"]["opentargets"] == "error"
    # With OpenTargets down, every research code goes to ChEMBL; AMG510 has no recorded ChEMBL fixture.
    assert out["source_status"]["chembl"] == "ok"
    assert _by_mention(out)["BI-3406"]["ids"] == {"chembl_id": "CHEMBL4519023"}


@pytest.mark.asyncio
async def test_slow_source_times_out_within_deadline():
    up = Upstream(slow={"hgnc": 2.0})
    res = _resolver(up, timeouts={"hgnc": 0.2}, deadline_s=1.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await _resolve_question(res, SHP2_Q)
    assert loop.time() - t0 < 1.0
    assert out["source_status"]["hgnc"] == "timeout" and out["source_status"]["opentargets"] == "ok"
    assert _by_mention(out)["sotorasib"]["validated"]


@pytest.mark.asyncio
async def test_second_identical_call_is_served_from_cache():
    up = Upstream()
    res = _resolver(up)
    first = await _resolve_question(res, SHP2_Q)
    n = len(up.calls)
    second = await _resolve_question(res, SHP2_Q)
    assert len(up.calls) == n
    assert first["entities"] == second["entities"]


@pytest.mark.asyncio
async def test_registry_tool_validates_inputs_and_returns_resolver_data(monkeypatch):
    from phronesis.extensions import entities as ext
    from phronesis.tools.registry import BiomedicalToolRegistry
    from phronesis.utils.biocontext import entity_resolver as er

    up = Upstream()
    monkeypatch.setattr(er, "_DEFAULT_RESOLVER", _resolver(up))
    reg = BiomedicalToolRegistry()
    ext.register_tools(reg)
    entry = reg.get_entry("synapta_resolve_entities")
    assert entry.domain == "synapta" and entry.timeout_seconds == 12.0
    c = qp.extract_candidates(SHP2_Q)
    res = await reg.execute("synapta_resolve_entities", query=SHP2_Q, terms=c.terms, gene_tokens=c.gene_tokens, drug_codes=c.drug_codes)
    assert res.success and {e["label"] for e in res.data["entities"]} >= {"PTPN11", "KRAS", "SOTORASIB", "BATOPROTAFIB"}
    too_many = await reg.execute("synapta_resolve_entities", query="q", gene_tokens=[f"G{i}" for i in range(7)])
    assert too_many.success is False and too_many.error_type.value == "VALIDATION_ERROR"


def test_opentargets_ensembl_fills_in_only_for_the_same_symbol():
    up = Upstream()
    doc = dict(up.hgnc["alias_symbol/SHP2"]["response"]["docs"][0])
    doc.pop("ensembl_gene_id", None)
    q = "SHP2 inhibition"
    same = EntityResolver._gene_from_doc(q, "SHP2", {"doc": doc, "matched_on": "alias_symbol"}, up.ot["SHP2"][0])
    assert same.ids["ensembl_id"] == up.ot["SHP2"][0]["id"]
    other = EntityResolver._gene_from_doc(q, "SHP2", {"doc": doc, "matched_on": "alias_symbol"}, up.ot["KRAS"][0])
    assert "ensembl_id" not in other.ids
