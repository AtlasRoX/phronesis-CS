"""
Offline tests for the citation map (feature 4). Every identifier, title and passage comes from trimmed
live responses under tests/fixtures/citations/ (EuropePMC, BioModels, Open Targets, ClinicalTrials.gov,
ChEMBL); no test touches the network.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.citations import (
    MEMORY_NOTE,
    build_citation_map,
    scan_answer,
    scan_keys,
)
from phronesis.phronesis.agents.verifier import ClinicalVerifier, DefectItem, DefectSeverity, VerificationReport
from phronesis.sources.base import CitationRecord
from phronesis.tools.registry import ToolResult
from tests.fakes import make_offline_orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "citations"


def _load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


LIVE = _load("live_samples.json")


def _record(name: str) -> Dict[str, Any]:
    return LIVE["europepmc_core"][name][0]


def _passages(art: Dict[str, Any], start: int = 0, *, grading_fields: bool = True, pmid: Any = "auto") -> List[Dict[str, Any]]:
    """Lithoscribe-shaped passages from an EuropePMC core record (abstract chunked 2 sentences at a time,
    as LiteratureSynthesisEngine does). grading_fields adds the fields the grading feature passes through."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", art["abstractText"]) if len(s.strip()) > 30]
    out = []
    for i in range(0, min(len(sentences), 6), 2):
        p = {
            "passage_id": f"psg_{start + len(out):03d}",
            "pmid": art.get("pmid") if pmid == "auto" else pmid,
            "doi": art.get("doi"),
            "pmcid": art.get("pmcid"),
            "title": art.get("title", ""),
            "authors": art.get("authorString", ""),
            "journal": (art.get("journalInfo") or {}).get("journal", {}).get("title") or "",
            "year": art.get("pubYear", ""),
            "section": "Abstract",
            "passage_text": " ".join(sentences[i : i + 2]),
            "has_full_text": False,
        }
        if grading_fields:
            p["pub_types"] = list((art.get("pubTypeList") or {}).get("pubType") or [])
        out.append(p)
    return out


def _lit(passages: List[Dict[str, Any]], **extra: Any) -> Dict[str, ToolResult]:
    return {
        "lithoscribe": ToolResult(
            success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": passages, **extra}
        )
    }


def _report(verified=(), defects=(), claim_support=(), vetoed=()) -> VerificationReport:
    return VerificationReport(
        passed=not defects,
        verification_score=1.0 if not defects else 0.45,
        verified_identifiers=list(verified),
        defects=list(defects),
        claim_support=list(claim_support),
        vetoed_identifiers=list(vetoed),
    )


# -------------------------------------------------------------
# Builder
# -------------------------------------------------------------

def test_literature_entry_has_passage_highlight_title_and_level():
    review = _record("pmid_42511798")
    html_title = _record("pmid_42768907_html_title")
    tools = _lit(_passages(review) + _passages(html_title, start=3))
    answer = (
        "Covalent inhibitors were enabled by a cryptic binding pocket accessible in the GDP-bound state "
        "(PMID: 42511798). Covalent MiniFrags were screened against KRas G12C (PMID: 42768907)."
    )
    cmap = build_citation_map(answer, tools, _report(verified=["PMID:42511798", "PMID:42768907"]))

    entry = cmap["entries"]["PMID:42511798"]
    assert entry["url"] == "https://pubmed.ncbi.nlm.nih.gov/42511798/"
    assert entry["source_agent"] == "Lithoscribe" and entry["verified"] is True
    assert entry["journal"] == "International journal of molecular sciences" and entry["year"] == "2026"
    assert entry["evidence_level"] == "review"  # real pubTypeList: review-article, Review, Journal Article
    assert entry["retracted"] is None

    passage = entry["passages"][0]
    assert passage["highlight"].startswith("The discovery of a cryptic binding pocket accessible in the GDP-bound state")
    assert passage["highlight"] in passage["text"]
    assert passage["text"] in clean(review["abstractText"])

    support = cmap["supports"][0]
    assert support["key"] == "PMID:42511798" and support["occurrence"] == 0
    assert support["highlight"] == passage["highlight"] and support["grounded_by_verifier"] is False
    assert {"cryptic", "pocket", "gdp-bound"} <= set(support["shared_terms"])

    titled = cmap["entries"]["PMID:42768907"]
    assert "&lt;" not in titled["title"] and "<sup>" not in titled["title"]
    assert titled["title"].endswith("Covalent KRasG12C Inhibitors.")
    assert "<sup>" not in titled["passages"][0]["text"]


def clean(text: str) -> str:
    from phronesis.agents.citations import clean_text

    return clean_text(text)


