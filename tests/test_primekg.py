"""
PrimeKG integration (phronesis.kgintel + phronesis.extensions.primekg), fully offline.

The fixture is a 27-node PrimeKG written in the release's raw formats (comma-separated nodes.tab, edges.csv
with both directions of every edge, one disease_features row per MONDO member) and compiled with the real
build_store, so the build, the graph, every analysis, the evidence source, the registry tools and the
orchestrator path are exercised end to end. Relations follow DrugBank/MONDO/HPO facts for these entities;
the graph is a small slice, not a copy of the release.
"""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, List

import pytest

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.agents.search_plan import build_search_plan
from phronesis.extensions import hooks
from phronesis.extensions import primekg as primekg_ext
from phronesis.kgintel import analytics as an
from phronesis.kgintel import service
from phronesis.kgintel.graph import PrimeKG, PrimeKGError, get_primekg, normalize_name, reset_primekg
from phronesis.kgintel.planner import extract_phenotypes, has_kg_intent, plan_investigation
from phronesis.kgintel.render import citation_dicts, evidence_xml, fallback_markdown, identifiers
from phronesis.kgintel.store import build_store, store_available, store_manifest
from phronesis.tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult

from tests.fakes import called, make_offline_orchestrator

NODES = [
    # node_index, node_id, node_type, node_name, node_source
    (0, "25", "gene/protein", "ABL1", "NCBI"),
    (1, "613", "gene/protein", "BCR", "NCBI"),
    (2, "3815", "gene/protein", "KIT", "NCBI"),
    (3, "1576", "gene/protein", "CYP3A4", "NCBI"),
    (4, "1559", "gene/protein", "CYP2C9", "NCBI"),
    (5, "7157", "gene/protein", "TP53", "NCBI"),
    (6, "DB00619", "drug", "Imatinib", "DrugBank"),
    (7, "DB01254", "drug", "Dasatinib", "DrugBank"),
    (8, "DB00945", "drug", "Acetylsalicylic acid", "DrugBank"),
    (9, "DB00682", "drug", "Warfarin", "DrugBank"),
    (10, "DB00196", "drug", "Fluconazole", "DrugBank"),
    (11, "9455_11996", "disease", "chronic myelogenous leukemia, BCR-ABL1 positive", "MONDO_grouped"),
    (12, "11719", "disease", "gastrointestinal stromal tumor", "MONDO"),
    (13, "5101", "disease", "ulcerative colitis (disease)", "MONDO"),
    (14, "1744", "effect/phenotype", "Splenomegaly", "HPO"),
    (15, "12378", "effect/phenotype", "Fatigue", "HPO"),
    (16, "1903", "effect/phenotype", "Anemia", "HPO"),
    (17, "2027", "effect/phenotype", "Abdominal pain", "HPO"),
    (18, "1892", "effect/phenotype", "Abnormal bleeding", "HPO"),
    (19, "R-HSA-1433557", "pathway", "Signaling by SCF-KIT", "REACTOME"),
    (20, "6468", "biological_process", "protein phosphorylation", "GO"),
    (21, "178", "anatomy", "blood", "UBERON"),
    (22, "DB04868", "drug", "Nilotinib", "DrugBank"),
    (23, "4967", "disease", "acute lymphoblastic leukemia", "MONDO"),
    (24, "5742", "gene/protein", "PTGS1", "NCBI"),
    (25, "79001", "gene/protein", "VKORC1", "NCBI"),
    (26, "4247", "disease", "peptic ulcer disease", "MONDO"),
]

