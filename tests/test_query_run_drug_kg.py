"""
Query-run repairs for the drug (ChEMBL / openFDA) and PrimeKG evidence, fully offline:

  1.6  PrimeKG analyses are labelled with disease names, never 'MONDO_0005184'; two named diseases get two profiles.
  1.7  A drug queried by ChEMBL id keeps the user's mention ("MRTX1133") for the card and the evidence.
  2.2  A target with no approved drug (MYC) still lists the compounds ChEMBL records activity for.
  2.3  Mechanisms recorded on the parent and on a salt form appear once.
  2.4  FAERS top reactions exclude death, progression, off-label and product-use terms.
  2.5  A gene profile ranks the question's disease and its drugs before global hubs.
"""

from __future__ import annotations

import asyncio
import csv
from typing import Any, Dict, List, Optional

import httpx
import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.drugintel import checks, names
from phronesis.drugintel.chembl_client import ChemblClient, clear_release_cache
from phronesis.drugintel.engine import DrugIntelEngine, is_non_reaction_term
from phronesis.drugintel.openfda_client import OpenFdaClient
from phronesis.drugintel.render import bioactive_summary, chembl_evidence_xml, safety_evidence_xml
from phronesis.drugintel.sources import ChemblSource
from phronesis.extensions import primekg as primekg_ext
from phronesis.kgintel import service
from phronesis.kgintel.graph import PrimeKG, reset_primekg
from phronesis.kgintel.planner import plan_investigation
from phronesis.kgintel.render import evidence_xml
from phronesis.kgintel.store import build_store
from phronesis.sources import SourceRunState
from phronesis.tools.registry import ToolResult
from phronesis.utils.http_limits import TTLCache
from tests.fakes import DrugToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_release_cache():
    clear_release_cache()
    yield
    clear_release_cache()


# -----------------------------------------------------------------------------
# Fake ChEMBL / openFDA
# -----------------------------------------------------------------------------

MOLECULES: Dict[str, Dict[str, Any]] = {
    # MRTX1133 has no ChEMBL preferred name: the card must not fall back to its id.
    "CHEMBL5081048": {"molecule_chembl_id": "CHEMBL5081048", "pref_name": None, "max_phase": None,
                      "molecule_type": "Small molecule", "molecule_hierarchy": {"parent_chembl_id": "CHEMBL5081048"}},
    "CHEMBL1094636": {"molecule_chembl_id": "CHEMBL1094636", "pref_name": "NIRAPARIB", "max_phase": "4.0",
                      "molecule_type": "Small molecule", "first_approval": 2017,
                      "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1094636"}},
    "CHEMBL100": {"molecule_chembl_id": "CHEMBL100", "pref_name": "OMOMYC-LIKE PROBE", "max_phase": None,
                  "molecule_type": "Small molecule"},
    "CHEMBL200": {"molecule_chembl_id": "CHEMBL200", "pref_name": None, "max_phase": "1.0", "molecule_type": "Small molecule"},
}

MYC = "CHEMBL1250348"
KRAS = "CHEMBL2189121"
PARP1, PARP2 = "CHEMBL3105", "CHEMBL5366"


def _activity(aid: int, mol: str, parent: str, pchembl: float, stype: str = "IC50", value: float = 100.0,
              name: Optional[str] = None) -> Dict[str, Any]:
    return {"activity_id": aid, "assay_chembl_id": f"CHEMBL9{aid}", "assay_description": "binding assay",
            "assay_type": "B", "document_chembl_id": "CHEMBL777", "molecule_chembl_id": mol,
            "parent_molecule_chembl_id": parent, "target_chembl_id": MYC, "target_organism": "Homo sapiens",
            "standard_type": stype, "standard_relation": "=", "standard_value": str(value), "standard_units": "nM",
            "pchembl_value": str(pchembl), "data_validity_comment": None, "molecule_pref_name": name}