def test_doi_in_another_case_resolves_to_the_pmid_entry():
    review = _record("pmid_42511798")
    tools = _lit(_passages(review))
    answer = "Review of KRAS G12C therapy (doi:10.3390/IJMS27146455)."
    cmap = build_citation_map(answer, tools, _report(verified=["DOI:10.3390/IJMS27146455"]))
    assert cmap["aliases"]["DOI:10.3390/ijms27146455"] == "PMID:42511798"
    assert list(cmap["entries"]) == ["PMID:42511798"]
    assert cmap["supports"][0]["key"] == "PMID:42511798"


def test_unobserved_pmid_with_defect_goes_to_unverified():
    tools = _lit(_passages(_record("pmid_42511798")))
    answer = "KRAS G12C review (PMID: 42511798); another claim (PMID: 99999999)."
    report = ClinicalVerifier().verify_response("KRAS G12C", answer, tools)
    assert any(d.claim == "PMID 99999999" for d in report.defects)
    cmap = build_citation_map(answer, tools, report)
    assert "PMID:99999999" not in cmap["entries"]
    assert cmap["unverified"] == ["PMID:99999999"]
    assert "PMID:42511798" in cmap["entries"]


def test_retracted_flag_true_only_when_a_source_says_so():
    wakefield = _record("pmid_9500320")
    review = _record("pmid_42511798")
    tools = _lit(_passages(wakefield) + _passages(review, start=3, grading_fields=False))
    answer = "Retracted case series (PMID: 9500320). Review (PMID: 42511798)."
    cmap = build_citation_map(answer, tools, _report(verified=["PMID:9500320", "PMID:42511798"]))
    assert cmap["entries"]["PMID:9500320"]["retracted"] is True
    assert "Retracted Publication" in cmap["entries"]["PMID:9500320"]["pub_types"]
    # No publication types reported: unknown, never False.
    assert cmap["entries"]["PMID:42511798"]["retracted"] is None
    assert cmap["entries"]["PMID:42511798"]["evidence_level"] is None


def test_retraction_notice_from_graded_article_integrity():
    wakefield = _record("pmid_9500320")
    notice = next(c for c in wakefield["commentCorrectionList"]["commentCorrection"] if c["id"] == "20137807")
    graded = {
        "pmid": "9500320",
        "doi": wakefield["doi"],
        "integrity": {"status": "retracted", "notices": [{"kind": "retraction", "source": "europepmc", "reference": notice["reference"]}]},
    }
    tools = _lit(_passages(wakefield, grading_fields=False), excluded_articles=[graded])
    cmap = build_citation_map("Case series (PMID: 9500320).", tools, _report(verified=["PMID:9500320"]))
    entry = cmap["entries"]["PMID:9500320"]
    assert entry["retracted"] is True
    assert entry["retraction_notice"] == "Lancet. 2010 Feb 6;375(9713):445. doi: 10.1016/S0140-6736(10)60175-4"


def test_vetoed_retracted_pmid_is_unverified_and_not_linked():
    tools = _lit(_passages(_record("pmid_9500320")))
    report = _report(
        defects=[DefectItem(category="RETRACTED_CITATION", severity=DefectSeverity.CRITICAL, description="retracted",
                            suggested_correction="remove", claim="PMID 9500320")],
        vetoed=["PMID:9500320"],
    )
    cmap = build_citation_map("Vaccines and autism (PMID: 9500320).", tools, report)
    assert cmap["entries"] == {} and cmap["unverified"] == ["PMID:9500320"]


def test_ids_inside_code_are_not_counted():
    tools = _lit(_passages(_record("pmid_42511798")))
    answer = (
        "```\nPMID: 42511798\n```\n"
        "Inline `PMID: 42511798` is a code sample.\n"
        "Covalent inhibitors target the GDP-bound pocket (PMID: 42511798). Again PMID 42511798."
    )
    cmap = build_citation_map(answer, tools, _report(verified=["PMID:42511798"]))
    supports = [s for s in cmap["supports"] if s["key"] == "PMID:42511798"]
    assert [s["occurrence"] for s in supports] == [0, 1]
    assert supports[0]["sentence"].startswith("Covalent inhibitors target")
    assert supports[1]["sentence"] == "Again PMID 42511798."


def test_links_count_once_by_href_and_images_are_skipped():
    answer = (
        "See [the review](https://pubmed.ncbi.nlm.nih.gov/42511798/) and ![fig](https://example.org/PMID:42511798.png) "
        "and <https://doi.org/10.3390/IJMS27146455> then PMID 42511798."
    )
    keys = [k for _, k in scan_answer(answer)]
    assert keys == ["PMID:42511798", "DOI:10.3390/ijms27146455", "PMID:42511798"]


