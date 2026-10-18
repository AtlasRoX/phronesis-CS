"""Audit fixes KG-009, KG-010, KG-013, KG-014: Open Targets trial-outcome polarity, approval scope, association
datatype ids, and multi-word disease acceptance in the entity resolver. No network."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from phronesis.diseaseintel.opentargets import POSITIVE_STOP, stop_text_polarity, summarize, trial_outcome
from phronesis.extensions.disease_targets import DiseaseTargetsSource
from phronesis.tools.registry import ToolResult
from phronesis.utils.biocontext.client import BioContextMCPClient
from phronesis.utils.biocontext.entity_resolver import EntityResolver

# -----------------------------------------------------------------------------
# KG-009: stop-reason polarity
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("why,expected", [
    # successes and positive early stops
    ("Stopped early: primary endpoint met (overwhelming efficacy)", "positive"),
    ("Trial stopped early due to significant reduction in mortality in the treatment arm", "positive"),
    ("positive benefit-risk established", "positive"),
    ("The study met its primary endpoint at the interim analysis", "positive"),
    ("Stopped early for efficacy; no safety concerns", "positive"),
    ("DSMB recommended early termination for efficacy. Efficacy was demonstrated at interim.", "positive"),
    # failures
    ("Study did not meet its primary endpoint.", "negative"),
    ("Primary endpoint not met", "negative"),
    ("Terminated for futility at the interim analysis", "negative"),
    ("Lack of efficacy", "negative"),
    ("The benefit-risk profile no longer supports continuing", "negative"),
    ("Interim analysis showed no significant benefit", "negative"),
    ("futility analysis; safety fine", "negative"),
    # safety
    ("Excess of mortality in the treatment group", "safety"),
    ("Increased number of deaths in the experimental arm", "safety"),
    ("Unacceptable toxicity", "safety"),
    ("Safety concerns raised by the DSMB", "safety"),
    # neither
    ("Sponsor decision; not due to safety reasons", None),
    ("Slow enrollment", None),
    ("Funding withdrawn", None),
    ("", None),
])
def test_stop_text_polarity_table(why, expected):
    assert stop_text_polarity(why) == expected


def test_positive_early_stop_is_never_failed_and_registry_category_wins():
    # The audit's failure scenario (KG-009): a "Success" category and positive text -> never "failed".
    out = trial_outcome([{"trialOverallStatus": "TERMINATED", "trialWhyStopped": "Stopped early: primary endpoint met (overwhelming efficacy)",
                          "trialStopReasonCategories": ["Success"]}])
    assert out["outcome"] == POSITIVE_STOP and not out["outcome"].startswith(("failed", "stopped"))
    assert out["stop_reason"].startswith("Stopped early: primary endpoint met")
    # The structured category is preferred over ambiguous text.
    assert trial_outcome([{"trialWhyStopped": "primary endpoint analysis completed", "trialStopReasonCategories": ["Endpoint_Met"]}])["outcome"] == POSITIVE_STOP
    assert trial_outcome([{"trialWhyStopped": "see publication", "trialStopReasonCategories": ["Safety_Sideeffects"]}])["outcome"] == "stopped for safety"
    assert trial_outcome([{"trialWhyStopped": "see publication", "trialStopReasonCategories": ["Negative"]}])["outcome"].startswith("failed")
    # Mortality reduced in the treatment arm is efficacy, not a safety stop.
    assert trial_outcome([{"trialWhyStopped": "Trial stopped early due to significant reduction in mortality in the treatment arm",
                           "trialStopReasonCategories": []}])["outcome"] == POSITIVE_STOP
    # A business-filed stop whose text says the benefit-risk no longer holds is still a failure.
    assert trial_outcome([{"trialWhyStopped": "The benefit-risk profile no longer supports continuing",
                           "trialStopReasonCategories": ["Business_Administrative"]}])["outcome"].startswith("failed")


def _drug(name: str, stage: str, reports: List[Dict[str, Any]], targets=("EGFR",)) -> Dict[str, Any]:
    return {"maxClinicalStage": stage, "drug": {"id": f"CHEMBL{abs(hash(name)) % 10**6}", "name": name, "drugType": "Small molecule",
                                                 "mechanismsOfAction": {"rows": [{"mechanismOfAction": "inhibitor",
                                                                                  "targets": [{"approvedSymbol": t} for t in targets]}]}},
            "clinicalReports": reports}


def _raw(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": "MONDO_0005192", "name": "pancreatic carcinoma",
            "associatedTargets": {"count": 1, "rows": [{"score": 0.5, "datatypeScores": [{"id": "clinical", "score": 0.9}],
                                                         "target": {"id": "ENSG00000146648", "approvedSymbol": "EGFR",
                                                                    "approvedName": "egfr", "tractability": []}}]},
            "drugAndClinicalCandidates": {"count": len(rows), "rows": rows}}


def test_positive_early_stop_stays_with_promising_drugs_not_failures():
    data = summarize(_raw([_drug("WINNER", "PHASE_3", [{"trialOverallStatus": "TERMINATED", "trialPhase": "PHASE3",
                                                         "trialWhyStopped": "Stopped early: primary endpoint met",
                                                         "trialStopReasonCategories": ["Success"]}])]),
                     target_limit=5, candidate_limit=5)
    tested = data["tiers"]["clinically_tested"]
    assert [(c["drug"], c["outcome"]) for c in tested] == [("WINNER", POSITIVE_STOP)]
    xml = DiseaseTargetsSource().evidence_xml(ToolResult(success=True, tool_name="opentargets_disease_targets", data=data))
    assert "failed" not in xml.split("<clinically_tested", 1)[1].split("</clinically_tested>", 1)[0].split("note=")[1].split('">', 1)[1]
    desc = DiseaseTargetsSource().describe(ToolResult(success=True, tool_name="opentargets_disease_targets", data=data), None)[1]
    assert "(0 failed or stopped)" in desc


# -----------------------------------------------------------------------------
# KG-010: approval scope and basis
# -----------------------------------------------------------------------------


def test_approval_stage_without_disease_trials_is_labelled_not_claimed_for_this_disease():
    data = summarize(_raw([
        _drug("ELSEWHERE", "APPROVAL", [{"trialOverallStatus": "COMPLETED", "trialPhase": "PHASE1"}]),
        _drug("TESTEDHERE", "APPROVAL", [{"trialOverallStatus": "COMPLETED", "trialPhase": "PHASE2/PHASE3"}]),
        _drug("LABELLED", "APPROVAL", [{"trialOverallStatus": "APPROVED_FOR_MARKETING"}], targets=("KRAS",)),
    ]), target_limit=5, candidate_limit=5)
    tested = {c["drug"]: c for c in data["tiers"]["clinically_tested"]}
    assert "no phase 2 or later trial for this disease" in tested["ELSEWHERE"]["approval_scope"]
    assert tested["ELSEWHERE"]["disease_trial_phase"] == 1
    assert tested["TESTEDHERE"]["disease_trial_phase"] == 3
    assert "no record here confirms approval for this disease" in tested["TESTEDHERE"]["approval_scope"]
    assert "no phase 2 or later" not in tested["TESTEDHERE"]["approval_scope"]
    validated = data["tiers"]["validated"]
    assert [v["drug"] for v in validated] == ["LABELLED"] and "expanded-access" in validated[0]["approval_basis"]
    xml = DiseaseTargetsSource().evidence_xml(ToolResult(success=True, tool_name="opentargets_disease_targets", data=data))
    assert "phase 2 or later for this disease, with the recorded" not in xml  # the old over-claim
    assert "expanded-access status" in xml
    assert 'approval_scope="approved for some indication' in xml and 'disease_trial_phase="1"' in xml


# -----------------------------------------------------------------------------
# KG-013: Open Targets association datatype ids
# -----------------------------------------------------------------------------

# A row shaped as the OT v4 GraphQL associatedDiseases response (datatype ids as Open Targets returns them).
OT_ASSOC_PAYLOAD = {"data": {"target": {
    "id": "ENSG00000133703", "approvedSymbol": "KRAS", "approvedName": "KRAS proto-oncogene, GTPase", "biotype": "protein_coding",
    "associatedDiseases": {"count": 2, "rows": [
        {"disease": {"id": "EFO_1000044", "name": "pancreatic adenocarcinoma", "description": ""}, "score": 0.8123,
         "datatypeScores": [{"id": "somatic_mutation", "score": 0.9311}, {"id": "clinical", "score": 0.8702},
                            {"id": "literature", "score": 0.95}, {"id": "genetic_association", "score": 0.61}]},
        {"disease": {"id": "MONDO_0005148", "name": "type 2 diabetes mellitus", "description": ""}, "score": 0.12,
         "datatypeScores": [{"id": "literature", "score": 0.3}]},
    ]}}}}


@pytest.mark.asyncio
async def test_ot_association_datatypes_match_current_ids_and_absent_is_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OT_ASSOC_PAYLOAD)

    client = BioContextMCPClient()
    monkeypatch.setattr(client, "_get_async_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = await client.async_query_opentargets_associations("ENSG00000133703", size=2)
    first, second = res["associations"]
    assert first["somatic_mutation_score"] == 0.9311
    assert first["known_drug_score"] == 0.8702  # OT "clinical" datatype
    assert first["genetic_association_score"] == 0.61 and first["literature_score"] == 0.95
    assert first["datatype_scores"]["somatic_mutation"] == 0.9311
    # Datatypes absent from the row are None (unknown), not a measured 0.0.
    assert second["somatic_mutation_score"] is None and second["known_drug_score"] is None
    assert second["genetic_association_score"] is None and second["literature_score"] == 0.3


# -----------------------------------------------------------------------------
# KG-014: multi-word disease acceptance
# -----------------------------------------------------------------------------


def _hit(name: str, hid: str = "MONDO_0000001") -> Dict[str, Any]:
    return {"entity": "disease", "id": hid, "name": name, "score": 1}


@pytest.mark.parametrize("term,hits", [
    ("colon cancer", [_hit("breast cancer")]),
    ("pancreatic cancer", [_hit("pancreatic insufficiency")]),
    ("chronic myeloid leukemia", [_hit("acute myeloid leukemia")]),
    ("lung adenocarcinoma", [_hit("lung disease")]),
])
def test_multiword_disease_needs_every_distinctive_word(term, hits):
    assert EntityResolver._accept_disease(term, hits) is None


@pytest.mark.parametrize("term,hits,expected", [
    ("pancreatic cancer", [_hit("pancreatic carcinoma", "MONDO_0005192")], "pancreatic carcinoma"),
    ("breast cancer", [_hit("breast carcinoma", "EFO_0000305")], "breast carcinoma"),
    ("prostate cancers", [_hit("prostate cancer")], "prostate cancer"),
    ("type 2 diabetes", [_hit("type 2 diabetes mellitus")], "type 2 diabetes mellitus"),
    ("Alzheimer's disease", [_hit("Alzheimer disease")], "Alzheimer disease"),
    ("acute myeloid leukaemia", [_hit("acute myeloid leukemia")], "acute myeloid leukemia"),
    ("metastatic pancreatic cancer", [_hit("pancreatic carcinoma")], "pancreatic carcinoma"),
    # Of two qualifying hits the one adding the fewest words wins: the concept, not a subtype.
    ("colon cancer", [_hit("sigmoid colon cancer"), _hit("colon carcinoma")], "colon carcinoma"),
])
def test_multiword_disease_accepts_equivalent_names(term, hits, expected):
    hit = EntityResolver._accept_disease(term, hits)
    assert hit is not None and hit["name"] == expected