class Upstream:
    def __init__(self, faers_terms: Optional[List[Dict[str, Any]]] = None) -> None:
        self.requests: List[httpx.Request] = []
        self.faers_terms = faers_terms or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.fda.gov":
            return self._openfda(request.url.params)
        return self._chembl(request.url.path, request.url.params)

    def _chembl(self, path: str, q: httpx.QueryParams) -> httpx.Response:
        if path.endswith("/status.json"):
            return httpx.Response(200, json={"chembl_db_version": "ChEMBL_37"})
        if path.endswith("/molecule.json"):
            if "molecule_chembl_id__in" in q:
                ids = q["molecule_chembl_id__in"].split(",")
                return httpx.Response(200, json={"molecules": [MOLECULES[i] for i in ids if i in MOLECULES]})
            return httpx.Response(200, json={"molecules": []})
        if "/molecule/" in path:
            mid = path.rsplit("/", 1)[-1].removesuffix(".json")
            return httpx.Response(200, json=MOLECULES[mid]) if mid in MOLECULES else httpx.Response(404)
        if path.endswith("/mechanism.json"):
            if q.get("max_phase") == "4":
                return httpx.Response(200, json={"mechanisms": [], "page_meta": {"total_count": 0}})
            if q.get("parent_molecule_chembl_id") == "CHEMBL1094636":
                rows = [
                    # The same PARP1 mechanism recorded on the tosylate salt and on the parent, and PARP2 once.
                    {"molecule_chembl_id": "CHEMBL3989908", "parent_molecule_chembl_id": "CHEMBL1094636",
                     "mechanism_of_action": "Poly [ADP-ribose] polymerase-1 inhibitor", "action_type": "INHIBITOR",
                     "target_chembl_id": PARP1, "max_phase": 4},
                    {"molecule_chembl_id": "CHEMBL1094636", "parent_molecule_chembl_id": "CHEMBL1094636",
                     "mechanism_of_action": "Poly [ADP-ribose] polymerase-1 inhibitor", "action_type": "INHIBITOR",
                     "target_chembl_id": PARP1, "max_phase": 4},
                    {"molecule_chembl_id": "CHEMBL3989908", "parent_molecule_chembl_id": "CHEMBL1094636",
                     "mechanism_of_action": "Poly [ADP-ribose] polymerase 2 inhibitor", "action_type": "INHIBITOR",
                     "target_chembl_id": PARP2, "max_phase": 4},
                ]
                return httpx.Response(200, json={"mechanisms": rows})
            return httpx.Response(200, json={"mechanisms": []})
        if path.endswith("/target.json"):
            if "target_chembl_id__in" in q:
                known = {MYC: "Myc proto-oncogene protein", KRAS: "GTPase KRas", PARP1: "PARP 1", PARP2: "PARP 2"}
                ids = q["target_chembl_id__in"].split(",")
                return httpx.Response(200, json={"targets": [{"target_chembl_id": i, "pref_name": known[i]} for i in ids if i in known]})
            sym = q.get("target_components__target_component_synonyms__component_synonym__iexact")
            tid = {"MYC": MYC, "KRAS": KRAS}.get(sym or "")
            return httpx.Response(200, json={"targets": [{"target_chembl_id": tid, "pref_name": sym}] if tid else []})
        if path.endswith("/activity.json"):
            if q.get("target_chembl_id") == MYC and "parent_molecule_chembl_id" not in q:
                rows = [
                    _activity(1, "CHEMBL100", "CHEMBL100", 6.8, name="OMOMYC-LIKE PROBE"),
                    _activity(2, "CHEMBL201", "CHEMBL200", 6.1, value=794.0),  # a salt form of CHEMBL200
                    _activity(3, "CHEMBL100", "CHEMBL100", 5.2, value=6310.0),
                    _activity(4, "CHEMBL200", "CHEMBL200", 5.9, stype="Kd", value=1259.0),
                ]
                return httpx.Response(200, json={"activities": rows, "page_meta": {"total_count": len(rows)}})
            return httpx.Response(200, json={"activities": [], "page_meta": {"total_count": 0}})
        raise AssertionError(f"unexpected ChEMBL request {path}?{q}")

    def _openfda(self, q: httpx.QueryParams) -> httpx.Response:
        meta = {"disclaimer": "Do not rely on openFDA to make decisions regarding medical care.", "last_updated": "2026-07-30"}
        if q.get("count") == "patient.reaction.reactionmeddrapt.exact":
            limit = int(q["limit"])
            return httpx.Response(200, json={"meta": meta, "results": self.faers_terms[:limit]})
        if q.get("count") == "serious":
            return httpx.Response(200, json={"meta": meta, "results": [{"term": "1", "count": 9000}, {"term": "2", "count": 1000}]})
        return httpx.Response(200, json={"meta": {**meta, "results": {"total": 10000}}, "results": [{}]})


