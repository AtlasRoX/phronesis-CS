"""
Offline tests for evidence grading and retraction checks (feature 5). EuropePMC and Crossref are served
from trimmed real responses in tests/fixtures/literature through httpx.MockTransport; no network is used.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
import pytest

from phronesis.agents.discovery import BioDiscoveryEngine
from phronesis.agents.prompts import build_evidence_block
from phronesis.extensions import grading as grading_ext
from phronesis.extensions import hooks, load_extensions
from phronesis.lithoscribe.evidence_grading import GradeBasis, StudyType, grade_article, grade_from_mesh, is_integrity_notice, mesh_descriptors
from phronesis.lithoscribe.integrity import (
    CrossrefIntegrityClient,
    europepmc_integrity,
    merge_integrity,
    parse_crossref_updates,
)
from phronesis.lithoscribe.synthesis_engine import LiteratureSynthesisEngine
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import LithoscribeSynthesisInput, ToolResult
from phronesis.utils.biocontext.client import BioContextMCPClient
from tests.fakes import make_offline_orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "literature"
RECORDS: Dict[str, Dict[str, Any]] = json.loads((FIXTURES / "epmc_core_samples.json").read_text(encoding="utf-8"))["records"]
CROSSREF: Dict[str, Any] = json.loads((FIXTURES / "crossref_updated_by.json").read_text(encoding="utf-8"))

WAKEFIELD_DOI = "10.1016/s0140-6736(97)11096-0"
WAKEFIELD_NOTICE_DOI = "10.1016/s0140-6736(10)60175-4"


@pytest.fixture
def grading_hooks():
    """Register this feature's hooks in isolation, whatever other tests did to the registries."""
    # Import every extension first: one first imported inside the isolated block would lose its
    # registrations on exit and, being imported once per process, never register them again.
    load_extensions()
    with hooks.isolated_hooks():
        importlib.reload(grading_ext)
        yield


# -----------------------------------------------------------------------------
# Fakes: real fixture records behind mock transports
# -----------------------------------------------------------------------------

def epmc_client(names: Iterable[str]) -> BioContextMCPClient:
    """A real BioContextMCPClient whose EuropePMC search pages through the given core records."""
    records = [RECORDS[n] for n in names]
    cursor = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            size = int(request.url.params.get("pageSize", 5))
            page = records[cursor["i"]: cursor["i"] + size]
            cursor["i"] += len(page)
            return httpx.Response(200, json={"hitCount": len(records), "resultList": {"result": page}})
        return httpx.Response(404, text="")  # full text: not available offline

    client = BioContextMCPClient()
    client._get_async_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[method-assign]
    return client


