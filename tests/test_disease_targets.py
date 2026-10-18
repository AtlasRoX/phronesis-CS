"""Disease-level target evidence (Open Targets), the acronym fold and multi-part briefs. No network."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.agents.query_planner import _fold_defined_acronyms
from phronesis.agents.search_plan import build_search_plan, classify_question
from phronesis.diseaseintel.opentargets import summarize, trial_outcome
from phronesis.extensions.disease_targets import DiseaseTargetsSource
from phronesis.tools.registry import ToolResult

KINASES = ["PDGFRA", "PDGFRB", "FLT1", "FLT4", "KDR", "FGFR1", "FGFR3", "FGFR4", "FGFR2"]  # nine: past the display cut


def _assoc(symbol: str, score: float, **evidence: float) -> Dict[str, Any]:
    return {"score": score, "datatypeScores": [{"id": k, "score": v} for k, v in evidence.items()],
            "target": {"id": f"ENSG{abs(hash(symbol)) % 10**11:011d}", "approvedSymbol": symbol, "approvedName": symbol.lower(),
                       "tractability": [{"label": "Approved Drug", "modality": "SM", "value": True}]}}


def _drug(name: str, stage: str, targets: List[str], mechanism: str = "", reports: List[Dict[str, Any]] = ()) -> Dict[str, Any]:
    moa = {"rows": [{"mechanismOfAction": mechanism, "targets": [{"approvedSymbol": t} for t in targets]}]} if mechanism else {"rows": []}
    return {"maxClinicalStage": stage, "drug": {"id": f"CHEMBL{abs(hash(name)) % 10**6}", "name": name, "drugType": "Small molecule",
                                                 "mechanismsOfAction": moa}, "clinicalReports": list(reports)}


RAW = {
    "id": "EFO_0000768", "name": "idiopathic pulmonary fibrosis",
    "associatedTargets": {"count": 100, "rows": [
        _assoc("TERT", 0.73, genetic_association=0.88), _assoc("FGFR2", 0.60, clinical=0.97),
        _assoc("NR3C1", 0.59, clinical=0.95), _assoc("ACTB", 0.2, literature=0.2)]},
    "drugAndClinicalCandidates": {"count": 6, "rows": [
        _drug("NINTEDANIB", "APPROVAL", KINASES, "Tyrosine kinase inhibitor", [{"trialOverallStatus": "APPROVED_FOR_MARKETING"}]),
        _drug("PREDNISOLONE", "APPROVAL", ["NR3C1"], "Glucocorticoid receptor agonist", [{"trialOverallStatus": "COMPLETED"}]),
        _drug("PAMREVLUMAB", "PHASE_3", ["CCN2"], "CTGF inhibitor", [{"trialOverallStatus": "TERMINATED", "trialPhase": "PHASE3",
              "trialWhyStopped": "Study did not meet its primary endpoint.", "trialStopReasonCategories": ["Negative"]}]),
        _drug("ZIRITAXESTAT", "PHASE_3", ["ENPP2"], "Autotaxin inhibitor", [{"trialOverallStatus": "TERMINATED",
              "trialWhyStopped": "The benefit-risk profile no longer supports continuing", "trialStopReasonCategories": ["Business_Administrative"]}]),
        _drug("NERANDOMILAST", "PREAPPROVAL", [], "", [{"trialOverallStatus": "RECRUITING"}]),
        _drug("WARFARIN", "PHASE_3", ["VKORC1"], "VKOR inhibitor", [{"trialOverallStatus": "TERMINATED",
              "trialWhyStopped": "Excess of mortality in the treatment group", "trialStopReasonCategories": []}]),
    ]},
}


def test_trial_outcomes_read_status_category_and_stop_text():
    assert trial_outcome([{"trialOverallStatus": "APPROVED_FOR_MARKETING"}])["outcome"] == "approved for this disease"
    assert trial_outcome([{"trialWhyStopped": "benefit-risk no longer supports", "trialStopReasonCategories": []}])["outcome"].startswith("failed")
    assert trial_outcome([{"trialWhyStopped": "Excess of mortality", "trialStopReasonCategories": []}])["outcome"] == "stopped for safety"
    # Futility wording wins over a passing mention of safety.
    assert trial_outcome([{"trialWhyStopped": "futility analysis; safety fine"}])["outcome"].startswith("failed")
    assert trial_outcome([{"trialOverallStatus": "RECRUITING"}])["outcome"] == "trials ongoing"


def test_summary_tiers_classes_and_failures():
    data = summarize(RAW, target_limit=10, candidate_limit=1)
    tiers = data["tiers"]
    # Only an approval recorded for this disease validates: prednisolone's 'APPROVAL' stage comes from elsewhere.
    assert [v["drug"] for v in tiers["validated"]] == ["NINTEDANIB"]
    classes = {t["symbol"]: t["evidence_class"] for t in data["targets"]}
    assert classes == {"TERT": "human_genetics", "FGFR2": "clinically_validated", "NR3C1": "clinically_tested", "ACTB": "exploratory"}
    tested = [(c["drug"], c["outcome"]) for c in tiers["clinically_tested"]]
    assert tested[0] == ("NERANDOMILAST", "trials ongoing")  # filed for approval leads; its class comes from the INN stem
    assert "PDE4" in tiers["clinically_tested"][0]["mechanism"] and "INN stem" in tiers["clinically_tested"][0]["mechanism"]
    failed = {d for d, o in tested if o.startswith(("failed", "stopped"))}
    assert {"PAMREVLUMAB", "ZIRITAXESTAT", "WARFARIN"} <= failed  # kept beyond candidate_limit as cautionary evidence
    assert tiers["genetic_risk_mechanisms"] == ["TERT"]


def _ctx(query: str, disease: ResolvedEntity, primary: Any = None) -> QueryContext:
    ctx = QueryContext(raw_query=query, clean_query=query, entities=[disease])
    ctx.primary_gene = primary
    ctx.primary_target = primary.label if primary else None
    ctx.search_plan = build_search_plan(ctx, query)
    return ctx


IPF = ResolvedEntity(type="disease", mention="idiopathic pulmonary fibrosis", span=(39, 68), label="idiopathic pulmonary fibrosis",
                     ids={"ontology_id": "EFO_0000768"}, source="OpenTargets", matched_on="name", validated=True)


def test_source_routes_disease_target_questions_only():
    src = DiseaseTargetsSource()
    assert src.should_run(_ctx("Identify the top 5 therapeutic targets for idiopathic pulmonary fibrosis", IPF)).run
    kras = ResolvedEntity(type="gene", mention="KRAS", label="KRAS", source="HGNC", validated=True)
    assert not src.should_run(_ctx("Is KRAS a target in idiopathic pulmonary fibrosis?", IPF, primary=kras)).run
    unresolved = IPF.model_copy(update={"ids": {}})
    assert not src.should_run(_ctx("targets for idiopathic pulmonary fibrosis", unresolved)).run


def test_evidence_groups_one_drugs_targets_and_drops_rank_order():
    data = summarize(RAW, target_limit=10, candidate_limit=5)
    res = ToolResult(success=True, tool_name="opentargets_disease_targets", data=data)
    xml = DiseaseTargetsSource().evidence_xml(res)
    assert xml.index("<validated") < xml.index("<clinically_tested") < xml.index("<associated_genes")
    assert 'rank="' not in xml and "<gene_class" in xml and 'symbol="FGFR2"' not in xml  # FGFR2 sits in the class
    assert 'outcome="failed (efficacy or futility)" stop_reason="Study did not meet its primary endpoint."' in xml
    ids = DiseaseTargetsSource().allowed_identifiers(res)
    assert ids["ontology"] == {"EFO_0000768"} and ids["chembl"] and ids["ensembl"]


def test_acronym_defined_in_the_question_is_an_alias_not_a_lookup():
    q = "top targets for idiopathic pulmonary fibrosis (IPF)?"
    full = ResolvedEntity(type="disease", mention="idiopathic pulmonary fibrosis", span=(16, 45), label="idiopathic pulmonary fibrosis",
                          ids={"ontology_id": "EFO_0000768"}, source="OpenTargets", validated=True)
    wrong = ResolvedEntity(type="disease", mention="IPF", span=(47, 50), label="platelet quantity",
                           ids={"ontology_id": "OBA_VT0003179"}, source="OpenTargets", validated=True)
    out = _fold_defined_acronyms(q, [full, wrong])
    assert out == [full] and full.aliases == ["IPF"]


def test_long_brief_searches_entities_and_ranks_by_its_checklist():
    q = ("Identify and rank the top 5 therapeutic targets for idiopathic pulmonary fibrosis (IPF). For each target, explain its "
         "biological role, disease mechanism, supporting human evidence, druggability, known inhibitors, safety risks, and key "
         "evidence gaps. Distinguish validated targets from exploratory hypotheses, and cite primary sources.")
    disease = IPF.model_copy(update={"span": (52, 81), "aliases": ["IPF"]})
    plan = build_search_plan(SimpleNamespace(primary_gene=None, entities=[disease]), q)
    assert classify_question(q) == "target"  # the ask, not the incidental "safety risks"
    assert plan.concepts == [["idiopathic pulmonary fibrosis", "IPF"]]
    assert "known inhibitors" in plan.ranking_terms