def make_engine(upstream: Optional[Upstream] = None):
    upstream = upstream or Upstream()
    transport = httpx.MockTransport(upstream)
    return DrugIntelEngine(ChemblClient(transport=transport),
                           OpenFdaClient(transport=transport, api_key="", cache=TTLCache())), upstream




def _ctx(query: str, entities: List[ResolvedEntity] = (), target: Optional[str] = None) -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query, entities=list(entities), primary_target=target)


async def _chembl_run(ctx: QueryContext, upstream: Optional[Upstream] = None):
    engine, up = make_engine(upstream)
    reg = DrugToolRegistry(engine)
    res = await ChemblSource().run(ctx, reg, SourceRunState(reg, ctx))
    return res, up


# -----------------------------------------------------------------------------
# 1.7 the user's drug name survives a ChEMBL-id lookup
# -----------------------------------------------------------------------------

MRTX = ResolvedEntity(type="drug", mention="MRTX1133", label="MRTX-1133", ids={"chembl_id": "CHEMBL5081048"},
                      source="ChEMBL", validated=True)


def test_display_names_map_chembl_ids_to_the_users_words():
    ctx = _ctx("What does MRTX1133 do?", [MRTX])
    assert names.drug_mentions_for(ctx) == ["CHEMBL5081048"]
    assert names.drug_display_names(ctx) == {"CHEMBL5081048": "MRTX1133"}


def test_drug_queried_by_chembl_id_keeps_its_mention():
    ctx = _ctx("Mechanism of action of MRTX1133", [MRTX])
    res, _ = run(_chembl_run(ctx))
    assert res.success, res.error
    drug = res.data["drugs"][0]
    assert drug["query_name"] == "CHEMBL5081048" and drug["pref_name"] is None
    assert drug["mention"] == "MRTX1133"
    title, _ = ChemblSource().describe(res, ctx)
    assert title.startswith("Resolved MRTX1133 to CHEMBL5081048")
    assert 'user_name="MRTX1133"' in chembl_evidence_xml(res.data)
    key, art = ChemblSource().artifact(res)
    assert key == "drug_intel" and art["chembl"]["drugs"][0]["mention"] == "MRTX1133"


# -----------------------------------------------------------------------------
# 2.2 compounds with measured activity when no approved drug acts on the target
# -----------------------------------------------------------------------------

def test_target_without_approved_drugs_lists_bioactive_compounds():
    engine, up = make_engine()
    res = run(engine.drug_profile(target_chembl_id=MYC, target_symbol="MYC"))
    assert res.success, res.error
    d = res.data
    assert d["target_approved_drugs"] == [] and d["target_approved_total"] == 0
    compounds = d["target_bioactive_compounds"]
    # Deduplicated by parent (the salt CHEMBL201 is CHEMBL200), best pChEMBL kept, best first.
    assert [c["chembl_id"] for c in compounds] == ["CHEMBL100", "CHEMBL200"]
    assert compounds[0]["best_pchembl"] == 6.8 and compounds[0]["records"] == 2
    assert compounds[0]["pref_name"] == "OMOMYC-LIKE PROBE"
    assert compounds[1]["best_pchembl"] == 6.1 and compounds[1]["max_phase"] == 1.0
    assert d["target_bioactive_distinct"] == 2 and d["target_activity_records_total"] == 4
    act = [r for r in up.requests if r.url.path.endswith("/activity.json")][0].url.params
    assert act["target_chembl_id"] == MYC and "parent_molecule_chembl_id" not in act
    assert act["standard_type__in"] == "IC50,Ki,Kd,EC50" and act["order_by"] == "-pchembl_value"
    assert "CHEMBL100" in res.canonical_ids["chembl_ids"]

    summary = bioactive_summary(d)
    assert summary.startswith("0 approved drugs act on MYC")
    assert "ChEMBL lists 2 compounds with measured IC50/Ki/Kd/EC50 activity against MYC" in summary
    assert "OMOMYC-LIKE PROBE (CHEMBL100, pChEMBL 6.8)" in summary
    xml = chembl_evidence_xml(d)
    assert "<target_summary>0 approved drugs act on MYC" in xml and "<bioactive_compounds_for_target" in xml
    assert 'chembl_id="CHEMBL200"' in xml and 'best_pchembl="6.1"' in xml


