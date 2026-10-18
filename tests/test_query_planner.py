"""
Query planner (feature 3): deterministic parsing, the offline lexicon, the live resolver seen through a fake
registry tool fed by recorded upstream responses, hard guards, model-assisted planner degradation, the latency
budget, verifier acceptance of resolved identifiers and the <query_entities> evidence block. No network.
"""

from __future__ import annotations

import asyncio
import json

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agents import authoritative_orchestrator as orch_module
from phronesis.agents import query_planner as qp
from phronesis.agents.discovery import BioDiscoveryEngine
from phronesis.agents.prompts import build_evidence_block
from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.extensions.entities import EntityResolveInput, render_query_entities
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import ToolResult
from tests.fakes import build_recording_registry, called, make_offline_orchestrator
from tests.test_entity_resolver import SHP2_Q, Upstream, _resolve_question, _resolver


async def _ctx(query: str, registry: Any = None, **kw: Any) -> QueryContext:
    args: Dict[str, Any] = dict(
        query=query, clean_query=query, registry=registry, enable_web_search=False, attachments=[], attachment_errors=[],
        user_id="u", thread_id="t", model_name="m", api_key=None,
    )
    args.update(kw)
    return await qp.build_query_context(**args)


def _genes(ctx: QueryContext) -> List[str]:
    return [g.label for g in ctx.genes]


# =============================================================================
# 1. Deterministic parsing and the offline lexicon
# =============================================================================

@pytest.mark.asyncio
async def test_english_words_are_not_genes():
    ctx = await _ctx("We met to discuss ERK2 kinetics")
    assert ctx.primary_symbol == "MAPK1" and ctx.primary_gene.mention == "ERK2"
    assert "MET" not in _genes(ctx)
    assert ctx.primary_display == "MAPK1 (ERK2)" and ctx.primary_aliases() == ["ERK2"]
    none = await _ctx("Does the kit work at the ATM?")
    assert none.entities == [] and none.primary_target is None
    assert none.decisions["synapta"].reason == "no gene named"


@pytest.mark.asyncio
async def test_aliases_and_multi_gene_questions():
    pdl1 = await _ctx("PD-L1 expression in NSCLC")
    assert pdl1.primary_symbol == "CD274"
    two = await _ctx("Role of SOS1 and PTPN11 in adaptive resistance")
    assert _genes(two) == ["SOS1", "PTPN11"] and two.primary_symbol == "SOS1"
    family = await _ctx("PI3K inhibitors in breast cancer")
    assert family.primary_target is None and family.genes[0].ambiguous_with[:2] == ["PIK3CA", "PIK3CB"]
    assert family.decisions["synapta"].reason == "gene family is ambiguous"


@pytest.mark.asyncio
async def test_shp2_question_offline_picks_the_intervention_gene():
    ctx = await _ctx(SHP2_Q)
    assert ctx.normalization == "offline_lexicon"
    assert ctx.primary_symbol == "PTPN11" and ctx.primary_target == "PTPN11"
    kras_variant = next(v for v in ctx.variants if v.protein_change == "G12C")
    assert kras_variant.gene == "KRAS" and kras_variant.label == "KRAS G12C"
    assert [d.mention for d in ctx.drugs] == ["sotorasib"] and not ctx.drugs[0].validated
    assert all(not e.ids for e in ctx.entities if e.type != "variant")  # the lexicon never supplies identifiers
    assert "Target: PTPN11 (SHP2)" in ctx.plan_facts()