EDGES = [
    # relation, display_relation, x, y (written in both directions, as in edges.csv)
    ("protein_protein", "ppi", 0, 1), ("protein_protein", "ppi", 0, 2), ("protein_protein", "ppi", 2, 5),
    ("protein_protein", "ppi", 1, 5),
    ("drug_protein", "target", 6, 0), ("drug_protein", "target", 6, 2), ("drug_protein", "target", 7, 0),
    ("drug_protein", "target", 7, 2), ("drug_protein", "target", 22, 0), ("drug_protein", "target", 8, 24),
    ("drug_protein", "target", 9, 25),
    ("drug_protein", "enzyme", 6, 3), ("drug_protein", "enzyme", 7, 3), ("drug_protein", "enzyme", 9, 3),
    ("drug_protein", "enzyme", 9, 4), ("drug_protein", "enzyme", 10, 3), ("drug_protein", "enzyme", 10, 4),
    ("drug_protein", "enzyme", 8, 4),
    ("indication", "indication", 6, 11), ("indication", "indication", 6, 12), ("indication", "indication", 7, 11),
    ("indication", "indication", 22, 11), ("indication", "indication", 7, 23),
    ("contraindication", "contraindication", 8, 26), ("contraindication", "contraindication", 9, 26),
    ("drug_drug", "synergistic interaction", 9, 10), ("drug_drug", "synergistic interaction", 9, 8),
    ("disease_protein", "associated with", 11, 0), ("disease_protein", "associated with", 11, 1),
    ("disease_protein", "associated with", 12, 2), ("disease_protein", "associated with", 23, 0),
    ("disease_protein", "associated with", 23, 1),
    ("disease_phenotype_positive", "phenotype present", 11, 14), ("disease_phenotype_positive", "phenotype present", 11, 15),
    ("disease_phenotype_positive", "phenotype present", 11, 16), ("disease_phenotype_positive", "phenotype present", 12, 17),
    ("disease_phenotype_positive", "phenotype present", 12, 16), ("disease_phenotype_positive", "phenotype present", 12, 18),
    ("disease_phenotype_positive", "phenotype present", 23, 15), ("disease_phenotype_positive", "phenotype present", 23, 16),
    ("disease_phenotype_positive", "phenotype present", 23, 18), ("disease_phenotype_positive", "phenotype present", 26, 17),
    ("disease_phenotype_negative", "phenotype absent", 12, 14),
    ("drug_effect", "side effect", 6, 15), ("drug_effect", "side effect", 6, 17), ("drug_effect", "side effect", 7, 15),
    ("drug_effect", "side effect", 9, 18), ("drug_effect", "side effect", 8, 18), ("drug_effect", "side effect", 10, 17),
    ("pathway_protein", "interacts with", 19, 2), ("pathway_protein", "interacts with", 19, 0),
    ("pathway_protein", "interacts with", 19, 1),
    ("bioprocess_protein", "interacts with", 20, 0), ("bioprocess_protein", "interacts with", 20, 2),
    ("bioprocess_protein", "interacts with", 20, 1), ("bioprocess_protein", "interacts with", 20, 5),
    ("anatomy_protein_present", "expression present", 21, 0), ("anatomy_protein_present", "expression present", 21, 1),
]

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
        # Two MONDO members of one grouped node: the representative row (name equals the node name) supplies
        # the text; the member name becomes a synonym.
        w.writerow({"node_index": 11, "mondo_id": "11996", "mondo_name": "Philadelphia-positive chronic myeloid leukaemia"})
        w.writerow({"node_index": 11, "mondo_id": "9455", "mondo_name": "chronic myelogenous leukemia, BCR-ABL1 positive",
                    "mondo_definition": "A myeloproliferative neoplasm driven by the BCR-ABL1 fusion.",
                    "mayo_symptoms": "Fatigue, weight loss, left-sided abdominal fullness."})
        w.writerow({"node_index": 12, "mondo_id": "11719", "mondo_name": "gastrointestinal stromal tumor",
                    "mondo_definition": "A mesenchymal tumour of the gastrointestinal tract, usually KIT-driven."})
        w.writerow({"node_index": 13, "mondo_id": "5101", "mondo_name": "ulcerative colitis"})
        w.writerow({"node_index": 23, "mondo_id": "4967", "mondo_name": "acute lymphoblastic leukemia"})
        w.writerow({"node_index": 26, "mondo_id": "4247", "mondo_name": "peptic ulcer disease"})
    with open(raw / "drug_features.tab", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, DRUG_COLS)
        w.writeheader()
        w.writerow({"node_index": 6, "description": "Imatinib is a tyrosine kinase inhibitor.",
                    "mechanism_of_action": "Imatinib inhibits the BCR-ABL tyrosine kinase.",
                    "state": "Imatinib is a solid.", "group": "Imatinib is approved.",
                    "atc_1": "Imatinib is anatomically related to antineoplastic and immunomodulating agents.",
                    "atc_4": "The chemical and functional group of  is BCR-ABL tyrosine kinase inhibitors.",
                    "category": "Imatinib is part of Antineoplastic Agents ; Protein Kinase Inhibitors.",
                    "molecular_weight": "The molecular weight is 493.6."})
        for idx, name in ((7, "Dasatinib"), (22, "Nilotinib"), (8, "Acetylsalicylic acid"), (9, "Warfarin"),
                          (10, "Fluconazole")):
            w.writerow({"node_index": idx, "group": f"{name} is approved and investigational."})