def test_scan_grammar_matches_shared_parity_fixture():
    for case in _load("scan_parity.json")["cases"]:
        assert [k for _, k in scan_keys(case["text"])] == case["keys"], case["text"]


def test_vivarix_biomodel_entry_with_tool_output_passage():
    name = LIVE["biomodels_BIOMD0000000010"]["name"]
    tools = {
        "vivarix": ToolResult(
            success=True,
            tool_name="vivarix_simulate_time_course",
            data={"model_id": "BIOMD0000000010", "model_name": name, "simulation_type": "TIME_COURSE",
                  "duration": 100.0, "solver_method": "LSODA (Deterministic ODE)"},
        )
    }
    cmap = build_citation_map("Vivarix simulated the MAPK cascade model BIOMD0000000010.", tools, _report(verified=["BIOMD0000000010"]))
    entry = cmap["entries"]["BIOMD:BIOMD0000000010"]
    assert entry["type"] == "biomodel" and entry["url"] == "https://www.biomodels.org/BIOMD0000000010"
    assert entry["title"] == name and entry["source_agent"] == "Vivarix" and entry["evidence_level"] == "simulation"
    passage = entry["passages"][0]
    assert passage["kind"] == "tool_output"
    assert passage["text"] == f"Vivarix simulated {name} (BIOMD0000000010) as a time course over 100 with LSODA (Deterministic ODE)."


def test_synapta_ensembl_and_kegg_entries():
    hits = LIVE["opentargets_search_KRAS"]
    tools = {
        "synapta": ToolResult(
            success=True,
            tool_name="synapta_opentargets_associations",
            data={"search_hits": hits, "associations": [],
                  "pathways": [{"id": "hsa04010", "description": "MAPK signaling pathway - Homo sapiens (human)"}]},
            canonical_ids={"ensembl_id": hits[0]["id"], "target_symbol": "KRAS"},
        )
    }
    answer = "Synapta resolved KRAS to ENSG00000133703, a member of KEGG hsa04010 (MAPK signaling pathway)."
    cmap = build_citation_map(answer, tools, _report(verified=["ENSEMBL:ENSG00000133703"]))
    ensg = cmap["entries"]["ENSG:ENSG00000133703"]
    assert ensg["url"] == "https://platform.opentargets.org/target/ENSG00000133703"
    assert ensg["title"] == "KRAS" and ensg["source_agent"] == "Synapta" and ensg["verified"] is True
    assert ensg["passages"][0]["text"] == "Open Targets target KRAS (ENSG00000133703)."
    kegg = cmap["entries"]["KEGG:hsa04010"]
    assert kegg["url"] == "https://www.kegg.jp/entry/hsa04010"
    assert kegg["title"] == "MAPK signaling pathway - Homo sapiens (human)"
    assert kegg["verified"] is False  # KEGG is not a verifier family; it is linked because the tool returned it
    assert cmap["supports"][1]["highlight"] is not None  # 'mapk', 'signaling', 'pathway' shared


def test_memory_entry_from_historical_identifiers():
    cmap = build_citation_map(
        "As found earlier, the review covers KRAS G12C (PMID: 42511798).",
        {},
        _report(verified=["PMID:42511798"]),
        historical_identifiers={"pmids": {"42511798"}},
    )
    entry = cmap["entries"]["PMID:42511798"]
    assert entry["source_agent"] == "Memory" and entry["passages"] == [] and entry["note"] == MEMORY_NOTE
    assert entry["url"] == "https://pubmed.ncbi.nlm.nih.gov/42511798/"


def test_budget_caps_size_and_marks_truncated():
    page = _load("europepmc_kras_g12c_page.json")["results"]
    passages = []
    for i, art in enumerate(page):
        passages.append({
            "passage_id": f"psg_{i:03d}", "pmid": art["pmid"], "doi": art.get("doi"), "title": art["title"],
            "authors": art.get("authorString"), "year": art.get("pubYear"), "section": "Abstract",
            "passage_text": art["abstractText"],
        })
    answer = " ".join(f"KRAS G12C inhibitor evidence (PMID: {a['pmid']})." for a in page)
    cmap = build_citation_map(answer, _lit(passages), _report(verified=[f"PMID:{a['pmid']}" for a in page]))
    assert len(json.dumps(cmap)) <= 24000
    assert cmap["truncated"] is True
    assert 0 < len(cmap["entries"]) <= 40
    first = page[0]
    kept = cmap["entries"][f"PMID:{first['pmid']}"]
    assert kept["url"] == f"https://pubmed.ncbi.nlm.nih.gov/{first['pmid']}/"
    assert kept["title"]
    for entry in cmap["entries"].values():
        assert len(entry["passages"]) <= 2
        for p in entry["passages"]:
            assert len(p["text"]) <= 800
            assert p["highlight"] is None or p["highlight"] in p["text"]


