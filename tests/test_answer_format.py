"""
Offline tests for the expert-brief answer format: the output rules in the synthesis prompt, the evidence level
each literature document carries, and the retrieval date that time-sensitive facts are dated with.
"""

import re
import time
from datetime import date

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.prompts import (
    CORRECTION_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
    build_evidence_block,
    build_synthesis_request,
    evidence_level,
)
from phronesis.tools.registry import ToolResult
from tests import fakes
from tests.test_audit_regressions import offline_orchestrator  # noqa: F401  (fixture)


def test_prompt_carries_the_expert_brief_rules():
    p = SYNTHESIS_SYSTEM_PROMPT
    # 1. fixed evidence-level scale, in words, after key claims
    assert "guideline/label > phase III RCT or meta-analysis > phase I/II trial > observational > preclinical in vivo > in vitro > computational" in p
    assert '"(PMID: 12345678; phase III RCT)"' in p and "never upgrade it" in p
    # 2. approval vs trial phase
    assert '"FDA-approved for X (NDA/BLA number, date)" only when an FDA label or approval record is in the evidence' in p
    assert "highest trial phase reached with its NCT ID" in p
    # 3. effect sizes with context
    assert "endpoint, population, comparator, estimate with 95% CI or p-value, and follow-up" in p
    assert 'never a bare "improved survival"' in p
    # 4. bottom line with calibrated confidence
    assert "Bottom line first" in p and "high, moderate or low" in p
    # 5. conflicting evidence in the body
    assert "conflicting, null or negative evidence its own short paragraph in the body" in p
    # 6. what would change the conclusion
    assert '"What would change this conclusion" (pending trials by NCT ID, missing analyses)' in p
    # 7. compact summary table, at most 5 columns
    assert "Candidate | Mechanism/target | Evidence level | Key result | Source" in p and "At most 5 columns" in p
    # 8. time-sensitive facts dated
    assert '"as of" the <retrieval_date>' in p


def test_correction_prompt_keeps_the_same_format_rules():
    assert "same grounding, scope, and format rules" in CORRECTION_SYSTEM_PROMPT
    assert "evidence levels" in CORRECTION_SYSTEM_PROMPT and '"as of" dates' in CORRECTION_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "passage, level",
    [
        ({"evidence_grade": {"study_type": "rct"}, "title": "A phase 3 trial of sotorasib"}, "phase III RCT"),
        ({"evidence_grade": {"study_type": "rct"}}, "RCT"),
        ({"evidence_grade": {"study_type": "clinical_trial"}, "pub_types": ["Clinical Trial, Phase I", "Clinical Trial, Phase II"]}, "phase I/II trial"),
        ({"evidence_grade": {"study_type": "clinical_trial"}, "title": "A phase 1b/2 study"}, "phase I/II trial"),
        ({"evidence_grade": {"study_type": "rct"}, "passage_text": "In this randomized phase IIb trial ..."}, "phase II RCT"),
        ({"evidence_grade": {"study_type": "observational"}}, "observational"),
        ({"evidence_grade": {"study_type": "preclinical_in_vivo"}, "title": "phase II-like xenograft"}, "preclinical in vivo"),
        ({"evidence_grade": {"study_type": "in_vitro"}}, "in vitro"),
        ({"evidence_grade": {"study_type": "computational"}}, "computational"),
        ({"evidence_grade": {"study_type": "meta_analysis"}}, "meta-analysis"),
        ({"pub_types": ["Practice Guideline"], "evidence_grade": {"study_type": "review"}}, "guideline"),
        ({"experimental_setting": "in-vivo"}, "preclinical in vivo"),  # ungraded: legacy setting
        ({"experimental_setting": "human-clinical", "title": "phase in"}, "human clinical"),
        ({}, "unclassified"),
    ],
)
def test_evidence_level_uses_the_fixed_scale(passage, level):
    assert evidence_level(passage) == level


def test_graded_passage_renders_its_level_without_duplicate_attributes():
    passage = {
        "pmid": "33721583", "year": "2021", "title": "Sotorasib for KRAS G12C: a phase 3 randomized trial",
        "passage_text": "PFS was longer with sotorasib.", "evidence_grade": {"study_type": "rct", "label": "Randomized controlled trial"},
        "grade_label": "Randomized controlled trial", "experimental_setting": "human-clinical",
    }
    block = build_evidence_block({"lithoscribe": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [passage]})}, "ranking")
    doc = re.search(r"<document [^>]*>", block).group(0)
    assert 'evidence_level="phase III RCT"' in doc
    names = re.findall(r'\s([\w-]+)="', doc)
    assert len(names) == len(set(names))
    # Ungraded passages keep the legacy rendering (the foundation golden is byte-identical without extensions).
    ungraded = build_evidence_block({"lithoscribe": ToolResult(success=True, tool_name="l", data={"ranked_passages": [{"pmid": "1", "passage_text": "x"}]})}, "r")
    assert "evidence_level" not in ungraded


def test_synthesis_request_is_dated():
    req = build_synthesis_request("<evidence></evidence>", "<tool_status></tool_status>", "none", "q", retrieval_date="2026-10-16")
    assert "<retrieval_date>2026-10-16</retrieval_date>" in req
    assert req.index("<retrieval_date>") < req.index("<user_query")
    default = build_synthesis_request("<evidence></evidence>", "<tool_status></tool_status>", "none", "q")
    assert f"<retrieval_date>{date.today().isoformat()}</retrieval_date>" in default


@pytest.mark.asyncio
async def test_orchestrator_sends_date_and_evidence_levels(offline_orchestrator, monkeypatch):  # noqa: F811
    orch, _calls = offline_orchestrator
    original = fakes._fake_output

    def graded(tool_name, kwargs):
        if tool_name == "lithoscribe_literature_synthesis":
            return {"ranked_passages": [
                {"pmid": "123456", "title": "A phase III randomized trial of KRAS inhibition", "year": "2023",
                 "passage_text": "KRAS signaling in pancreatic cancer.", "evidence_grade": {"study_type": "rct", "label": "Randomized controlled trial"},
                 "grade_label": "Randomized controlled trial"},
                {"pmid": "234567", "passage_text": "KRAS mutant xenografts regressed.",
                 "evidence_grade": {"study_type": "preclinical_in_vivo", "label": "Preclinical in vivo"}},
            ]}
        return original(tool_name, kwargs)

    monkeypatch.setattr(fakes, "_fake_output", graded)
    captured = {}

    class FakeLLM:
        async def ainvoke(self, messages):
            captured["messages"] = messages

            class R:
                content = "KRAS signaling in pancreatic cancer is reported (PMID: 123456; phase III RCT)."

            return R()

    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: FakeLLM())
    await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-format", enable_web_search=False)
    _system, human = captured["messages"]
    assert f"<retrieval_date>{time.strftime('%Y-%m-%d')}</retrieval_date>" in human.content
    assert 'evidence_level="phase III RCT"' in human.content
    assert 'evidence_level="preclinical in vivo"' in human.content