@pytest.fixture(scope="module")
def kg_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("primekg")
    _write_raw(root / "raw")
    manifest = build_store(root, download=False, log=lambda _m: None)
    assert manifest["counts"]["nodes"] == len(NODES)
    return root


@pytest.fixture(scope="module")
def kg(kg_root) -> PrimeKG:
    return PrimeKG(kg_root)


@pytest.fixture
def active_store(kg_root, monkeypatch):
    """Point the process-wide PrimeKG at the fixture store."""
    monkeypatch.setenv("PHRONESIS_PRIMEKG_DIR", str(kg_root))
    reset_primekg()
    yield kg_root
    reset_primekg()


def _ctx(query: str, entities: List[ResolvedEntity] = ()) -> QueryContext:
    ctx = QueryContext(raw_query=query, clean_query=query, entities=list(entities))
    ctx.search_plan = build_search_plan(ctx, query)
    return ctx


# -----------------------------------------------------------------------------
# store and graph
# -----------------------------------------------------------------------------

def test_build_deduplicates_both_directions_and_records_provenance(kg_root):
    m = store_manifest(kg_root)
    assert m["counts"]["edges_raw_rows"] == 2 * len(EDGES)
    assert m["counts"]["edges_undirected"] == len(EDGES)
    assert m["counts"]["relations"]["drug_protein/enzyme"] == 7
    assert m["raw_files"]["edges.csv"]["dataverse_file_id"] == 6180616
    assert len(m["raw_files"]["edges.csv"]["sha256"]) == 64
    assert m["dataverse_doi"] == "doi:10.7910/DVN/IXA7BM"
    assert store_available(kg_root)
    assert not store_available(kg_root / "missing")


def test_graph_adjacency_is_symmetric_and_typed(kg):
    nb, _, _ = kg.neighbors(6, relations=["drug_protein"], displays=["target"])
    assert sorted(kg.name(int(i)) for i in nb) == ["ABL1", "KIT"]
    back, _, _ = kg.neighbors(0, relations=["drug_protein"], node_type="drug")
    assert sorted(kg.name(int(i)) for i in back) == ["Dasatinib", "Imatinib", "Nilotinib"]
    assert kg.edge_labels(9, 10) == ["synergistic interaction"]
    assert int(kg.degree.sum()) == 2 * len(EDGES)


def test_missing_store_is_a_typed_error(tmp_path):
    with pytest.raises(PrimeKGError) as exc:
        PrimeKG(tmp_path)
    assert exc.value.kind == "not_built"


@pytest.mark.parametrize("query,types,expected,how", [
    ("imatinib", ["drug"], "Imatinib", "name"),
    ("DB00945", None, "Acetylsalicylic acid", "id"),
    ("aspirin", ["drug"], "Acetylsalicylic acid", "alias"),
    ("imatinib mesylate", ["drug"], "Imatinib", "salt_stripped"),
    ("TP53", ["gene"], "TP53", "symbol"),
    ("7157", None, "TP53", "id"),
    ("MONDO_0009455", None, "chronic myelogenous leukemia, BCR-ABL1 positive", "id"),
    ("MONDO:11996", None, "chronic myelogenous leukemia, BCR-ABL1 positive", "id"),
    ("CML", ["disease"], "chronic myelogenous leukemia, BCR-ABL1 positive", "alias"),
    ("Philadelphia-positive chronic myeloid leukaemia", ["disease"], "chronic myelogenous leukemia, BCR-ABL1 positive", "synonym"),
    ("ulcerative colitis", ["disease"], "ulcerative colitis (disease)", "synonym"),
    ("HP:0001744", None, "Splenomegaly", "id"),
    ("gastrointestinal stromal tumour", ["disease"], "gastrointestinal stromal tumor", "fuzzy"),
])
def test_resolution(kg, query, types, expected, how):
    m = kg.resolve(query, node_types=types, limit=1)
    assert m and kg.name(m[0].index) == expected
    assert m[0].matched_on == how


