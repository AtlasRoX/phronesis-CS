"""
Offline tests for the foundation hook surface that feature extensions plug into: evidence sources,
prompt/evidence decorators, verifier families/vetoes/filters/doc providers/checks, the run recorder,
memory identifier families, and the ChatRequest dataset id contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from html import escape
from pathlib import Path
from typing import Any, Dict, List

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents.prompts import SYNTHESIS_SYSTEM_PROMPT, build_evidence_block, get_synthesis_system_prompt
from phronesis.agents.query_context import SourceDecision
from phronesis.extensions import hooks
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.provenance import run_manifest
from phronesis.sources import CitationRecord, EvidenceSource, register_source, unregister_source
from phronesis.tools.registry import ErrorType, ToolResult, WebSurferSearchInput
from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine
from tests.fakes import build_recording_registry, called, make_offline_orchestrator
from tests.test_audit_regressions import api_client  # noqa: F401  (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "foundation"


def _trial_study() -> Dict[str, Any]:
    raw = json.loads((FIXTURES / "ctgov_search_trimmed.json").read_text(encoding="utf-8"))
    return raw["studies"][0]


def _trial_data() -> Dict[str, Any]:
    st = _trial_study()
    ps = st["protocolSection"]
    return {
        "trials": [
            {
                "nct_id": ps["identificationModule"]["nctId"],
                "brief_title": ps["identificationModule"]["briefTitle"],
                "overall_status": ps["statusModule"]["overallStatus"],
                "phases": ps["designModule"]["phases"],
                "enrollment_count": ps["designModule"]["enrollmentInfo"]["count"],
                "has_results": st["hasResults"],
            }
        ]
    }


NCT = "NCT02157883"


class _FakeTrialSource(EvidenceSource):
    key = "fake_trials"
    agent = "Lithoscribe"
    action = "Registry search (test source)"
    required_tools = ("fake_trial_search",)
    order = 5

    def should_run(self, ctx):
        if "trial" in ctx.clean_query.lower():
            return SourceDecision(run=True, reason="")
        return SourceDecision(run=False, reason="no trial question")

    def running_title(self, ctx):
        return "Searching the test registry"

    async def run(self, ctx, registry, state):
        first = await state.once("shared", lambda: registry.execute("fake_trial_search", query="osimertinib"))
        second = await state.once("shared", lambda: registry.execute("fake_trial_search", query="never-called"))
        assert first is second
        return first

    def describe(self, result, ctx):
        n = len((result.data or {}).get("trials", []))
        return f"Found {n} registered trial(s)", NCT

    def evidence_xml(self, result):
        rows = "".join(
            f'<trial nct_id="{escape(t["nct_id"], quote=True)}">{escape(t["brief_title"])}</trial>'
            for t in result.data["trials"]
        )
        return f"<fake_registry>{rows}</fake_registry>"

    def allowed_identifiers(self, result):
        return {"nct": {t["nct_id"] for t in result.data["trials"]}}

    def citations(self, result):
        return [CitationRecord(type="nct", id=t["nct_id"], title=t["brief_title"]) for t in result.data["trials"]]

    def artifact(self, result):
        return ("fake_trials", {"n": len(result.data["trials"])})

    def fallback_markdown(self, result):
        return "#### Test registry\n" + "\n".join(f"- {t['nct_id']}" for t in result.data["trials"])

    def scope_notes(self, ctx, result):
        return ["TEST-SCOPE-NOTE: registry searched"] if result is not None else ["TEST-SCOPE-NOTE: not run"]

    def plan_fact(self, ctx):
        return "Trials: test registry"


@pytest.fixture
def isolated():
    """Foundation behaviour with no feature prompt lines; isolated_hooks restores them on exit."""
    with hooks.isolated_hooks():
        hooks.clear("prompt_rule")
        hooks.clear("prompt_component")
        try:
            yield
        finally:
            unregister_source("fake_trials")


def _trial_tool():
    def fake(kwargs):
        return _trial_data()

    return {"fake_trial_search": (WebSurferSearchInput, fake)}


class _CapturingLLM:
    def __init__(self, answer: str, sink: Dict[str, Any]):
        self.answer = answer
        self.sink = sink

    async def ainvoke(self, messages):
        self.sink.setdefault("calls", []).append(messages)

        class R:
            content = self.answer

        return R()


# 1. A registered source runs in Stage 1 and feeds every downstream stage.
@pytest.mark.asyncio
async def test_evidence_source_runs_end_to_end(isolated, monkeypatch, tmp_path):
    register_source(_FakeTrialSource())
    hooks.register_artifact_builder("extra", lambda tr, ctx: {"fake_trials": {"extra": True}})
    hooks.register_step_augmenter("fake_trials", "aug", lambda res, tctx: (None, ["augmented detail"]))
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=_trial_tool())

    sink: Dict[str, Any] = {}
    answer = f"Lithoscribe found registry record {NCT} on osimertinib trial pharmacokinetics."
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _CapturingLLM(answer, sink))
    captured: Dict[str, Any] = {}

    def fake_citation_map(response, tool_results, report, historical, ctx, source_citations):
        captured.update(response=response, tool_results=tool_results, source_citations=source_citations)
        return {"version": 1, "entries": {f"NCT:{NCT}": {"id": NCT}}}

    monkeypatch.setattr(orch_module, "build_citation_map", fake_citation_map)
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(
        "Which osimertinib trial records exist? Papers only.", thread_id="t-src", enable_web_search=False, event_callback=cb
    )

    steps = [e["data"] for e in events if e["type"] == "thought" and e["data"]["id"] == "fake_trials"]
    assert [s["status"] for s in steps] == ["running", "done"]
    assert steps[0]["title"] == "Searching the test registry"
    assert steps[-1]["title"] == "Found 1 registered trial(s)"
    assert steps[-1]["text"] == f"{NCT} · augmented detail"
    assert len(called(calls, "fake_trial_search")) == 1  # SourceRunState.once shared the call

    assert "fake_trials" in captured["tool_results"]
    assert captured["source_citations"]["fake_trials"][0].id == NCT
    assert res.citations == {"version": 1, "entries": {f"NCT:{NCT}": {"id": NCT}}}

    system, human = next(c for c in sink["calls"] if "routing plan" not in c[0].content)  # the synthesis call
    # draft.py appends the effort level's deliberation instruction (medium by default) to the system prompt.
    assert system.content.startswith(SYNTHESIS_SYSTEM_PROMPT)
    assert system.content.endswith("[EFFORT LEVEL: MEDIUM / BALANCED]\nConduct standard scientific reasoning. "
                                   "Balance depth with clarity, highlighting core evidence, mechanisms, and caveats.")
    assert f'<trial nct_id="{NCT}">' in human.content
    assert human.content.index("<fake_registry>") < human.content.index("<biodiscovery_ranking>")
    assert "TEST-SCOPE-NOTE: registry searched" in human.content

    assert res.execution_artifacts["fake_trials"] == {"n": 1, "extra": True}
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Trials: test registry" in plan["text"]
    assert {"name": "fake_trials", "run": True, "reason": ""} in plan["extra"]["sources"]
    assert "Lithoscribe" in res.selected_agents
    assert f"NCT:{NCT}" in res.verification_report["verified_identifiers"]


# 2. A source whose tool is not registered is skipped silently.
@pytest.mark.asyncio
async def test_evidence_source_skipped_without_tool(isolated, monkeypatch, tmp_path):
    register_source(_FakeTrialSource())
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry("Which osimertinib trial records exist?", thread_id="t-skip", enable_web_search=False, event_callback=cb)
    assert not [e for e in events if e["type"] == "thought" and e["data"]["id"] == "fake_trials"]
    assert "fake_trials" not in res.execution_artifacts
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert {"name": "fake_trials", "run": False, "reason": "tool not available"} in plan["extra"]["sources"]
    assert "Trials: test registry" not in plan["text"]


@pytest.mark.asyncio
async def test_evidence_source_fallback_markdown_when_model_unavailable(isolated, monkeypatch, tmp_path):
    register_source(_FakeTrialSource())
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=_trial_tool())
    res = await orch.execute_inquiry("Which osimertinib trial records exist?", thread_id="t-fb", enable_web_search=False)
    assert f"#### Test registry\n- {NCT}" in res.response
    assert res.response.index("#### Test registry") < res.response.index("BioDiscoveryAgent")


def _trial_result() -> Dict[str, ToolResult]:
    return {"fake_trials": ToolResult(success=True, tool_name="fake_trial_search", data=_trial_data())}


# 3. Unobserved NCT ids are flagged even in hedged sentences; observed ones pass.
def test_nct_family_is_hedge_independent():
    v = ClinicalVerifier()
    bad = v.verify_response("osimertinib trials", "NCT09999999 has not posted results.", _trial_result())
    flagged = [d for d in bad.defects if d.category == "IDENTIFIER" and d.claim == "NCT09999999"]
    assert flagged and flagged[0].severity.value == "CRITICAL"

    good = v.verify_response("osimertinib trials", f"Registry record {NCT} covers osimertinib.", _trial_result())
    assert not [d for d in good.defects if d.category == "IDENTIFIER"]
    assert f"NCT:{NCT}" in good.verified_identifiers


# 4. Context identifiers are accepted; unknown ChEMBL ids are flagged.
def test_context_identifiers_accept_chembl():
    report = ClinicalVerifier().verify_response(
        "sotorasib potency",
        "Sotorasib is CHEMBL4535757; CHEMBL9999999 is something else.",
        {},
        context_identifiers={"chembl": {"CHEMBL4535757"}},
    )
    flagged = {d.claim for d in report.defects if d.category == "IDENTIFIER"}
    assert flagged == {"CHEMBL9999999"}
    assert "CHEMBL:CHEMBL4535757" in report.verified_identifiers


# 5. An identifier the researcher typed is observed.
def test_user_typed_rsid_passes():
    report = ClinicalVerifier().verify_response(
        "Is rs113488022 relevant in melanoma?", "rs113488022 was named in the question; no evidence was retrieved.", {}
    )
    assert not [d for d in report.defects if d.category == "IDENTIFIER"]


# 6. A veto beats historical identifiers.
def test_identifier_veto_beats_history(isolated):
    hooks.register_identifier_veto(
        "test", lambda tr: {"pmids": {"9500320": "PMID 9500320 is retracted (test veto)."}}
    )
    report = ClinicalVerifier().verify_response(
        "MMR vaccine autism", "Lithoscribe retrieved PMID: 9500320.", {}, historical_identifiers={"pmids": {"9500320"}}
    )
    retracted = [d for d in report.defects if d.category == "RETRACTED_CITATION"]
    assert retracted and retracted[0].severity.value == "CRITICAL" and retracted[0].claim == "PMID 9500320"
    assert "PMID:9500320" not in report.verified_identifiers
    assert report.vetoed_identifiers == ["PMID:9500320"]
    assert report.passed is False


# 7. A serialization filter keeps removed keys out of the observed identifiers.
def test_serialization_filter_hides_excluded_doi(isolated):
    doi = "10.3390/ijms27146455"
    tool_results = {
        "lithoscribe": ToolResult(
            success=True, tool_name="lithoscribe_literature_synthesis", data={"ranked_passages": [], "excluded": {"doi": doi}}
        )
    }
    text = f"See DOI {doi}."
    assert not [d for d in ClinicalVerifier().verify_response("q doi", text, tool_results).defects if d.category == "CITATION"]
    hooks.register_serialization_filter("drop", lambda key, data: {k: v for k, v in data.items() if k != "excluded"})
    report = ClinicalVerifier().verify_response("q doi", text, tool_results)
    assert [d for d in report.defects if d.category == "CITATION"]
    assert tool_results["lithoscribe"].data["excluded"] == {"doi": doi}  # the filter worked on a copy


# 8. An evidence-doc provider replaces the whole-tool doc; claim_support records its key.
def test_evidence_doc_provider_and_claim_support(isolated):
    seen: Dict[str, Any] = {}

    def provider(tool_key, result):
        if tool_key != "fake_trials":
            return None
        return [(f"NCT:{t['nct_id']}", json.dumps(t)) for t in result.data["trials"]]

    def spy(vctx):
        seen["keys"] = list(vctx.evidence_keys)
        seen["table_rows"] = list(vctx.table_rows)
        return []

    hooks.register_evidence_doc_provider("trials", provider)
    hooks.register_verification_check("spy", spy)
    response = (
        f"{NCT} was a phase 1 trial assessing the effect of itraconazole on AZD9291 pharmacokinetics.\n\n"
        f"| NCT ID | Phase |\n|---|---|\n| {NCT} | PHASE1 |"
    )
    report = ClinicalVerifier().verify_response("itraconazole AZD9291", response, _trial_result())
    assert seen["keys"] == [f"NCT:{NCT}"]
    assert seen["table_rows"] == ["| NCT ID | Phase |", f"| {NCT} | PHASE1 |"]
    assert report.claim_support and report.claim_support[0]["evidence_key"] == f"NCT:{NCT}"
    assert not [d for d in report.defects if d.category == "CLINICAL_CLAIM"]


def test_registered_check_defects_are_scored(isolated):
    hooks.register_verification_check(
        "always", lambda vctx: [{"category": "NUMERICAL", "severity": "CRITICAL", "description": "d", "suggested_correction": "s", "claim": "c"}]
    )
    hooks.register_verification_check("broken", lambda vctx: 1 / 0)
    report = ClinicalVerifier().verify_response("Mos simulation", "Mos simulation answered.", {})
    assert [d for d in report.defects if d.category == "NUMERICAL" and d.claim == "c"]
    assert report.passed is False


# 9. Registered prompt rules and components are composed; none leaves the prompt unchanged.
def test_synthesis_system_prompt_composition(isolated):
    assert get_synthesis_system_prompt() == SYNTHESIS_SYSTEM_PROMPT
    hooks.register_prompt_component("c", "Lithoscribe also searches a test registry")
    hooks.register_prompt_rule("r1", "First test rule.")
    hooks.register_prompt_rule("r2", "Second test rule.")
    prompt = get_synthesis_system_prompt()
    assert "- BioDiscovery: an evidence-weighted hypothesis ranking computed from the outputs above\n- Lithoscribe also searches a test registry\n" in prompt
    assert "</grounding_rules>\n\n<additional_rules>\n9. First test rule.\n10. Second test rule.\n</additional_rules>\n" in prompt
    for word in ("openai", "nvidia", "langgraph"):
        assert word not in prompt.lower()


# 10. Without extensions the evidence block is byte-identical to the pre-hook output.
def _golden_inputs() -> Dict[str, ToolResult]:
    raw = json.loads((FIXTURES / "evidence_inputs.json").read_text(encoding="utf-8"))
    return {k: ToolResult(**v) for k, v in raw.items()}


def test_evidence_block_byte_identical_without_extensions():
    tr = _golden_inputs()
    # Goldens were captured from the pre-hook build_evidence_block; tolerate CRLF checkouts of the files only.
    golden = (FIXTURES / "evidence_block_golden.txt").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert build_evidence_block(tr, "### ranking\n| a | b |") == golden
    no_lit = {k: v for k, v in tr.items() if k != "lithoscribe"}
    golden_no_lit = (FIXTURES / "evidence_block_golden_nolit.txt").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert build_evidence_block(no_lit, "ranking") == golden_no_lit


def test_evidence_decorators_are_applied_and_escaped(isolated):
    tr = _golden_inputs()
    hooks.register_evidence_decorator("literature_document_attrs", "g", lambda p: {"study_type": 'a"b'})
    hooks.register_evidence_decorator("literature_tail", "t", lambda lit: "<excluded_literature count=\"0\"/>")
    hooks.register_evidence_decorator("lumicell_tail", "l", lambda lum: "<dataset_provenance/>")
    hooks.register_evidence_decorator("vivarix_attrs", "v", lambda viv: {"model_source": "user_upload"})
    hooks.register_evidence_decorator("query_entities", "q", lambda ctx: "<query_entities/>")
    block = build_evidence_block(tr, "r", query_context=object())
    assert block.startswith("<evidence>\n<query_entities/>")
    assert 'study_type="a&quot;b"' in block
    assert block.index("<excluded_literature") < block.index("</literature>")
    assert block.index("<dataset_provenance/>") < block.index("</lumicell>")
    assert 'model_source="user_upload">' in block
    no_passages = {"lithoscribe": ToolResult(success=True, tool_name="l", data={"ranked_passages": []})}
    assert "<literature" in build_evidence_block(no_passages, "r")


# 11. Default results carry the new fields and stay JSON-serializable.
@pytest.mark.asyncio
async def test_result_fields_default(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-fields", enable_web_search=False)
    # The offline fallback answer cites only identifiers present in the fake tool outputs.
    assert isinstance(res.citations, dict)
    assert set(res.citations.get("entries", {})) <= {"KEGG:hsa04010", "ENSG:ENSG00000133703", "PMID:123456"}
    assert res.run_manifest["schema_version"] == "phronesis.run_manifest/1" and res.run_manifest["tool_calls"]
    assert isinstance(res.query_context, dict) and res.query_context["primary_target"] == "KRAS"
    json.dumps(res.to_dict())
    casual = await orch.execute_inquiry("hello", thread_id="t-hi", enable_web_search=False)
    assert casual.run_manifest["framework"] == "conversational-direct" and casual.run_manifest["tool_calls"] == []
    assert casual.thoughts == []


# 11b. Stretched or misspelled greetings take the same direct path and call no tools.
@pytest.mark.asyncio
@pytest.mark.parametrize("greeting", ["heelooo", "hiii!", "heyy there", "yo", "what can you do for mee??", "How can you help me?"])
async def test_stretched_greeting_skips_tools(monkeypatch, tmp_path, greeting):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry(greeting, thread_id="t-hi2", enable_web_search=True)
    assert res.framework_used == "conversational-direct" and res.run_manifest["tool_calls"] == []
    assert calls == [] and res.thoughts == []


@pytest.mark.asyncio
async def test_unknown_attachment_reports_error_step(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry(
        "Cluster this dataset.", thread_id="t-att", enable_web_search=False, dataset_ids=["ds_0123456789ab"]
    )
    step = next(t for t in res.thoughts if t["id"] == "attachments")
    assert step["status"] == "error" and step["title"] == "Attached dataset ds_0123456789ab not found"
    assert not called(calls, "lumicell_analyze_single_cell")


@pytest.mark.asyncio
async def test_persist_and_recall_memory_flags(monkeypatch, tmp_path):
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-mem", enable_web_search=False, persist_memory=False)
    assert orch.memory.get_memory_stats()["total_nodes"] == 0
    await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-mem", enable_web_search=False)
    assert orch.memory.get_memory_stats()["total_nodes"] > 0
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-mem", enable_web_search=False, recall_memory=False)
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Recalled" not in plan["text"]


# 12. The registry reports every call to the recorder and is not affected by a failing recorder.
@pytest.mark.asyncio
async def test_recorder_noop_and_registry_reporting(monkeypatch):
    reg, _ = build_recording_registry()
    rec = run_manifest.RunRecorder(
        query="q", thread_id="t", model_name="m", enable_web_search=False, user_id="u", api_key_supplied=False
    )
    rec.persist = False
    token = run_manifest.activate_recorder(rec)
    try:
        assert run_manifest.current_recorder() is rec
        seen: List[Dict[str, Any]] = []
        monkeypatch.setattr(run_manifest, "record_tool_call", lambda **kw: seen.append(kw))
        ok = await reg.execute("synapta_kegg_pathways", target_symbol="KRAS")
        bad = await reg.execute("synapta_kegg_pathways", query="x")
        missing = await reg.execute("not_a_tool")
        assert ok.success and bad.error_type == ErrorType.VALIDATION_ERROR and missing.error_type == ErrorType.NOT_FOUND
        assert [s["tool_name"] for s in seen] == ["synapta_kegg_pathways", "synapta_kegg_pathways", "not_a_tool"]
        assert seen[0]["inputs"] == {"target_symbol": "KRAS"} and seen[1]["inputs"] is None
        assert seen[1]["raw_inputs"] == {"query": "x"} and seen[0]["entry"].name == "synapta_kegg_pathways"

        def boom(**_kw):
            raise RuntimeError("recorder bug")

        monkeypatch.setattr(run_manifest, "record_tool_call", boom)
        assert (await reg.execute("synapta_kegg_pathways", target_symbol="KRAS")).success
        # record_tool_call was monkeypatched for every call, so nothing reached the recorder.
        m = await rec.finalize(response="r", duration=1.0)
        assert m["schema_version"] == "phronesis.run_manifest/1" and m["tool_calls"] == []
        assert m["response_sha256"] == hashlib.sha256(b"r").hexdigest()
    finally:
        run_manifest.reset_recorder(token)
    assert run_manifest.current_recorder() is None


# 13. Memory harvests identifier families.
def test_memory_collects_family_identifiers():
    ids = ClinicalMemoryEngine.collect_identifiers(_trial_data())
    assert ids["nct"] == [NCT]
    assert {"biomodels", "pmids", "dois", "ensembl", "uniprot", "chembl", "rsid"} <= set(ids)


@pytest.mark.asyncio
async def test_memory_history_returns_family_identifiers(tmp_path):
    mem = ClinicalMemoryEngine(storage_dir=str(tmp_path))
    await mem.consolidate_turn("t", "q", "r", {"lithoscribe_trials": _trial_data()}, is_turn_verified=True)
    assert NCT in mem.get_verified_identifiers("t")["nct"]


# 14. ChatRequest rejects malformed dataset ids.
def test_chat_rejects_bad_dataset_id(api_client):  # noqa: F811
    client, _ = api_client
    headers = {"Authorization": "Bearer alice-token"}
    resp = client.post("/api/chat", json={"message": "x", "dataset_ids": ["../etc/passwd"]}, headers=headers)
    assert resp.status_code == 422
    too_many = client.post("/api/chat", json={"message": "x", "dataset_ids": [f"ds_{i:012x}" for i in range(6)]}, headers=headers)
    assert too_many.status_code == 422


def test_source_run_state_shares_one_task():
    from phronesis.agents.query_context import QueryContext
    from phronesis.sources import SourceRunState

    async def main():
        state = SourceRunState(None, QueryContext(raw_query="q", clean_query="q"))
        n = {"calls": 0}

        async def factory():
            n["calls"] += 1
            await asyncio.sleep(0.01)
            return n["calls"]

        a, b = await asyncio.gather(state.once("k", factory), state.once("k", factory))
        return a, b, n["calls"]

    assert asyncio.run(main()) == (1, 1, 1)
