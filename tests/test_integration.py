"""
Cross-feature integration checks: one question that routes to the trials, drugs and variants sources, the
planner's parser fallbacks, verifier de-duplication and identifier matching, and literature grades in exports.
All offline; plugin fakes replay recorded fixtures or report an upstream failure, never invented data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from phronesis.agents.query_planner import build_query_context
from phronesis.export.report_model import ExportMessage, build_report_model, citation_line
from phronesis.extensions import hooks
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import ErrorType, ToolResult
from phronesis.variants.models import VariantIntelInput
from phronesis.variants.source import TOOL_NAME as VARIANT_TOOL
from tests.fakes import called, make_offline_orchestrator
from tests.test_clinical_trials import TOOL as TRIALS_TOOL
from tests.test_clinical_trials import osimertinib_result, trial_tool
from tests.test_drug_intel import _fake_drug_tools

FIXTURES = Path(__file__).parent / "fixtures"
TRIPLE_QUERY = "osimertinib trials and adverse events in EGFR L858R NSCLC"


def _variant_offline(kwargs: Dict[str, Any]) -> ToolResult:
    return ToolResult(
        success=False,
        tool_name=VARIANT_TOOL,
        error="variant databases are not reachable in offline tests",
        error_type=ErrorType.UPSTREAM_API_ERROR,
    )


@pytest.fixture
def all_plugin_tools():
    tools = dict(trial_tool(osimertinib_result()))
    tools.update(_fake_drug_tools())
    tools[VARIANT_TOOL] = (VariantIntelInput, _variant_offline)
    return tools


async def _ask(orch, query: str):
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(query, thread_id="t-integ", enable_web_search=False, event_callback=cb)
    steps = {e["data"]["id"]: e["data"] for e in events if e["type"] == "thought"}
    return res, steps


@pytest.mark.asyncio
async def test_one_question_routes_to_trials_drugs_and_variants(monkeypatch, tmp_path, all_plugin_tools):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=all_plugin_tools)
    res, steps = await _ask(orch, TRIPLE_QUERY)

    (trial_kw,) = called(calls, TRIALS_TOOL)
    assert trial_kw["intervention"] == "osimertinib"
    assert called(calls, "chembl_resolve_drugs")[0]["names"] == ["osimertinib"]
    (variant_kw,) = called(calls, VARIANT_TOOL)
    assert (variant_kw["gene"], variant_kw["protein_change"]) == ("EGFR", "L858R")

    assert steps["clinical_trials"]["status"] == "done" and steps["clinical_trials"]["agent"] == "Lithoscribe"
    assert steps["chembl"]["agent"] == "Synapta"
    assert steps["variant_intel"]["status"] == "error"
    plan = steps["plan"]
    assert "Trials: ClinicalTrials.gov" in plan["text"] and "Variant: EGFR" in plan["text"]
    assert "clinical_trials" in res.execution_artifacts and "variant_intel" not in res.execution_artifacts

    # The failed variant lookup is reported as a failure, not filled in.
    assert "UPSTREAM_API_ERROR" in steps["variant_intel"]["text"]
    assert steps["openfda"]["title"] == "Skipped the FDA lookup for osimertinib"  # no ChEMBL fixture for it
    assert res.verification_report["passed"] is True
    recorded = {c["tool_name"] for c in res.run_manifest["tool_calls"]}
    assert {TRIALS_TOOL, "chembl_resolve_drugs", VARIANT_TOOL} <= recorded
    assert res.query_context["primary_target"] == "EGFR L858R"
    json.dumps(res.to_dict())


@pytest.mark.asyncio
async def test_planner_falls_back_to_feature_parsers(monkeypatch):
    monkeypatch.setenv("PHRONESIS_PLANNER", "off")

    async def ctx_for(q: str):
        return await build_query_context(
            query=q, clean_query=q, registry=None, enable_web_search=False, attachments=[], attachment_errors=[],
            user_id="u", thread_id="t", model_name="m", api_key=None,
        )

    cdna = await ctx_for("Clinical significance of KRAS c.35G>A")
    (variant,) = cdna.variants
    assert (variant.gene, variant.label, variant.variant_kind, variant.validated) == ("KRAS", "c.35G>A", "hgvs", False)

    brand = await ctx_for("Adverse events of Tagrisso")
    (drug,) = brand.drugs
    assert drug.label == "tagrisso" and not drug.validated and drug.ids == {}
    assert brand.observed_identifiers()["chembl"] == set()

    dosing = await ctx_for("Pembrolizumab 200 mg IV Q3W toxicity")
    assert dosing.variants == []


def test_registered_checks_do_not_double_flag_a_claim():
    defect = {"category": "NUMERICAL", "severity": "MAJOR", "description": "d", "suggested_correction": "s", "claim": "ORR 99%"}
    with hooks.isolated_hooks():
        hooks.register_verification_check("a", lambda vctx: [dict(defect)])
        hooks.register_verification_check("b", lambda vctx: [dict(defect), dict(defect, category="CLINICAL_CLAIM")])
        report = ClinicalVerifier().verify_response("q", "The ORR was 99%.", {})
    flagged = [(d.category, d.claim) for d in report.defects if d.claim == "ORR 99%"]
    assert flagged == [("NUMERICAL", "ORR 99%"), ("CLINICAL_CLAIM", "ORR 99%")]


def test_a_number_in_passage_text_is_not_a_pmid():
    passage = json.loads((FIXTURES / "ctgov" / "pubmed_flaura_os_31751012.json").read_text(encoding="utf-8"))
    lit = {
        "lithoscribe": ToolResult(
            success=True,
            tool_name="lithoscribe_literature_synthesis",
            data={"ranked_passages": [{"pmid": passage["pmid"], "passage_text": passage["passage_text"]}]},
        )
    }
    verifier = ClinicalVerifier()
    # "79 of 279 patients" is in the passage; 279 is not a PMID it names.
    report = verifier.verify_response("q", "Osimertinib improved overall survival (PMID: 279).", lit)
    assert [d for d in report.defects if d.claim == "PMID 279"]
    ok = verifier.verify_response("q", "Median overall survival was 38.6 months (PMID: 31751012).", lit)
    assert "PMID:31751012" in ok.verified_identifiers


def test_export_citations_carry_literature_grades():
    message = ExportMessage(
        content="Lithoscribe retrieved PMID: 31751012.",
        execution_artifacts={
            "sources": [{"kind": "literature", "pmid": "31751012", "title": "Overall Survival with Osimertinib", "tool": "lithoscribe"}],
            "literature": {
                "graded_articles": [
                    {
                        "article_key": "MED:31751012",
                        "pmid": "31751012",
                        "grade": {"study_type": "rct", "label": "Randomized controlled trial"},
                        "integrity": {"status": "none"},
                    }
                ]
            },
        },
    )
    report = build_report_model("q", message)
    (c,) = report.citations
    assert c.grade == "Randomized controlled trial" and c.integrity == "" and c.cited_in_answer
    assert citation_line(c).endswith("[cited in answer; Randomized controlled trial]")


def test_all_checks_over_one_composite_answer():
    """Trials, drugs, variants, a retracted PMID and a GEO id in one answer: each wrong claim is flagged once."""
    import asyncio
    from collections import Counter

    from tests.test_clinical_trials import flaura_laura_result
    from tests.test_drug_intel import _run_sources
    from tests.test_evidence_grading import run_engine
    from tests.test_variant_intel import tool_result

    _, chembl, openfda, _ = asyncio.run(_run_sources("imatinib potency and adverse events against ABL1", target="ABL1"))
    lit = asyncio.run(run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808"]))
    tool_results = {
        "clinical_trials": flaura_laura_result(),
        "chembl": chembl,
        "openfda": openfda,
        "variant_intel": tool_result(),
        "lithoscribe": lit,
    }
    answer = (
        "In FLAURA (NCT02296125), the objective response rate with Osimertinib 80 mg (Global Cohort) was 80.1%.\n\n"
        "Imatinib binds ABL1 with a Kd of 1.3 nM in assay CHEMBL1908688.\n\n"
        "FAERS lists 10,200 reports mentioning imatinib.\n\n"
        "Across studies, KRAS G12D is present in 40% of pancreatic cancers.\n\n"
        "MMR vaccination was linked to autism in a randomized trial (PMID: 9500320).\n\n"
        "Single-cell data from GSE132465 show KRAS expression in tumour cells.\n"
    )
    report = ClinicalVerifier().verify_response("composite", answer, tool_results)
    pairs = Counter((d.category, d.claim) for d in report.defects if d.claim)
    assert [p for p, n in pairs.items() if n > 1] == []
    def about(prefix):
        return [d.description for d in report.defects if (d.claim or "").startswith(prefix)]

    assert about("In FLAURA") == ["figure 80.1 not found in the posted record of NCT02296125"]
    assert about("Imatinib binds ABL1") == ["Potency value 1.3 nM matches no ChEMBL activity record or retrieved passage."]
    assert about("FAERS lists") == ["FAERS count 10,200 does not equal any retrieved openFDA count."]
    (prevalence,) = about("Across studies")
    assert "'40%' matches no per-study prevalence" in prevalence
    assert {d.category for d in report.defects if d.claim == "PMID 9500320"} == {"RETRACTED_CITATION"}
    assert {d.category for d in report.defects if d.claim == "GSE132465"} == {"IDENTIFIER"}


def test_chembl_target_ids_link_to_target_pages():
    from phronesis.agents.citations import build_citation_map
    from phronesis.drugintel.sources import ChemblSource
    from tests.test_drug_intel import _run_sources

    import asyncio

    _, chembl, _, _ = asyncio.run(_run_sources("What is the mechanism of imatinib?"))
    answer = "Imatinib (CHEMBL941) inhibits Tyrosine-protein kinase ABL1 (CHEMBL1862)."
    report = ClinicalVerifier().verify_response("imatinib mechanism", answer, {"chembl": chembl})
    cmap = build_citation_map(answer, {"chembl": chembl}, report, {}, None, {"chembl": ChemblSource().citations(chembl)})
    entries = cmap["entries"]
    assert entries["CHEMBL:CHEMBL941"]["url"] == "https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL941"
    assert entries["CHEMBL:CHEMBL1862"]["url"] == "https://www.ebi.ac.uk/chembl/explore/target/CHEMBL1862"


def test_bracket_citation_markers_become_plain_citations():
    from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator

    text = (
        "Target engagement was shown【42310679】. Trials exist (NCT04303780)【clinical_trials】. "
        "KRAS G12D in 49/179 samples【cbioportal_prevalence study_id=\"paad_tcga_pan_can_atlas_2018\"】. "
        "Shorter survival【CIViC EID: EID1311】."
    )
    assert AuthoritativeOrchestrator._sanitize_agent_names(text) == (
        "Target engagement was shown (PMID: 42310679). Trials exist (NCT04303780). "
        "KRAS G12D in 49/179 samples. Shorter survival (EID1311)."
    )


def test_export_sources_keep_every_plugin_source_under_the_cap():
    import asyncio

    from phronesis.extensions.export import MAX_SOURCES, build_source_records
    from tests.test_variant_intel import tool_result

    _, chembl, openfda, _ = asyncio.run(_run_sources_for_export())
    records = build_source_records({
        "clinical_trials": osimertinib_result(),
        "chembl": chembl,
        "openfda": openfda,
        "variant_intel": tool_result(),
    })
    kinds = [r["kind"] for r in records]
    assert len(records) <= MAX_SOURCES
    assert {"nct", "chembl", "dailymed", "clinvar"} <= set(kinds)


def _run_sources_for_export():
    from tests.test_drug_intel import _run_sources

    return _run_sources("What are the adverse events of imatinib?")