def crossref_transport(mode: str = "fixture", seen: Optional[List[httpx.Request]] = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if mode == "empty":
            return httpx.Response(200, content=b"")
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if mode == "429":
            return httpx.Response(429, json={"status": "error"})
        wanted = {f.split(":", 1)[1].lower() for f in request.url.params["filter"].split(",")}
        items = [it for it in CROSSREF["message"]["items"] if it["DOI"].lower() in wanted]
        return httpx.Response(200, json={"status": "ok", "message": {"total-results": len(items), "items": items}})

    return httpx.MockTransport(handler)


async def run_engine(names: Iterable[str], query: str = "MMR vaccine autism", mode: str = "fixture", check_integrity: bool = True) -> ToolResult:
    engine = LiteratureSynthesisEngine(
        biocontext_client=epmc_client(names),
        crossref_client=CrossrefIntegrityClient(transport=crossref_transport(mode)),
    )
    return await engine.execute_literature_synthesis(query, max_articles=8, check_integrity=check_integrity)


def _codes(report, category: str):
    return [d for d in report.defects if d.category == category]


# -----------------------------------------------------------------------------
# 1-6. Grading
# -----------------------------------------------------------------------------

def test_grade_from_pubtype_rct():
    g = grade_article(RECORDS["rct_21639808"])
    assert g.study_type == StudyType.RCT and g.basis == GradeBasis.PUBTYPE
    assert g.cues == ["Randomized Controlled Trial"] and g.confidence == "high"
    assert (g.tier, g.weight) == (2, 0.9)


def test_grade_meta_analysis_precedence():
    rec = RECORDS["meta_analysis_42500683"]
    assert {"Meta-Analysis", "Systematic Review"} <= set(rec["pubTypeList"]["pubType"])
    g = grade_article(rec)
    assert g.study_type == StudyType.META_ANALYSIS and g.cues == ["Meta-Analysis"]


def test_grade_mesh_in_vivo_vs_topic_headings():
    g = grade_article(RECORDS["in_vivo_42082482"])
    assert g.study_type == StudyType.PRECLINICAL_IN_VIVO and g.basis == GradeBasis.MESH and g.confidence == "medium"
    assert "Xenograft Model Antitumor Assays" in g.cues and "Humans" not in g.cues

    topic = RECORDS["topic_mesh_only_32546273"]
    assert "Randomized Controlled Trials as Topic" in mesh_descriptors(topic)
    assert grade_from_mesh(mesh_descriptors(topic)) is None
    g2 = grade_article(topic)
    assert g2.study_type != StudyType.RCT and g2.basis != GradeBasis.MESH


def test_grade_heuristic_title_before_abstract():
    rec = RECORDS["no_mesh_real_world_42771901"]
    assert "meshHeadingList" not in rec and "phase 3" in rec["abstractText"]
    g = grade_article(rec)
    assert g.study_type == StudyType.OBSERVATIONAL and g.basis == GradeBasis.HEURISTIC and g.confidence == "low"
    assert g.cues == ['title: "Real-world"']


def test_trial_protocol_is_not_graded_as_a_trial():
    rec = RECORDS["protocol_42814483"]
    assert "Randomized Controlled" in rec["title"]  # the title heuristic alone would say RCT
    g = grade_article(rec)
    assert g.study_type == StudyType.TRIAL_PROTOCOL and g.label == "Trial protocol" and g.basis == GradeBasis.PUBTYPE
    assert g.cues == ["Clinical Trial Protocol"] and g.confidence == "high"


def test_preprint_flag_and_client_fields():
    raw = RECORDS["preprint_PPR1250239"]
    assert grade_article(raw).is_preprint is True

    async def fetch():
        return await epmc_client(["preprint_PPR1250239", "rct_21639808"]).async_query_europepmc("KRAS", limit=5)

    import asyncio

    arts = asyncio.run(fetch())["articles"]
    ppr, rct = arts
    assert ppr["pmid"] is None and ppr["source"] == "PPR"  # a PPR id is not a PMID
    assert rct["pmid"] == "21639808" and rct["journalTitle"] == "The New England journal of medicine"
    assert "Randomized Controlled Trial" in rct["pubType"] and rct["pubTypeList"]["pubType"]
    assert grade_article(ppr).is_preprint is True


def test_notice_detected():
    assert is_integrity_notice(RECORDS["retraction_notice_20137807"]) is True
    assert is_integrity_notice(RECORDS["eoc_notice_42791264"]) is True
    assert is_integrity_notice(RECORDS["wakefield_9500320"]) is False  # the retracted paper is not the notice


# -----------------------------------------------------------------------------
# 7-9. Integrity
# -----------------------------------------------------------------------------

def test_europepmc_integrity_wakefield():
    st = europepmc_integrity(RECORDS["wakefield_9500320"])
    assert st.status == "retracted" and st.sources_checked == ["europepmc"]
    assert {n.kind for n in st.notices} == {"retraction", "expression_of_concern"}
    assert "20137807" in {n.notice_pmid for n in st.notices}


@pytest.mark.asyncio
async def test_crossref_parse_and_merge():
    items = CROSSREF["message"]["items"]
    parsed = parse_crossref_updates(items)
    wake = parsed[WAKEFIELD_DOI]
    assert {(n.kind, n.source) for n in wake} == {("retraction", "crossref_retraction_watch"), ("correction", "crossref_retraction_watch")}
    surgi = parsed["10.1016/s0140-6736(20)31180-6"]
    assert ("retraction", "crossref_publisher") in {(n.kind, n.source) for n in surgi}
    assert ("expression_of_concern", "crossref_retraction_watch") in {(n.kind, n.source) for n in surgi}
    # update-to entries (the Surgisphere quirk) never become notices of the paper itself
    assert all(n.notice_doi != "10.1016/s0140-6736(20)31290-3" or n.kind == "expression_of_concern" for n in surgi)
    assert parsed["10.1056/nejmoa1103782"] == []
    assert parse_crossref_updates([{"DOI": "10.1/X", "updated-by": [{"DOI": "10.1/y", "type": "expression-of-concern", "source": "publisher"}]}])["10.1/x"][0].kind == "expression_of_concern"

    seen: List[httpx.Request] = []
    client = CrossrefIntegrityClient(transport=crossref_transport(seen=seen), mailto="")
    batch = await client.async_check_dois(["10.1016/S0140-6736(97)11096-0", "10.1056/NEJMoa1103782", "10.1000/a,b"])
    assert len(seen) == 1 and "10.1000/a,b" not in seen[0].url.params["filter"]  # one batch; comma DOI skipped
    assert batch.status == "checked" and batch.check_label() == "checked"

    rct = merge_integrity(europepmc_integrity(RECORDS["rct_21639808"]), batch, "10.1056/NEJMoa1103782")
    assert rct.status == "none" and rct.sources_checked == ["europepmc", "crossref"]
    wake_merged = merge_integrity(europepmc_integrity(RECORDS["wakefield_9500320"]), batch, "10.1016/S0140-6736(97)11096-0")
    assert wake_merged.status == "retracted" and "crossref" in wake_merged.sources_checked


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,label", [("empty", "unavailable: invalid response"), ("timeout", "unavailable: timeout"), ("429", "unavailable: HTTP 429")])
async def test_crossref_failure_is_unchecked_not_clean(mode, label):
    batch = await CrossrefIntegrityClient(transport=crossref_transport(mode)).async_check_dois(["10.1056/NEJMoa1103782"])
    assert batch.status == "unavailable" and batch.check_label() == label
    assert merge_integrity(None, batch, "10.1056/NEJMoa1103782").status == "unchecked"
    epmc_only = merge_integrity(europepmc_integrity(RECORDS["rct_21639808"]), batch, "10.1056/NEJMoa1103782")
    assert epmc_only.sources_checked == ["europepmc"] and epmc_only.crossref_error

    res = await run_engine(["rct_21639808"], query="vemurafenib melanoma", mode=mode)
    assert res.data["integrity_check"]["crossref"].startswith("unavailable")
    assert any("Crossref retraction check unavailable" in lim for lim in res.data["limitations"])
    assert res.data["graded_articles"][0]["integrity"]["sources_checked"] == ["europepmc"]


# -----------------------------------------------------------------------------
# 10. Synthesis
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_excludes_retracted():
    res = await run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808", "in_vivo_42082482", "surgisphere_32450107"])
    assert res.success
    data = res.data
    excluded = {a["pmid"]: a for a in data["excluded_articles"]}
    assert excluded["9500320"]["excluded_reason"] == "retracted"
    assert excluded["32450107"]["excluded_reason"] == "retracted"
    assert excluded["20137807"]["excluded_reason"] == "integrity_notice"
    assert "crossref" in excluded["9500320"]["integrity"]["sources_checked"]

    passage_pmids = {p["pmid"] for p in data["ranked_passages"]}
    assert passage_pmids <= {"21639808", "42082482"} and passage_pmids
    assert "9500320" not in res.canonical_ids["pmids"] and "20137807" not in res.canonical_ids["pmids"]
    assert WAKEFIELD_DOI not in res.canonical_ids["dois"].lower()
    assert data["integrity_check"] == {"europepmc": "checked", "crossref": "checked"}
    assert data["grade_summary"] == {"rct": 1, "preclinical_in_vivo": 1}
    for p in data["ranked_passages"]:
        assert p["evidence_grade"]["study_type"] in ("rct", "preclinical_in_vivo")
        assert p["integrity_status"] == "none" and p["retracted"] is False
        assert p["citation"].startswith("[") and f"{p['year']} | PMID: {p['pmid']}]" in p["citation"]
        # legacy enums pinned by tests/test_scientific_validation.py
        assert p["experimental_setting"] in ["human-clinical", "in-vivo", "in-vitro", "computational"]
        assert p["pub_type"] in ["clinical-trial", "review", "research-article", "meta-analysis"]
    rct = next(p for p in data["ranked_passages"] if p["pmid"] == "21639808")
    assert (rct["pub_type"], rct["experimental_setting"], rct["grade_label"]) == ("clinical-trial", "human-clinical", "Randomized controlled trial")
    assert any("excluded: retracted" in lim for lim in data["limitations"])

    # Memory keeps the first 250 serialized characters: the excluded list must come after the passages.
    keys = list(data)
    assert keys.index("excluded_articles") > keys.index("ranked_passages")

    disabled = await run_engine(["rct_21639808"], query="vemurafenib melanoma", check_integrity=False)
    assert disabled.data["integrity_check"]["crossref"] == "disabled"
    assert disabled.data["ranked_passages"][0]["integrity_status"] == "none"  # EuropePMC still checked


# -----------------------------------------------------------------------------
# 11. Discovery weighting
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_grade_weighting():
    rct = await run_engine(["rct_21639808"], query="vemurafenib melanoma BRAF")
    passages = rct.data["ranked_passages"]
    assert len(passages) == 1 and passages[0]["section"] == "Abstract"  # the complete abstract is one base passage
    assert "vemurafenib" in passages[0]["passage_text"].lower() and len(passages[0]["passage_text"]) > 500
    rep = BioDiscoveryEngine.prioritize_hypotheses("BRAF melanoma", "BRAF", {"lithoscribe": rct})
    cand = rep.candidates[0]
    assert cand.observed_components["literature_support"] == 0.45
    assert "Supported by 1 EuropePMC article (1 randomized controlled trial)" in cand.rationale
    assert any(s.startswith("PMID 21639808: randomized controlled trial (EuropePMC pubType)") for s in cand.supporting_sources)

    in_vitro = await run_engine(["in_vitro_40737322", "in_vitro_40959979", "in_vitro_39344518", "in_vitro_39424923"], query="KRAS cell lines")
    assert {p["evidence_grade"]["study_type"] for p in in_vitro.data["ranked_passages"]} == {"in_vitro"}
    n_articles = len({p["article_key"] for p in in_vitro.data["ranked_passages"]})
    rep2 = BioDiscoveryEngine.prioritize_hypotheses("KRAS", "KRAS", {"lithoscribe": in_vitro})
    assert rep2.candidates[0].observed_components["literature_support"] == round(min(1.0, n_articles * 0.25 / 2), 2)
    if n_articles == 4:
        assert rep2.candidates[0].observed_components["literature_support"] == 0.5

    retracted = await run_engine(["wakefield_9500320", "rct_21639808"], query="vemurafenib melanoma BRAF")
    rep3 = BioDiscoveryEngine.prioritize_hypotheses("BRAF melanoma", "BRAF", {"lithoscribe": retracted})
    assert "1 retracted article excluded" in rep3.candidates[0].rationale

    legacy = ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [{"pmid": "1", "passage_text": "x"}, {"pmid": "1", "passage_text": "y"}]})
    rep4 = BioDiscoveryEngine.prioritize_hypotheses("KRAS", "KRAS", {"lithoscribe": legacy})
    assert rep4.candidates[0].observed_components["literature_support"] == 0.5  # ungraded: count / 4


