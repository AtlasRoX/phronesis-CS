"""Answer prose and step text from a live query run: no internal field names or source keys in the answer,
no near-duplicate hypotheses, and step texts cut on a word boundary with an ellipsis."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agentic import hypothesis as hyp_mod
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.config import AGENTIC
from phronesis.agentic.control import RunControl
from phronesis.agentic.hypothesis import GROUNDED_STATEMENT_RULE, Hypothesis, HypothesisPhase, dedupe_ranked
from phronesis.agentic.llm import AgentLLM
from phronesis.agentic.state import AgentState
from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator, clean_internal_keys
from phronesis.agents.prompts import SYNTHESIS_SYSTEM_PROMPT
from phronesis.tools.registry import ToolResult
from tests.agentic_fakes import ScriptedLLM
from tests.test_audit_regressions import offline_orchestrator  # noqa: F401  (fixture)


# (1) Internal keys in the answer --------------------------------------------------------------------------------
def test_field_values_become_plain_words():
    text = (
        "Balsalazide: total_reports = 1454, serious_reports = 797, reports_listing_death = 22; label warns of "
        "colitis (application_number ANDA077806) (openFDA)."
    )
    assert clean_internal_keys(text) == (
        "Balsalazide: 1,454 FAERS reports, 797 serious reports, 22 reports listing death; label warns of "
        "colitis (application number ANDA077806) (openFDA)."
    )
    assert clean_internal_keys("ranked first (overall_score: 0.62).") == "ranked first (overall score 0.62)."


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("Dasatinib binds SRC (structure_intel).", "Dasatinib binds SRC (AlphaFold DB / RCSB PDB)."),
        ("Two trials are recruiting (clinical_trials).", "Two trials are recruiting (ClinicalTrials.gov)."),
        ("See NCT01234567 (ClinicalTrials).", "See NCT01234567 (ClinicalTrials.gov)."),
        ("No posted results (tool_status note).", "No posted results."),
        ("Scores were combined (evidence integration step).", "Scores were combined."),
        ("MEK ranks first (BioDiscovery) overall.", "MEK ranks first overall."),
        ("MEK ranks first (BioDiscovery_ranking).", "MEK ranks first."),
        ("KRAS association 0.85 (Synapta; PMID: 12345678).", "KRAS association 0.85 (OpenTargets/KEGG; PMID: 12345678)."),
        ("Graph neighbours (PrimeKG) and labels (openFDA).", "Graph neighbours (PrimeKG) and labels (openFDA)."),
        ("Plain clinical trials (phase II) remain.", "Plain clinical trials (phase II) remain."),
        ("the clinical_trials evidence", "the ClinicalTrials.gov evidence"),
    ],
)
def test_internal_source_keys_get_public_names(raw, cleaned):
    assert clean_internal_keys(raw) == cleaned


def test_code_urls_and_identifiers_are_untouched():
    text = (
        "Run `total_reports = 5 (clinical_trials)` then\n```\noverall_score = 0.9 (tool_status)\n```\n"
        "See https://example.org/clinical_trials/total_reports and [link](https://x.org/a_(structure_intel)) "
        "CHEMBL941, PMID: 12345678, NCT01234567."
    )
    assert clean_internal_keys(text) == text


def test_answer_sanitizer_combines_agent_names_and_keys():
    out = AuthoritativeOrchestrator._sanitize_answer("Trials exist (clinical_trials) 【NCT01234567】.")
    assert out == "Trials exist (ClinicalTrials.gov) (NCT01234567)."


def test_prompt_forbids_field_names_and_source_keys():
    p = SYNTHESIS_SYSTEM_PROMPT
    assert "total_reports" in p and "structure_intel" in p and "1,454 FAERS reports" in p
    assert "cite sources by public name or identifier" in p


@pytest.mark.asyncio
async def test_streamed_and_stored_answer_are_cleaned(offline_orchestrator, monkeypatch):  # noqa: F811
    orch, _calls = offline_orchestrator
    raw = "KRAS signaling in pancreatic cancer is reported (clinical_trials) (PMID: 123456) (tool_status note)."

    class FakeLLM:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=raw)

    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: FakeLLM())
    events: List[Dict[str, Any]] = []

    async def collect(ev: Dict[str, Any]) -> None:
        events.append(ev)

    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-prose", enable_web_search=False, event_callback=collect)
    shown = ""
    for ev in events:
        if ev.get("type") == "content_reset":
            shown = ""
        elif ev.get("type") == "content_delta":
            shown += ev["delta"]
    for text in (shown, res.response):
        assert "(clinical_trials)" not in text and "tool_status" not in text
        assert "(ClinicalTrials.gov)" in text
    assert any(ev.get("type") == "content_reset" for ev in events)


# (2) Hypotheses -------------------------------------------------------------------------------------------------
def _h(i: int, statement: str, parameter: Any = "SRC_activity", kind: Any = "inhibition", source: str = "llm",
       readout: Any = None, status: str = "untested", prediction: str = "decrease", factor: Any = None) -> Hypothesis:
    return Hypothesis(id=f"H{i}", statement=statement, mechanism="", kind=kind, parameter=parameter, factor=factor,
                      readout=readout, prediction=prediction, source=source, status=status)


def test_same_intervention_hypotheses_are_merged_keeping_the_higher_ranked():
    ranked = [
        _h(1, "FDA-approved MEK inhibitors will reduce KRAS-mutant tumour growth.", parameter="MEK_activity"),
        _h(2, "FDA-approved SRC inhibitors (e.g., dasatinib) warrant evaluation in KRAS-mutant pancreatic cancer."),
        _h(3, "FDA-approved SRC inhibitors (e.g., dasatinib) will show favorable repurposing potential.", readout="other"),
        _h(4, "SRC inhibition with dasatinib will reduce migration of pancreatic cancer cells.", parameter=None, kind=None),
    ]
    rejected: List[Dict[str, str]] = []
    kept = dedupe_ranked(ranked, rejected)
    assert [h.id for h in kept] == ["H1", "H2", "H4"]
    assert rejected == [{"hypothesis": ranked[2].statement, "reason": "near-duplicate of H2"}]


def test_near_identical_text_is_merged_but_distinct_targets_and_tested_children_are_kept():
    a = _h(1, "Dasatinib will reduce SRC-driven invasion of KRAS-mutant pancreatic cancer cells.", parameter=None, kind=None)
    b = _h(2, "Dasatinib will reduce SRC driven invasion in KRAS-mutant pancreatic cancer cells.", parameter=None, kind=None)
    # Deterministic model hypotheses share a template but name different targets.
    c = _h(3, "Knockdown of MEK (initial amount) (x0.1) will decrease the ERK-PP endpoint.", parameter="MEK", kind="knockdown", source="deterministic")
    d = _h(4, "Knockdown of RAF (initial amount) (x0.1) will decrease the ERK-PP endpoint.", parameter="RAF", kind="knockdown", source="deterministic")
    # A tested dose-response child shares its parent's target and is kept.
    e = _h(5, "Inhibition of k1 (x0.1) will decrease the B endpoint.", parameter="k1", status="tested", factor=0.1)
    f = _h(6, "Inhibition of k1 (x0.316) will decrease the B endpoint.", parameter="k1", status="tested", factor=0.316, source="evolved")
    # Evidence hypotheses about different diseases share a template and are kept.
    g = _h(7, "KRAS is a candidate therapeutic target in pancreatic carcinoma; inhibiting it is predicted to slow disease progression.",
           parameter="KRAS", source="evidence", readout="pancreatic carcinoma phenotype")
    h = _h(8, "KRAS is a candidate therapeutic target in lung adenocarcinoma; inhibiting it is predicted to slow disease progression.",
           parameter="KRAS", source="evidence", readout="lung adenocarcinoma phenotype")
    kept = dedupe_ranked([a, b, c, d, e, f, g, h], [])
    assert [x.id for x in kept] == ["H1", "H3", "H4", "H5", "H6", "H7", "H8"]


def _state(scripted: ScriptedLLM, emitted: List[Dict[str, Any]]) -> AgentState:
    async def emit(step_id: str, title: str, text: str = "", **_k: Any) -> None:
        emitted.append({"id": step_id, "title": title, "text": text})

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    budget = RunBudget()
    ctx = SimpleNamespace(primary_symbol="KRAS", primary_display="KRAS", drugs=[], diseases=[], scope="research",
                          biomodel_id=None, model_attachment=None, suppress_simulation=False)
    return AgentState(
        query="Which FDA-approved drugs could be repurposed for KRAS-mutant pancreatic cancer? Generate hypotheses.",
        clean_query="Which FDA-approved drugs could be repurposed for KRAS-mutant pancreatic cancer? Generate hypotheses.",
        ctx=ctx, registry=None, user_id="u1", thread_id="t1", model_name="fake", api_key=None, framework=AGENTIC,
        budget=budget, control=RunControl("u1", interactive=False),
        llm=AgentLLM(lambda **_k: scripted, "fake", None, budget),
        tool_results={"lithoscribe": ToolResult(success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": []})},
        emit=emit, send=noop,
    )


@pytest.mark.asyncio
async def test_hypothesis_phase_dedupes_grounds_prompt_and_cuts_step_text_on_words():
    long_tail = " in KRAS-mutant pancreatic ductal adenocarcinoma models with persistent MAPK pathway reactivation"
    hyps = [
        {"statement": "FDA-approved MEK inhibitors will show repurposing potential" + long_tail, "mechanism": "m",
         "intervention": {"kind": "inhibition", "parameter": "MEK_activity", "factor": 0.1}, "prediction": "decrease"},
        {"statement": "FDA-approved SRC inhibitors (e.g., dasatinib) warrant evaluation" + long_tail, "mechanism": "m",
         "intervention": {"kind": "inhibition", "parameter": "SRC_activity", "factor": 0.1}, "prediction": "decrease"},
        {"statement": "FDA-approved SRC inhibitors (e.g., dasatinib) will show favorable repurposing potential" + long_tail,
         "mechanism": "m", "intervention": {"kind": "inhibition", "parameter": "SRC_activity"}, "prediction": "decrease"},
    ]
    scripted = ScriptedLLM({"hypothesis_generate": json.dumps({"reflection": "r", "research_plan": "p", "hypotheses": hyps})})
    emitted: List[Dict[str, Any]] = []
    state = _state(scripted, emitted)
    await HypothesisPhase().run(state)

    gen_system = next(c[1] for c in scripted.calls if c[0] == "hypothesis_generate")
    assert GROUNDED_STATEMENT_RULE in gen_system and "unless the evidence given here contains them" in gen_system
    art = state.artifacts["hypotheses"]
    src = [h for h in art["hypotheses"] if h["intervention"]["parameter"] == "SRC_activity"]
    assert len(src) == 1 and len(art["hypotheses"]) == 2
    assert any(r["reason"].startswith("near-duplicate of") for r in art["rejected"])

    step = [e for e in emitted if e["id"] == "agent_hypothesis"][-1]
    for part in step["text"].split("; "):
        body = part.split(" ", 1)[1]
        assert len(body) <= 90 and body.endswith("…")
        assert body[:-1].rstrip().split(" ")[-1] in long_tail.split() + ["potential", "evaluation"]


# (3) PrimeKG step text --------------------------------------------------------------------------------------------
def test_primekg_step_text_is_cut_on_a_word_with_an_ellipsis():
    from phronesis.extensions.primekg import PrimeKGSource

    reason = ("PrimeKG profile of exocrine pancreatic carcinoma, using indications of 'pancreatic adenocarcinoma' "
              "(exocrine pancreatic carcinoma has no recorded indications in PrimeKG)")
    data = {"analyses": [{"operation": "disease_profile", "reason": reason}, {"operation": "repurposing", "reason": reason}]}
    title, text = PrimeKGSource().describe(ToolResult(success=True, tool_name="primekg_investigate", data=data), None)
    assert title == "Ran 2 PrimeKG analyses"
    assert len(text) <= 300 and text.endswith("…")
    last = text[:-1].split(" ")[-1]
    assert last in reason.split()


def test_hypothesis_module_uses_word_truncation():
    assert hyp_mod.truncate_words("alpha beta gamma delta", 12) == "alpha beta…"


def test_typography_is_normalised_so_flagged_claims_can_be_removed():
    from types import SimpleNamespace

    from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator, normalize_typography
    from phronesis.phronesis.agents.verifier import remove_flagged_sentences

    raw = ("### Counter-evidence\n- Hallin et\u202fal. (2022) reported on\u2011target activity but did not address "
           "resistance (PMID: 36216931).\n- Other point (PMID: 1).\n")
    clean = AuthoritativeOrchestrator._sanitize_answer(raw)
    assert "\u202f" not in clean and "\u2011" not in clean and "on-target" in clean
    sentence = normalize_typography("- Hallin et\u202fal. (2022) reported on\u2011target activity but did not address "
                                    "resistance (PMID: 36216931).")
    out, removed = remove_flagged_sentences(clean, [SimpleNamespace(category="CLAIM_CONTRADICTED", sentence=sentence,
                                                                    claim=sentence, description="")])
    assert removed and "Hallin" not in out.split("### Evidence limitations")[0]


def test_bare_snake_case_keys_in_parentheses_are_dropped():
    from phronesis.agents.authoritative_orchestrator import clean_internal_keys

    assert clean_internal_keys("via PI3K-AKT bypass (hypothesis_experiments).") == "via PI3K-AKT bypass."
    assert clean_internal_keys("as shown (PMID: 123; phase III RCT).") == "as shown (PMID: 123; phase III RCT)."


def test_buffered_stream_is_retried_once_after_a_dropped_connection(monkeypatch):
    import asyncio

    from phronesis.agents import authoritative_orchestrator as ao

    class ReadError(Exception):
        pass

    calls = []

    async def fake_stream(llm, messages, on_reasoning=None, on_text=None):
        calls.append(1)
        await on_text("partial ")
        if len(calls) == 1:
            raise ReadError("connection dropped")
        await on_text("full answer")
        return "partial full answer", ""

    monkeypatch.setattr(ao, "astream_with_reasoning", fake_stream)
    async def no_wait(*_a):
        return None

    monkeypatch.setattr(ao.asyncio, "sleep", no_wait)
    parts = []
    text, _ = asyncio.run(ao.stream_buffered_with_retry(object(), [], parts))
    assert len(calls) == 2 and text == "partial full answer" and parts == ["partial ", "full answer"]


def test_bracketed_component_tags_are_rewritten_but_links_and_refs_kept():
    from phronesis.agents.authoritative_orchestrator import clean_internal_keys

    out = clean_internal_keys("KEGG pathways [Synapta]; see [1], [docs](http://x) and [ ] item.")
    assert "Synapta" not in out and "(OpenTargets/KEGG)" in out
    assert "[1]" in out and "[docs](http://x)" in out and "[ ]" in out


def test_internal_step_keys_are_removed_from_the_answer():
    from phronesis.agents.authoritative_orchestrator import clean_internal_keys

    assert clean_internal_keys("Docking was not retrieved (agent.plan_execute.4.structure).") == "Docking was not retrieved."
    assert clean_internal_keys("New evidence (agent.gaps.9-12 lithoscribe) was found.") == "New evidence was found."
    out = clean_internal_keys("Partners came from evidence: agent.swarm.2.chembl and PubTator.")
    assert "agent." not in out and "and PubTator" in out
    assert clean_internal_keys("Olaparib (PMID: 123); NCT01844986.") == "Olaparib (PMID: 123); NCT01844986."