@pytest.mark.parametrize(
    "query, kind, label, gene",
    [
        ("BRAF V600E in melanoma", "protein_change", "BRAF V600E", "BRAF"),
        ("EGFR T790M after osimertinib", "protein_change", "EGFR T790M", "EGFR"),
        ("Is KRAS p.Gly12Asp oncogenic?", "protein_change", "KRAS G12D", "KRAS"),
        ("What does rs113488022 do?", "rsid", "rs113488022", None),
        ("Is NM_004985.5:c.35G>A pathogenic?", "hgvs", "NM_004985.5:c.35G>A", None),
        ("EML4-ALK fusion in NSCLC", "fusion", "EML4-ALK", None),
        ("EGFR exon 19 deletion response", "exon_event", "EGFR exon 19 deletion", "EGFR"),
    ],
)
def test_variant_kinds(query, kind, label, gene):
    cands = qp.extract_candidates(query)
    variants = qp.parse_variants(query)
    qp.attach_variant_genes(query, variants, cands.lexicon_genes)
    assert [(v.variant_kind, v.label, v.gene) for v in variants] == [(kind, label, gene)]
    v = variants[0]
    assert query[v.span[0]:v.span[1]] == v.mention and not v.validated


@pytest.mark.asyncio
async def test_exon_events_keep_their_gene_and_hide_event_words():
    ins = await _ctx("EGFR exon 20 ins in lung cancer")
    assert _genes(ins) == ["EGFR"]  # "ins" is not INS
    assert [(v.label, v.gene) for v in ins.variants] == [("EGFR exon 20 insertion", "EGFR")]
    assert "ins" not in qp.extract_candidates("EGFR exon 20 ins in lung cancer").gene_tokens
    two = await _ctx("KRAS and EGFR exon 19 deletion in AML")
    assert [(v.label, v.gene) for v in two.variants] == [("EGFR exon 19 deletion", "EGFR")]
    assert two.primary_symbol == "EGFR" and "Variant: EGFR exon 19 deletion" in two.plan_facts()


@pytest.mark.asyncio
async def test_slash_pairs_and_repeated_variants():
    assert [v.variant_kind for v in qp.parse_variants("BRAF/MEK inhibitors in melanoma")] == []
    assert [v.variant_kind for v in qp.parse_variants("EGFR/ALK testing in NSCLC")] == []
    assert [v.label for v in qp.parse_variants("BCR/ABL1 in CML")] == ["BCR-ABL1"]
    ctx = await _ctx("BCR-ABL1 and BCR::ABL1 in CML; KRAS G12D vs NRAS G12D")
    assert [v.label for v in ctx.variants] == ["BCR-ABL1", "KRAS G12D", "NRAS G12D"]


def test_variant_fact_flags_resolver_rejections():
    ctx = QueryContext(raw_query="q", clean_query="q")
    ctx.entities = [
        ResolvedEntity(type="variant", mention="rs1", label="rs113488022", source="dbSNP", validated=True),
        ResolvedEntity(type="variant", mention="rs2", label="rs99999999999", source="dbSNP", validated=False),
        ResolvedEntity(type="variant", mention="G12C", label="KRAS G12C"),
    ]
    assert "Variants: rs113488022, rs99999999999 (not found in dbSNP), KRAS G12C" in ctx.plan_facts()


def test_cell_lines_and_accessions_are_not_variants_or_genes():
    q = "Simulate A549 response with BIOMD0000000010 and HCT116"
    assert qp.parse_variants(q) == []
    cands = qp.extract_candidates(q)
    assert cands.gene_tokens == [] and cands.drug_codes == []
    assert qp.extract_primary_target("Find KRAS papers only.") == "KRAS"
    assert qp.extract_primary_target("Analyze KRAS G12D signaling") == "KRAS G12D"
    assert orch_module.AuthoritativeOrchestrator._extract_primary_target("Role of HER2 in breast cancer") == "ERBB2"


@pytest.mark.asyncio
async def test_hard_guard_dataset_path_and_biomodel_hint():
    ctx = await _ctx("Compute the steady state of BIOMD0000000010.")
    assert ctx.primary_symbol == "MAPK1" and ctx.primary_aliases() == ["ERK2"]
    assert ctx.decisions["synapta"].run is False and ctx.decisions["synapta"].reason == "BioModel simulation requested"
    assert ctx.wants_steady_state and "steady_state" in ctx.intents
    no_gene = await _ctx("Analyze this uploaded test_atlas.h5ad dataset.")
    assert no_gene.dataset_path == "test_atlas.h5ad" and no_gene.lumicell_target_gene() is None
    variant = await _ctx("Analyze KRAS G12D markers in test_atlas.h5ad")
    assert variant.primary_target == "KRAS G12D" and variant.lumicell_target_gene() == "KRAS"