def test_resolution_respects_type_and_never_crosses_numbers(kg):
    assert not kg.resolve("imatinib", node_types=["disease"], fuzzy=False)
    assert normalize_name("Crohn's diseases") == "crohn disease"
    with pytest.raises(PrimeKGError) as exc:
        kg.resolve_one("no such thing at all", ["drug"])
    assert exc.value.kind == "not_found"


def test_node_curies_and_links(kg):
    assert kg.node(11)["curie"] == "MONDO_0011996" or kg.node(11)["curie"] == "MONDO_0009455"
    assert set(kg.node(11)["mondo_ids"]) == {"MONDO_0009455", "MONDO_0011996"}
    assert kg.node(14)["curie"] == "HP:0001744"
    assert kg.node(6)["url"] == "https://go.drugbank.com/drugs/DB00619"
    assert kg.node(0)["curie"] == "NCBIGene:25"


def test_scan_text_finds_longest_names(kg):
    hits = kg.scan_text("Is imatinib used for chronic myelogenous leukemia, BCR-ABL1 positive?", ["drug", "disease"])
    assert {kg.name(i) for i, _ in hits} == {"Imatinib", "chronic myelogenous leukemia, BCR-ABL1 positive"}


# -----------------------------------------------------------------------------
# analytics
# -----------------------------------------------------------------------------

def test_drug_profile(kg):
    p = an.drug_profile(kg, 6, limit=10)
    assert [d["name"] for d in p["indications"]["items"]] == ["chronic myelogenous leukemia, BCR-ABL1 positive",
                                                              "gastrointestinal stromal tumor"]
    assert {g["name"] for g in p["proteins"]["target"]["items"]} == {"ABL1", "KIT"}
    assert [g["name"] for g in p["proteins"]["enzyme"]["items"]] == ["CYP3A4"]
    assert p["clinical"]["groups"] == ["approved"]
    assert p["clinical"]["atc"] == {"anatomical": "antineoplastic and immunomodulating agents",
                                    "chemical": "BCR-ABL tyrosine kinase inhibitors"}
    assert p["clinical"]["categories"] == ["Antineoplastic Agents", "Protein Kinase Inhibitors"]
    assert p["clinical"]["molecular_weight"] == "493.6"
    with pytest.raises(PrimeKGError):
        an.drug_profile(kg, 0)


def test_disease_profile_merges_member_rows(kg):
    p = an.disease_profile(kg, 11)
    assert p["clinical"]["definition"] == "A myeloproliferative neoplasm driven by the BCR-ABL1 fusion."
    assert "Philadelphia-positive chronic myeloid leukaemia" in p["clinical"]["synonyms"]
    assert {d["name"] for d in p["indicated_drugs"]["items"]} == {"Imatinib", "Dasatinib", "Nilotinib"}
    assert {g["name"] for g in p["genes"]["items"]} == {"ABL1", "BCR"}


def test_gene_profile(kg):
    p = an.gene_profile(kg, 2)
    assert {d["name"] for d in p["drugs"]["target"]["items"]} == {"Imatinib", "Dasatinib"}
    assert [d["name"] for d in p["diseases"]["items"]] == ["gastrointestinal stromal tumor"]
    assert [x["name"] for x in p["pathways"]["items"]] == ["Signaling by SCF-KIT"]
    assert p["interactors"]["total"] == 2


