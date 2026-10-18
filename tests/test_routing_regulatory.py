"""Routing, regulatory retrieval and decomposition repairs from the KRAS G12C NSCLC/CRC brief run audit:

  - an FDA-approval question that names no drug looks up approvals for the target's approved drugs (openFDA ran
    only for safety questions before), with the label's INDICATIONS AND USAGE kept;
  - web sources carry who published them (regulator, registry, manufacturer), so a press release is not an FDA
    announcement;
  - Auto does not send a multi-part evidence brief to Debate because it also asks for two hypotheses;
  - decomposition keeps output instructions and the date out of the sub-questions, and its entity check neither
    misfires on "FDA-approved" or a year nor swaps in an unrelated clause.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket

import pytest

from phronesis.agentic import router
from phronesis.agentic.config import AGENTIC, DEBATE
from phronesis.agentic.decompose import (
    heuristic_split,
    is_meta_sub_question,
    parse_decomposition,
    strip_output_instructions,
)
from phronesis.agents.prompts import build_synthesis_request, source_authority, utc_today
from phronesis.drugintel.names import has_regulatory_intent, query_alleles
from phronesis.drugintel.openfda_client import _label_fields
from phronesis.drugintel.sources import ChemblSource, OpenFdaSource
from phronesis.sources import SourceRunState
from tests.agentic_fakes import ScriptedLLM
from tests.fakes import DrugToolRegistry, build_recording_registry
from tests.test_agentic_planning import _ctx, make_state
from tests.test_query_run_repurposing_safety import Upstream, _install, kras_ctx, make_engine

FDA_Q = "What are the exact FDA-approved indications for KRAS G12C inhibitors in NSCLC and CRC?"
BRIEF = (
    "Research brief: KRAS G12C in NSCLC and CRC. 1) Explain mechanisms of resistance by disease. "
    "2) What are the exact FDA-approved indications for KRAS G12C inhibitors? 3) Compare the clinical evidence for "
    "sotorasib versus adagrasib by regimen. 4) List recruiting trials with last-update dates. "
    "5) Propose two testable hypotheses.\n"
    "Formatting: use Markdown tables, cite the evidence for every claim, state the current UTC date at the top, "
    "keep under 1500 words."
)


# -----------------------------------------------------------------------------
# Regulatory retrieval
# -----------------------------------------------------------------------------

def test_regulatory_intent_is_narrower_than_label_intent():
    assert has_regulatory_intent(FDA_Q)
    assert has_regulatory_intent("What is the accelerated approval status of adagrasib plus cetuximab?")
    assert not has_regulatory_intent("Which approved drugs target KRAS?")  # a mechanism question: ChEMBL answers it
    assert not has_regulatory_intent("KRAS signaling in pancreatic cancer")
    assert query_alleles("KRAS G12C and BRAF V600E inhibitors") == ["G12C", "V600E"]


def test_fda_question_naming_no_drug_routes_chembl_and_openfda():
    ctx = kras_ctx(FDA_Q)
    chembl = ChemblSource().should_run(ctx)
    assert chembl.run and "regulatory" in chembl.reason
    ctx.decisions["chembl"] = chembl
    fda = OpenFdaSource().should_run(ctx)
    assert fda.run and fda.reason.startswith("regulatory question naming no drug")
    assert "Regulatory: Drugs@FDA" in (OpenFdaSource().plan_fact(ctx) or "")
    # A question that asks neither about safety nor about regulatory status still skips openFDA.
    plain = kras_ctx("Which approved drugs target KRAS?", chembl=True)
    d = OpenFdaSource().should_run(plain)
    assert not d.run and d.reason == "no drug named"


def test_fda_question_checks_approvals_for_chembl_candidates(monkeypatch):
    up = Upstream()
    reg = DrugToolRegistry(make_engine(up))
    chembl, openfda = ChemblSource(), OpenFdaSource()
    _install(monkeypatch, chembl, openfda)
    ctx = kras_ctx(FDA_Q, chembl=True, primekg=False, openfda=True)

    async def stage1():
        state = SourceRunState(reg, ctx)
        return await asyncio.gather(chembl.run(ctx, reg, state), openfda.run(ctx, reg, state))

    _, fda_res = asyncio.run(stage1())

    assert fda_res.success
    sel = fda_res.data["candidate_selection"]
    assert sel["status"] == "selected" and sel["purpose"] == "regulatory"
    assert [d["drug"] for d in sel["drugs"]] == ["sotorasib", "adagrasib"]
    # ChEMBL's mechanism says "GTPase KRas inhibitor", not G12C: the candidates are labelled target-level.
    assert all(d["allele_specific"] is False for d in sel["drugs"]) and "G12C" in sel["allele_note"]
    assert "may be approved" in sel["coverage_note"]  # no approval record is not nonapproval
    assert "ehr_note" not in fda_res.data  # the FAERS-is-not-EHR note belongs to safety questions
    assert {e["ingredient"] for e in fda_res.data["safety"]} == {"sotorasib", "adagrasib"}


def test_label_indications_section_is_kept():
    body = {"meta": {}, "results": [{"set_id": "s1", "indications_and_usage": ["LUMAKRAS is indicated for ..."],
                                     "adverse_reactions": ["long ADR prose"], "openfda": {"brand_name": ["LUMAKRAS"]}}]}
    kept = _label_fields(body)["results"][0]
    assert kept["indications_and_usage"] == ["LUMAKRAS is indicated for ..."] and "adverse_reactions" not in kept


@pytest.fixture(scope="module")
def default_registry():
    from phronesis.tools.registry import BiomedicalToolRegistry, register_default_biomedical_tools

    real_connect = socket.socket.connect

    def blocked(self, *args, **kwargs):
        raise OSError("network disabled in tool-retrieval tests")

    socket.socket.connect = blocked  # type: ignore[method-assign]
    try:
        reg = BiomedicalToolRegistry()
        register_default_biomedical_tools(reg)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"default registry could not be built offline: {exc}")
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
    return reg


def test_tool_retrieval_offers_openfda_for_fda_questions(default_registry):
    from phronesis.agentic.tool_retrieval import retrieve_tools

    if default_registry.get_entry("openfda_drug_safety") is None:
        pytest.skip("openfda_drug_safety is not registered in this checkout")
    for q in (FDA_Q, "Is adagrasib plus cetuximab approved by the FDA for colorectal cancer?"):
        assert "openfda_drug_safety" in [e.name for e in retrieve_tools(default_registry, q, k=3)]


# -----------------------------------------------------------------------------
# Source authority
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("url,authority", [
    ("https://www.fda.gov/drugs/resources-information-approved-drugs/fda-approves-sotorasib", "regulatory"),
    ("https://www.accessdata.fda.gov/drugsatfda_docs/label/2021/214665s000lbl.pdf", "regulatory"),
    ("https://www.ema.europa.eu/en/medicines/human/EPAR/lumykras", "regulatory"),
    ("https://clinicaltrials.gov/study/NCT05198934", "trial_registry"),
    ("https://www.amgen.com/newsroom/press-releases/2025/01/lumakras-approval", "manufacturer"),
    ("https://www.prnewswire.com/news-releases/krazati-approval-302.html", "manufacturer"),
    ("https://pubmed.ncbi.nlm.nih.gov/39369609/", "literature"),
    ("https://example.com/kras-blog", "web"),
    ("not a url", "web"),
])
def test_source_authority_by_host(url, authority):
    assert source_authority(url) == authority


def test_web_evidence_and_citations_carry_authority():
    from phronesis.agentic.evidence import render_result
    from phronesis.agents.citations import _Builder
    from phronesis.agents.prompts import build_evidence_block
    from phronesis.tools.registry import ToolResult

    data = {"results": [{"url": "https://www.amgen.com/newsroom/x", "title": "Amgen announces", "snippet": "approval"},
                        {"url": "https://www.fda.gov/drugs/x", "title": "FDA approves", "snippet": "approval"}]}
    res = ToolResult(success=True, tool_name="websurfer_search", data=data)
    xml = render_result("agent.gaps.1.websurfer_search", res, {"phase": "gaps", "purpose": "fda"})
    assert 'authority="manufacturer"' in xml and 'authority="regulatory"' in xml
    block = build_evidence_block({"websurfer": res}, "")
    assert 'authority="manufacturer"' in block
    builder = _Builder({})
    builder.websurfer("websurfer", data)
    by_url = {e.url: e.source_authority for e in builder.entries.values()}
    assert by_url == {"https://www.amgen.com/newsroom/x": "manufacturer", "https://www.fda.gov/drugs/x": "regulatory"}


# -----------------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------------

def _route(query: str):
    return router.rule_decision(query, _ctx(query))


def test_multi_part_brief_asking_for_hypotheses_is_not_forced_into_debate():
    decision = _route(BRIEF)
    assert decision.features["hypothesis"] == 1.0 and decision.features["brief"] == 1.0
    assert decision.signals.parts == 5
    terms = dict(router.score_terms(decision.features)[DEBATE])
    assert terms["brief"] < 0  # Debate's hypothesis/contested terms are damped in a multi-part brief
    # Without the damping the hypothesis keyword alone would have tipped the brief into Debate.
    undamped = sum(w for k, w in terms.items() if k != "brief")
    assert undamped > sum(w for _, w in router.score_terms(decision.features)[AGENTIC])
    assert decision.framework == AGENTIC
    assert decision.to_artifact()["cost_note"] is None and "not calibrated" in decision.to_artifact()["scores_note"]


def test_formatting_text_does_not_set_contested():
    assert not router.routing_signals("Summarise KRAS G12C resistance. Formatting: cite the evidence for every claim.", None).contested
    assert router.routing_signals("Is sotorasib better than adagrasib in KRAS G12C NSCLC?", None).contested


@pytest.mark.parametrize("query", [
    "Propose testable hypotheses for why KRAS G12C inhibitors fail in colorectal cancer",
    "Is sotorasib really superior to docetaxel? Should clinicians prefer it in KRAS G12C NSCLC?",
])
def test_focused_hypothesis_or_contested_questions_still_route_to_debate(query):
    decision = _route(query)
    assert decision.framework == DEBATE
    assert "critic" in (decision.to_artifact()["cost_note"] or "")


# -----------------------------------------------------------------------------
# Decomposition
# -----------------------------------------------------------------------------

def test_output_instructions_are_stripped_and_never_sub_questions():
    question, removed = strip_output_instructions(BRIEF)
    assert "Formatting" not in question and "1500 words" not in question
    assert removed and removed[0].startswith("Formatting:")
    subs = heuristic_split(question, max_sub_questions=8)
    assert len(subs) == 5 and not any(re.search(r"markdown|utc date|words", s, re.I) for s in subs)
    assert subs[0].startswith("Explain mechanisms of resistance by disease") and "KRAS G12C" in subs[0]
    assert not any(s.startswith("Research brief") for s in subs)  # the title is context, not a sub-question
    # A sentence asking for a table of trials is research, not only formatting, and stays.
    assert strip_output_instructions("Compare sotorasib and adagrasib in a Markdown table of trials.")[1] == []


def test_meta_and_hallucinated_sub_questions_are_dropped_not_replaced():
    question, _ = strip_output_instructions(BRIEF)
    reply = {"complex": True, "sub_questions": [
        {"id": "q1", "text": "What are the FDA-approved indications for KRAS G12C inhibitors as of 2026?"},
        {"id": "q2", "text": "What is the current UTC date?"},
        {"id": "q3", "text": "Which recruiting KRAS G12C trials in NSCLC and CRC were updated recently?"},
        {"id": "q4", "text": "What does divarasib add over sotorasib in CRC?"},
    ]}
    _, subs = parse_decomposition(reply, query=question, max_sub_questions=8)
    texts = [s.text for s in subs]
    # "FDA-" and "2026" are not unknown entities; the date question and the unnamed drug are dropped, and no
    # clause of the question is substituted for them.
    assert texts == [reply["sub_questions"][0]["text"], reply["sub_questions"][2]["text"]]
    assert is_meta_sub_question("What is today's date?") and not is_meta_sub_question("Which trials changed status after 2025?")


def test_decompose_prompt_carries_utc_date_and_question_without_formatting():
    reg, _ = build_recording_registry()
    reply = json.dumps({"complex": True, "sub_questions": [
        {"id": "a", "text": "What are the FDA-approved indications for KRAS G12C inhibitors?", "focus": "drug"},
        {"id": "b", "text": "Which recruiting KRAS G12C trials are listed for NSCLC and CRC?", "focus": "clinical"},
    ]})
    llm = ScriptedLLM({"decompose": reply})
    state, _steps, _ = make_state(reg, ctx=_ctx(BRIEF), llm=llm)
    from phronesis.agentic.decompose import DecomposePhase

    asyncio.run(DecomposePhase().run(state))
    user = llm.prompts("decompose")[0]
    assert f"Today's date (UTC): {utc_today()}" in user and "1500 words" not in user
    assert state.bag["decomposition"]["output_instructions"]
    assert len(state.sub_questions) == 2


def test_synthesis_request_dates_in_utc_and_labels_memory_honestly():
    req = build_synthesis_request("<evidence/>", "", "- [unverified] KRAS fact", "q")
    assert f"<retrieval_date>{utc_today()}</retrieval_date>" in req
    assert "<verified_memory>" not in req and "<prior_memory" in req and "only items marked verified" in req