# -----------------------------------------------------------------------------
# 12-15. Verifier
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verifier_rejects_retracted_pmid_and_doi(grading_hooks):
    res = await run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808"])
    text = f"The MMR vaccine was linked to autism in a 1998 case series (PMID: 9500320; DOI {WAKEFIELD_DOI})."
    report = ClinicalVerifier().verify_response(
        "Does the MMR vaccine cause autism?", text, {"lithoscribe": res},
        historical_identifiers={"pmids": {"9500320"}, "dois": {WAKEFIELD_DOI}},
    )
    retracted = _codes(report, "RETRACTED_CITATION")
    assert {d.claim for d in retracted} == {"PMID 9500320", f"DOI {WAKEFIELD_DOI}"}
    assert all(d.severity.value == "CRITICAL" for d in retracted)
    pmid_defect = next(d for d in retracted if d.claim == "PMID 9500320")
    assert pmid_defect.description == "PMID 9500320 is retracted (Retraction in PMID 20137807, 2010; sources: EuropePMC, Crossref/Retraction Watch)"
    assert report.passed is False
    assert "PMID:9500320" in report.vetoed_identifiers and "PMID:9500320" not in report.verified_identifiers


@pytest.mark.asyncio
async def test_verifier_does_not_harvest_notice_dois(grading_hooks):
    res = await run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808"])
    assert WAKEFIELD_NOTICE_DOI in json.dumps(res.data)  # present in the raw tool output
    report = ClinicalVerifier().verify_response(
        "MMR vaccine autism", f"The Lancet notice is DOI {WAKEFIELD_NOTICE_DOI}.", {"lithoscribe": res}
    )
    assert [d for d in _codes(report, "CITATION") if WAKEFIELD_NOTICE_DOI in d.claim]