def test_mechanism_paths_are_typed_and_shortest(kg):
    r = an.mechanism_paths(kg, 22, 12, max_hops=4)  # nilotinib -> GIST
    assert r["shortest_length"] == 3
    texts = [p["text"] for p in r["paths"]]
    assert "Nilotinib — [target] ABL1 — [ppi] KIT — [associated with] gastrointestinal stromal tumor" in texts
    assert r["metapaths"][0]["pattern"].startswith("drug → [")
    # Drug-drug interaction edges are not used as mechanism steps by default.
    r2 = an.mechanism_paths(kg, 9, 10, max_hops=2)
    assert r2["shortest_length"] == 2 and all("synergistic" not in p["text"] for p in r2["paths"])


def test_drug_disease_reports_status_and_shared_genes(kg):
    r = an.drug_disease(kg, 7, 12)  # dasatinib, GIST
    assert r["status"] == "no direct relation in PrimeKG"
    assert [g["name"] for g in r["shared_genes"]] == ["KIT"]
    assert an.drug_disease(kg, 8, 26)["status"] == "contraindicated"


def test_repurposing_ranks_target_hits_and_holds_out_known_indications(kg):
    r = an.repurposing(kg, 12, limit=10, n_random=20)
    names = [c["name"] for c in r["candidates"]]
    assert names[0] == "Dasatinib"  # targets KIT, the GIST gene, and is indicated for CML (similar disease)
    assert "Imatinib" not in names  # already indicated: calibration only
    assert r["already_indicated"][0]["name"] == "Imatinib" and r["already_indicated"][0]["rank"] >= 1
    cal = r["calibration"]
    assert cal["known_indications"] == 1 and cal["known_indications_scored"] == 1
    assert 0.0 <= cal["auroc_known_indications"] <= 1.0
    top = r["candidates"][0]
    assert top["targets_hitting_disease_genes"] == ["KIT"]
    assert set(top["components"]) == {"target_overlap", "network_proximity", "similar_disease_indication"}
    assert r["weights"] == an.REPURPOSING_WEIGHTS
    assert "Hypothesis generation only" in r["caveat"]


def test_repurposing_excludes_contraindicated(kg):
    r = an.repurposing(kg, 26, limit=10, n_random=10)
    assert all(c["name"] not in ("Acetylsalicylic acid", "Warfarin") for c in r["candidates"])


def test_phenotype_diagnosis_ranks_by_information_and_coverage(kg):
    r = an.phenotype_diagnosis(kg, [15, 16, 14], absent=[])  # fatigue, anemia, splenomegaly
    assert r["ranking"][0]["name"] == "chronic myelogenous leukemia, BCR-ABL1 positive"
    assert r["ranking"][0]["coverage"] == "3/3"
    # Splenomegaly stated absent: CML is penalised, ALL (no splenomegaly annotated) moves ahead.
    r2 = an.phenotype_diagnosis(kg, [15, 16, 18], absent=[14])
    assert r2["ranking"][0]["name"] == "acute lymphoblastic leukemia"
    # GIST is annotated as lacking splenomegaly, so asking for it is penalised.
    r3 = an.phenotype_diagnosis(kg, [14, 17], absent=[])
    gist = next(d for d in r3["ranking"] if d["name"] == "gastrointestinal stromal tumor") if any(
        d["name"] == "gastrointestinal stromal tumor" for d in r3["ranking"]) else None
    assert gist is None or gist["contradicted"]


def test_polypharmacy_flags_interactions_enzymes_and_contraindications(kg):
    r = an.polypharmacy(kg, [9, 10, 8], diseases=[26])
    pair = next(p for p in r["pairs"] if {d["name"] for d in p["drugs"]} == {"Warfarin", "Fluconazole"})
    assert pair["recorded_interaction"] is True
    assert pair["shared"]["enzyme"] == ["CYP2C9", "CYP3A4"]
    assert r["disease_contraindications"][0]["contraindicated_drugs"] == ["Warfarin", "Acetylsalicylic acid"]
    with pytest.raises(PrimeKGError):
        an.polypharmacy(kg, [9])