def test_myc_question_routes_to_chembl_and_never_reads_as_no_compounds():
    query = "Are there PROTAC degraders or small molecules targeting MYC?"
    assert names.has_drug_intent(query) and names.has_degrader_intent(query)
    assert not names.has_drug_intent("KRAS signaling in pancreatic cancer")
    ctx = _ctx(query, target="MYC")
    assert ChemblSource().should_run(ctx).run
    res, _ = run(_chembl_run(ctx))
    assert res.success and res.data["degrader_question"] is True
    title, detail = ChemblSource().describe(res, ctx)
    assert title == f"Found 0 approved drugs acting on MYC ({MYC}); 2 compounds with measured activity"
    assert "top compounds: OMOMYC-LIKE PROBE, CHEMBL200" in detail
    notes = ChemblSource().scope_notes(ctx, res)
    assert any(n.startswith("0 approved drugs act on MYC") and "2 compounds" in n for n in notes)
    assert any("does not label compounds as PROTACs" in n for n in notes)
    assert "ChEMBL does not label compounds as PROTACs" in chembl_evidence_xml(res.data)
    key, art = ChemblSource().artifact(res)
    assert key == "drug_intel" and len(art["chembl"]["target_bioactive_compounds"]) == 2
    # Potency values of these compounds are retrieved evidence for the verifier.
    values = checks.chembl_activity_values({"chembl": res})
    assert 6.8 in values["pchembl"] and 794.0 in values["nm"]


def test_named_drug_does_not_fetch_target_wide_activities():
    engine, up = make_engine()
    res = run(engine.drug_profile(parent_chembl_ids=["CHEMBL5081048"], target_chembl_id=MYC))
    assert res.success and res.data["target_bioactive_compounds"] == []
    assert all("parent_molecule_chembl_id" in r.url.params for r in up.requests if r.url.path.endswith("/activity.json"))


# -----------------------------------------------------------------------------
# 2.3 salt-form mechanisms collapse into the parent's
# -----------------------------------------------------------------------------

def test_mechanisms_on_parent_and_salt_are_deduplicated():
    engine, _ = make_engine()
    res = run(engine.drug_profile(parent_chembl_ids=["CHEMBL1094636"]))
    mechs = res.data["drugs"][0]["mechanisms"]
    assert [(m["target_chembl_id"], m["form_chembl_id"]) for m in mechs] == [
        (PARP1, "CHEMBL1094636"),  # the parent-form row wins over the salt row
        (PARP2, "CHEMBL3989908"),
    ]
    assert "CHEMBL3989908" in res.data["drugs"][0]["forms"]


# -----------------------------------------------------------------------------
# 2.4 FAERS: non-reaction terms are excluded, deaths reported separately
# -----------------------------------------------------------------------------

FAERS_TERMS = [
    {"term": "DEATH", "count": 3000}, {"term": "MALIGNANT NEOPLASM PROGRESSION", "count": 2500},
    {"term": "OFF LABEL USE", "count": 2000}, {"term": "NAUSEA", "count": 1500},
    {"term": "PRODUCT USE IN UNAPPROVED INDICATION", "count": 1400}, {"term": "FATIGUE", "count": 1300},
    {"term": "DRUG INEFFECTIVE", "count": 1200}, {"term": "THROMBOCYTOPENIA", "count": 1100},
    {"term": "PRODUCT PREPARATION ISSUE", "count": 1050}, {"term": "ANAEMIA", "count": 1000},
] + [{"term": f"REACTION {i}", "count": 900 - i} for i in range(20)]


