"""
Query-run repairs for PrimeKG repurposing and the trial condition (2026-10-16 run: "FDA-approved drugs that could be
repurposed for KRAS-mutant pancreatic cancer"), fully offline:

  1  A resolved disease node with no indication edges ("exocrine pancreatic carcinoma") borrows those of a close,
     name-related MONDO relative ("pancreatic adenocarcinoma") for repurposing calibration and the profile, labelled.
  2  Diagnostic / imaging agents and nutrients are not candidates (reported, not hidden); supportive-care drugs and
     neuroendocrine agents are tagged; a "<GENE>-mutant" question ranks pathway-targeting drugs up.
  3  ClinicalTrials.gov is searched with the user's disease wording ("pancreatic cancer"), not the ontology label.
"""

from __future__ import annotations

import asyncio
import csv
from typing import Any, Dict, List, Optional

import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.extensions import primekg as primekg_ext
from phronesis.kgintel import analytics as an
from phronesis.kgintel import service
from phronesis.kgintel.graph import PrimeKG, reset_primekg
from phronesis.kgintel.planner import mutant_genes_in_text, plan_investigation
from phronesis.kgintel.render import evidence_xml, fallback_markdown
from phronesis.kgintel.store import build_store
from phronesis.sources import SourceRunState
from phronesis.tools.registry import ToolResult
from phronesis.trials.trial_query import plan_trial_query

QUESTION = ("Design a step-by-step pipeline to identify FDA-approved drugs that could be repurposed for treating "
            "KRAS-mutant pancreatic cancer, incorporating clinical-trial status, in-silico binding predictions, and "
            "real-world EHR safety data.")


def run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# PrimeKG fixture
# -----------------------------------------------------------------------------

GENES = ["KRAS", "BRAF", "MAP2K1", "SSTR2", "PTGS2", "CNR1", "TYMS", "EGFR", "MMP2", "TP53"]
G = {name: i for i, name in enumerate(GENES)}
DISEASES = [
    (20, "5192", "exocrine pancreatic carcinoma"),
    (21, "6047", "pancreatic adenocarcinoma"),
    (22, "4335", "digestive system carcinoma"),
    (23, "3523", "Cushing syndrome"),
    (24, "5061", "lung adenocarcinoma"),
    (25, "8170", "ovarian cancer"),
]
D = {name: idx for idx, _id, name in DISEASES}
# (index, DrugBank id, name, category, ATC therapeutic group, groups)
DRUGS = [
    (40, "DB00441", "Gemcitabine", "Antimetabolites", "antineoplastic agents", "approved"),
    (41, "DB00530", "Erlotinib", "Protein Kinase Inhibitors", "antineoplastic agents", "approved"),
    (42, "DB00544", "Fluorouracil", "Antimetabolites", "antineoplastic agents", "approved"),
    (43, "DB15873", "Edotreotide gallium Ga-68", "Diagnostic Radiopharmaceuticals ; Radiopharmaceuticals",
     "diagnostic radiopharmaceuticals", "approved"),
    (44, "DB06663", "Pasireotide", "Hormones ; Somatostatin Analogs", "pituitary and hypothalamic hormones and analogues",
     "approved"),
    (45, "DB00470", "Dronabinol", "Antiemetics and Antinauseants ; Cannabinoids and similars",
     "antiemetics and antinauseants", "approved"),
    (46, "DB08911", "Trametinib", "Protein Kinase Inhibitors", "antineoplastic agents", "approved"),
    (47, "DB01404", "Ginseng", "Herbs and Natural Products", "", "approved and investigational and nutraceutical"),
    (48, "DB00482", "Celecoxib", "Anti-Inflammatory Agents, Non-Steroidal", "antiinflammatory and antirheumatic products",
     "approved"),
    (49, "DB00398", "Sorafenib", "Protein Kinase Inhibitors", "antineoplastic agents", "approved"),
    (50, "DB00316", "Acetaminophen", "Analgesics", "analgesics", "approved"),
    (51, "DB00682", "Warfarin", "Anticoagulants", "antithrombotic agents", "approved"),
]
DRUG = {name: idx for idx, _id, name, *_ in DRUGS}

NODES = ([(i, str(1000 + i), "gene/protein", name, "NCBI") for name, i in G.items()]
         + [(idx, nid, "disease", name, "MONDO") for idx, nid, name in DISEASES]
         + [(idx, did, "drug", name, "DrugBank") for idx, did, name, *_ in DRUGS])