def test_enrichment_finds_shared_pathway(kg):
    r = an.enrichment(kg, [0, 1, 2], collections=["pathway", "biological_process"], min_size=3)
    terms = r["collections"]["pathway"]["terms"]
    assert terms and terms[0]["name"] == "Signaling by SCF-KIT" and terms[0]["overlap"] == 3
    assert 0 < terms[0]["q_value"] <= 1


def test_similar_drugs_share_targets(kg):
    r = an.similar(kg, 6, limit=3)
    assert r["similar"][0]["name"] == "Dasatinib"


# -----------------------------------------------------------------------------
# service, planner, render
# -----------------------------------------------------------------------------

def test_service_resolves_names_and_reports_alternatives(kg):
    out = service.run("drug_disease", kg=kg, drug="aspirin", disease="peptic ulcer disease")
    assert out["result"]["status"] == "contraindicated"
    assert [r["matched_on"] for r in out["resolved"]] == ["alias", "name"]
    with pytest.raises(PrimeKGError) as exc:
        service.run("drug_profile", kg=kg, drug="definitely not a drug")
    assert exc.value.kind == "not_found"
    with pytest.raises(PrimeKGError):
        service.run("no_such_operation", kg=kg)
    unresolved = service.run("polypharmacy", kg=kg, drugs=["warfarin", "fluconazole", "unobtainium"])
    assert unresolved["unresolved"] == ["unobtainium"]


@pytest.mark.parametrize("query,ops", [
    ("Which existing drugs could be repurposed for gastrointestinal stromal tumor?", ["repurposing", "disease_profile"]),
    ("Is it safe to take warfarin together with fluconazole?", ["polypharmacy", "drug_profile", "drug_profile"]),
    ("How might dasatinib be linked to gastrointestinal stromal tumor?", ["drug_disease", "drug_profile", "disease_profile"]),
    ("Patient presents with fatigue, anemia and splenomegaly. Which disease could this be?", ["phenotype_diagnosis"]),
    ("What pathways are enriched in ABL1, BCR and KIT?", ["enrichment"]),
    ("Which drugs are similar to imatinib?", ["drug_profile", "similar"]),
    ("Tell me about the weather", []),
])
def test_planner_chooses_analyses(kg, query, ops):
    plan = plan_investigation(kg, query)
    assert [op for op, _, _ in plan.analyses] == ops


def test_planner_marks_negated_phenotypes_absent(kg):
    present, absent = extract_phenotypes(kg, "fatigue and anemia, but no splenomegaly")
    assert present == ["Fatigue", "Anemia"] and absent == ["Splenomegaly"]
    assert has_kg_intent("differential for these symptoms")
    assert not has_kg_intent("simulate the MAPK model")


def _investigation(kg: PrimeKG, query: str) -> Dict[str, Any]:
    plan = plan_investigation(kg, query)
    analyses = []
    for op, kwargs, reason in plan.analyses:
        out = service.run(op, kg=kg, **kwargs)
        analyses.append({"operation": op, "reason": reason, "arguments": kwargs, "resolved": out["resolved"],
                         "result": out["result"]})
    return {"analyses": analyses, "provenance": kg.provenance(), "unresolved": []}


def test_evidence_xml_escapes_and_carries_reading_rules(kg):
    data = _investigation(kg, "Which existing drugs could be repurposed for gastrointestinal stromal tumor?")
    data["analyses"][0]["result"]["candidates"][0]["mechanism_of_action"] = "<b>inhibits & binds</b>"
    xml = evidence_xml(data)
    assert xml.startswith("<primekg ") and xml.endswith("</primekg>")
    assert "&lt;b&gt;inhibits &amp; binds&lt;/b&gt;" in xml and "<b>" not in xml
    assert "<how_to_read>" in xml and "<calibration " in xml and 'drugbank_id="DB01254"' in xml


def test_identifiers_and_citations(kg):
    data = _investigation(kg, "How might dasatinib be linked to gastrointestinal stromal tumor?")
    ids = identifiers(data)
    assert {"DB01254", "DB00619"} <= ids["drugbank"]
    assert "MONDO_0011719" in ids["ontology"]
    cites = citation_dicts(data)
    db = next(c for c in cites if c["id"] == "DB01254")
    assert db["type"] == "drugbank" and db["url"] == "https://go.drugbank.com/drugs/DB01254"
    assert any(c["type"] == "url" and c["id"] == "http://purl.obolibrary.org/obo/MONDO_0011719" for c in cites)


