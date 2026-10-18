"""
Offline tests for two source-level gaps a live BRCA2 / SOLO-1 run exposed:

1. SynLethDB partners are enriched with the approved drugs acting on them (one batched DGIdb GraphQL query, Drugs@FDA
   first approval dates for the leading drugs); a failed lookup never fails the SynLethDB tool. The hypothesis phase
   prefers partners with an approved drug, and DGIdb results for a lost tumour suppressor are labelled as
   associations, not drugs acting on it.
2. ClinicalTrials.gov: trials the question names ("SOLO-1") are listed first and their posted results fetched first,
   and a named trial the searches missed is looked up once by title/acronym.

Every upstream is an httpx.MockTransport; no test touches the network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from urllib.parse import parse_qsl

import httpx
import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.drugintel import openfda_client
from phronesis.sources_extra import common, dgidb, synlethdb
from phronesis.tools.registry import BiomedicalToolRegistry, ToolResult
from phronesis.trials import clinical_trials as ct
from phronesis.trials.trial_query import plan_trial_query
from phronesis.utils.http_limits import TTLCache


def run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# Fixtures: a SynLethDB file, DGIdb GraphQL and Drugs@FDA
# -----------------------------------------------------------------------------

SL_HEADER = "x:START_ID\tx_type\tx_name\tx_source\ty:END_ID\ty_type\ty_name\ty_source\trelation\t:TYPE\trel_source\tedge_index\tcell_line\tpubmed_id\tcancer"
SL_ROWS = [
    "675\tGene\tBRCA2\tNCBI\t142\tGene\tPARP1\tNCBI\tSL\tGene_SL_Gene\tCRISPR/CRISPRi\t1\tDLD-1\t27453043;15829966\t",
    "675\tGene\tBRCA2\tNCBI\t142\tGene\tPARP1\tNCBI\tSL\tGene_SL_Gene\tLow Throughput\t2\t\t15829967\tovarian cancer_D010051",
    "675\tGene\tBRCA2\tNCBI\t695\tGene\tBTK\tNCBI\tSL\tGene_SL_Gene\tHigh Throughput\t3\t\t29000001\t",
    "675\tGene\tBRCA2\tNCBI\t672\tGene\tBRCA1\tNCBI\tSL\tGene_SL_Gene\tLow Throughput\t4\t\t29000002\t",
    "675\tGene\tBRCA2\tNCBI\t51755\tGene\tCDK12\tNCBI\tSL\tGene_SL_Gene\tGenomeRNAi\t5\t\t29000003\t",
    "675\tGene\tBRCA2\tNCBI\t9999\tGene\tPREDX\tNCBI\tSL\tGene_SL_Gene\tComputational Prediction\t6\t\t25171417\t",
]


@pytest.fixture
def sl_file(monkeypatch, tmp_path):
    d = tmp_path / "synlethdb"
    d.mkdir()
    (d / "Human.SL.detailed.tsv").write_text("\n".join([SL_HEADER, *SL_ROWS]) + "\n", encoding="utf-8")
    monkeypatch.setenv("PHRONESIS_SYNLETHDB_DIR", str(d))
    return d


def _interaction(drug: str, approved: bool, score: float, types=(), pmids=(), sources=("ChEMBL",), concept: str = "rxcui:1") -> Dict[str, Any]:
    return {"drug": {"name": drug, "conceptId": concept, "approved": approved}, "interactionScore": score,
            "interactionTypes": [{"type": t, "directionality": None} for t in types],
            "publications": [{"pmid": p} for p in pmids], "sources": [{"sourceDbName": s} for s in sources]}


DGIDB_NODES = {
    "PARP1": {"name": "PARP1", "conceptId": "hgnc:270", "longName": "poly(ADP-ribose) polymerase 1", "interactions": [
        _interaction("VELIPARIB", False, 3.0, ("inhibitor",), (21000001,)),  # not approved: dropped
        _interaction("SCREENOL", True, 9.0, ("inhibitor",), (), ("ChEMBL",)),  # single source, no publication: dropped
        _interaction("OLAPARIB", True, 2.5, ("inhibitor",), (26510020,), ("ChEMBL", "DrugBank"), "chembl:CHEMBL521686"),
        _interaction("NIRAPARIB", True, 1.9, ("inhibitor",), (27717299,), ("ChEMBL", "GuideToPharmacology"), "chembl:CHEMBL1094636"),
    ]},
    "BTK": {"name": "BTK", "conceptId": "hgnc:1133", "longName": "Bruton tyrosine kinase", "interactions": [
        _interaction("CETUXIMAB", True, 5.0, (), (22000001,), ("CIViC", "CGI")),  # untyped: ranked after the inhibitor
        _interaction("IBRUTINIB", True, 1.2, ("inhibitor",), (23782157,), ("ChEMBL", "TTD"), "chembl:CHEMBL1873475"),
    ]},
    "CDK12": {"name": "CDK12", "conceptId": "hgnc:24224", "longName": "cyclin dependent kinase 12", "interactions": [
        _interaction("SR-4835", False, 2.0, ("inhibitor",), (31000001,)),
    ]},
}


def _sub(kind: str, number: str, date: str, cls: str) -> Dict[str, Any]:
    return {"submission_type": kind, "submission_number": number, "submission_status": "AP",
            "submission_status_date": date, "submission_class_code": cls}


def _fda_body(app: str, brand: str, ingredient: str, date: str) -> Dict[str, Any]:
    return {"meta": {}, "results": [{
        "application_number": app, "sponsor_name": "SPONSOR",
        "products": [{"brand_name": brand, "marketing_status": "Prescription",
                      "active_ingredients": [{"name": ingredient, "strength": "100MG"}]}],
        "submissions": [_sub("ORIG", "1", date, "TYPE 1")]}]}


FDA_BODIES = {
    "OLAPARIB": _fda_body("NDA206162", "LYNPARZA", "OLAPARIB", "20141219"),
    "IBRUTINIB": _fda_body("NDA205552", "IMBRUVICA", "IBRUTINIB", "20131113"),
}


class Upstream:
    """DGIdb (POST GraphQL) and Drugs@FDA (GET) behind one MockTransport, recording every request."""

    def __init__(self, dgidb_status: int = 200) -> None:
        self.dgidb_status = dgidb_status
        self.dgidb_requests: List[List[str]] = []
        self.fda_requests: List[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "dgidb.org":
            names = json.loads(request.content.decode("utf-8"))["variables"]["names"]
            self.dgidb_requests.append(names)
            if self.dgidb_status != 200:
                return httpx.Response(self.dgidb_status, json={"error": "down"})
            return httpx.Response(200, json={"data": {"genes": {"nodes": [DGIDB_NODES[n] for n in names if n in DGIDB_NODES]}}})
        if request.url.host == "api.fda.gov":
            search = dict(parse_qsl(request.url.query.decode()))["search"]
            self.fda_requests.append(search)
            for name, body in FDA_BODIES.items():
                if f'"{name}"' in search:
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        raise AssertionError(f"unexpected request {request.url}")


@pytest.fixture
def upstream(monkeypatch):
    dgidb.clear_cache()
    monkeypatch.setattr(openfda_client, "_CACHE", TTLCache(maxsize=64, ttl_s=60))

    def install(**kw: Any) -> Upstream:
        up = Upstream(**kw)
        monkeypatch.setattr(common, "TRANSPORT", httpx.MockTransport(up))
        return up

    yield install
    dgidb.clear_cache()


def _enriched(gene: str = "BRCA2", disease: str = "ovarian cancer") -> ToolResult:
    out = synlethdb.synlethdb_partners(gene, disease=disease, druggable_partners=True)
    assert inspect.isawaitable(out)
    return run(out)


# -----------------------------------------------------------------------------
# 1. Druggable synthetic-lethal partners
# -----------------------------------------------------------------------------

def test_approved_drugs_by_gene_is_one_batched_query_with_curated_approved_drugs(upstream):
    up = upstream()
    out = run(dgidb.approved_drugs_by_gene(["PARP1", "BTK", "CDK12", "NOTAGENE"], per_gene=5))
    assert up.dgidb_requests == [["PARP1", "BTK", "CDK12", "NOTAGENE"]]  # one request for every gene
    assert [d["name"] for d in out["PARP1"]] == ["OLAPARIB", "NIRAPARIB"]  # unapproved and single-source records dropped
    ola = out["PARP1"][0]
    assert ola["chembl_id"] == "CHEMBL521686" and ola["interaction_types"] == ["inhibitor"] and ola["pmids"] == ["26510020"]
    assert ola["interaction_class"] == "inhibits" and ola["approved"] and ola["sources"] == ["ChEMBL", "DrugBank"]
    # An inhibitor-type interaction ranks before an untyped (possibly biomarker) one with a higher score.
    assert [(d["name"], d["interaction_class"]) for d in out["BTK"]] == [("IBRUTINIB", "inhibits"), ("CETUXIMAB", "untyped")]
    assert out["CDK12"] == [] and out["NOTAGENE"] == []
    run(dgidb.approved_drugs_by_gene(["PARP1", "BTK", "CDK12", "NOTAGENE"], per_gene=5))
    assert len(up.dgidb_requests) == 1  # cached


def test_approved_drugs_by_gene_splits_large_batches(upstream):
    up = upstream()
    genes = ["PARP1", "BTK", *[f"GENE{i}" for i in range(10)]]
    out = run(dgidb.approved_drugs_by_gene(genes))
    assert sorted(len(r) for r in up.dgidb_requests) == [2, 10] and set(out) == set(genes)


def test_synlethdb_direct_call_stays_offline(sl_file, upstream):
    up = upstream()
    res = synlethdb.synlethdb_partners("BRCA2")
    assert isinstance(res, ToolResult) and res.success and "druggable_partners" not in res.data
    assert up.dgidb_requests == [] and up.fda_requests == []


def test_synlethdb_enriches_experimental_partners_with_approved_drugs_and_fda_dates(sl_file, upstream):
    up = upstream()
    res = _enriched()
    assert res.success
    # Experimental, non-tumour-suppressor partners only: BRCA1 (a lost caretaker) and the predicted PREDX are not
    # looked up, and the subject itself never is.
    assert up.dgidb_requests == [["PARP1", "BTK", "CDK12"]]
    d = res.data
    lookup = d["druggable_lookup"]
    assert lookup["status"] == "ok" and lookup["partners_checked"] == ["PARP1", "BTK", "CDK12"]
    assert lookup["excluded_tumour_suppressors"] == ["BRCA1"]
    rows = {r["partner"]: r for r in d["druggable_partners"]}
    assert list(rows) == ["PARP1", "BTK"]  # CDK12 has no approved drug
    parp1 = rows["PARP1"]
    assert parp1["evidence"] == ["CRISPR/CRISPRi", "Low Throughput"] and parp1["sl_pmids"][0] == "27453043"
    ola, nira = parp1["drugs"]
    assert ola["name"] == "OLAPARIB" and ola["chembl_id"] == "CHEMBL521686" and ola["pmids"] == ["26510020"]
    assert ola["fda"]["status"] == "approved" and ola["fda"]["first_approval_date"] == "2014-12-19"
    assert ola["fda"]["application_number"] == "NDA206162"
    assert rows["BTK"]["drugs"][0]["fda"]["first_approval_date"] == "2013-11-13"
    assert nira["fda"]["status"] == "not_found"  # looked up (leading drugs), absent from this fake Drugs@FDA
    assert "approval dates" in lookup["fda_dates"] or "first approval dates" in lookup["fda_dates"]
    # The druggable list sits before the long partner list (compact JSON views truncate from the end).
    keys = list(d)
    assert keys.index("druggable_partners") < keys.index("items")
    # Drug PMIDs and ChEMBL ids are citable.
    assert "26510020" in res.canonical_ids["pmids"] and "27453043" in res.canonical_ids["pmids"]
    assert "CHEMBL521686" in res.canonical_ids["chembl_ids"]
    assert any(c["id"] == "DGIdb:PARP1" for c in d["citation_records"])

    src = synlethdb.SynlethdbSource()
    xml = src.evidence_xml(res)
    assert "<druggable_partners" in xml and '<partner name="PARP1"' in xml
    assert 'name="OLAPARIB"' in xml and 'fda_first_approval="2014-12-19"' in xml and 'class="inhibits"' in xml
    assert "BRCA1" in xml and "not drugs acting on them" in xml
    ids = src.allowed_identifiers(res)
    assert {"26510020", "23782157", "27453043"} <= ids["pmids"] and "CHEMBL1873475" in ids["chembl"]
    compact = src.compact(res.data)
    assert compact["druggable_partners"][0]["partner"] == "PARP1"
    assert compact["druggable_partners"][0]["drugs"][0]["fda_first_approval"] == "2014-12-19"
    ctx = QueryContext(raw_query="druggable synthetic-lethal partners of BRCA2 loss", clean_query="druggable synthetic-lethal partners of BRCA2 loss")
    title, detail = src.describe(res, ctx)
    assert "druggable partners" in detail and "PARP1 (OLAPARIB, NIRAPARIB)" in detail and "BTK (IBRUTINIB" in detail


def test_synlethdb_enrichment_failure_keeps_the_partner_list(sl_file, upstream):
    up = upstream(dgidb_status=503)
    res = _enriched()
    assert res.success and len(res.data["items"]) == 5 and res.data["druggable_partners"] == []
    assert res.data["druggable_lookup"]["status"] == "failed" and "DGIdb" in res.data["druggable_lookup"]["detail"]
    assert len(up.dgidb_requests) == 2  # one retry after a 5xx, then degrade
    src = synlethdb.SynlethdbSource()
    assert 'status="failed"' in src.evidence_xml(res) and "outage" in src.evidence_xml(res)
    ctx = QueryContext(raw_query="synthetic lethal partners of BRCA2", clean_query="synthetic lethal partners of BRCA2")
    notes = src.scope_notes(ctx, res)
    assert any("not evidence the partners are undruggable" in n for n in notes)


def test_synlethdb_enrichment_timeout_degrades(sl_file, upstream, monkeypatch):
    upstream()

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(synlethdb, "find_druggable_partners", slow)
    monkeypatch.setattr(synlethdb, "ENRICH_BUDGET_S", 3.0)
    res = _enriched()
    assert res.success and res.data["druggable_lookup"]["status"] == "failed"
    assert "exceeded" in res.data["druggable_lookup"]["detail"]


def test_synlethdb_through_the_registry_awaits_the_enrichment(sl_file, upstream):
    upstream()
    reg = BiomedicalToolRegistry()
    reg.register(synlethdb.tool_entry(), synlethdb.synlethdb_partners)
    res = run(reg.execute(synlethdb.TOOL_NAME, gene="BRCA2", disease="ovarian cancer"))
    assert res.success and [r["partner"] for r in res.data["druggable_partners"]] == ["PARP1", "BTK"]


def test_hypotheses_prefer_partners_with_an_approved_drug_and_name_it():
    from phronesis.agentic.hypothesis import HypothesisPhase

    syn = ToolResult(success=True, tool_name=synlethdb.TOOL_NAME, data={
        "items": [
            {"partner": "CDK12", "experimental": True, "evidence_types": ["GenomeRNAi"], "pmids": ["29000003"]},
            {"partner": "BRCA1", "experimental": True, "evidence_types": ["Low Throughput"], "pmids": ["29000002"]},
            {"partner": "PARP1", "experimental": True, "evidence_types": ["CRISPR/CRISPRi"], "pmids": ["27453043"]},
        ],
        # BTK is ranked below the shown partner list but has an approved drug: it is still proposed.
        "druggable_partners": [
            {"partner": "PARP1", "evidence": ["CRISPR/CRISPRi"], "sl_pmids": ["27453043"], "drugs": [
                {"name": "OLAPARIB", "interaction_types": ["inhibitor"], "interaction_class": "inhibits",
                 "fda": {"status": "approved", "first_approval_date": "2014-12-19"}}]},
            {"partner": "BTK", "evidence": ["High Throughput"], "sl_pmids": ["29000001"], "drugs": [
                {"name": "IBRUTINIB", "interaction_types": ["inhibitor"], "interaction_class": "inhibits", "fda": {"status": "not_checked"}}]},
        ],
    })
    ctx = SimpleNamespace(primary_symbol="BRCA2", diseases=[SimpleNamespace(mention="ovarian cancer", label="ovarian cancer")], drugs=[])
    state = SimpleNamespace(ctx=ctx, clean_query="druggable synthetic-lethal partners of BRCA2 loss", query="",
                            tool_results={"synlethdb": syn})
    hyps = HypothesisPhase()._synthetic_lethal_hypotheses(state, "BRCA2")
    assert [h.parameter for h in hyps] == ["PARP1", "BTK", "CDK12"]  # druggable first; BRCA1 never inhibited
    assert "olaparib (inhibitor, FDA first approval 2014-12-19)" in hyps[0].mechanism
    assert "ibrutinib (inhibitor)" in hyps[1].mechanism and "PMID 29000001" in hyps[1].mechanism
    assert "approved drug" not in hyps[2].mechanism
    assert hyps[0].statement == "Inhibiting PARP1 is predicted to be selectively lethal in BRCA2-deficient ovarian cancer."
    assert hyps[0].evidence_score > hyps[2].evidence_score


def _dgidb_result(gene: str) -> ToolResult:
    return ToolResult(success=True, tool_name=dgidb.TOOL_NAME, data={
        "subject": gene, "genes": [{"gene": gene, "concept_id": "hgnc:1", "drugs_total": 12, "approved_total": 8, "shown": 1}],
        "missing_genes": [], "items": [{"gene": gene, "drug": "OLAPARIB", "approved": True, "score": 1.0,
                                       "interaction_types": [], "pmids": [], "sources": ["CIViC"], "chembl_id": None}],
    })


def test_dgidb_labels_a_lost_tumour_suppressor_as_associations():
    src = dgidb.DgidbSource()
    ctx = QueryContext(raw_query="drugs for BRCA2 loss", clean_query="drugs for BRCA2 loss")
    res = _dgidb_result("BRCA2")
    title, _ = src.describe(res, ctx)
    assert "BRCA2: 12 drug–gene associations (mostly biomarker/indirect), not drugs that act on BRCA2" in title
    xml = src.evidence_xml(res)
    assert 'role="lost tumour suppressor / loss-of-function gene"' in xml and "they do not act on BRCA2" in xml
    assert "not drugs that act on BRCA2" in src.compact(res.data)["note"]
    # An oncogene keeps the plain summary.
    kras = _dgidb_result("KRAS")
    assert "drugs (8 approved)" in src.describe(kras, ctx)[0] and "role=" not in src.evidence_xml(kras)
    assert "note" not in src.compact(kras.data)


def test_dgidb_source_labels_a_gene_the_question_calls_lost(upstream):
    upstream()

    class Reg:
        async def execute(self, _name: str, **_kw: Any) -> ToolResult:
            return _dgidb_result("MYC")

    gene = ResolvedEntity(type="gene", mention="MYC", label="MYC", ids={}, validated=True, source="HGNC")
    ctx = QueryContext(raw_query="Which drugs exploit MYC loss in tumours?", clean_query="Which drugs exploit MYC loss in tumours?",
                       entities=[gene], primary_gene=gene, primary_target="MYC")
    src = dgidb.DgidbSource()
    res = run(src.run(ctx, Reg(), None))
    assert res.data["loss_of_function_genes"] == ["MYC"]
    assert "not drugs that act on MYC" in src.describe(res, ctx)[0]


# -----------------------------------------------------------------------------
# 2. Question-named trials first
# -----------------------------------------------------------------------------

SOLO1, OTHERS = "NCT01844986", [f"NCT0200000{i}" for i in range(8)]


def _study(nct: str, title: str, acronym: str = None, phase: str = "PHASE3", has_results: bool = True,
           enrollment: int = 100, official: str = None) -> Dict[str, Any]:
    return {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": title, "officialTitle": official or title, "acronym": acronym},
        "statusModule": {"overallStatus": "COMPLETED"},
        "designModule": {"phases": [phase], "studyType": "INTERVENTIONAL", "enrollmentInfo": {"count": enrollment, "type": "ACTUAL"}},
    }, "hasResults": has_results}


def _results(nct: str) -> Dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct}}, "hasResults": True, "resultsSection": {
        "outcomeMeasuresModule": {"outcomeMeasures": [{
            "type": "PRIMARY", "title": "Progression-free Survival", "reportingStatus": "POSTED", "unitOfMeasure": "months",
            "groups": [{"id": "OG000", "title": "Olaparib"}],
            "classes": [{"categories": [{"measurements": [{"groupId": "OG000", "value": "NA"}]}]}],
            "analyses": [{"groupIds": ["OG000"], "paramType": "Hazard Ratio (HR)", "paramValue": "0.30", "pValue": "<0.0001"}],
        }]}}}


class CtUpstream:
    def __init__(self, studies: List[Dict[str, Any]], titled: List[Dict[str, Any]] = ()) -> None:
        self.studies, self.titled = studies, list(titled)
        self.requests: List[Dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.url.query.decode()))
        self.requests.append(params)
        if params.get("fields") == ct.RESULT_FIELDS:
            wanted = params["filter.ids"].split(",")
            return httpx.Response(200, json={"studies": [_results(n) for n in wanted]})
        if "query.titles" in params:
            return httpx.Response(200, json={"studies": self.titled, "totalCount": len(self.titled)})
        studies = [s for s in self.studies if params.get("aggFilters") != "results:with" or s["hasResults"]]
        return httpx.Response(200, json={"studies": studies, "totalCount": len(studies)})

    def client(self, **kw: Any) -> ct.ClinicalTrialsGovClient:
        return ct.ClinicalTrialsGovClient(httpx.MockTransport(self), cache=TTLCache(ttl_s=600), retry_backoff_s=0, **kw)


def _landscape() -> List[Dict[str, Any]]:
    # Eight larger phase 3 trials with posted results outrank SOLO-1 on enrollment; SOLO-1 is listed last.
    out = [_study(n, f"Olaparib trial {i}", enrollment=900) for i, n in enumerate(OTHERS)]
    out.append(_study(SOLO1, "Olaparib Maintenance Monotherapy in BRCA Mutated Ovarian Cancer", "SOLO-1", enrollment=391))
    return out


def test_trial_name_matching():
    rec = ct.parse_trial(_study(SOLO1, "Olaparib maintenance", "SOLO1"))
    assert ct.named_trial(rec, ["SOLO-1"]) == "SOLO-1"
    assert ct.named_trial(ct.parse_trial(_study(OTHERS[0], "x", "SOLO-12")), ["SOLO-1"]) is None
    by_title = ct.parse_trial(_study(OTHERS[1], "A study", official="Phase III PAOLA-1 trial of olaparib plus bevacizumab"))
    assert ct.named_trial(by_title, ["PAOLA-1"]) == "PAOLA-1"
    # A bare-word name matches the acronym only (titles use such words in prose).
    assert ct.named_trial(ct.parse_trial(_study(OTHERS[2], "Prima facie evidence study")), ["PRIMA"]) is None
    assert ct.trial_name_pattern("CheckMate 76K").search("CHECKMATE-76K")


@pytest.mark.parametrize("fetch_size", [None, ct.FETCH_SIZE])
def test_named_trial_is_listed_first_and_its_results_fetched(fetch_size):
    up = CtUpstream(_landscape())
    res = run(ct.search_trials(client=up.client(fetch_size=fetch_size), intervention="olaparib", condition="ovarian cancer",
                               trial_names=["SOLO-1"], max_trials=10))
    assert res.success
    trials = res.data["trials"]
    assert trials[0]["nct_id"] == SOLO1 and trials[0]["acronym"] == "SOLO-1"
    assert SOLO1 in res.data["results_fetched_for"] and len(res.data["results_fetched_for"]) == 5
    batch = next(p for p in up.requests if p.get("fields") == ct.RESULT_FIELDS)
    assert batch["filter.ids"].split(",")[0] == SOLO1
    solo = trials[0]
    assert solo["posted_results"][0]["analyses"][0]["param_value"] == "0.30"
    assert res.data["query"]["trial_names"] == ["SOLO-1"]
    assert not any("query.titles" in p for p in up.requests)  # found by the searches: no extra lookup


def test_without_trial_names_the_ranking_is_unchanged():
    up = CtUpstream(_landscape())
    res = run(ct.search_trials(client=up.client(fetch_size=ct.FETCH_SIZE), intervention="olaparib", condition="ovarian cancer"))
    assert res.data["trials"][0]["nct_id"] != SOLO1 and SOLO1 not in res.data["results_fetched_for"]


def test_named_trial_missing_from_the_searches_is_looked_up_by_title():
    others = [s for s in _landscape() if s["protocolSection"]["identificationModule"]["nctId"] != SOLO1]
    solo = _study(SOLO1, "Olaparib Maintenance Monotherapy in BRCA Mutated Ovarian Cancer", "SOLO-1")
    up = CtUpstream(others, titled=[solo, _study("NCT09999999", "Unrelated", "OTHER-2")])
    res = run(ct.search_trials(client=up.client(fetch_size=ct.FETCH_SIZE), intervention="olaparib", condition="ovarian cancer",
                               trial_names=["SOLO-1", "NOVA-9"], max_trials=5))
    titles = next(p for p in up.requests if "query.titles" in p)
    assert titles["query.titles"] == '"SOLO-1" OR "NOVA-9"'
    ids = [t["nct_id"] for t in res.data["trials"]]
    assert ids[0] == SOLO1 and "NCT09999999" not in ids and len(ids) == 5
    assert SOLO1 in res.data["results_fetched_for"]
    lim = " ".join(res.data["limitations"])
    assert f"SOLO-1 ({SOLO1})" in lim and "No registered trial with the acronym or title NOVA-9" in lim


def test_trial_query_plan_passes_question_trial_names():
    q = ("Summarize the SOLO-1 phase 3 trial result (PFS HR) for olaparib maintenance in BRCA-mutated ovarian cancer "
         "and report FAERS/label warnings for olaparib")
    plan = plan_trial_query(q)
    assert plan is not None and plan.trial_names == ["SOLO-1"]
    kwargs = plan.to_tool_kwargs()
    assert kwargs["trial_names"] == ["SOLO-1"]
    assert ct.ClinicalTrialsSearchInput(**kwargs).trial_names == ["SOLO-1"]
    plain = plan_trial_query("olaparib trials in ovarian cancer")
    assert plain is not None and "trial_names" not in plain.to_tool_kwargs()


def test_trial_names_input_is_cleaned():
    inp = ct.ClinicalTrialsSearchInput(intervention="olaparib", trial_names='SOLO-1; "PAOLA-1", solo-1, x')
    assert inp.trial_names == ["SOLO-1", "PAOLA-1"]