# =============================================================================
# 2. The live resolver through the registry (fake tool fed by recorded responses)
# =============================================================================

async def _recorded_resolution(query: str) -> Dict[str, Any]:
    return await _resolve_question(_resolver(Upstream()), query)


def _resolver_tool(data: Dict[str, Any]):
    def fake(_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return json.loads(json.dumps(data))

    return {"synapta_resolve_entities": (EntityResolveInput, fake)}


class _CapturingLLM:
    def __init__(self, sink: Dict[str, Any], answer: str) -> None:
        self.sink, self.answer = sink, answer

    async def ainvoke(self, messages):
        self.sink.setdefault("calls", []).append(messages)
        return SimpleNamespace(content=self.answer)


@pytest.mark.asyncio
async def test_resolved_entities_drive_synapta_and_stay_out_of_tool_results(monkeypatch, tmp_path):
    data = await _recorded_resolution(SHP2_Q)
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=_resolver_tool(data))
    sink: Dict[str, Any] = {}
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _CapturingLLM(sink, "PTPN11 (SHP2) is the queried target."))
    seen: Dict[str, Any] = {}
    real_map = orch_module.build_citation_map

    def spy(response, tool_results, *args, **kwargs):
        seen["tool_results"] = dict(tool_results)
        return real_map(response, tool_results, *args, **kwargs)

    monkeypatch.setattr(orch_module, "build_citation_map", spy)
    res = await orch.execute_inquiry(SHP2_Q, thread_id="t-resolve", enable_web_search=False)

    assert called(calls, "synapta_kegg_pathways") == [{"target_symbol": "PTPN11"}]
    assert called(calls, "synapta_opentargets_associations")[0]["ensembl_id"] == "ENSG00000179295"
    assert not called(calls, "synapta_opentargets_search")
    assert len(called(calls, "synapta_resolve_entities")) == 1

    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Target: PTPN11 (SHP2)" in plan["text"]
    ids = {k: v for e in plan["extra"]["entities"] for k, v in e["ids"].items()}
    assert ids["chembl_id"] in ("CHEMBL4650521", "CHEMBL4535757")
    assert {e["ids"].get("chembl_id") for e in plan["extra"]["entities"]} >= {"CHEMBL4535757", "CHEMBL4650521"}
    assert "MONDO_0005233" in {e["ids"].get("ontology_id") for e in plan["extra"]["entities"]}
    assert plan["extra"]["normalization"] == "resolved" and plan["extra"]["planner"] == "deterministic"

    assert res.query_context["primary_symbol"] == "PTPN11"
    assert "synapta_resolve_entities" not in seen["tool_results"] and "entities" not in seen["tool_results"]
    assert not [t for t in res.thoughts if "resolve" in t["id"]]

    system, human = next(c for c in sink["calls"] if "routing plan" not in c[0].content)  # the synthesis call
    block = human.content
    assert block.index("<query_entities") < block.index("<synapta")
    assert 'hgnc_id="HGNC:9644"' in block and 'chembl_id="CHEMBL4535757"' in block
    assert "<query_entities>" in system.content  # the registered usage rule


@pytest.mark.asyncio
async def test_resolver_failure_falls_back_to_lexicon(monkeypatch, tmp_path):
    def broken(_kwargs):
        return ToolResult(success=False, tool_name="synapta_resolve_entities", error="upstream down")

    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools={"synapta_resolve_entities": (EntityResolveInput, broken)})
    # Not "papers only": that restricts the turn to literature, and this test needs Synapta to run.
    res = await orch.execute_inquiry("Summarize KRAS signaling literature.", thread_id="t-fallback", enable_web_search=False)
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert plan["extra"]["normalization"] == "offline_lexicon"
    assert "Target: KRAS" in plan["text"] and called(calls, "synapta_kegg_pathways") == [{"target_symbol": "KRAS"}]
    assert called(calls, "synapta_opentargets_search")  # no Ensembl id offline, so Synapta searches as before