# Pad the index range densely (the store requires node_index 0..N-1).
_USED = {n[0] for n in NODES}
NODES += [(i, f"P{i}", "effect/phenotype", f"phenotype {i}", "HPO") for i in range(max(_USED) + 1) if i not in _USED]
NODES.sort()

EDGES = (
    # the resolved node carries the genes; its child carries the approved drugs
    [("disease_protein", "associated with", D["exocrine pancreatic carcinoma"], G[g])
     for g in ("SSTR2", "PTGS2", "CNR1", "TYMS", "BRAF", "EGFR", "MMP2")]
    + [("disease_protein", "associated with", D["lung adenocarcinoma"], G[g]) for g in ("EGFR", "TP53", "KRAS")]
    + [("disease_protein", "associated with", D["ovarian cancer"], G[g]) for g in ("TP53", "MMP2")]
    + [("disease_protein", "associated with", D["Cushing syndrome"], G["SSTR2"])]
    + [("disease_disease", "parent-child", D["exocrine pancreatic carcinoma"], D["pancreatic adenocarcinoma"]),
       ("disease_disease", "parent-child", D["digestive system carcinoma"], D["exocrine pancreatic carcinoma"])]
    + [("indication", "indication", DRUG[d], D["pancreatic adenocarcinoma"]) for d in ("Gemcitabine", "Erlotinib", "Fluorouracil")]
    # a broader parent with more indications must not stand in: it shares no specific name word
    + [("indication", "indication", DRUG[d], D["digestive system carcinoma"])
       for d in ("Gemcitabine", "Erlotinib", "Fluorouracil", "Celecoxib")]
    + [("indication", "indication", DRUG["Pasireotide"], D["Cushing syndrome"])]
    + [("drug_protein", "target", DRUG[d], G[g]) for d, g in (
        ("Gemcitabine", "TYMS"), ("Erlotinib", "EGFR"), ("Fluorouracil", "TYMS"), ("Edotreotide gallium Ga-68", "SSTR2"),
        ("Pasireotide", "SSTR2"), ("Dronabinol", "CNR1"), ("Trametinib", "MAP2K1"), ("Ginseng", "PTGS2"),
        ("Celecoxib", "PTGS2"), ("Sorafenib", "BRAF"), ("Acetaminophen", "PTGS2"), ("Warfarin", "TP53"))]
    + [("protein_protein", "ppi", G[a], G[b]) for a, b in (
        ("KRAS", "BRAF"), ("BRAF", "MAP2K1"), ("EGFR", "KRAS"), ("PTGS2", "TP53"), ("MMP2", "TP53"), ("TYMS", "TP53"),
        ("SSTR2", "CNR1"), ("CNR1", "PTGS2"))]
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
        for idx, nid, name in DISEASES:
            w.writerow({"node_index": idx, "mondo_id": nid, "mondo_name": name})
    with open(raw / "drug_features.tab", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, DRUG_COLS)
        w.writeheader()
        for idx, _did, name, cat, atc, groups in DRUGS:
            w.writerow({"node_index": idx, "group": f"{name} is {groups}.", "category": f"{name} is part of {cat}.",
                        "atc_2": f"{name} is in the therapeutic group of {atc}." if atc else ""})


@pytest.fixture(scope="module")
def kg_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("primekg_repurpose")
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


def _names(items: List[Dict[str, Any]]) -> List[str]:
    return [c["name"] for c in items]


# -----------------------------------------------------------------------------
# 1  indication proxy
# -----------------------------------------------------------------------------

def test_indication_proxy_picks_the_name_related_relative(kg):
    proxy = an.indication_proxy(kg, D["exocrine pancreatic carcinoma"], ["pancreatic cancer"])
    assert proxy is not None and proxy["name"] == "pancreatic adenocarcinoma"
    assert proxy["curie"] == "MONDO_0006047" and proxy["indications"] == 3
    assert proxy["reason"] == "exocrine pancreatic carcinoma has no recorded indications"
    # 'digestive system carcinoma' has more indications but shares no specific word with the disease
    assert an.indication_proxy(kg, D["pancreatic adenocarcinoma"]) is None  # it has its own indications
    assert an.indication_proxy(kg, D["ovarian cancer"], ["ovarian cancer"]) is None  # no relative qualifies


def test_indication_proxy_takes_a_node_named_by_the_user(kg):
    proxy = an.indication_proxy(kg, D["ovarian cancer"], ["pancreatic adenocarcinoma"])
    assert proxy is not None and proxy["name"] == "pancreatic adenocarcinoma" and proxy["relation"] == "named in the question"


def test_planner_borrows_indications_and_says_so(kg):
    plan = plan_investigation(kg, QUESTION, diseases=["MONDO_0005192"], genes=["KRAS"], question_type="treatment",
                              disease_names=["exocrine pancreatic carcinoma"], disease_mentions=["pancreatic cancer"])
    ops = {op: (kw, reason) for op, kw, reason in plan.analyses}
    kw, reason = ops["repurposing"]
    assert kw["indications_from"] == ["MONDO_0006047"] and kw["mutant_genes"] == ["KRAS"]
    assert ("using indications of 'pancreatic adenocarcinoma' (exocrine pancreatic carcinoma has no recorded "
            "indications)") in reason
    assert "KRAS-pathway targets ranked up" in reason
    kw_d, reason_d = ops["disease_profile"]
    assert kw_d["indications_from"] == ["MONDO_0006047"] and "pancreatic adenocarcinoma" in reason_d


def test_disease_profile_shows_the_relatives_drugs_labelled(kg):
    res = service.disease_profile("MONDO_0005192", indications_from=["MONDO_0006047"], kg=kg)["result"]
    assert res["indicated_drugs"]["total"] == 0
    rel = res["indications_from_related"][0]
    assert rel["disease"]["name"] == "pancreatic adenocarcinoma"
    assert set(_names(rel["indicated_drugs"]["items"])) == {"Gemcitabine", "Erlotinib", "Fluorouracil"}
    xml = evidence_xml({"analyses": [{"operation": "disease_profile", "result": res}]})
    assert '<indications_from_related disease="pancreatic adenocarcinoma"' in xml and "Gemcitabine" in xml
    plain = service.disease_profile("MONDO_0005192", kg=kg)["result"]
    assert "indications_from_related" not in plain


def test_repurposing_calibrates_on_borrowed_indications(kg):
    without = service.repurposing("MONDO_0005192", kg=kg)["result"]
    assert without["calibration"]["known_indications"] == 0
    assert without["calibration"]["signal"].startswith("unknown")
    res = service.repurposing("MONDO_0005192", indications_from=["MONDO_0006047"], kg=kg)["result"]
    cal = res["calibration"]
    assert cal["known_indications"] == 3 and not cal["signal"].startswith("unknown")
    assert res["indications_borrowed_from"][0]["name"] == "pancreatic adenocarcinoma"
    assert set(_names(res["borrowed_indications"])) == {"Gemcitabine", "Erlotinib", "Fluorouracil"}
    # known indications are reported as such and never offered as repurposing candidates
    assert not {"Gemcitabine", "Erlotinib", "Fluorouracil"} & set(_names(res["candidates"]))
    assert {"Gemcitabine", "Erlotinib", "Fluorouracil"} <= set(_names(res["already_indicated"]))
    xml = evidence_xml({"analyses": [{"operation": "repurposing", "result": res}]})
    assert '<indications_borrowed_from disease="pancreatic adenocarcinoma" mondo_id="MONDO_0006047"' in xml


# -----------------------------------------------------------------------------
# 2  what a candidate is
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Edotreotide gallium Ga-68", "Fludeoxyglucose F-18", "Fludeoxyglucose (18F)", "Technetium Tc-99m sestamibi",
    "Indium In-111 pentetreotide", "Gadobutrol", "Iohexol", "Gallium Ga 68 dotatate", "Flortaucipir F 18",
])
def test_diagnostic_names_are_recognised(name):
    assert an._DIAGNOSTIC_NAME_RE.search(name)