def test_fallback_markdown_summarises_every_analysis(kg):
    md = fallback_markdown(_investigation(kg, "Is it safe to take warfarin together with fluconazole?"))
    assert md.startswith("### PrimeKG knowledge graph")
    assert "Warfarin + Fluconazole: recorded drug-drug interaction" in md
    assert "doi:10.7910/DVN/IXA7BM" in md


# -----------------------------------------------------------------------------
# extension: source, registry tools, orchestrator
# -----------------------------------------------------------------------------

def test_source_runs_only_with_a_store_and_a_graph_question(active_store, monkeypatch, tmp_path):
    src = primekg_ext.PrimeKGSource()
    drug = ResolvedEntity(type="drug", mention="imatinib", label="IMATINIB", ids={"chembl_id": "CHEMBL941"},
                          source="ChEMBL", validated=True)
    assert src.should_run(_ctx("What does imatinib do?", [drug])).run
    assert src.should_run(_ctx("Patient presents with fatigue and anemia; differential?")).run
    assert not src.should_run(_ctx("Simulate BIOMD0000000010 for 60 seconds")).run
    ctx = _ctx("What does imatinib do?", [drug])
    ctx.source_allowlist = {"lithoscribe"}
    assert not src.should_run(ctx).run
    monkeypatch.setenv("PHRONESIS_PRIMEKG_DIR", str(tmp_path / "none"))
    decision = src.should_run(_ctx("What does imatinib do?", [drug]))
    assert not decision.run and "not built" in decision.reason


def test_disease_entities_resolve_by_mondo_id(active_store):
    disease = ResolvedEntity(type="disease", mention="GIST", label="gastrointestinal stromal tumor",
                             ids={"ontology_id": "MONDO_0011719"}, source="OpenTargets", validated=True)
    drugs, diseases, genes = primekg_ext._entities(_ctx("drugs for GIST", [disease]))
    assert diseases == ["MONDO_0011719"] and not drugs and not genes
    res = primekg_ext.investigate("Which drugs could be repurposed?", diseases=diseases)
    assert res.success and res.data["analyses"][0]["operation"] == "repurposing"
    assert res.canonical_ids["ontology_id"] == "MONDO_0011719"


@pytest.mark.asyncio
async def test_registry_tools_execute_offline(active_store):
    reg = BiomedicalToolRegistry()
    with hooks.isolated_hooks():
        primekg_ext.register_tools(reg)
        res = await reg.execute("primekg_investigate", query="Is it safe to take warfarin with fluconazole and aspirin?")
        assert res.success and res.data["analyses"][0]["operation"] == "polypharmacy"
        assert res.canonical_ids["drugbank_id"] == "DB00682"
        q = await reg.execute("primekg_query", operation="gene_profile", arguments={"gene": "KIT"})
        assert q.success and q.data["result"]["gene"]["name"] == "KIT"
        bad = await reg.execute("primekg_query", operation="gene_profile", arguments={"gene": "NOTAGENE123"})
        assert not bad.success and bad.error_type == ErrorType.NOT_FOUND
        wrong = await reg.execute("primekg_query", operation="gene_profile", arguments={"nonsense": 1})
        assert not wrong.success and wrong.error_type == ErrorType.VALIDATION_ERROR