def test_faers_excludes_non_reaction_terms_and_reports_deaths_separately():
    engine, up = make_engine(Upstream(faers_terms=FAERS_TERMS))
    res = run(engine.drug_safety("niraparib", include_label=False))
    assert res.success, res.error
    d = res.data
    count_req = [r for r in up.requests if r.url.params.get("count") == "patient.reaction.reactionmeddrapt.exact"][0]
    assert int(count_req.url.params["limit"]) >= 25
    terms = [r["term"] for r in d["top_reactions"]]
    assert terms[:4] == ["NAUSEA", "FATIGUE", "THROMBOCYTOPENIA", "ANAEMIA"] and len(terms) == 10
    assert not any(is_non_reaction_term(t) for t in terms)
    assert d["deaths_reported"] == 3000
    assert d["excluded_terms"] == 6
    assert {r["term"] for r in d["excluded_reactions"]} >= {"DEATH", "OFF LABEL USE", "PRODUCT PREPARATION ISSUE"}
    xml = safety_evidence_xml(d)
    assert 'reports_listing_death="3000"' in xml and '<reaction term="DEATH"' not in xml
    assert '<excluded_non_reaction_terms count="6">' in xml
    # The excluded counts are still retrieved numbers for the verifier.
    assert {3000, 2500, 1500} <= checks.faers_counts({"openfda": res})


@pytest.mark.parametrize("term,excluded", [
    ("DEATH", True), ("Disease progression", True), ("INTENTIONAL PRODUCT USE ISSUE", True),
    ("PRODUCT STORAGE ISSUE", True), ("OFF LABEL USE", True), ("NAUSEA", False), ("DRUG INTOLERANCE", False),
])
def test_non_reaction_terms(term, excluded):
    assert is_non_reaction_term(term) is excluded


# -----------------------------------------------------------------------------
# PrimeKG fixture: one gene, a context disease with a MONDO parent, and better-connected hubs
# -----------------------------------------------------------------------------

NODES = [
    (0, "3845", "gene/protein", "KRAS", "NCBI"),
    (1, "5184", "disease", "pancreatic ductal adenocarcinoma", "MONDO"),
    (2, "4192", "disease", "pancreatic carcinoma", "MONDO"),
    (3, "5061", "disease", "lung adenocarcinoma", "MONDO"),
    (4, "21100", "disease", "breast neoplasm", "MONDO"),
    (5, "3450", "disease", "hereditary breast ovarian cancer syndrome", "MONDO"),
    (6, "DB01593", "drug", "Zinc", "DrugBank"),
    (7, "DB00530", "drug", "Erlotinib", "DrugBank"),
    (8, "DB15569", "drug", "Sotorasib", "DrugBank"),
    (9, "2018", "effect/phenotype", "Nausea", "HPO"),
    (10, "12378", "effect/phenotype", "Fatigue", "HPO"),
    (11, "1903", "effect/phenotype", "Anemia", "HPO"),
    (12, "2315", "effect/phenotype", "Headache", "HPO"),
    (13, "1945", "effect/phenotype", "Fever", "HPO"),
    (14, "2014", "effect/phenotype", "Vomiting", "HPO"),
]
EDGES = (
    [("disease_protein", "associated with", d, 0) for d in (1, 2, 3, 4, 5)]
    + [("drug_protein", "target", d, 0) for d in (6, 7, 8)]
    + [("disease_disease", "parent-child", 1, 2), ("disease_disease", "parent-child", 4, 5)]
    + [("indication", "indication", 7, 2), ("indication", "indication", 8, 3)]
    + [("disease_phenotype_positive", "phenotype present", 4, p) for p in range(9, 15)]
    + [("disease_phenotype_positive", "phenotype present", 5, p) for p in range(9, 14)]
    + [("drug_effect", "side effect", 6, p) for p in range(9, 15)]
)
DISEASE_COLS = ["node_index", "mondo_id", "mondo_name", "group_id_bert", "group_name_bert", "mondo_definition",
                "umls_description", "orphanet_definition", "orphanet_prevalence", "orphanet_epidemiology",
                "orphanet_clinical_description", "orphanet_management_and_treatment", "mayo_symptoms", "mayo_causes",
                "mayo_risk_factors", "mayo_complications", "mayo_prevention", "mayo_see_doc"]