@pytest.mark.parametrize("name", ["Gemcitabine", "Fluorouracil", "Ifosfamide", "Indomethacin", "Iodine", "Irinotecan",
                                  "Lutetium Lu 177 dotatate", "Infliximab", "Gallium nitrate"])
def test_therapeutic_names_are_not_diagnostic(name):
    assert not an._DIAGNOSTIC_NAME_RE.search(name)


def test_drug_classification_uses_name_and_drugbank_fields(kg):
    assert an.drug_exclusion(kg, DRUG["Edotreotide gallium Ga-68"]) == "diagnostic or imaging agent"
    assert an.drug_exclusion(kg, DRUG["Ginseng"]) == "nutrient or supplement"
    for name in ("Gemcitabine", "Dronabinol", "Pasireotide", "Celecoxib", "Trametinib"):
        assert an.drug_exclusion(kg, DRUG[name]) is None, name
    assert an.drug_tags(kg, DRUG["Dronabinol"]) == ["supportive care"]
    assert an.drug_tags(kg, DRUG["Celecoxib"]) == []


def test_repurposing_drops_diagnostics_and_tags_supportive_and_neuroendocrine_agents(kg):
    res = service.repurposing("MONDO_0005192", indications_from=["MONDO_0006047"], limit=20, kg=kg)["result"]
    names = _names(res["candidates"])
    assert "Edotreotide gallium Ga-68" not in names and "Ginseng" not in names
    excluded = {e["name"]: e["reason"] for e in res["excluded_non_therapeutic"]}
    assert excluded == {"Edotreotide gallium Ga-68": "diagnostic or imaging agent", "Ginseng": "nutrient or supplement"}
    assert res["excluded_non_therapeutic_total"] == 2
    by = {c["name"]: c for c in res["candidates"]}
    # supportive care is flagged, not dropped and not moved
    assert by["Dronabinol"]["tags"] == ["supportive care"] and "base_score" not in by["Dronabinol"]
    # a somatostatin analogue for a non-neuroendocrine cancer is tagged and halved
    pas = by["Pasireotide"]
    assert pas["tags"] == ["neuroendocrine-tumour agent"]
    assert pas["score"] == pytest.approx(pas["base_score"] * an.NET_AGENT_FACTOR, abs=1e-3)
    xml = evidence_xml({"analyses": [{"operation": "repurposing", "result": res}]})
    assert 'tags="supportive care"' in xml and "<excluded_non_therapeutic" in xml
    md = fallback_markdown({"analyses": [{"operation": "repurposing", "result": res}]})
    assert "[supportive care]" in md and "known indications from pancreatic adenocarcinoma" in md


