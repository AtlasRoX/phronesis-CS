"""Regression tests for the variant-intelligence audit fixes DAT-005, DAT-006, DAT-010 (variants part) and DAT-015.

All offline: captured fixtures in tests/fixtures/variant_intel, copied and modified in memory only.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agents.query_context import QueryContext
from phronesis.tools.registry import ToolResult
from phronesis.variants.checks import check_variant_frequencies
from phronesis.variants.clients import parse_clinvar_record
from phronesis.variants.models import review_stars
from phronesis.variants.render import (
    describe_variant_result,
    render_variant_evidence,
    variant_markdown_summary,
)
from phronesis.variants.service import compute_prevalence, parse_gnomad
from phronesis.variants.source import VariantIntelSource
from tests.test_variant_intel import PAAD, Upstream, _offline_clients, _run  # noqa: F401  (autouse fixture)

FIX = Path(__file__).parent / "fixtures" / "variant_intel"
NO_HEDGE = re.compile(r"(?!x)x")


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def kras_result(data: Dict[str, Any] = None) -> ToolResult:
    raw = fx("tool_result_kras_g12d_pancreatic.json")
    if data is not None:
        raw["data"] = data
    return ToolResult(**raw)


def vctx(data: Dict[str, Any], sentence: str) -> SimpleNamespace:
    res = ToolResult(success=True, tool_name="synapta_variant_intelligence", data=data)
    return SimpleNamespace(tool_results={"variant_intel": res}, evidence_documents=[], evidence_keys=[],
                           sentences=[sentence], hedge_pattern=NO_HEDGE)


def _ctx(query: str) -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query)


# -----------------------------------------------------------------------------
# DAT-005: gnomAD QC filters and flags
# -----------------------------------------------------------------------------

def _failed_gnomad_doc() -> Dict[str, Any]:
    doc = copy.deepcopy(fx("gnomad_12-25245350-C-T.json")["data"]["variant"])
    doc["exome"]["filters"] = ["AS_VQSR", "InbreedingCoeff"]
    doc["genome"]["filters"] = []
    doc["flags"] = ["lcr"]
    return doc


def test_parse_gnomad_keeps_per_dataset_filters_and_flags():
    g = parse_gnomad(_failed_gnomad_doc())
    assert g.exome_filters == ["AS_VQSR", "InbreedingCoeff"]
    assert g.genome_filters == []
    assert g.flags == ["lcr"]
    assert g.filters == ["AS_VQSR", "InbreedingCoeff"]  # merged list kept for compatibility
    clean = parse_gnomad(fx("gnomad_12-25245350-C-T.json")["data"]["variant"])
    assert clean.exome_filters == [] and clean.genome_filters == [] and clean.flags == []


def _kras_data_with_failed_exome() -> Dict[str, Any]:
    data = copy.deepcopy(fx("tool_result_kras_g12d_pancreatic.json")["data"])
    data["gnomad"] = parse_gnomad(_failed_gnomad_doc()).model_dump()
    return data


def test_failed_gnomad_call_is_rendered_and_noted():
    data = _kras_data_with_failed_exome()
    xml = render_variant_evidence(data)
    assert 'exome_qc="FAIL: AS_VQSR, InbreedingCoeff"' in xml
    assert 'genome_qc="PASS"' in xml
    assert 'flags="lcr"' in xml
    assert "GnomAD marks the exome call as failing QC filter(s) AS_VQSR, InbreedingCoeff" in xml
    md = variant_markdown_summary(data)
    assert "QC: gnomAD marks the exome call as failing QC filter(s) AS_VQSR, InbreedingCoeff" in md
    assert "gnomAD flags: lcr" in md
    _title, detail = describe_variant_result(data)
    assert "gnomAD AF 2.7e-06 (exome, germline; failed QC: AS_VQSR, InbreedingCoeff)" in detail
    notes = VariantIntelSource().scope_notes(_ctx("KRAS G12D prevalence in pancreatic cancer"), kras_result(data))
    assert any("gnomAD marks the exome call as failing QC filter(s) AS_VQSR, InbreedingCoeff" in n
               and "unreliable" in n for n in notes)


def test_passing_gnomad_call_has_no_qc_note():
    data = fx("tool_result_kras_g12d_pancreatic.json")["data"]
    data["gnomad"] = parse_gnomad(fx("gnomad_12-25245350-C-T.json")["data"]["variant"]).model_dump()
    xml = render_variant_evidence(data)
    assert 'exome_qc="PASS"' in xml and "failing QC" not in xml
    notes = VariantIntelSource().scope_notes(_ctx("KRAS G12D prevalence in pancreatic cancer"), kras_result(data))
    assert not any("QC" in n for n in notes)


def test_qc_failed_frequency_without_caveat_is_a_major_defect():
    data = _kras_data_with_failed_exome()
    defects = check_variant_frequencies(vctx(data, "In gnomAD, the KRAS G12D exome allele frequency is 2.7e-06."))
    assert [d["severity"] for d in defects] == ["MAJOR"]
    assert "failed QC filters" in defects[0]["description"]
    # The same figure with the QC caveat is a correct quote.
    caveated = "In gnomAD, the KRAS G12D exome allele frequency is 2.7e-06, but this call fails QC filters."
    assert check_variant_frequencies(vctx(data, caveated)) == []
    # The exome allele counts are covered too; the genome call passed QC, so its counts need no caveat.
    g = data["gnomad"]
    pair = f"In gnomAD, {g['exome_ac']}/{g['exome_an']} exome alleles carry KRAS G12D."
    assert [d["severity"] for d in check_variant_frequencies(vctx(data, pair))] == ["MAJOR"]
    genome = f"In gnomAD, {g['genome_ac']}/{g['genome_an']} genome alleles carry KRAS G12D."
    assert check_variant_frequencies(vctx(data, genome)) == []


def test_clean_gnomad_frequency_passes_without_caveat():
    data = fx("tool_result_kras_g12d_pancreatic.json")["data"]
    data["gnomad"] = parse_gnomad(fx("gnomad_12-25245350-C-T.json")["data"]["variant"]).model_dump()
    assert check_variant_frequencies(vctx(data, "In gnomAD, the KRAS G12D exome allele frequency is 2.7e-06.")) == []


# -----------------------------------------------------------------------------
# DAT-006: ClinVar conditions and review stars
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("status, stars", [
    ("practice guideline", 4),
    ("reviewed by expert panel", 3),
    ("criteria provided, multiple submitters, no conflicts", 2),
    ("criteria provided, conflicting classifications", 1),
    ("criteria provided, single submitter", 1),
    ("no assertion criteria provided", 0),
    ("no classification provided", 0),
    (None, None),
    ("", None),
])
def test_review_stars_known_answers(status, stars):
    assert review_stars(status) == stars


def test_parse_clinvar_record_sets_stars_from_captured_esummary():
    doc = fx("clinvar_esummary_12582_12583.json")["result"]["12582"]
    rec = parse_clinvar_record(doc)
    assert rec.germline_review_status == "criteria provided, multiple submitters, no conflicts"
    assert rec.germline_review_stars == 2
    assert rec.oncogenicity_review_stars == 1 and rec.somatic_clinical_impact_review_stars == 1
    assert rec.germline_traits[0] == "KRAS-related disorder"
    conflicting = parse_clinvar_record(fx("clinvar_esummary_13961.json")["result"]["13961"])
    stars = {conflicting.germline_review_status: conflicting.germline_review_stars}
    assert all(review_stars(k) == v for k, v in stars.items())


def test_kras_g12d_germline_classification_rendered_with_conditions_and_stars():
    data = fx("tool_result_kras_g12d_pancreatic.json")["data"]
    xml = render_variant_evidence(data)
    assert 'germline_review_stars="2"' in xml
    assert 'germline_conditions="KRAS-related disorder; Congenital Pulmonary Airway Malformations; Linear nevus ' \
           'sebaceous syndrome; Cardiovascular phenotype; Cerebral arteriovenous malformation; not provided"' in xml
    assert ("Germline classification Pathogenic/Likely pathogenic for condition(s): KRAS-related disorder; "
            "Congenital Pulmonary Airway Malformations") in xml
    assert "(criteria provided, multiple submitters, no conflicts, 2 of 4 review stars)" in xml
    assert 'oncogenicity_review_stars="1"' in xml and 'somatic_clinical_impact_review_stars="1"' in xml
    assert "applies only to the condition(s) listed with it" in xml
    md = variant_markdown_summary(data)
    assert ("Germline: Pathogenic/Likely pathogenic for KRAS-related disorder; Congenital Pulmonary Airway "
            "Malformations; Linear nevus sebaceous syndrome; Cardiovascular phenotype; +2 more "
            "(criteria provided, multiple submitters, no conflicts, 2 of 4 review stars)") in md


def test_prompt_rule_says_classification_applies_to_listed_conditions():
    from phronesis.extensions.variants import PROMPT_RULE

    assert "applies only to the condition(s) listed with it" in PROMPT_RULE
    assert "star rating" in PROMPT_RULE and "failing QC filters" in PROMPT_RULE


# -----------------------------------------------------------------------------
# DAT-015: patient numerators over patient denominators only
# -----------------------------------------------------------------------------

def _two_samples_per_patient():
    rows, muts = [], []
    for i in range(1, 6):  # 5 patients, 2 profiled samples each -> 10 samples
        for s in ("T01", "T02"):
            rows.append({"sampleId": f"P{i}-{s}", "patientId": f"P{i}", "profiled": True})
    for sid, pid in (("P1-T01", "P1"), ("P1-T02", "P1"), ("P2-T01", "P2")):
        muts.append({"sampleId": sid, "patientId": pid, "proteinChange": "G12D", "mutationStatus": "SOMATIC"})
    return muts, rows


def test_compute_prevalence_counts_profiled_patients():
    muts, rows = _two_samples_per_patient()
    st = compute_prevalence(muts, rows, {}, gene="KRAS", protein_change="G12D", study={"studyId": "s1", "name": "S1"})
    assert (st.mutated_samples, st.profiled_samples) == (3, 10)
    assert (st.mutated_patients, st.profiled_patients) == (2, 5)
    # Captured PAAD study: one sequenced sample per patient.
    paad = compute_prevalence(fx(f"cbio_mutations_{PAAD}_kras.json"), fx(f"cbio_gene_panel_data_{PAAD}.json"), {},
                              gene="KRAS", protein_change="G12D", study={"studyId": PAAD})
    assert (paad.mutated_samples, paad.profiled_samples, paad.profiled_patients) == (49, 179, 179)
    # Rows without patient ids: the patient denominator is unknown, never guessed.
    no_pid = [{k: v for k, v in r.items() if k != "patientId"} for r in rows]
    assert compute_prevalence(muts, no_pid, {}, gene="KRAS", protein_change="G12D").profiled_patients is None


def _study_data() -> Dict[str, Any]:
    muts, rows = _two_samples_per_patient()
    st = compute_prevalence(muts, rows, {}, gene="KRAS", protein_change="G12D", study={"studyId": "s1", "name": "S1"})
    return {"gene": "KRAS", "protein_change": "G12D", "cbioportal": [st.model_dump()], "clinvar": [], "gnomad": None}


@pytest.mark.parametrize("sentence, ok", [
    ("In cBioPortal, 2/10 patients carry KRAS G12D.", False),   # patients over the sample denominator
    ("In cBioPortal, 3/10 patients carry KRAS G12D.", False),   # sample counts called patients
    ("In cBioPortal, 2/5 patients carry KRAS G12D.", True),
    ("In cBioPortal, 3/10 profiled samples carry KRAS G12D.", True),
    ("In cBioPortal, 3/10 samples (30%) carry KRAS G12D.", True),
    ("In cBioPortal, 2/5 samples carry KRAS G12D.", False),      # patient counts called samples
])
def test_patient_and_sample_units_are_not_mixed(sentence, ok):
    defects = check_variant_frequencies(vctx(_study_data(), sentence))
    assert (defects == []) is ok, defects


def test_patient_claim_without_patient_denominator_is_not_certified():
    data = _study_data()
    data["cbioportal"][0]["profiled_patients"] = None
    assert check_variant_frequencies(vctx(data, "In cBioPortal, 2/10 patients carry KRAS G12D.")) != []


# -----------------------------------------------------------------------------
# DAT-010 (variants): release provenance in metadata, rendered profiled_patients
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_metadata_records_source_releases():
    upstream = Upstream()
    res = await _run(upstream)
    assert res.success
    rel = res.metadata["source_releases"]
    assert rel["gnomad"] == "gnomad_r4"
    assert rel["cbioportal"] == {PAAD: None}  # the trimmed studies fixture has no importDate
    assert "clinvar" in rel and "civic" in rel
    assert "retrieved_at" not in res.metadata  # stamped centrally by the registry, not by the tool
    hosts = {r.url.host for r in upstream.requests}
    assert hosts <= {"eutils.ncbi.nlm.nih.gov", "civicdb.org", "gnomad.broadinstitute.org", "www.cbioportal.org"}
    st = res.data["cbioportal"][0]
    assert st["profiled_patients"] == 179
    assert 'profiled_patients="179"' in render_variant_evidence(res.data)