DRUG_COLS = ["node_index", "description", "half_life", "indication", "mechanism_of_action", "protein_binding",
             "pharmacodynamics", "state", "atc_1", "atc_2", "atc_3", "atc_4", "category", "group", "pathway",
             "molecular_weight", "tpsa", "clogp"]


def _write_raw(raw) -> None:
    raw.mkdir(parents=True)
    with open(raw / "nodes.tab", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["node_index", "node_id", "node_type", "node_name", "node_source"])
        w.writerows(NODES)
    with open(raw / "edges.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["relation", "display_relation", "x_index", "y_index"])
        for rel, disp, x, y in EDGES:
            w.writerow([rel, disp, x, y])
            w.writerow([rel, disp, y, x])
    with open(raw / "disease_features.tab", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, DISEASE_COLS)
        w.writeheader()
        for idx, nid, _t, name, _s in NODES:
            if _t == "disease":
                w.writerow({"node_index": idx, "mondo_id": nid, "mondo_name": name})
    with open(raw / "drug_features.tab", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, DRUG_COLS)
        w.writeheader()
        for idx, name in ((6, "Zinc"), (7, "Erlotinib"), (8, "Sotorasib")):
            w.writerow({"node_index": idx, "group": f"{name} is approved."})


@pytest.fixture(scope="module")
def kg_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("primekg_ctx")
    _write_raw(root / "raw")
    build_store(root, download=False, log=lambda _m: None)
    return root


@pytest.fixture(scope="module")
def kg(kg_root) -> PrimeKG:
    return PrimeKG(kg_root)


@pytest.fixture
def active_store(kg_root, monkeypatch):
    monkeypatch.setenv("PHRONESIS_PRIMEKG_DIR", str(kg_root))
    reset_primekg()
    yield kg_root
    reset_primekg()


def _names(section: Dict[str, Any]) -> List[str]:
    return [n["name"] for n in section["items"]]


# -----------------------------------------------------------------------------
# 2.5 context-ranked gene profile
# -----------------------------------------------------------------------------

def test_gene_profile_without_context_keeps_degree_order(kg):
    res = service.gene_profile("KRAS", limit=10, kg=kg)["result"]
    assert _names(res["diseases"])[:2] == ["breast neoplasm", "hereditary breast ovarian cancer syndrome"]
    assert _names(res["drugs"]["target"])[0] == "Zinc"
    assert "context" not in res and not any(n.get("context_match") for n in res["diseases"]["items"])


def test_gene_profile_ranks_the_questions_disease_first(kg):
    res = service.gene_profile("KRAS", limit=10, context_diseases=["MONDO_0005184"], kg=kg)["result"]
    diseases = _names(res["diseases"])
    # The disease, then its MONDO parent, then a disease sharing a specific word, then the hubs.
    assert diseases == ["pancreatic ductal adenocarcinoma", "pancreatic carcinoma", "lung adenocarcinoma",
                        "breast neoplasm", "hereditary breast ovarian cancer syndrome"]
    assert [bool(n.get("context_match")) for n in res["diseases"]["items"]] == [True, True, True, False, False]
    assert res["diseases"]["context_matches"] == 3 and res["diseases"]["total"] == 5
    # Drugs indicated for the context disease family come before the promiscuous hub.
    assert _names(res["drugs"]["target"]) == ["Erlotinib", "Sotorasib", "Zinc"]
    assert res["drugs"]["target"]["items"][0]["context_match"] is True
    assert res["context"]["diseases"][0]["name"] == "pancreatic ductal adenocarcinoma"


def test_gene_profile_ignores_unresolvable_context(kg):
    res = service.gene_profile("KRAS", limit=10, context_diseases=["not a disease xyz"], kg=kg)["result"]
    assert _names(res["drugs"]["target"])[0] == "Zinc" and "context" not in res


# -----------------------------------------------------------------------------
# 1.6 disease names in PrimeKG reasons; two diseases profiled; gene profile carries the context
# -----------------------------------------------------------------------------

def test_planner_labels_mondo_ids_with_names_and_passes_context(kg):
    plan = plan_investigation(kg, "What is the role of KRAS in this cancer?", diseases=["MONDO_0005184"], genes=["KRAS"])
    reasons = {op: reason for op, _, reason in plan.analyses}
    assert reasons["disease_profile"] == "PrimeKG profile of pancreatic ductal adenocarcinoma"
    assert "MONDO" not in " ".join(reasons.values())
    gp = next(kw for op, kw, _ in plan.analyses if op == "gene_profile")
    assert gp["context_diseases"] == ["MONDO_0005184"]
    assert reasons["gene_profile"] == "PrimeKG profile of KRAS, ranked for pancreatic ductal adenocarcinoma"


def test_planner_prefers_the_callers_disease_name_and_profiles_two_diseases(kg):
    plan = plan_investigation(kg, "How does pembrolizumab compare across these cancers?", drugs=["PEMBROLIZUMAB"],
                              diseases=["MONDO_0005184", "MONDO_0005061"],
                              disease_names=["PDAC (pancreatic ductal adenocarcinoma)", "lung adenocarcinoma"])
    reasons = [reason for _, _, reason in plan.analyses]
    assert "how PEMBROLIZUMAB relates to PDAC (pancreatic ductal adenocarcinoma)" in reasons
    profiles = [kw["disease"] for op, kw, _ in plan.analyses if op == "disease_profile"]
    assert profiles == ["MONDO_0005184", "MONDO_0005061"]
    assert "PrimeKG profile of lung adenocarcinoma" in reasons
    assert not any("MONDO_" in r for r in reasons)


def test_source_sends_disease_names_and_investigation_reasons_use_them(active_store):
    disease = ResolvedEntity(type="disease", mention="PDAC", label="pancreatic ductal adenocarcinoma",
                             ids={"ontology_id": "MONDO_0005184"}, source="OpenTargets", validated=True)
    gene = ResolvedEntity(type="gene", mention="KRAS", label="KRAS", gene="KRAS", source="HGNC", validated=True)
    ctx = _ctx("What is the role of KRAS in PDAC?", [disease, gene])
    src = primekg_ext.PrimeKGSource()
    decision = src.should_run(ctx)
    assert decision.run and "MONDO" not in decision.reason and "pancreatic ductal adenocarcinoma" in decision.reason
    assert "MONDO" not in (src.plan_fact(ctx) or "")

    calls: List[Dict[str, Any]] = []

    class Reg:
        async def execute(self, tool: str, **kwargs: Any) -> ToolResult:
            calls.append(kwargs)
            validated = primekg_ext.PrimeKGInvestigateInput(**kwargs).model_dump()
            return primekg_ext.investigate(**validated)

    res = run(src.run(ctx, Reg(), SourceRunState(Reg(), ctx)))
    assert calls[0]["diseases"] == ["MONDO_0005184"]
    assert calls[0]["disease_names"] == ["pancreatic ductal adenocarcinoma"]
    assert res.success
    reasons = [a["reason"] for a in res.data["analyses"]]
    assert "PrimeKG profile of pancreatic ductal adenocarcinoma" in reasons
    assert not any("MONDO_" in r for r in reasons)
    gp = next(a for a in res.data["analyses"] if a["operation"] == "gene_profile")
    assert gp["result"]["diseases"]["items"][0]["name"] == "pancreatic ductal adenocarcinoma"
    assert 'ranked_for="pancreatic ductal adenocarcinoma"' in evidence_xml(res.data)
    title, detail = src.describe(res, ctx)
    assert "MONDO_" not in detail