def test_mutant_genes_are_read_from_the_question(kg):
    assert mutant_genes_in_text(kg, QUESTION) == ["KRAS"]
    assert mutant_genes_in_text(kg, "sotorasib in KRAS G12C non-small cell lung cancer") == ["KRAS"]
    assert mutant_genes_in_text(kg, "EGFR-mutated lung adenocarcinoma") == ["EGFR"]
    assert mutant_genes_in_text(kg, "drugs for pancreatic cancer") == []
    assert mutant_genes_in_text(kg, "XYZ1-mutant tumours") == []  # not a PrimeKG gene


def test_pathway_neighbourhood_includes_the_curated_axis(kg):
    hood = an.pathway_neighbourhood(kg, G["KRAS"])
    assert hood[G["KRAS"]] == "core" and hood[G["BRAF"]] == "core"
    assert hood[G["MAP2K1"]] == "core"  # no KRAS-MAP2K1 PPI edge; the curated RAS axis supplies it
    assert G["EGFR"] not in hood  # a PPI partner with no shared specific pathway is not in the module


def test_kras_mutant_question_ranks_pathway_drugs_up(kg):
    base = service.repurposing("MONDO_0005192", indications_from=["MONDO_0006047"], limit=20, kg=kg)["result"]
    res = service.repurposing("MONDO_0005192", indications_from=["MONDO_0006047"], mutant_genes=["KRAS"], limit=20,
                              kg=kg)["result"]
    assert res["mutant_genes"] == ["KRAS"]
    by = {c["name"]: c for c in res["candidates"]}
    sor = by["Sorafenib"]
    assert "KRAS pathway" in sor["tags"] and sor["pathway_targets"] == ["BRAF"]
    assert sor["score"] == pytest.approx(sor["base_score"] + an.PATHWAY_BOOST["core"], abs=1e-3)
    assert "KRAS pathway neighbourhood" in sor["basis"]
    rank = lambda r, n: _names(r["candidates"]).index(n)  # noqa: E731
    assert rank(res, "Sorafenib") < rank(base, "Sorafenib") or rank(base, "Sorafenib") == 0
    # the calibration is computed on the unboosted score, so it does not move
    assert res["calibration"] == base["calibration"]
    # a MEK inhibitor with no disease-gene signal still surfaces, tagged, among the candidates or the pathway list
    pathway_names = _names(res["candidates"]) + _names(res.get("pathway_candidates") or [])
    assert "Trametinib" in pathway_names
    tram = next(c for c in res["candidates"] + (res.get("pathway_candidates") or []) if c["name"] == "Trametinib")
    assert tram["pathway_targets"] == ["MAP2K1"] and "KRAS pathway" in tram["tags"]
    assert "Trametinib" not in _names(base["candidates"])
    xml = evidence_xml({"analyses": [{"operation": "repurposing", "result": res}]})
    assert 'pathway_targets="MAP2K1"' in xml and '<pathway_context mutant_genes="KRAS"' in xml