@pytest.mark.asyncio
async def test_latency_budget_is_respected():
    # Audit TST-009: no sub-second wall-clock assert (it failed on loaded runners with no code change). The resolver
    # never answers, so only the 0.2 s budget can end the wait; if the budget were ignored, the generous outer
    # timeout fails the test instead of a hang.
    async def slow():
        await asyncio.sleep(3600)
        return {"entities": []}

    reg, _ = build_recording_registry({"synapta_resolve_entities": (EntityResolveInput, lambda _kw: slow())})
    ctx = await asyncio.wait_for(_ctx("Find KRAS papers only.", registry=reg, budget_s=0.2), timeout=60)
    assert ctx.normalization == "offline_lexicon" and ctx.primary_symbol == "KRAS"


@pytest.mark.asyncio
async def test_resolver_disabled_by_env(monkeypatch):
    data = await _recorded_resolution(SHP2_Q)
    reg, calls = build_recording_registry(_resolver_tool(data))
    monkeypatch.setenv("PHRONESIS_RESOLVER_DISABLED", "1")
    ctx = await _ctx(SHP2_Q, registry=reg)
    assert ctx.normalization == "offline_lexicon" and not called(calls, "synapta_resolve_entities")


# =============================================================================
# 3. Hard guards under a model-assisted planner
# =============================================================================

class _PlannerAndAnswerLLM:
    """Answers the planner prompt with a hostile plan and the synthesis prompt with plain text."""

    def __init__(self, plan: Dict[str, Any], sink: Dict[str, Any]) -> None:
        self.plan, self.sink = plan, sink

    async def ainvoke(self, messages):
        if "routing plan" in str(messages[0].content):
            self.sink["planner_calls"] = self.sink.get("planner_calls", 0) + 1
            return SimpleNamespace(content=json.dumps(self.plan))
        return SimpleNamespace(content="Lithoscribe retrieved PMID: 123456 on KRAS signaling.")


HOSTILE_PLAN = {
    "intents": ["simulation", "single_cell", "not_an_intent"],
    "dataset_path": "fake.h5ad",
    "entities": [{"text": "PTPN11", "type": "gene"}, {"text": "KRAS", "type": "gene", "id": "ENSG00000000001"}],
}


@pytest.mark.asyncio
async def test_llm_planner_cannot_override_hard_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_PLANNER", "always")
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    sink: Dict[str, Any] = {}
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _PlannerAndAnswerLLM(HOSTILE_PLAN, sink))
    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-guard", enable_web_search=False)
    assert sink["planner_calls"] == 1
    assert not called(calls, "vivarix_simulate_time_course") and not called(calls, "lumicell_analyze_single_cell")
    qc = res.query_context
    assert qc["planner"] == "llm" and qc["suppress_simulation"] and qc["dataset"] is None
    assert "simulation" in qc["intents"] and "not_an_intent" not in qc["intents"]
    assert [e["label"] for e in qc["entities"]] == ["KRAS"]  # PTPN11 is not in the question
    assert all(not e["ids"] for e in qc["entities"])

    bm = await orch.execute_inquiry("Simulate BIOMD0000000010 for 50 seconds.", thread_id="t-guard2", enable_web_search=False)
    assert bm.query_context["planner"] == "llm" and not called(calls, "synapta_kegg_pathways")[1:]
    assert called(calls, "vivarix_simulate_time_course")[-1]["biomodel_id"] == "BIOMD0000000010"


# =============================================================================
# 4. Model-assisted planner degradation
# =============================================================================

NO_ENTITY_Q = "What are the primary enzymes involved in the light-independent reactions of photosynthesis?"


class _Prose:
    async def ainvoke(self, _messages):
        return SimpleNamespace(content="I think this is about plant biochemistry, not oncology.")