def test_investigate_without_store_is_dataset_required(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_PRIMEKG_DIR", str(tmp_path))
    reset_primekg()
    res = primekg_ext.investigate("drugs for CML")
    assert not res.success and res.error_type == ErrorType.DATASET_REQUIRED
    reset_primekg()


def test_source_outputs(active_store):
    src = primekg_ext.PrimeKGSource()
    res = primekg_ext.investigate("Which existing drugs could be repurposed for gastrointestinal stromal tumor?")
    title, detail = src.describe(res, _ctx("x"))
    assert title == "Ran 2 PrimeKG analyses" and "drug candidates for gastrointestinal stromal tumor" in detail
    key, art = src.artifact(res)
    assert key == "primekg" and [a["operation"] for a in art["analyses"]] == ["repurposing", "disease_profile"]
    json.dumps(art)  # JSON-safe
    assert "DB01254" in src.allowed_identifiers(res)["drugbank"]
    assert any(c.type == "drugbank" and c.source_tool == "primekg_investigate" for c in src.citations(res))
    notes = src.scope_notes(_ctx("x"), res)
    assert any("snapshot" in n for n in notes)
    failed = ToolResult(success=False, tool_name="primekg_investigate", error="boom")
    assert src.scope_notes(_ctx("x"), failed)[0].startswith("The PrimeKG analysis failed")


@pytest.mark.asyncio
async def test_orchestrator_runs_primekg_and_renders_fallback(active_store, monkeypatch, tmp_path):
    extra = {"primekg_investigate": (primekg_ext.PrimeKGInvestigateInput, lambda kw: primekg_ext.investigate(**kw))}
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=extra)
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry("Which existing drugs could be repurposed for gastrointestinal stromal tumor?",
                                     thread_id="t-primekg", enable_web_search=False, event_callback=cb)
    assert called(calls, "primekg_investigate")
    steps = {e["data"]["id"]: e["data"] for e in events if e["type"] == "thought"}
    assert steps["primekg"]["agent"] == "Synapta" and steps["primekg"]["title"].startswith("Ran ")
    art = res.execution_artifacts["primekg"]
    assert art["analyses"][0]["operation"] == "repurposing"
    assert "### PrimeKG knowledge graph" in res.response


def test_get_primekg_is_cached_per_root(active_store):
    assert get_primekg() is get_primekg()


def test_cli_query_and_stats(active_store, capsys):
    from phronesis.kgintel.cli import main

    assert main(["query", "drug_profile", "drug=imatinib", "limit=3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["drug"]["id"] == "DB00619"
    assert main(["stats"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["nodes"] == len(NODES)
    assert main(["query", "drug_profile", "drug=unobtainium"]) == 2


def test_synapta_langchain_tool(active_store):
    from phronesis.synapta.tools.primekg_query import PrimeKGQueryTool

    tool = PrimeKGQueryTool()
    out = json.loads(tool.invoke({"operation": "drug_disease", "drug": "dasatinib", "disease": "GIST tumour",
                                  "genes": ["ignored"]}))
    assert "error" in out or out["result"]["drug"]["name"] == "Dasatinib"
    ok = json.loads(tool.invoke({"operation": "polypharmacy", "drugs": ["warfarin", "fluconazole"]}))
    assert ok["result"]["pairs"][0]["recorded_interaction"] is True
    assert "index" not in json.dumps(ok)  # internal indices are stripped
    missing = json.loads(tool.invoke({"operation": "gene_profile", "gene": "NOTAGENE123"}))
    assert missing["kind"] == "not_found"


def test_run_manifest_digest():
    from phronesis.provenance.run_manifest import output_digest

    data = {"provenance": {"dataverse_version": "2.1", "edges_sha256": "ab" * 32},
            "analyses": [{"operation": "repurposing", "resolved": [{"curie": "MONDO_0011719"}]},
                         {"operation": "paths", "error": "no path"}]}
    d = output_digest("primekg_investigate", data)
    assert d == {"primekg_version": "2.1", "primekg_edges_sha256": "ab" * 32, "analyses": ["repurposing", "paths"],
                 "failed": ["paths"], "resolved": ["MONDO_0011719"]}


def test_drugbank_ids_are_citations_and_verified_family():
    from phronesis.agents.citations import make_key, scan_keys, url_for_key
    from phronesis.extensions.id_families import extract_family_ids

    assert make_key("DRUGBANK", "db00945") == "DRUGBANK:DB00945"
    assert url_for_key("DRUGBANK:DB00945") == "https://go.drugbank.com/drugs/DB00945"
    assert [k for _, k in scan_keys("Aspirin (DB00945) and DB123456.")] == ["DRUGBANK:DB00945"]
    assert extract_family_ids("aspirin DB00945")["drugbank"] == {"DB00945"}