def test_preprint_id_is_not_a_pmid_entry():
    preprint = _record("preprint_PPR1337147")
    # Lithoscribe's current EuropePMC client falls back to the record id when 'pmid' is absent.
    tools = _lit(_passages(preprint, pmid=preprint["id"]))
    answer = "Prior ICI exposure was associated with severe sotorasib hepatotoxicity (10.64898/2026.10.05.26364739; PMID: PPR1337147)."
    cmap = build_citation_map(answer, tools, _report(verified=["DOI:10.64898/2026.10.05.26364739"]))
    assert not any(k.startswith("PMID:") for k in cmap["entries"])
    entry = cmap["entries"]["DOI:10.64898/2026.10.05.26364739"]
    assert entry["evidence_level"] == "preprint"
    assert entry["url"] == "https://doi.org/10.64898/2026.10.05.26364739"


def test_nct_mentioned_in_passage_becomes_a_mention_entry():
    pooled = _record("nct03600883_mention")
    tools = _lit(_passages(pooled))
    answer = (
        "Sotorasib safety was pooled across CodeBreaK 100 (NCT03600883) and other trials (PMID: 39846981). "
        "KRYSTAL-12 (NCT04613596) is not in the evidence."
    )
    cmap = build_citation_map(answer, tools, _report(verified=["NCT:NCT03600883", "PMID:39846981"]))
    entry = cmap["entries"]["NCT:NCT03600883"]
    assert entry["type"] == "nct" and entry["title"] is None
    assert entry["url"] == "https://clinicaltrials.gov/study/NCT03600883"
    assert entry["note"] == "Mentioned in passage from PMID 39846981"
    assert entry["passages"][0]["kind"] == "mention" and "NCT03600883" in entry["passages"][0]["highlight"]
    assert "NCT:NCT04613596" not in cmap["entries"]


def test_verifier_claim_support_marks_grounded_highlight():
    pooled = _record("nct03600883_mention")
    tools = _lit(_passages(pooled))
    answer = (
        "Treatment-related adverse events of sotorasib were pooled from CodeBreaK 100 (NCT03600883) "
        "and graded according to CTCAE v5.0 (PMID: 39846981)."
    )
    report = ClinicalVerifier().verify_response("sotorasib safety", answer, tools)
    assert report.claim_support and report.claim_support[0]["evidence_key"] == "PMID:39846981"
    cmap = build_citation_map(answer, tools, report)
    support = next(s for s in cmap["supports"] if s["key"] == "PMID:39846981")
    assert support["grounded_by_verifier"] is True
    passage = next(p for p in cmap["entries"]["PMID:39846981"]["passages"] if p["passage_id"] == support["passage_id"])
    assert support["highlight"] and support["highlight"] in passage["text"]
    assert "CTCAE v5.0" in support["highlight"]


def test_citation_records_from_tool_data_and_sources_are_ingested():
    study = _load("ctgov_NCT03600883.json")["study"]
    molecule = _load("chembl_CHEMBL4535757.json")["molecule"]
    tools = {
        "clinical_trials": ToolResult(
            success=True,
            tool_name="lithoscribe_clinical_trials_search",
            data={"citation_records": [{"type": "nct", "id": study["nctId"], "title": study["briefTitle"],
                                        "passage": f"Overall status: {study['overallStatus']}.",
                                        "source_agent": "Lithoscribe"}]},
        )
    }
    sources = {"chembl": [CitationRecord(type="chembl", id=molecule["molecule_chembl_id"], title=molecule["pref_name"],
                                         source_agent="Synapta", source_tool="chembl")]}
    answer = "CodeBreaK 100 (NCT03600883) evaluated sotorasib (CHEMBL4535757)."
    cmap = build_citation_map(
        answer, tools, _report(verified=["NCT:NCT03600883", "CHEMBL:CHEMBL4535757"]), source_citations=sources
    )
    nct = cmap["entries"]["NCT:NCT03600883"]
    assert nct["title"] == study["briefTitle"] and nct["source_tool"] == "clinical_trials"
    assert nct["passages"][0]["text"] == "Overall status: COMPLETED." and nct["evidence_level"] == "database-record"
    chembl = cmap["entries"]["CHEMBL:CHEMBL4535757"]
    assert chembl["title"] == "SOTORASIB" and chembl["source_agent"] == "Synapta"
    assert chembl["url"] == "https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL4535757"