class _ThinkJson:
    async def ainvoke(self, _messages):
        return SimpleNamespace(content='<think>{"intents": ["safety"]} is wrong</think>Plan: {"intents": ["literature"], "entities": [{"text": "photosynthesis", "type": "disease"}]}')


class _AstreamOnly:
    async def astream(self, _messages):
        yield SimpleNamespace(content="x")


class _Slow:
    async def ainvoke(self, _messages):
        await asyncio.sleep(3600)  # never answers in time: only the planner budget can end the wait
        return SimpleNamespace(content='{"intents": ["literature"]}')


def _raise(**_kw):
    raise RuntimeError("no model configured")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory, planner",
    [
        (_raise, "llm_unavailable"),
        (lambda **_kw: _AstreamOnly(), "llm_unavailable"),
        (lambda **_kw: _Prose(), "llm_unavailable"),
        (lambda **_kw: _ThinkJson(), "llm"),
    ],
)
async def test_planner_degrades_gracefully(factory, planner):
    ctx = await _ctx(NO_ENTITY_Q, llm_factory=factory)
    assert ctx.planner == planner
    assert ctx.decisions["synapta"].run is False and ctx.decisions["lithoscribe"].run is True
    if planner == "llm":
        assert "literature" in ctx.intents and "safety" not in ctx.intents
        ent = ctx.entities[0]
        assert (ent.mention, ent.validated, ent.ids, ent.source) == ("photosynthesis", False, {}, "query_text")


@pytest.mark.asyncio
async def test_slow_planner_respects_budget_and_auto_mode_skips_when_entities_exist():
    # Audit TST-009: the planner budget, not the runner's speed, decides; an ignored budget fails the outer timeout.
    ctx = await asyncio.wait_for(_ctx(NO_ENTITY_Q, llm_factory=lambda **_kw: _Slow(), budget_s=0.1), timeout=60)
    assert ctx.planner == "llm_unavailable"

    asked: List[Any] = []
    ctx2 = await _ctx("Find KRAS papers only.", llm_factory=lambda **kw: asked.append(kw) or _Slow())
    assert ctx2.planner == "deterministic" and asked == []


@pytest.mark.asyncio
async def test_planner_off_and_no_reasoning_from_planner(monkeypatch, tmp_path):
    monkeypatch.setenv("PHRONESIS_PLANNER", "off")
    assert (await _ctx(NO_ENTITY_Q, llm_factory=_raise)).planner == "deterministic"
    monkeypatch.delenv("PHRONESIS_PLANNER")

    class Both:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content='<think>planner thoughts</think>{"intents": ["literature"]}')

        async def astream(self, _messages):
            for piece in ["<think>Answer reasoning.</think>", "No gene was named."]:
                yield SimpleNamespace(content=piece, additional_kwargs={})

    monkeypatch.setenv("PHRONESIS_SHOW_REASONING", "1")
    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: Both())
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(NO_ENTITY_Q, thread_id="t-noreason", enable_web_search=False, event_callback=cb)
    reasoning = "".join(e["delta"] for e in events if e["type"] == "reasoning_delta")
    # Synthesis (and any correction pass) streams reasoning; the planner call never does.
    assert reasoning.startswith("Answer reasoning.") and "planner thoughts" not in reasoning
    assert res.query_context["planner"] == "llm"


# =============================================================================
# 4b. Scope: messages that are not research questions search nothing
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, scope",
    [
        ("heelooo", "unclear"), ("???", "unclear"), ("hmmm", "unclear"),
        ("thanks!", "conversational"), ("ok", "conversational"), ("bye", "conversational"),
        # Conservative without a model: anything that might be a question is research.
        ("asdf", "research"), ("why?", "research"), ("tell me more", "research"), ("DNA", "research"),
        ("autophagy", "research"), ("KRAS", "research"), ("sotorasib", "research"), (NO_ENTITY_Q, "research"),
    ],
)
async def test_offline_scope_heuristic(query, scope):
    ctx = await _ctx(query, llm_factory=_raise)
    assert ctx.scope == scope
    if scope != "research":
        assert not any(d.run for d in ctx.decisions.values())
        assert {d.reason for d in ctx.decisions.values()} == {"not a research question"}