@pytest.mark.asyncio
async def test_verifier_eoc_requires_caveat(grading_hooks):
    res = await run_engine(["eoc_in_vitro_25299775"], query="CPI203 rapamycin neuroendocrine")
    ga = res.data["graded_articles"][0]
    assert ga["integrity"]["status"] == "expression_of_concern"
    assert {n["source"] for n in ga["integrity"]["notices"]} == {"europepmc", "crossref_publisher"}
    tr = {"lithoscribe": res}
    bad = ClinicalVerifier().verify_response("CPI203 rapamycin", "CPI203 enhanced rapamycin effects in PanNET cell lines (PMID: 25299775).", tr)
    flags = _codes(bad, "INTEGRITY_FLAG")
    assert flags and flags[0].severity.value == "MAJOR" and flags[0].claim == "PMID 25299775"
    ok = ClinicalVerifier().verify_response(
        "CPI203 rapamycin",
        "CPI203 enhanced rapamycin effects in PanNET cell lines (PMID: 25299775), a paper subject to an expression of concern.",
        tr,
    )
    assert not _codes(ok, "INTEGRITY_FLAG")


@pytest.mark.asyncio
async def test_verifier_design_misstatement(grading_hooks):
    res = await run_engine(["in_vitro_40737322"], query="adagrasib neoantigens KRAS G12C")
    tr = {"lithoscribe": res}
    bad = ClinicalVerifier().verify_response(
        "adagrasib neoantigens", "Adagrasib generated KRAS G12C neoantigens in a randomized phase III trial (PMID: 40737322).", tr
    )
    grade_defects = _codes(bad, "EVIDENCE_GRADE")
    assert grade_defects and grade_defects[0].severity.value == "MAJOR"
    assert "in vitro" in grade_defects[0].description and "MeSH: Cell Line, Tumor" in grade_defects[0].description
    ok = ClinicalVerifier().verify_response(
        "adagrasib neoantigens", "In tumor cell lines, adagrasib generated KRAS G12C neoantigens (PMID: 40737322).", tr
    )
    assert not _codes(ok, "EVIDENCE_GRADE")