def test_unobserved_ids_are_never_linked_even_without_a_defect():
    tools = _lit(_passages(_record("pmid_42511798")))
    cmap = build_citation_map("Unrelated model BIOMD0000000019 and CHEMBL4535757.", tools, _report())
    assert cmap["entries"] == {} and cmap["unverified"] == []


def test_verifier_verdict_alone_never_links_a_number_from_passage_text():
    # The verifier accepts a cited PMID whose digits occur anywhere in a passage ('549 patients').
    tools = _lit(_passages(_record("nct03600883_mention")))
    assert any(re.search(r"\b549\b", p["passage_text"]) for p in tools["lithoscribe"].data["ranked_passages"])
    cmap = build_citation_map("Safety was pooled across trials (PMID: 549).", tools, _report(verified=["PMID:549"]))
    assert cmap["entries"] == {}


def test_verifier_only_entry_needs_the_id_in_tool_data():
    refs = _load("../ctgov/references_adaura.json")
    tools = {"clinical_trials": ToolResult(success=True, tool_name="lithoscribe_clinical_trials_search", data=refs)}
    answer = "ADAURA results were published (PMID: 33596365); see also NCT02511106."
    cmap = build_citation_map(answer, tools, _report(verified=["PMID:33596365", "NCT:NCT02511106"]))
    entry = cmap["entries"]["PMID:33596365"]
    assert entry["source_agent"] == "Verifier" and entry["url"] == "https://pubmed.ncbi.nlm.nih.gov/33596365/"
    assert entry["title"] is None and entry["passages"] == [] and entry["verified"] is True
    assert "NCT:NCT02511106" in cmap["entries"]


def test_long_padded_table_row_keeps_verifier_grounding():
    pooled = _record("nct03600883_mention")
    tools = _lit(_passages(pooled))
    first = tools["lithoscribe"].data["ranked_passages"][0]
    row = (
        "| Sotorasib pooled safety   |   treatment-related adverse events from CodeBreaK 100 "
        + "graded with CTCAE  " * 12
        + "| PMID: 39846981 |"
    )
    assert len(row) > 240
    support = [{"sentence": row[:240], "evidence_key": "PMID:39846981", "passage_id": first["passage_id"]}]
    cmap = build_citation_map(row, tools, _report(verified=["PMID:39846981"], claim_support=support))
    assert cmap["supports"][0]["grounded_by_verifier"] is True


def test_raw_html_attributes_are_not_counted():
    answer = '<img src="https://example.org/fig.png" title="PMID: 42511798"> Pocket (PMID: 42511798).'
    assert [k for _, k in scan_answer(answer)] == ["PMID:42511798"]


# -------------------------------------------------------------
# Orchestrator integration
# -------------------------------------------------------------

class FakeStreamingLLM:
    async def astream(self, _messages):
        for piece in ["Lithoscribe retrieved ", "PMID: 123456 on KRAS."]:
            yield SimpleNamespace(content=piece, additional_kwargs={})


@pytest.mark.asyncio
async def test_orchestrator_result_and_stream_carry_citations(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: FakeStreamingLLM())
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-cite", enable_web_search=False)
    entry = res.citations["entries"]["PMID:123456"]
    assert entry["passages"][0]["text"] == "KRAS signaling in pancreatic cancer."
    assert entry["url"] == "https://pubmed.ncbi.nlm.nih.gov/123456/"
    assert res.citations["supports"][0]["occurrence"] == 0
    json.dumps(res.to_dict())

    from fastapi.testclient import TestClient

    from app.backend import main as backend_main

    monkeypatch.setenv("PHRONESIS_AUTH_MODE", "disabled")
    monkeypatch.setattr(backend_main, "get_authoritative_orchestrator", lambda: orch)
    client = TestClient(backend_main.app)
    with client.stream("POST", "/api/chat/stream", json={"message": "Find KRAS papers only.", "thread_id": "t-cite-sse",
                                                         "enable_web_search": False}) as resp:
        assert resp.status_code == 200
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    done = next(e for e in events if e["type"] == "done")
    assert done["result"]["citations"]["entries"]["PMID:123456"]["passages"][0]["text"] == "KRAS signaling in pancreatic cancer."


@pytest.mark.asyncio
async def test_citation_builder_failure_still_returns_an_answer(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: FakeStreamingLLM())

    def boom(*_a, **_k):
        raise RuntimeError("builder failed")

    monkeypatch.setattr(orch_module, "build_citation_map", boom)
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-cite-err", enable_web_search=False)
    assert res.citations == {}
    assert "PMID: 123456" in res.response