class _ScopePlanner:
    """Answers the planner prompt with a fixed scope and the synthesis prompt with a grounded sentence."""

    def __init__(self, scope: Any) -> None:
        self.scope = scope

    async def ainvoke(self, messages):
        if "routing plan" in str(messages[0].content):
            return SimpleNamespace(content=json.dumps({"scope": self.scope, "intents": [], "entities": []}))
        return SimpleNamespace(content="Lithoscribe retrieved PMID: 123456 on KRAS signaling.")


@pytest.mark.asyncio
async def test_planner_scope_verdict_never_overrides_concrete_entities(monkeypatch):
    assert (await _ctx("blorp zing", llm_factory=lambda **_kw: _ScopePlanner("unclear"))).scope == "unclear"
    assert (await _ctx("blorp zing", llm_factory=lambda **_kw: _ScopePlanner("maybe"))).scope == "research"
    monkeypatch.setenv("PHRONESIS_PLANNER", "always")
    ctx = await _ctx("Find KRAS papers only.", llm_factory=lambda **_kw: _ScopePlanner("conversational"))
    assert ctx.planner == "llm" and ctx.scope == "research" and ctx.decisions["lithoscribe"].run


@pytest.mark.asyncio
async def test_unclear_message_searches_nothing_and_says_so(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _ScopePlanner("unclear"))
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry("blorp zing", thread_id="t-unclear", enable_web_search=True, event_callback=cb)
    assert calls == [] and res.selected_agents == [] and res.run_manifest["tool_calls"] == []
    assert "couldn't find a biomedical question" in res.response and "blorp zing" in res.response
    assert res.query_context["scope"] == "unclear"
    # The plan step that was announced as running is closed, and nothing else is shown.
    assert [t["id"] for t in res.thoughts] == ["plan"] and res.thoughts[0]["status"] == "done"
    assert not [e for e in events if e["type"] in ("content_delta", "reasoning_delta")]


@pytest.mark.asyncio
async def test_vague_follow_up_in_a_thread_with_history_is_still_researched(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: _ScopePlanner("unclear"))
    await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-follow", enable_web_search=False)
    n_before = len(called(calls, "lithoscribe_literature_synthesis"))

    follow = await orch.execute_inquiry("why is that?", thread_id="t-follow", enable_web_search=False)
    assert follow.query_context["scope"] == "research"
    assert len(called(calls, "lithoscribe_literature_synthesis")) == n_before + 1

    n_calls = len(calls)
    thanks = await orch.execute_inquiry("thanks!", thread_id="t-follow", enable_web_search=False)
    assert thanks.response.startswith("You're welcome") and len(calls) == n_calls


@pytest.mark.asyncio
async def test_model_reasoning_is_not_shown_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PHRONESIS_SHOW_REASONING", raising=False)

    class Thinks:
        async def astream(self, _messages):
            for piece in ["<think>The instruction says cite only <evidence> ids.</think>", "No gene was named."]:
                yield SimpleNamespace(content=piece, additional_kwargs={})

    orch, _ = make_offline_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: Thinks())
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry("Find KRAS papers only.", thread_id="t-hidden", enable_web_search=False, event_callback=cb)
    assert not [e for e in events if e["type"] == "reasoning_delta"] and res.reasoning == ""
    assert "No gene was named." in "".join(e["delta"] for e in events if e["type"] == "content_delta")


# =============================================================================
# 5. Verifier, discovery and the evidence block
# =============================================================================

