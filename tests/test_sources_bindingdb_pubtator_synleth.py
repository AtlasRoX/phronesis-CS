"""
Offline tests for the BindingDB, PubTator3/LitVar2 and SynLethDB evidence sources (phronesis/sources_extra).

HTTP goes through an httpx.MockTransport (common.TRANSPORT). The payloads are trimmed copies of live responses
captured on 2026-10-16 (BindingDB getLigandsByUniprots for P00533, PubTator3 search/autocomplete for KRAS and EGFR,
LitVar2 autocomplete for EGFR T790M/C797S); the SynLethDB rows are the first lines of the SynLethDB 3.0
Human.SL.detailed.tsv on Zenodo plus synthetic rows for ranking.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.sources import get_sources
from phronesis.sources_extra import all_sources, bindingdb, common, pubtator, synlethdb, tool_entries
from phronesis.tools.registry import ErrorType

ERLOTINIB_BDB = "COCCOc1cc2c(cc1OCCOC)ncnc2Nc3cccc(c3)C#C"
ERLOTINIB_PUBCHEM = "COCCOC1=C(C=C2C(=C1)C(=NC=N2)NC3=CC=CC(=C3)C#C)OCCOC"
GEFITINIB_BDB = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1"

BDB_ROWS = [
    {"query": "Epidermal growth factor receptor", "monomerid": "3032", "smile": "COc1cc2ncnc(Nc3cccc(Br)c3)c2cc1OC",
     "affinity_type": "Ki", "affinity": "0.006", "pmid": "18077363", "doi": "10.1073/pnas.0708800104"},
    {"query": "Epidermal growth factor receptor", "monomerid": "5446", "smile": ERLOTINIB_BDB,
     "affinity_type": "Ki", "affinity": "0.100000", "pmid": "25383627", "doi": "10.1021/jm501578n"},
    {"query": "Epidermal growth factor receptor", "monomerid": "5446", "smile": ERLOTINIB_BDB,
     "affinity_type": "IC50", "affinity": "0.029", "pmid": "37197473", "doi": "10.7270/Q2X92GK8"},
    {"query": "Epidermal growth factor receptor", "monomerid": "50221516", "smile": "COc1cc2c(N[C@@H]3C[C@H]3c3ccccc3)c(cnc2cc1OCCCN1CCOCC1)C#N",
     "affinity_type": "Ki", "affinity": "8.4", "pmid": "17827009", "doi": "10.1016/j.bmcl.2007.07.071"},
    {"query": "Epidermal growth factor receptor", "monomerid": "999", "smile": "CCO", "affinity_type": "IC50",
     "affinity": ">0.530000", "pmid": "", "doi": "10.1/x"},
    {"query": "Epidermal growth factor receptor", "monomerid": "998", "smile": "CCN", "affinity_type": "IC50",
     "affinity": "0", "pmid": "", "doi": "10.1/y"},
    {"query": "Receptor tyrosine-protein kinase erbB-2", "monomerid": "77", "smile": "CCCl", "affinity_type": "IC50",
     "affinity": "0.001", "pmid": "11111111", "doi": "10.1/z"},
]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Upstream:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.overrides: Dict[str, Callable[[httpx.Request], Optional[httpx.Response]]] = {}
        self.bdb_rows: List[Dict[str, Any]] = list(BDB_ROWS)
        self.search_results: Dict[str, List[Dict[str, Any]]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.host + request.url.path
        for prefix, fn in self.overrides.items():
            if key.startswith(prefix):
                out = fn(request)
                if out is not None:
                    return out
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        if key == "rest.uniprot.org/uniprotkb/search":
            return httpx.Response(200, json={"results": [{"primaryAccession": "P00533"}] if "EGFR" in q["query"] else []})
        if key == "bindingdb.org/rest/getLigandsByUniprots":
            return httpx.Response(200, json={"getLindsByUniprotsResponse": {"affinities": self.bdb_rows if q["uniprot"] == "P00533" else []}})
        if request.url.host == "pubchem.ncbi.nlm.nih.gov":
            name = request.url.path.split("/compound/name/")[1].split("/")[0].lower()
            smiles = {"erlotinib": ERLOTINIB_PUBCHEM + ".Cl", "gefitinib": GEFITINIB_BDB}.get(name)
            if smiles is None:
                return httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})
            return httpx.Response(200, json={"PropertyTable": {"Properties": [{"CID": 176870, "ConnectivitySMILES": smiles}]}})
        if key == "www.ncbi.nlm.nih.gov/research/pubtator3-api/entity/autocomplete/":
            hits = {
                "pancreatic ductal carcinoma": [{"_id": "@DISEASE_Carcinoma_Pancreatic_Ductal", "biotype": "disease", "db_id": "D021441", "name": "Carcinoma Pancreatic Ductal"}],
                "non-small cell lung carcinoma": [{"_id": "@DISEASE_Carcinoma_Non_Small_Cell_Lung", "biotype": "disease", "db_id": "D002289", "name": "Carcinoma Non-Small-Cell Lung"}],
            }.get(q["query"], [])
            return httpx.Response(200, json=hits)
        if key == "www.ncbi.nlm.nih.gov/research/pubtator3-api/search/":
            results = self.search_results.get(q["text"], [])
            return httpx.Response(200, json={"results": results, "count": len(results) * 100, "page_size": 10})
        if key == "www.ncbi.nlm.nih.gov/research/litvar2-api/variant/autocomplete/":
            rows = {
                "EGFR T790M": [{"_id": "litvar@rs121434569##", "rsid": "rs121434569", "gene": ["EGFR", "EGFR-AS1"], "name": "p.T790M",
                                "pmids_count": 12196, "data_clinical_significance": ["likely-pathogenic", "drug-response"]}],
                "EGFR C797S": [{"_id": "litvar@#1956#p.C797S", "gene": ["EGFR"], "name": "p.C797S", "pmids_count": 3},
                               {"_id": "litvar@rs1057519861##", "rsid": "rs1057519861", "gene": ["EGFR", "EGFR-AS1"], "name": "p.C797S",
                                "pmids_count": 2029, "data_clinical_significance": ["not-provided"]}],
            }.get(q["query"], [])
            return httpx.Response(200, json=rows)
        if key.startswith("www.ncbi.nlm.nih.gov/research/litvar2-api/variant/get/"):
            return httpx.Response(200, json={"pmids": [26910730, 33660941]})
        if key == "www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson":
            docs = [{"id": p, "pmid": int(p), "journal": "J Test", "authors": ["A B", "C D"], "passages": [
                {"infons": {"type": "title", "year": "2016"}, "text": f"Title {p}"},
                {"infons": {"type": "abstract"}, "text": f"Abstract of {p} about C797S."}]} for p in q["pmids"].split(",")]
            return httpx.Response(200, json={"PubTator3": docs})
        return httpx.Response(404, json={})


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(up.handle))
    monkeypatch.setattr(pubtator, "MIN_INTERVAL_S", 0.0)
    monkeypatch.delenv("PHRONESIS_SYNLETHDB_DIR", raising=False)
    monkeypatch.delenv("PHRONESIS_DEPMAP_DIR", raising=False)
    bindingdb._CACHE.clear()
    yield up


def ent(kind: str, label: str, **kw: Any) -> ResolvedEntity:
    return ResolvedEntity(type=kind, mention=kw.pop("mention", label), label=label, validated=True, **kw)


def make_ctx(query: str, entities: Optional[List[ResolvedEntity]] = None, **kw: Any) -> QueryContext:
    entities = list(entities or [])
    primary = next((e for e in entities if e.type == "gene"), None)
    return QueryContext(raw_query=query, clean_query=query, entities=entities, primary_gene=primary,
                        primary_target=primary.label if primary else None, **kw)


SOURCES = {s.key: s for s in all_sources()}
EGFR = ent("gene", "EGFR")
KRAS = ent("gene", "KRAS")


# -----------------------------------------------------------------------------
# BindingDB
# -----------------------------------------------------------------------------

def test_skeleton_key_matches_structures_not_spelling():
    k = bindingdb.skeleton_key(ERLOTINIB_BDB)
    assert k and k == bindingdb.skeleton_key(ERLOTINIB_PUBCHEM) == bindingdb.skeleton_key(ERLOTINIB_PUBCHEM + ".Cl")
    assert k.startswith("29:")
    assert k != bindingdb.skeleton_key(GEFITINIB_BDB)
    assert bindingdb.skeleton_key("C1CC(") is None and bindingdb.skeleton_key("") is None
    assert bindingdb.skeleton_key("[NH4+].[Cl-]") is not None


def test_bindingdb_routing_and_plan():
    src = SOURCES["bindingdb"]
    erl = ent("drug", "ERLOTINIB", mention="erlotinib", ids={"chembl_id": "CHEMBL553"})
    t790m = ent("variant", "EGFR T790M", gene="EGFR", protein_change="T790M")
    ctx = make_ctx("What are the binding affinities of erlotinib for EGFR T790M and C797S?", [EGFR, erl, t790m])
    assert src.should_run(ctx).run
    plan = src.plan(ctx)
    assert plan["gene"] == "EGFR" and plan["drugs"] == ["erlotinib"] and plan["variants"] == ["T790M", "C797S"]
    assert "uniprot" not in plan  # resolved through UniProt REST at run time
    for q in ("Ki of gefitinib on EGFR", "How potent is osimertinib against EGFR?", "IC50 values for EGFR inhibitors"):
        assert src.should_run(make_ctx(q, [EGFR])).run, q
    for q in ("What does EGFR do in lung cancer?", "kinase inhibitors of EGFR"):
        assert not src.should_run(make_ctx(q, [EGFR])).run, q
    assert not src.should_run(make_ctx("binding affinity of erlotinib", [erl])).run  # no protein target
    papers = make_ctx("IC50 of erlotinib for EGFR, papers only", [EGFR], source_allowlist={"lithoscribe"},
                      source_restriction_reason="papers only requested")
    assert src.should_run(papers).reason == "papers only requested"


def test_bindingdb_matches_named_drugs_by_structure_and_reports_citable_rows(upstream):
    res = run(bindingdb.bindingdb_affinities("EGFR", drugs=["erlotinib", "unknownib"], variants=["T790M"]))
    assert res.success
    d = res.data
    bdb_req = [r for r in upstream.requests if r.url.host == "bindingdb.org"][0]
    assert parse_qs(bdb_req.url.query.decode())["cutoff"] == ["1000"]  # wider window when drugs are named
    assert d["uniprot"] == "P00533" and d["n_records"] == 6  # the zero affinity is dropped
    first, second = d["items"][0], d["items"][1]
    # DAT-003: the ligand label stays the BindingDB monomer; the named drug is given as matched_drug with its basis.
    assert first["ligand"] == "BindingDB monomer 5446" and first["matched_drug"] == "erlotinib"
    assert first["stereo_relation"] == "unspecified" and "stereochemistry specified in neither" in first["match_basis"]
    assert first["affinity_type"] == "IC50" and first["value_text"] == "0.029 nM"
    assert first["pmid"] == "37197473" and first["doi"] == "10.7270/Q2X92GK8"
    assert second["matched_drug"] == "erlotinib" and second["value"] == "0.1"
    status = {s["drug"]: s for s in d["drugs"]}
    assert status["erlotinib"]["status"] == "matched" and status["erlotinib"]["n_records"] == 2
    assert status["unknownib"]["structure"] == "unresolved"
    others = [i for i in d["items"] if "matched_drug" not in i]
    assert others[0]["monomer_id"] == "3032"  # most potent queried-target ligand
    assert all(i["relation"] != ">" for i in others)
    assert others[-1]["target_name"].startswith("Receptor tyrosine")  # partner-chain rows sort last
    assert "T790M" in d["mutant_note"] and "mutant-specific" in d["mutant_note"]
    assert "37197473" in res.canonical_ids["pmids"]
    src = SOURCES["bindingdb"]
    xml = src.evidence_xml(res)
    assert 'matched_drug="erlotinib"' in xml and 'ligand="BindingDB monomer 5446"' in xml and 'value="0.029"' in xml  # DAT-003 and 'units="nM"' in xml and "<mutant_note>" in xml
    ids = src.allowed_identifiers(res)
    assert "37197473" in ids["pmids"] and "10.7270/q2x92gk8" in ids["dois"] and ids["uniprot"] == {"P00533"}
    assert any(c.type == "pmid" and c.id == "37197473" for c in src.citations(res))
    ctx = make_ctx("IC50 of erlotinib and unknownib for EGFR T790M", [EGFR])
    notes = " ".join(src.scope_notes(ctx, res))
    assert "unknownib could not be matched" in notes and "Do not present these values as mutant-specific" in notes
    assert src.artifact(res)[1]["bindingdb"]["items"][0]["matched_drug"] == "erlotinib"  # DAT-003


def test_bindingdb_without_drugs_lists_top_affinity_ligands(upstream):
    res = run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533"))
    assert res.success
    assert not [r for r in upstream.requests if r.url.host == "rest.uniprot.org"]
    bdb_req = [r for r in upstream.requests if r.url.host == "bindingdb.org"][0]
    assert parse_qs(bdb_req.url.query.decode())["cutoff"] == ["10"]
    items = res.data["items"]
    assert [i["monomer_id"] for i in items] == ["3032", "5446", "50221516", "77"]  # one row per ligand, best value
    assert items[1]["affinity_type"] == "IC50" and items[1]["value_text"] == "0.029 nM"
    assert res.data["mutant_note"] is None and len(items) <= bindingdb.MAX_ROWS
    run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533"))
    assert len([r for r in upstream.requests if r.url.host == "bindingdb.org"]) == 1  # cached


def test_bindingdb_outage_is_a_failure_not_an_empty_result(upstream):
    upstream.overrides["bindingdb.org"] = lambda r: httpx.Response(503, text="Service Unavailable")
    res = run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533"))
    assert not res.success and res.error_type == ErrorType.UPSTREAM_API_ERROR
    src = SOURCES["bindingdb"]
    ctx = make_ctx("IC50 values for EGFR", [EGFR])
    note = src.scope_notes(ctx, res)[0]
    assert "could not be queried" in note and "not evidence" in note
    assert src.describe(res, ctx)[0].endswith("failed")

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    upstream.overrides["bindingdb.org"] = timeout
    assert run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533")).error_type == ErrorType.TIMEOUT
    upstream.overrides.clear()
    missing = run(bindingdb.bindingdb_affinities("NOTAGENE"))
    assert missing.error_type == ErrorType.NOT_FOUND
    empty = run(bindingdb.bindingdb_affinities("KRAS", uniprot="P01116"))
    assert empty.success and empty.data["items"] == [] and "returned no affinity records" in src.scope_notes(ctx, empty)[0]


def test_bindingdb_empty_body_is_retried_once_then_reported(monkeypatch, upstream):
    monkeypatch.setattr(bindingdb, "EMPTY_RETRY_DELAY_S", 0.0)
    answers = [httpx.Response(200, content=b"", headers={"content-type": "text/json"})]
    upstream.overrides["bindingdb.org"] = lambda r: answers.pop() if answers else None
    res = run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533"))
    assert res.success and res.data["n_records"] == 6
    bindingdb._CACHE.clear()
    upstream.overrides["bindingdb.org"] = lambda r: httpx.Response(200, content=b"", headers={"content-type": "text/json"})
    res = run(bindingdb.bindingdb_affinities("EGFR", uniprot="P00533"))
    assert not res.success and "non-JSON" in res.error
    assert len([r for r in upstream.requests if r.url.host == "bindingdb.org"]) == 4


def test_verifier_accepts_quoted_bindingdb_values_and_pmids(upstream):
    from phronesis.phronesis.agents.verifier import ClinicalVerifier

    res = run(bindingdb.bindingdb_affinities("EGFR", drugs=["erlotinib"]))
    src = SOURCES["bindingdb"]
    ids = src.allowed_identifiers(res)
    good = "In BindingDB, erlotinib inhibited EGFR with an IC50 of 0.029 nM (PMID: 37197473)."
    bad = "In BindingDB, erlotinib inhibited EGFR with an IC50 of 7.7 nM (PMID: 37197473)."
    fake_pmid = "In BindingDB, erlotinib inhibited EGFR with an IC50 of 0.029 nM (PMID: 99999999)."
    v = ClinicalVerifier()

    def defects(answer):
        report = v.verify_response("IC50 of erlotinib for EGFR", answer, {"bindingdb": res}, context_identifiers=ids)
        return [(d.category, d.description) for d in report.defects]

    assert not [d for d in defects(good) if d[0] in ("NUMERICAL", "IDENTIFIER")]
    assert any(c == "NUMERICAL" and "7.7" in t for c, t in defects(bad))
    assert any("99999999" in t for _, t in defects(fake_pmid))


# -----------------------------------------------------------------------------
# PubTator3 / LitVar2
# -----------------------------------------------------------------------------

def _pt_result(pmid: int, title: str, text_hl: str = "") -> Dict[str, Any]:
    return {"_id": str(pmid), "pmid": pmid, "title": title, "journal": "Cancers (Basel)", "authors": ["A A", "B B", "C C", "D D"],
            "date": "2021-01-11T00:00:00Z", "doi": f"10.1/{pmid}", "meta_date_publication": "2021", "text_hl": text_hl}


def test_highlight_markup_is_removed():
    raw = ("alternative drivers in @<m>GENE_KRAS</m> @GENE_3845 @@@KRAS@@@ wild-type (@<m>GENE_KRAS</m> @GENE_3845 "
           "@@@KRAS@@@ WT) @<m>DISEASE_Pancreatic_Neoplasms</m> @DISEASE_MESH:D010190 @@@pancreatic cancers@@@. "
           "low-level @VARIANT_p.T790M_EGFR_human @VARIANT_tmVar:p|SUB|T|790|M;HGVS:p.T790M;RS#:121434569 @@@T790M@@@ in "
           "@SPECIES_9606 @@@patients@@@")
    assert pubtator.clean_highlight(raw) == (
        "alternative drivers in KRAS wild-type (KRAS WT) pancreatic cancers. low-level T790M in patients")
    assert pubtator.disease_queries("pancreatic ductal adenocarcinoma") == [
        "pancreatic ductal adenocarcinoma", "pancreatic ductal carcinoma", "pancreatic cancer"]


def test_pubtator_routing():
    src = SOURCES["pubtator"]
    pdac = ent("disease", "pancreatic ductal adenocarcinoma", ids={"ontology_id": "MONDO:0005184"})
    assert src.should_run(make_ctx("How does KRAS drive pancreatic cancer?", [KRAS, pdac])).run
    assert src.plan(make_ctx("How does KRAS drive pancreatic cancer?", [KRAS, pdac]))["disease"] == "pancreatic ductal adenocarcinoma"
    t790m = ent("variant", "EGFR T790M", gene="EGFR", protein_change="T790M")
    assert src.plan(make_ctx("What is EGFR T790M?", [EGFR, t790m]))["variants"] == ["T790M"]
    assert not src.should_run(make_ctx("What does KRAS do?", [KRAS])).run
    assert not src.should_run(make_ctx("Treatments for pancreatic cancer", [pdac])).run
    papers = make_ctx("KRAS in pancreatic cancer, trials only", [KRAS, pdac], source_allowlist={"clinical_trials"},
                      source_restriction_reason="trials only requested")
    assert not src.should_run(papers).run


def test_pubtator_gene_disease_search(upstream):
    upstream.search_results["@GENE_KRAS AND @DISEASE_Carcinoma_Pancreatic_Ductal"] = [
        _pt_result(34250383 + i, f"KRAS paper {i}", "@<m>GENE_KRAS</m> @GENE_3845 @@@KRAS@@@ drives PDAC") for i in range(12)
    ]
    res = run(pubtator.pubtator_literature(gene="KRAS", disease="pancreatic ductal adenocarcinoma"))
    assert res.success
    d = res.data
    assert d["disease_entity"]["id"] == "@DISEASE_Carcinoma_Pancreatic_Ductal"  # second simplified form matched
    assert d["query"] == "@GENE_KRAS AND @DISEASE_Carcinoma_Pancreatic_Ductal" and d["total_hits"] == 1200
    assert len(d["items"]) == pubtator.MAX_PMIDS and d["items"][0]["snippet"] == "KRAS drives PDAC"
    assert d["items"][0]["authors"] == "A A, B B, C C et al." and d["items"][0]["year"] == "2021"
    assert res.canonical_ids["pmids"].split(",")[0] == "34250383"
    src = SOURCES["pubtator"]
    ids = src.allowed_identifiers(res)
    assert "34250383" in ids["pmids"] and len(ids["pmids"]) == 10
    cits = src.citations(res)
    assert cits[0].type == "pmid" and cits[0].title == "KRAS paper 0" and cits[0].source_agent == "Lithoscribe"
    xml = src.evidence_xml(res)
    assert '<article pmid="34250383"' in xml and "co-mention is not a finding" in xml
    acs = [r for r in upstream.requests if r.url.path.endswith("/entity/autocomplete/")]
    assert len(acs) == 2


def test_pubtator_variant_search_and_litvar_summary(upstream):
    both = "@VARIANT_p.T790M_EGFR_human AND @VARIANT_p.C797S_EGFR_human AND @DISEASE_Carcinoma_Non_Small_Cell_Lung"
    upstream.search_results[both] = [_pt_result(34612594, "Resistance after osimertinib")]
    res = run(pubtator.pubtator_literature(gene="EGFR", disease="non-small cell lung carcinoma", variants=["T790M", "p.C797S"]))
    d = res.data
    assert res.success and d["query"] == both and [a["pmid"] for a in d["items"]] == ["34612594"]
    t790m, c797s = d["variants"]
    assert t790m["rsid"] == "rs121434569" and t790m["pmids_count"] == 12196
    assert c797s["rsid"] == "rs1057519861"  # the rsID entry wins over the gene-level one
    assert res.canonical_ids["rsids"] == "rs121434569,rs1057519861"
    src = SOURCES["pubtator"]
    assert src.allowed_identifiers(res)["rsid"] == {"rs121434569", "rs1057519861"}
    assert 'pmids_count="12196"' in src.evidence_xml(res)
    title, detail = src.describe(res, make_ctx("x"))
    assert "1 articles" in title and "LitVar2: p.T790M rs121434569 (12196 papers)" in detail


def test_pubtator_falls_back_to_litvar_publications(upstream):
    res = run(pubtator.pubtator_literature(gene="EGFR", variants=["C797S"]))
    d = res.data
    assert res.success and d["channel"] == "litvar2_publications" and d["total_hits"] == 2
    assert [a["pmid"] for a in d["items"]] == ["26910730", "33660941"]
    assert d["items"][0]["title"] == "Title 26910730" and d["items"][0]["year"] == "2016"


def test_pubtator_outages_are_reported(upstream):
    upstream.overrides["www.ncbi.nlm.nih.gov/research/litvar2-api"] = lambda r: httpx.Response(502, text="bad gateway")
    upstream.search_results["@GENE_EGFR AND @DISEASE_Carcinoma_Non_Small_Cell_Lung"] = [_pt_result(1, "EGFR NSCLC")]
    res = run(pubtator.pubtator_literature(gene="EGFR", disease="non-small cell lung carcinoma", variants=["T790M"]))
    assert res.success and res.data["variants"][0]["status"] == "error" and res.data["items"][0]["pmid"] == "1"
    src = SOURCES["pubtator"]
    ctx = make_ctx("EGFR T790M in NSCLC")
    assert any("could not be queried" in n and "outage" in n for n in src.scope_notes(ctx, res))
    upstream.overrides["www.ncbi.nlm.nih.gov/research/pubtator3-api/search"] = lambda r: httpx.Response(500, text="err")
    down = run(pubtator.pubtator_literature(gene="EGFR", disease="non-small cell lung carcinoma"))
    assert not down.success and down.error_type == ErrorType.UPSTREAM_API_ERROR
    assert "not evidence" in src.scope_notes(ctx, down)[0]
    upstream.overrides.clear()
    none = run(pubtator.pubtator_literature(gene="TP53", disease="rare thing"))
    assert none.success and none.data["items"] == [] and none.data["query"] == '@GENE_TP53 AND "rare thing"'
    assert "returned no articles" in src.scope_notes(ctx, none)[0]


# -----------------------------------------------------------------------------
# SynLethDB
# -----------------------------------------------------------------------------

SL_HEADER = "x:START_ID\tx_type\tx_name\tx_source\ty:END_ID\ty_type\ty_name\ty_source\trelation\t:TYPE\trel_source\tedge_index\tcell_line\tpubmed_id\tcancer"
SL_ROWS = [
    "3845\tGene\tKRAS\tNCBI\t56910\tGene\tSTARD7\tNCBI\tSL\tGene_SL_Gene\tGenomeRNAi\t8608062\tDLD-1\t19490893\t",
    "3845\tGene\tKRAS\tNCBI\t5347\tGene\tPLK1\tNCBI\tSL\tGene_SL_Gene\tGenomeRNAi\t1\tDLD-1\t19490893\t",
    "5347\tGene\tPLK1\tNCBI\t3845\tGene\tKRAS\tNCBI\tSL\tGene_SL_Gene\tLow Throughput\t2\tTBD\t20000001\tpancreatic cancer_D010190",
    "3845\tGene\tKRAS\tNCBI\t9999\tGene\tPREDX\tNCBI\tSL\tGene_SL_Gene\tComputational Prediction\t3\t\"\"\t25171417\t",
    "596\tGene\tBCL2\tNCBI\t90427\tGene\tBMF\tNCBI\tSL\tGene_SL_Gene\tText Mining\t8606921\t\"\"\t19244105\tmelanoma_D008545",
]


def _sl_dir(tmp_path: Path) -> Path:
    d = tmp_path / "synlethdb"
    d.mkdir()
    (d / "Human.SL.detailed.tsv").write_text("\n".join([SL_HEADER, *SL_ROWS]) + "\n", encoding="utf-8")
    return d


def test_synlethdb_partners_from_the_bulk_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_SYNLETHDB_DIR", str(_sl_dir(tmp_path)))
    res = synlethdb.synlethdb_partners("kras", disease="pancreatic ductal adenocarcinoma")
    assert res.success
    d = res.data
    assert d["n_partners"] == 3 and d["n_experimental"] == 2 and d["n_prediction_only"] == 1
    plk1, stard7, predx = d["items"]
    assert plk1["partner"] == "PLK1" and plk1["in_question_disease"] and plk1["pmids"] == ["19490893", "20000001"]
    assert plk1["evidence_types"] == ["GenomeRNAi", "Low Throughput"] and plk1["cancers"] == ["pancreatic cancer"]
    assert stard7["partner"] == "STARD7" and predx["prediction_only"] and predx["cell_lines"] == []
    assert res.canonical_ids == {"pmids": "19490893,20000001,25171417"}
    src = SOURCES["synlethdb"]
    ctx = make_ctx("What are the synthetic-lethal partners of KRAS?", [KRAS])
    assert src.should_run(ctx).run
    xml = src.evidence_xml(res)
    assert 'partner="PLK1"' in xml and 'prediction_only="true"' in xml and "not experimental evidence" in xml
    assert src.allowed_identifiers(res)["pmids"] == {"19490893", "20000001", "25171417"}
    missing = synlethdb.synlethdb_partners("NOTAGENE")
    assert missing.error_type == ErrorType.NOT_FOUND
    assert "do not invent partners" in src.scope_notes(ctx, missing)[0]


def test_synlethdb_reads_the_2_0_csv_layout(monkeypatch, tmp_path):
    d = tmp_path / "sl2"
    d.mkdir()
    (d / "Human_SL.csv").write_text(
        "n1.name,n1.identifier,n2.name,n2.identifier,r.cell_line,r.pubmed_id,r.source,r.statistic_score\n"
        "BRCA1,672,PARP1,142,HeLa,16015333,Low Throughput,0.9\n"
        "PARP1,142,BRCA2,675,NA,16015333;15829966,Low Throughput,0.95\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHRONESIS_SYNLETHDB_DIR", str(d))
    res = synlethdb.synlethdb_partners("PARP1")
    assert [p["partner"] for p in res.data["items"]] == ["BRCA2", "BRCA1"]
    assert res.data["items"][0]["max_score"] == 0.95 and res.data["items"][0]["pmids"] == ["16015333", "15829966"]


def test_synlethdb_without_file_is_skipped_with_a_scope_note(monkeypatch, tmp_path):
    src = SOURCES["synlethdb"]
    ctx = make_ctx("Which genes are synthetically lethal with KRAS?", [KRAS])
    decision = src.should_run(ctx)
    assert not decision.run and "PHRONESIS_SYNLETHDB_DIR" in decision.reason
    note = src.scope_notes(ctx, None)[0]
    assert "cannot be listed from SynLethDB" in note and "Do not name SL partners" in note
    assert src.scope_notes(make_ctx("What does KRAS do?", [KRAS]), None) == []
    res = synlethdb.synlethdb_partners("KRAS")
    assert not res.success and res.error_type == ErrorType.DATASET_REQUIRED
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "Human.SL.detailed.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setenv("PHRONESIS_SYNLETHDB_DIR", str(bad))
    broken = synlethdb.synlethdb_partners("KRAS")
    assert not broken.success and broken.error_type == ErrorType.INTERNAL_ERROR and "could not be read" in broken.error


def test_download_script_points_at_the_documented_file():
    path = Path(__file__).resolve().parents[1] / "scripts" / "download_synlethdb.py"
    spec = importlib.util.spec_from_file_location("download_synlethdb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.URL == synlethdb.DOWNLOAD_URL and mod.FILE_NAME in synlethdb.FILE_NAMES
    assert mod.DEFAULT_DEST.parts[-3:] == (".cache", "phronesis", "synlethdb")


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

def test_sources_and_tools_are_registered_outside_the_omics_domain():
    keys = {s.key for s in get_sources()}
    assert {"bindingdb", "pubtator", "synlethdb"} <= keys
    entries = {e.name: e for e, _ in tool_entries()}
    assert entries[bindingdb.TOOL_NAME].domain == "drugs"
    assert entries[pubtator.TOOL_NAME].domain == "literature"
    assert entries[synlethdb.TOOL_NAME].domain == "genetics"
    assert entries[synlethdb.TOOL_NAME].adapter_type.value == "LOCAL_COMPUTE"