# -----------------------------------------------------------------------------
# 16. Evidence block
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evidence_block_grades_and_exclusions(grading_hooks):
    res = await run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808"])
    block = build_evidence_block({"lithoscribe": res}, "ranking")
    assert 'study_type="rct"' in block and 'integrity="none"' in block and 'grade_basis="europepmc_pubtype"' in block
    assert 'citation="[Chapman et al., 2011 | PMID: 21639808]"' in block
    tail = block[block.index("<excluded_literature"):block.index("</excluded_literature>")]
    assert 'count="2"' in tail and "Ileal-lymphoid-nodular hyperplasia" in tail and 'reason="retracted"' in tail
    for ident in ("9500320", "20137807", "10.1016", "pmid", "doi"):
        assert ident not in tail.lower()


# -----------------------------------------------------------------------------
# 17. Orchestrator artifact and step detail
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_literature_artifact(monkeypatch, tmp_path, grading_hooks):
    res = await run_engine(["wakefield_9500320", "retraction_notice_20137807", "rct_21639808"])
    orch, calls = make_offline_orchestrator(
        monkeypatch, tmp_path,
        extra_tools={"lithoscribe_literature_synthesis": (LithoscribeSynthesisInput, lambda kwargs: res.data)},
    )
    out = await orch.execute_inquiry("Does the MMR vaccine cause autism? Find papers only.", thread_id="t-grade", enable_web_search=False)
    lit = out.execution_artifacts["literature"]
    assert [a["pmid"] for a in lit["graded_articles"]] == ["21639808"]
    assert {a["pmid"] for a in lit["excluded_articles"]} == {"9500320", "20137807"}
    assert lit["grade_summary"] == {"rct": 1}
    step = next(t for t in out.thoughts if t["id"] == "lithoscribe")
    assert step["title"].startswith("Found ")
    assert "1 RCT" in step["text"] and "1 retracted article excluded" in step["text"]
    assert [kw for name, kw in calls if name == "lithoscribe_literature_synthesis"][0]["check_integrity"] is True


def test_prompt_rule_registers_with_the_tools(grading_hooks):
    from phronesis.agents.prompts import get_synthesis_system_prompt

    hooks.unregister("prompt_rule", "grading")
    assert grading_ext.PROMPT_RULE not in get_synthesis_system_prompt()
    grading_ext.register_tools(None)
    prompt = get_synthesis_system_prompt()
    assert grading_ext.PROMPT_RULE in prompt and "<excluded_literature>" in prompt
    for word in ("openai", "nvidia", "langgraph"):
        assert word not in prompt.lower()
