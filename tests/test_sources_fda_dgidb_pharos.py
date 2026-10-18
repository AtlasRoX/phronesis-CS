"""
Offline tests for three free sources: openFDA Drugs@FDA (inside openfda_drug_safety), DGIdb 5 and Pharos/TCRD.
Every upstream is an httpx.MockTransport; no test touches the network.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.drugintel import drugsfda
from phronesis.drugintel.checks import check_fda_approval, fda_unapproved_drugs
from phronesis.drugintel.chembl_client import ChemblClient
from phronesis.drugintel.engine import DrugIntelEngine
from phronesis.drugintel.openfda_client import OpenFdaClient
from phronesis.drugintel.render import safety_evidence_xml, safety_fallback_markdown
from phronesis.drugintel.sources import OpenFdaSource
from phronesis.sources_extra import all_sources, common, dgidb, pharos, tool_entries
from phronesis.tools.registry import ErrorType, ToolResult
from phronesis.utils.http_limits import TTLCache


def run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# Drugs@FDA fixtures (shapes captured from api.fda.gov/drug/drugsfda.json on 2026-10-16, trimmed)
# -----------------------------------------------------------------------------

def _sub(kind: str, number: str, date: str, cls: str, status: str = "AP", priority: str = "STANDARD") -> Dict[str, Any]:
    return {"submission_type": kind, "submission_number": number, "submission_status": status,
            "submission_status_date": date, "review_priority": priority, "submission_class_code": cls,
            "application_docs": [{"id": "1", "url": "http://example.invalid/doc.pdf", "type": "Letter"}]}


def _product(brand: str, *ingredients: str, status: str = "Prescription") -> Dict[str, Any]:
    return {"brand_name": brand, "marketing_status": status, "dosage_form": "TABLET", "route": "ORAL",
            "active_ingredients": [{"name": i, "strength": "100MG"} for i in ingredients]}


OLAPARIB_BODY = {
    "meta": {"disclaimer": "Do not rely on openFDA to make decisions regarding medical care.", "last_updated": "2026-10-14",
             "results": {"skip": 0, "limit": 100, "total": 3}},
    "results": [
        {"application_number": "NDA208558", "sponsor_name": "ASTRAZENECA",
         "products": [_product("LYNPARZA", "OLAPARIB")],
         "submissions": [_sub("ORIG", "1", "20170817", "TYPE 3", priority="PRIORITY"),
                         _sub("SUPPL", "6", "20181219", "EFFICACY", priority="PRIORITY"),
                         _sub("SUPPL", "2", "20180206", "LABELING"),
                         _sub("SUPPL", "1", "20180112", "EFFICACY", priority="PRIORITY")]},
        {"application_number": "NDA206162", "sponsor_name": "ASTRAZENECA",
         "products": [_product("LYNPARZA", "OLAPARIB", status="Discontinued")],
         "submissions": [_sub("ORIG", "1", "20141219", "TYPE 1", priority="PRIORITY"),
                         _sub("SUPPL", "3", "20170817", "EFFICACY")]},
        # A combination product and an application that only has a tentative approval.
        {"application_number": "NDA999991", "sponsor_name": "OTHER",
         "products": [_product("COMBO", "OLAPARIB", "ABIRATERONE ACETATE")],
         "submissions": [_sub("ORIG", "1", "20240101", "TYPE 4")]},
        {"application_number": "NDA999992", "sponsor_name": "PENDING",
         "products": [_product("OLAPARIB", "OLAPARIB", status="None (Tentative Approval)")],
         "submissions": [_sub("ORIG", "1", "20250101", "TYPE 5", status="TA")]},
    ],
}
NOT_FOUND = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}


def test_ingredient_matching_salts_suffixes_and_combinations():
    m = drugsfda.ingredient_matches
    assert m("OLAPARIB", "olaparib")
    assert m("OSIMERTINIB MESYLATE", "osimertinib")
    assert m("METFORMIN HYDROCHLORIDE", "metformin")
    assert m("FAM-TRASTUZUMAB DERUXTECAN-NXKI", "trastuzumab deruxtecan")
    assert m("TRASTUZUMAB-DKST", "trastuzumab")  # a biosimilar is the same molecule
    assert not m("ADO-TRASTUZUMAB EMTANSINE", "trastuzumab")  # a different drug (an ADC)
    assert not m("SITAGLIPTIN AND METFORMIN HYDROCHLORIDE", "metformin")
    assert not m("IMATINIBX", "imatinib")


def test_summarize_approval_reports_current_application_first_approval_and_efficacy_supplements():
    a = drugsfda.summarize_approval("olaparib", OLAPARIB_BODY, True, drugsfda.drugsfda_search("olaparib"))
    assert a.status == "approved"
    assert a.application_number == "NDA208558" and a.marketed is True and a.brand_names == ["LYNPARZA"]
    assert (a.first_approval_date, a.first_approval_application) == ("2014-12-19", "NDA206162")
    assert [x.application_number for x in a.applications] == ["NDA208558", "NDA206162"]
    assert a.applications_total == 2  # the combination product is not a single-ingredient approval
    assert [(x.application_number, x.submission_number, x.date) for x in a.efficacy_supplements] == [
        ("NDA206162", "3", "2017-08-17"), ("NDA208558", "1", "2018-01-12"), ("NDA208558", "6", "2018-12-19")]
    assert a.unapproved_applications == ["NDA999992"]
    phrase = drugsfda.approval_phrase(a.model_dump())
    assert phrase == "FDA-approved (NDA208558, first approved 2014-12-19 under NDA206162)"
    assert a.url and a.url.endswith("ApplNo=208558")
    assert drugsfda.drugsfda_search("olaparib") == (
        'products.active_ingredients.name:"OLAPARIB" AND application_number:(NDA* OR BLA*)')


def test_summarize_approval_not_found_combination_only_and_malformed():
    nf = drugsfda.summarize_approval("mrtx1133", None, False, "s")
    assert nf.status == "not_found"
    assert drugsfda.approval_phrase(nf.model_dump()).startswith("not found in Drugs@FDA")
    combo_body = {"results": [OLAPARIB_BODY["results"][2]]}
    combo = drugsfda.summarize_approval("olaparib", combo_body, True, "s")
    assert combo.status == "combination_only" and combo.application_number == "NDA999991"
    with pytest.raises(ValueError):
        drugsfda.summarize_approval("x", {"results": [{}]}, True, "s")


# -----------------------------------------------------------------------------
# openfda_drug_safety with the Drugs@FDA lookup
# -----------------------------------------------------------------------------

class FdaUpstream:
    """openFDA: no FAERS reports and no label; drugsfda answers with `drugsfda` (a callable per request)."""

    def __init__(self, drugsfda_reply: Callable[[httpx.Request], httpx.Response]) -> None:
        self.drugsfda_reply = drugsfda_reply
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.fda.gov"
        if request.url.path == "/drug/drugsfda.json":
            return self.drugsfda_reply(request)
        return httpx.Response(404, json=NOT_FOUND)

    def drugsfda_calls(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/drug/drugsfda.json"]


def _engine(up: FdaUpstream) -> DrugIntelEngine:
    transport = httpx.MockTransport(up)
    return DrugIntelEngine(ChemblClient(transport=transport), OpenFdaClient(transport=transport, api_key="", cache=TTLCache()))


def test_drug_safety_carries_drugsfda_approval_citation_and_application_ids():
    up = FdaUpstream(lambda r: httpx.Response(200, json=OLAPARIB_BODY))
    res = run(_engine(up).drug_safety("olaparib"))
    assert res.success
    call = up.drugsfda_calls()[0]
    assert call.url.params["search"] == drugsfda.drugsfda_search("olaparib") and int(call.url.params["limit"]) < 1000
    approval = res.data["approval"]
    assert approval["status"] == "approved" and approval["application_number"] == "NDA208558"
    assert res.canonical_ids["application_numbers"] == "NDA208558,NDA206162"
    cite = [c for c in res.data["citation_records"] if c["id"] == "NDA208558"]
    assert cite and cite[0]["type"] == "url" and "Drugs@FDA" in cite[0]["title"]

    xml = safety_evidence_xml({"safety": [res.data], "skipped": []})
    assert 'status="approved"' in xml and 'application_number="NDA208558"' in xml
    assert "FDA-approved (NDA208558, first approved 2014-12-19 under NDA206162)" in xml
    assert "NDA208558 S-6 approved 2018-12-19 (priority review)" in xml
    assert "Drugs@FDA: FDA-approved (NDA208558" in safety_fallback_markdown({"safety": [res.data]})

    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [res.data], "skipped": []})
    src = OpenFdaSource()
    ctx = QueryContext(raw_query="olaparib safety", clean_query="olaparib safety")
    title, detail = src.describe(wrapped, ctx)
    assert "FDA-approved (NDA208558, first approved 2014-12-19 under NDA206162)" in detail
    assert src.allowed_identifiers(wrapped)["fda_application"] >= {"NDA208558", "NDA206162"}
    notes = src.scope_notes(ctx, wrapped)
    assert any(n.startswith("Drugs@FDA: olaparib is FDA-approved (NDA208558") and "3 approved efficacy supplements" in n for n in notes)


def test_drug_not_in_drugsfda_is_stated_explicitly():
    up = FdaUpstream(lambda r: httpx.Response(404, json=NOT_FOUND))
    res = run(_engine(up).drug_safety("mrtx1133"))
    assert res.success and res.data["approval"]["status"] == "not_found"
    xml = safety_evidence_xml({"safety": [res.data]})
    assert 'status="not_in_drugs_at_fda"' in xml and "Do not call it FDA-approved" in xml
    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [res.data]})
    notes = OpenFdaSource().scope_notes(QueryContext(raw_query="q", clean_query="q"), wrapped)
    assert any("Drugs@FDA lists no approved NDA or BLA for mrtx1133" in n for n in notes)
    assert "not found in Drugs@FDA" in OpenFdaSource().describe(wrapped, QueryContext(raw_query="q", clean_query="q"))[1]


@pytest.mark.parametrize("failure", ["500", "timeout", "garbage"])
def test_drugsfda_failure_is_reported_as_failed_not_as_not_found(failure):
    def reply(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "garbage":
            return httpx.Response(200, json={"results": [{"unexpected": True}]})
        return httpx.Response(500, json={"error": {"code": "SERVER_ERROR", "message": "boom"}})

    up = FdaUpstream(reply)
    res = run(_engine(up).drug_safety("olaparib"))
    assert res.success  # FAERS still answers; only the approval part is unknown
    approval = res.data["approval"]
    assert approval["status"] == "error" and approval["error"]
    if failure in ("500", "timeout"):
        assert len(up.drugsfda_calls()) == 2  # one retry
    xml = safety_evidence_xml({"safety": [res.data]})
    assert 'status="lookup_failed"' in xml and "not_in_drugs_at_fda" not in xml
    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [res.data]})
    notes = OpenFdaSource().scope_notes(QueryContext(raw_query="q", clean_query="q"), wrapped)
    assert any("Drugs@FDA lookup for olaparib failed" in n for n in notes)
    assert not fda_unapproved_drugs({"openfda": wrapped})  # a failure never feeds the approval check


def test_drugsfda_success_is_cached():
    up = FdaUpstream(lambda r: httpx.Response(200, json=OLAPARIB_BODY))
    engine = _engine(up)
    run(engine.drug_approval("olaparib"))
    run(engine.drug_approval("olaparib"))
    assert len(up.drugsfda_calls()) == 1


# -----------------------------------------------------------------------------
# Verifier check: "FDA-approved" claims
# -----------------------------------------------------------------------------

def _vctx(sentences: List[str], data: Dict[str, Any]) -> Any:
    tr = {"openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data=data)}
    hedge = re.compile(r"\b(?:no|not|never|without|unknown)\b|n't\b", re.IGNORECASE)
    return SimpleNamespace(sentences=sentences, tool_results=tr, hedge_pattern=hedge)


NOT_IN_FDA = {"safety": [{"ingredient": "mrtx1133", "top_reactions": [], "approval": {"status": "not_found", "ingredient": "mrtx1133"}},
                         {"ingredient": "sotorasib", "top_reactions": [], "approval": {"status": "approved", "application_number": "NDA214665"}}],
              "skipped": [{"drug": "TIVANTINIB", "max_phase": 3.0, "reason": "ChEMBL max phase 3"}]}


def test_fda_approval_check_flags_unapproved_drug_called_fda_approved():
    defects = check_fda_approval(_vctx(["MRTX1133 is FDA-approved for KRAS G12D pancreatic cancer."], NOT_IN_FDA))
    assert len(defects) == 1
    d = defects[0]
    assert (d["category"], d["severity"]) == ("CLAIM_CONTRADICTED", "MAJOR") and "Drugs@FDA" in d["description"]
    also = check_fda_approval(_vctx(["MRTX1133 was approved by the FDA in 2024.", "The FDA-approved drug MRTX1133 is potent."], NOT_IN_FDA))
    assert len(also) == 2
    minor = check_fda_approval(_vctx(["Tivantinib received FDA approval in 2015."], NOT_IN_FDA))
    assert [(x["category"], x["severity"]) for x in minor] == [("CLINICAL_CLAIM", "MINOR")]


@pytest.mark.parametrize("sentence", [
    "MRTX1133 is not FDA-approved.",
    "MRTX1133 has not yet received FDA approval.",
    "MRTX1133 may receive FDA approval if phase 3 succeeds.",
    "MRTX1133 is investigational and could seek FDA approval.",
    "Unlike MRTX1133, sotorasib is FDA-approved.",
    "Sotorasib is FDA-approved, whereas MRTX1133 remains in phase 1.",
    "Sotorasib is FDA-approved for KRAS G12C NSCLC.",
    "MRTX1133 is approved in Japan.",
])
def test_fda_approval_check_skips_hedged_negated_and_other_drug_sentences(sentence):
    assert check_fda_approval(_vctx([sentence], NOT_IN_FDA)) == []


def test_fda_approval_check_is_conservative_on_conflicts_and_cber_products():
    label_conflict = {"safety": [{"ingredient": "drugx", "top_reactions": [], "approval": {"status": "not_found"},
                                  "label": {"set_id": "s", "application_number": "BLA125514"}}]}
    cber = {"safety": [{"ingredient": "tisagenlecleucel", "molecule_type": "Cell", "top_reactions": [], "approval": {"status": "not_found"}}]}
    failed = {"safety": [{"ingredient": "drugx", "top_reactions": [], "approval": {"status": "error", "error": "timeout"}}]}
    assert check_fda_approval(_vctx(["Drugx is FDA-approved."], label_conflict)) == []
    assert check_fda_approval(_vctx(["Tisagenlecleucel is FDA-approved."], cber)) == []
    assert check_fda_approval(_vctx(["Drugx is FDA-approved."], failed)) == []


def test_fda_approval_check_is_registered():
    from phronesis.extensions import hooks, load_extensions

    load_extensions()
    assert "drug_fda_approval" in dict(hooks.get_verification_checks())


# -----------------------------------------------------------------------------
# DGIdb and Pharos
# -----------------------------------------------------------------------------

def gene(symbol: str) -> ResolvedEntity:
    return ResolvedEntity(type="gene", mention=symbol, label=symbol, ids={}, validated=True, source="HGNC")


def make_ctx(query: str, symbols: Optional[List[str]] = None, **kw: Any) -> QueryContext:
    entities = [gene(s) for s in symbols or []]
    primary = entities[0] if entities else None
    return QueryContext(raw_query=query, clean_query=query, entities=entities, primary_gene=primary,
                        primary_target=primary.label if primary else None, **kw)


def _interaction(drug: str, approved: bool, score: float, types=(), pmids=(), sources=("ChEMBL",), concept: str = "rxcui:1") -> Dict[str, Any]:
    return {"drug": {"name": drug, "conceptId": concept, "approved": approved}, "interactionScore": score,
            "interactionTypes": [{"type": t, "directionality": None} for t in types],
            "publications": [{"pmid": p} for p in pmids], "sources": [{"sourceDbName": s} for s in sources]}


KRAS_NODE = {
    "name": "KRAS", "conceptId": "hgnc:6407", "longName": "KRAS proto-oncogene, GTPase",
    "interactions": [
        _interaction("MRTX1133", False, 3.5, ("inhibitor",), (37000001,)),
        _interaction("ADAGRASIB", True, 2.816, ("inhibitor",), (35658005, 37703579), ("CIViC",), "rxcui:2625882"),
        _interaction("ADAGRASIB", True, 0.352, ("inhibitor",), (), ("ChEMBL",), "chembl:CHEMBL4594350"),
        _interaction("SOTORASIB", True, 1.526, (), tuple(range(31000001, 31000010)), ("CIViC", "OncoKB")),
        _interaction("CETUXIMAB", True, 0.557, (), (28275037,), ("CGI",)),
    ],
}


class GraphQLUpstream:
    def __init__(self, handler: Callable[[httpx.Request, Dict[str, Any]], httpx.Response]) -> None:
        self.handler = handler
        self.requests: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append({"host": request.url.host, "body": body})
        return self.handler(request, body)


@pytest.fixture
def graphql(monkeypatch):
    dgidb.clear_cache()
    pharos.clear_cache()

    def install(handler: Callable[[httpx.Request, Dict[str, Any]], httpx.Response]) -> GraphQLUpstream:
        up = GraphQLUpstream(handler)
        monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(up))
        return up

    yield install
    dgidb.clear_cache()
    pharos.clear_cache()


def _dgidb_ok(nodes: List[Dict[str, Any]]) -> Callable[[httpx.Request, Dict[str, Any]], httpx.Response]:
    def handler(request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
        assert request.url.host == "dgidb.org" and request.method == "POST"
        names = body["variables"]["names"]
        return httpx.Response(200, json={"data": {"genes": {"nodes": [n for n in nodes if n["name"] in names]}}})
    return handler


def test_dgidb_merges_ranks_and_cites(graphql):
    up = graphql(_dgidb_ok([KRAS_NODE]))
    res = run(dgidb.dgidb_interactions(["KRAS", "NOTAGENE"]))
    assert res.success
    assert up.requests[0]["body"]["variables"] == {"names": ["KRAS", "NOTAGENE"]}
    items = res.data["items"]
    assert [i["drug"] for i in items] == ["ADAGRASIB", "SOTORASIB", "CETUXIMAB", "MRTX1133"]  # approved first, by score
    ada = items[0]
    assert ada["score"] == 2.816 and ada["pmids"] == ["35658005", "37703579"] and ada["chembl_id"] == "CHEMBL4594350"
    assert set(ada["sources"]) == {"CIViC", "ChEMBL"}
    assert items[1]["pmids_total"] == 9 and len(items[1]["pmids"]) == dgidb.MAX_PMIDS_PER_DRUG
    assert res.data["genes"][0] == {"gene": "KRAS", "concept_id": "hgnc:6407", "long_name": "KRAS proto-oncogene, GTPase",
                                    "drugs_total": 4, "approved_total": 3, "shown": 4}
    assert res.data["missing_genes"] == ["NOTAGENE"]

    src = dgidb.DgidbSource()
    ctx = make_ctx("Which drugs target KRAS?", ["KRAS"])
    xml = src.evidence_xml(res)
    assert '<interaction drug="ADAGRASIB" approved="true" score="2.816" types="inhibitor"' in xml
    assert 'drug="CETUXIMAB" approved="true" score="0.557" types="not stated"' in xml
    assert "not necessarily the FDA" in xml and '<gene_not_found symbol="NOTAGENE">' in xml
    ids = src.allowed_identifiers(res)
    assert "35658005" in ids["pmids"] and "CHEMBL4594350" in ids["chembl"]
    cites = src.citations(res)
    assert cites[0].type == "url" and cites[0].id == "DGIdb:KRAS"
    assert any(c.type == "pmid" and c.id == "35658005" and c.source_tool == dgidb.TOOL_NAME for c in cites)
    title, detail = src.describe(res, ctx)
    assert title == "DGIdb: KRAS: 4 drugs (3 approved)" and "ADAGRASIB (approved)" in detail and "NOTAGENE" in detail
    assert any("no gene record for NOTAGENE" in n for n in src.scope_notes(ctx, res))
    key, art = src.artifact(res)
    assert key == "omics_intel" and art["dgidb"]["items"][0]["drug"] == "ADAGRASIB"


def test_dgidb_caps_interactions_and_caches(graphql):
    node = {**KRAS_NODE, "interactions": [_interaction(f"DRUG{i}", i % 2 == 0, float(i)) for i in range(40)]}
    up = graphql(_dgidb_ok([node]))
    res = run(dgidb.dgidb_interactions(["KRAS"]))
    items = res.data["items"]
    assert len(items) == dgidb.MAX_INTERACTIONS
    assert all(i["approved"] for i in items[:20]) and items[0]["drug"] == "DRUG38"
    run(dgidb.dgidb_interactions(["KRAS"]))
    assert len(up.requests) == 1


@pytest.mark.parametrize("failure,error_type", [
    ("graphql_error", ErrorType.UPSTREAM_API_ERROR),
    ("500", ErrorType.UPSTREAM_API_ERROR),
    ("timeout", ErrorType.TIMEOUT),
    ("not_found", ErrorType.NOT_FOUND),
])
def test_dgidb_failures_surface_as_failed(graphql, failure, error_type):
    def handler(request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "500":
            return httpx.Response(502, text="bad gateway")
        if failure == "graphql_error":
            return httpx.Response(200, json={"errors": [{"message": "Field 'bogus' doesn't exist"}]})
        return httpx.Response(200, json={"data": {"genes": {"nodes": []}}})

    up = graphql(handler)
    res = run(dgidb.dgidb_interactions(["KRAS"]))
    assert not res.success and res.error_type == error_type
    assert len(up.requests) == (2 if failure in ("500", "timeout") else 1)
    notes = dgidb.DgidbSource().scope_notes(make_ctx("drugs for KRAS", ["KRAS"]), res)
    if failure == "not_found":
        assert notes and "has no entry" in notes[0]
    else:
        assert notes and "outage, not evidence that no drug-gene interactions exist" in notes[0]


def test_dgidb_retries_once_after_a_5xx(graphql):
    calls = {"n": 0}

    def handler(request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return _dgidb_ok([KRAS_NODE])(request, body)

    graphql(handler)
    res = run(dgidb.dgidb_interactions(["KRAS"]))
    assert res.success and calls["n"] == 2


PHAROS_RECORDS = {
    "MYC": {"name": "Myc proto-oncogene protein", "sym": "MYC", "uniprot": "P01106", "tdl": "Tchem", "fam": "TF", "novelty": 0.00005734},
    "KRAS": {"name": "GTPase KRas", "sym": "KRAS", "uniprot": "P01116", "tdl": "Tclin", "fam": "Enzyme", "novelty": 0.00013856},
    "MAX": {"name": "Protein max", "sym": "MAXL", "uniprot": "P61244", "tdl": "Tbio", "fam": "TF", "novelty": 0.001},
}


def _pharos_ok(request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
    assert request.url.host == "pharos-api.ncats.io"
    data = {}
    for alias, sym in re.findall(r'(t\d+):target\(q:\{sym:"([A-Z0-9-]+)"\}\)', body["query"]):
        data[alias] = PHAROS_RECORDS.get(sym)
    return httpx.Response(200, json={"data": data})


def test_pharos_tdl_for_several_genes_in_one_request(graphql):
    up = graphql(_pharos_ok)
    res = run(pharos.pharos_targets(["MYC", "KRAS", "MAX"]))
    assert res.success and len(up.requests) == 1
    assert [(i["gene"], i["tdl"]) for i in res.data["items"]] == [("MYC", "Tchem"), ("KRAS", "Tclin")]
    assert res.data["missing_genes"] == ["MAX"]  # a record for another symbol is not accepted
    src = pharos.PharosSource()
    xml = src.evidence_xml(res)
    assert "MYC: Pharos TDL Tchem (has ChEMBL/DrugCentral small molecules meeting the potency cut-offs, but no approved drug" in xml
    assert 'novelty="0.00005734"' in xml and '<target_not_found gene="MAX">' in xml
    assert src.allowed_identifiers(res) == {"uniprot": {"P01106", "P01116"}}
    title, _ = src.describe(res, make_ctx("Is MYC druggable?", ["MYC"]))
    assert title == "Pharos: MYC Tchem; KRAS Tclin"
    assert [c.id for c in src.citations(res)] == ["Pharos:MYC", "Pharos:KRAS"]


@pytest.mark.parametrize("failure,error_type", [("graphql_error", ErrorType.UPSTREAM_API_ERROR), ("timeout", ErrorType.TIMEOUT),
                                                ("null", ErrorType.NOT_FOUND)])
def test_pharos_failures_surface_as_failed(graphql, failure, error_type):
    def handler(request: httpx.Request, body: Dict[str, Any]) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        if failure == "graphql_error":
            return httpx.Response(200, json={"errors": [{"message": "Cannot query field"}]})
        return httpx.Response(200, json={"data": {"t0": None}})

    graphql(handler)
    res = run(pharos.pharos_targets(["MYC"]))
    assert not res.success and res.error_type == error_type
    notes = pharos.PharosSource().scope_notes(make_ctx("Is MYC druggable?", ["MYC"]), res)
    assert notes and ("outage" in notes[0] or "has no entry" in notes[0])


def test_routing_for_dgidb_and_pharos():
    d, p = dgidb.DgidbSource(), pharos.PharosSource()
    q3 = make_ctx("MYC target-to-lead strategy in glioblastoma: which chemical matter exists?", ["MYC"])
    assert d.should_run(q3).run and p.should_run(q3).run
    kras = make_ctx("Which approved drugs could be repurposed against KRAS G12D?", ["KRAS"])
    assert d.should_run(kras).run and not p.should_run(kras).run
    assert d.plan(kras) == {"genes": ["KRAS"], "_subject": "KRAS"}
    two = make_ctx("Is EGFR or MYC the more druggable target?", ["EGFR", "MYC"])
    assert p.should_run(two).run and p.plan(two)["genes"] == ["EGFR", "MYC"]
    for text in ("Which proteins interact with PCSK9?", "What GWAS associations are reported for PCSK9?",
                 "In which tissues is PCSK9 expressed?"):
        ctx = make_ctx(text, ["PCSK9"])
        assert not d.should_run(ctx).run and not p.should_run(ctx).run, text
    no_gene = make_ctx("Which drugs are druggable targets for fibrosis?")
    assert not d.should_run(no_gene).run and "no gene named" in d.should_run(no_gene).reason
    papers = make_ctx("Is MYC druggable? papers only", ["MYC"], source_allowlist={"lithoscribe"},
                      source_restriction_reason="papers only requested")
    assert not d.should_run(papers).run and not p.should_run(papers).run
    sim = make_ctx("Simulate BIOMD0000000010 for drug targets of MYC", ["MYC"], biomodel_id="BIOMD0000000010",
                   is_simulation_intent=True)
    assert not d.should_run(sim).run and not p.should_run(sim).run


def test_registration_and_tool_entries():
    keys = {s.key for s in all_sources()}
    assert {"dgidb", "pharos"} <= keys
    entries = {e.name: e for e, _ in tool_entries()}
    for name in (dgidb.TOOL_NAME, pharos.TOOL_NAME):
        assert entries[name].domain == "synapta" and entries[name].adapter_type.value == "DIRECT_GRAPHQL"
    from phronesis.sources import get_sources

    assert {"dgidb", "pharos"} <= {s.key for s in get_sources()}