@pytest.mark.asyncio
async def test_verifier_accepts_resolved_ids_only():
    data = await _recorded_resolution(SHP2_Q)
    reg, _ = build_recording_registry(_resolver_tool(data))
    ctx = await _ctx(SHP2_Q, registry=reg)
    observed = ctx.observed_identifiers()
    assert {"CHEMBL4535757", "CHEMBL4650521"} <= observed["chembl"] and "HGNC:9644" in observed["hgnc"]
    assert "MONDO_0005233" in observed["ontology"] and "ENSG00000179295" in observed["ensembl"]

    tool_results = {"lithoscribe": ToolResult(success=True, tool_name="lithoscribe", data={"ranked_passages": []})}
    v = ClinicalVerifier()
    report = v.verify_response(
        SHP2_Q,
        "Sotorasib (CHEMBL4535757) targets KRAS G12C; CHEMBL9999999 and MONDO_0000001 are also relevant.",
        tool_results,
        context_identifiers=observed,
        query_context=ctx,
    )
    flagged = {d.claim for d in report.defects if d.category == "IDENTIFIER"}
    assert "CHEMBL9999999" in flagged and "MONDO_0000001" in flagged and "CHEMBL4535757" not in flagged
    assert "CHEMBL:CHEMBL4535757" in report.verified_identifiers

    typed = v.verify_response("What does rs113488022 do?", "rs113488022 is a BRAF variant.", tool_results)
    assert not [d for d in typed.defects if d.category == "IDENTIFIER"]


def test_unvalidated_entities_never_whitelist_ids():
    ctx = QueryContext(raw_query="q", clean_query="q")
    ctx.entities = [
        ResolvedEntity(type="drug", mention="x", label="X", ids={"chembl_id": "CHEMBL1"}, validated=False),
        ResolvedEntity(type="drug", mention="y", label="Y", ids={"chembl_id": "CHEMBL2"}, source="ChEMBL", validated=True),
    ]
    assert ctx.observed_identifiers()["chembl"] == {"CHEMBL2"}


def test_discovery_matches_species_through_aliases():
    viv = ToolResult(success=True, tool_name="vivarix", data={"initial_concentrations": {"Erk2-PP": 10.0, "Mos": 5.0}, "final_concentrations": {"Erk2-PP": 200.0, "Mos": 5.1}})
    report = BioDiscoveryEngine.prioritize_hypotheses("q", "MAPK1", {"vivarix": viv}, target_aliases=["ERK2"])
    assert "target species" in json.dumps(report.model_dump())
    without = BioDiscoveryEngine.prioritize_hypotheses("q", "MAPK1", {"vivarix": viv})
    assert "model species" in json.dumps(without.model_dump())


def test_query_entities_xml_is_escaped():
    ctx = QueryContext(raw_query="q", clean_query="q")
    ctx.entities = [
        ResolvedEntity(type="gene", mention='SH"P2<', label="PTPN11", ids={"hgnc_id": "HGNC:9644"}, source="HGNC", validated=True),
        ResolvedEntity(type="variant", mention="G12C", label="KRAS G12C", gene="KRAS", variant_kind="protein_change"),
    ]
    xml = render_query_entities(ctx)
    assert xml.startswith('<query_entities source="HGNC / OpenTargets / ChEMBL / dbSNP">')
    assert 'mention="SH&quot;P2&lt;"' in xml and 'hgnc_id="HGNC:9644"' in xml and 'validated="true"' in xml
    assert 'gene="KRAS"' in xml and 'validated="false"' in xml
    assert render_query_entities(QueryContext(raw_query="q", clean_query="q")) == ""
    ctx.normalization = "offline_lexicon"
    assert render_query_entities(ctx).startswith('<query_entities source="HGNC / OpenTargets / ChEMBL / dbSNP" normalization="offline_lexicon">')
    ctx.normalization = "skipped"

    tr = {"lithoscribe": ToolResult(success=True, tool_name="l", data={"ranked_passages": [{"pmid": "1", "passage_text": "t"}]})}
    plain = build_evidence_block(tr, "ranking")
    assert plain == build_evidence_block(tr, "ranking", query_context=None) and "<query_entities" not in plain
    with_ctx = build_evidence_block(tr, "ranking", query_context=ctx)
    assert with_ctx.startswith("<evidence>\n<query_entities") and with_ctx.replace(xml + "\n", "", 1) == plain