# -----------------------------------------------------------------------------
# extension wiring: the user's disease wording reaches the planner
# -----------------------------------------------------------------------------

def test_source_passes_disease_mentions_and_reasons_name_the_relative(active_store):
    disease = ResolvedEntity(type="disease", mention="pancreatic cancer", label="exocrine pancreatic carcinoma",
                             ids={"ontology_id": "MONDO_0005192"}, source="OpenTargets", validated=True)
    gene = ResolvedEntity(type="gene", mention="KRAS", label="KRAS", gene="KRAS", source="HGNC", validated=True)
    ctx = QueryContext(raw_query=QUESTION, clean_query=QUESTION, entities=[gene, disease], primary_target="KRAS")
    src = primekg_ext.PrimeKGSource()
    calls: List[Dict[str, Any]] = []

    class Reg:
        async def execute(self, tool: str, **kwargs: Any) -> ToolResult:
            calls.append(kwargs)
            return primekg_ext.investigate(**primekg_ext.PrimeKGInvestigateInput(**kwargs).model_dump())

    res = run(src.run(ctx, Reg(), SourceRunState(Reg(), ctx)))
    assert calls[0]["disease_mentions"] == ["pancreatic cancer"]
    assert res.success
    rep = next(a for a in res.data["analyses"] if a["operation"] == "repurposing")
    assert "using indications of 'pancreatic adenocarcinoma'" in rep["reason"]
    assert rep["result"]["calibration"]["known_indications"] == 3
    notes = " ".join(src.scope_notes(ctx, res))
    assert "related node 'pancreatic adenocarcinoma'" in notes
    assert "has no known indication in PrimeKG" not in notes


# -----------------------------------------------------------------------------
# 3  ClinicalTrials.gov condition in the user's words
# -----------------------------------------------------------------------------

def _trial_ctx(mention: str, label: str = "exocrine pancreatic carcinoma", gene: bool = True) -> QueryContext:
    entities = [ResolvedEntity(type="disease", mention=mention, label=label, ids={"ontology_id": "MONDO_0005192"},
                               source="OpenTargets", validated=True)]
    if gene:
        entities.insert(0, ResolvedEntity(type="gene", mention="KRAS", label="KRAS", gene="KRAS", source="HGNC",
                                          validated=True))
    return QueryContext(raw_query=QUESTION, clean_query=QUESTION, entities=entities,
                        primary_target="KRAS" if gene else None)


def _cond_params(plan) -> List[Optional[str]]:
    return [s["params"].get("query.cond") for s in plan.strategies]


def test_trial_condition_prefers_the_users_wording():
    ctx = _trial_ctx("pancreatic cancer")
    plan = plan_trial_query(ctx.clean_query, ctx.primary_display, ctx)
    assert plan.condition == "pancreatic cancer" and plan.term == "KRAS"
    assert plan.to_tool_kwargs() == {"term": "KRAS", "condition": "pancreatic cancer"}
    assert "exocrine pancreatic carcinoma" not in _cond_params(plan)
    assert plan.label == "KRAS + pancreatic cancer"


def test_mutant_mention_splits_into_term_and_condition():
    ctx = _trial_ctx("KRAS-mutant pancreatic cancers", gene=False)
    plan = plan_trial_query(ctx.clean_query, None, ctx)
    assert plan.term == "KRAS" and plan.condition == "pancreatic cancer"
    assert plan.strategies[0] == {"name": "term+condition",
                                  "params": {"query.term": "KRAS", "query.cond": "pancreatic cancer"}}


def test_abbreviation_or_non_condition_mention_keeps_the_label():
    for mention in ("PDAC", "it"):
        ctx = _trial_ctx(mention, label="pancreatic ductal adenocarcinoma")
        plan = plan_trial_query(ctx.clean_query, ctx.primary_display, ctx)
        assert plan.condition == "pancreatic ductal adenocarcinoma", mention
