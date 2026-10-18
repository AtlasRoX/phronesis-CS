"""
Offline tests for the conservative tool-argument repair (phronesis/tools/arg_repair.py) at every point that
validates tool inputs (registry, specialist tool calls, plan nodes), for repeated identical specialist calls being
reused rather than shown as errors, and for the openFDA safety lookup falling back to the drug name when ChEMBL is
down.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from pydantic import BaseModel, ConfigDict, Field

from phronesis.agentic.plan_graph import parse_plan, validate_plan, validated_inputs
from phronesis.agentic.specialists import SPECS_BY_NAME, Specialist
from phronesis.agentic.topologies import REUSED_PREFIX, call_step_status, run_specialist_tool
from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.drugintel import chembl_client
from phronesis.drugintel.schemas import (
    MAX_RESOLVE_NAMES,
    ChemblProfileInput,
    ChemblResolveInput,
    OpenFdaSafetyInput,
)
from phronesis.drugintel.sources import OpenFdaSource, chembl_unavailable
from phronesis.extensions.primekg import PrimeKGInvestigateInput
from phronesis.sources import SourceRunState
from phronesis.tools.arg_repair import adjustment_text, repair_arguments
from phronesis.tools.registry import AdapterType, BiomedicalToolRegistry, ErrorType, ToolResult, ToolUniverseEntry


def run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# repair_arguments
# -----------------------------------------------------------------------------

def test_numbers_are_clamped_to_their_bounds():
    fixed, notes = repair_arguments(ChemblProfileInput, {"target_chembl_id": "CHEMBL203", "activity_limit": 100})
    assert fixed["activity_limit"] == 50 and fixed["target_chembl_id"] == "CHEMBL203"
    assert notes == ["activity_limit 100→50"]
    fixed, notes = repair_arguments(OpenFdaSafetyInput, {"ingredient": "imatinib", "top_n": 0})
    assert fixed["top_n"] == 1 and notes == ["top_n 0→1"]


def test_strict_bounds_on_size_fields_only():
    class M(BaseModel):
        max_n: int = Field(default=1, gt=0, lt=10)
        top_k: float = Field(default=0.5, ge=0.0, le=1.0)
        duration: float = Field(default=10.0, gt=0.0)

    fixed, _ = repair_arguments(M, {"max_n": 25, "top_k": 3.5})
    assert fixed == {"max_n": 9, "top_k": 1.0}
    fixed, _ = repair_arguments(M, {"max_n": -4})
    assert fixed == {"max_n": 1}
    # A scientific value (a simulation duration) is never silently changed: the error stands.
    assert repair_arguments(M, {"duration": 0.0}) is None


def test_lists_are_cut_to_max_length_keeping_the_first_items():
    names = [f"drug{i}" for i in range(12)]
    fixed, notes = repair_arguments(ChemblResolveInput, {"names": names})
    assert fixed["names"] == names[:MAX_RESOLVE_NAMES]
    assert notes == [f"names cut to the first {MAX_RESOLVE_NAMES} of 12 items"]
    fixed, _ = repair_arguments(ChemblProfileInput, {"parent_chembl_ids": ["CHEMBL1", "CHEMBL2", "CHEMBL3", "CHEMBL4"]})
    assert fixed["parent_chembl_ids"] == ["CHEMBL1", "CHEMBL2", "CHEMBL3"]


def test_resolve_takes_the_six_names_the_swarm_sent():
    # The swarm asked for six candidates at once; the cap is now MAX_RESOLVE_NAMES (8), so no repair is needed.
    six = ["omacetaxine", "venetoclax", "palbociclib", "ribociclib", "abemaciclib", "alisertib"]
    assert ChemblResolveInput(names=six).names == six
    assert repair_arguments(ChemblResolveInput, {"names": six}) is None


def test_objects_become_their_name_where_a_string_is_expected():
    args = {"query": "EGFR in NSCLC", "diseases": [{"name": "non-small cell lung carcinoma", "id": "MONDO_0005233"}],
            "genes": [{"symbol": "EGFR"}], "drugs": [{"id": "osimertinib"}, "gefitinib"]}
    fixed, notes = repair_arguments(PrimeKGInvestigateInput, args)
    assert fixed["diseases"] == ["non-small cell lung carcinoma"]
    assert fixed["genes"] == ["EGFR"] and fixed["drugs"] == ["osimertinib", "gefitinib"]
    assert "diseases.0 object→'non-small cell lung carcinoma'" in notes
    assert args["diseases"][0]["name"] == "non-small cell lung carcinoma"  # the caller's args are never modified
    fixed, _ = repair_arguments(OpenFdaSafetyInput, {"ingredient": {"label": "imatinib"}})
    assert fixed["ingredient"] == "imatinib"


def test_single_string_becomes_a_one_item_list():
    fixed, notes = repair_arguments(PrimeKGInvestigateInput, {"query": "q", "genes": "KRAS", "diseases": "melanoma"})
    assert fixed["genes"] == ["KRAS"] and fixed["diseases"] == ["melanoma"]
    assert len(notes) == 2


def test_combined_problems_are_repaired_in_stages():
    # Item errors are reported before the list length: the repair re-validates until both are fixed.
    names = [{"name": f"drug{i}"} for i in range(10)]
    fixed, notes = repair_arguments(ChemblResolveInput, {"names": names})
    assert fixed["names"] == [f"drug{i}" for i in range(MAX_RESOLVE_NAMES)]
    assert any("cut to the first" in n for n in notes)


def test_extra_keys_are_dropped_only_when_forbidden():
    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        gene: str

    class Lax(BaseModel):
        gene: str

    fixed, notes = repair_arguments(Strict, {"gene": "KRAS", "species": "human"})
    assert fixed == {"gene": "KRAS"} and notes == ["dropped unknown argument species"]
    assert repair_arguments(Lax, {"gene": "KRAS", "species": "human"}) is None  # valid as given (extra ignored)
    fixed, _ = repair_arguments(Lax, {"gene": "KRAS", "species": "human"}, drop_unknown=True)
    assert fixed == {"gene": "KRAS"}


def test_unrepairable_or_valid_arguments_are_left_alone():
    assert repair_arguments(ChemblProfileInput, {"activity_limit": 10}) is None  # already valid
    assert repair_arguments(ChemblProfileInput, {"target_chembl_id": "EGFR"}) is None  # wrong pattern: no guess
    assert repair_arguments(OpenFdaSafetyInput, {}) is None  # missing required field
    assert repair_arguments(OpenFdaSafetyInput, {"ingredient": {"smiles": "CCO"}}) is None  # no name in the object
    assert repair_arguments(ChemblProfileInput, {"activity_limit": "lots"}) is None
    # A fixable error next to an unfixable one: nothing is half-repaired.
    assert repair_arguments(ChemblProfileInput, {"activity_limit": 100, "target_chembl_id": "EGFR"}) is None


def test_adjustment_text():
    assert adjustment_text(["activity_limit 100→50"]) == "arguments adjusted: activity_limit 100→50"


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

def _registry(calls: List[Tuple[str, Dict[str, Any]]]) -> BiomedicalToolRegistry:
    reg = BiomedicalToolRegistry()

    def add(name: str, schema: Any, domain: str = "synapta") -> None:
        async def execute(**kwargs: Any) -> Dict[str, Any]:
            calls.append((name, kwargs))
            return {"echo": kwargs}

        reg.register(
            ToolUniverseEntry(name=name, description=name, domain=domain, adapter_type=AdapterType.DIRECT_REST,
                              input_schema=schema, is_async=True),
            executor=execute,
        )

    add("chembl_drug_profile", ChemblProfileInput)
    add("chembl_resolve_drugs", ChemblResolveInput)
    add("primekg_investigate", PrimeKGInvestigateInput)
    return reg


def test_registry_repairs_and_records_the_adjustment():
    calls: List[Tuple[str, Dict[str, Any]]] = []
    reg = _registry(calls)
    # Programmatic calls are validated strictly; agent-written calls opt in to repair.
    strict = run(reg.execute("chembl_drug_profile", target_chembl_id="CHEMBL203", activity_limit=100))
    assert not strict.success and strict.error_type == ErrorType.VALIDATION_ERROR
    res = run(reg.execute("chembl_drug_profile", _repair_arguments=True, target_chembl_id="CHEMBL203", activity_limit=100))
    assert res.success
    assert calls[-1][1]["activity_limit"] == 50
    assert res.metadata["arguments_adjusted"] == ["activity_limit 100→50"]

    res = run(reg.execute("chembl_drug_profile", _repair_arguments=True, target_chembl_id="EGFR"))
    assert not res.success and res.error_type == ErrorType.VALIDATION_ERROR  # original error kept
    assert "arguments_adjusted" not in res.metadata

    res = run(reg.execute("chembl_drug_profile", target_chembl_id="CHEMBL203"))
    assert res.success and "arguments_adjusted" not in res.metadata


# -----------------------------------------------------------------------------
# Specialist tool calls (supervisor / swarm)
# -----------------------------------------------------------------------------

def _specialist_state(reg: BiomedicalToolRegistry) -> SimpleNamespace:
    state = SimpleNamespace(ctx=QueryContext(raw_query="q", clean_query="q"), bag={}, tool_results={}, n=0)

    def next_key(phase: str, tool: str) -> str:
        state.n += 1
        return f"agent.{phase}.{state.n}.{tool.split('_', 1)[0]}"

    async def call_tool(tool: str, *, phase: str, purpose: str, key: Optional[str] = None, **kwargs: Any) -> Any:
        res = await reg.execute(tool, **kwargs)
        state.tool_results[key] = res
        return res

    state.next_key = next_key
    state.call_tool = call_tool
    return state


def _chemistry(reg: BiomedicalToolRegistry) -> Specialist:
    return Specialist(SPECS_BY_NAME["chemistry"], [reg.get_entry("chembl_drug_profile"), reg.get_entry("chembl_resolve_drugs")])


def test_specialist_call_with_repairable_arguments_runs():
    calls: List[Tuple[str, Dict[str, Any]]] = []
    reg = _registry(calls)
    state = _specialist_state(reg)
    sp = _chemistry(reg)
    obs, key, executed = run(run_specialist_tool(state, sp, "chembl_drug_profile",
                                                 {"target_chembl_id": "CHEMBL203", "activity_limit": 100},
                                                 phase="supervisor", purpose="potency"))
    assert executed and key
    assert obs.startswith("chembl_drug_profile succeeded (evidence key ")
    assert "arguments adjusted: activity_limit 100→50" in obs
    assert calls[-1][1]["activity_limit"] == 50
    assert call_step_status(obs, executed) == "done"

    target = Specialist(SPECS_BY_NAME["target"], [reg.get_entry("primekg_investigate")])
    obs, _, executed = run(run_specialist_tool(state, target, "primekg_investigate",
                                               {"query": "EGFR NSCLC", "diseases": [{"name": "NSCLC"}]},
                                               phase="supervisor", purpose="kg"))
    assert executed and calls[-1][1]["diseases"] == ["NSCLC"]


def test_specialist_call_with_unrepairable_arguments_is_still_rejected():
    reg = _registry([])
    state = _specialist_state(reg)
    obs, key, executed = run(run_specialist_tool(state, _chemistry(reg), "chembl_drug_profile",
                                                 {"target_chembl_id": "EGFR"}, phase="swarm", purpose="x"))
    assert not executed and key is None
    assert obs.startswith("Invalid arguments for chembl_drug_profile: target_chembl_id")
    assert call_step_status(obs, executed) == "error"


def test_repeated_identical_call_reuses_the_earlier_result_and_is_not_an_error():
    calls: List[Tuple[str, Dict[str, Any]]] = []
    reg = _registry(calls)
    state = _specialist_state(reg)
    sp = _chemistry(reg)
    args = {"names": ["imatinib", "dasatinib"]}
    _, key1, executed1 = run(run_specialist_tool(state, sp, "chembl_resolve_drugs", args, phase="swarm", purpose="x"))
    obs, key2, executed2 = run(run_specialist_tool(state, sp, "chembl_resolve_drugs", dict(args), phase="swarm", purpose="x"))
    assert executed1 and not executed2 and key2 is None
    assert len(calls) == 1  # not run twice
    assert obs.startswith(f"{REUSED_PREFIX} chembl_resolve_drugs call (evidence key {key1})")
    assert "imatinib" in obs  # the earlier result is handed back
    assert call_step_status(obs, executed2) == "skipped"
    # A repaired call that matches an earlier call after repair is reused too.
    _, _, ran = run(run_specialist_tool(state, sp, "chembl_drug_profile", {"target_chembl_id": "CHEMBL203", "activity_limit": 50},
                                        phase="swarm", purpose="x"))
    obs, _, again = run(run_specialist_tool(state, sp, "chembl_drug_profile", {"target_chembl_id": "CHEMBL203", "activity_limit": 99},
                                            phase="swarm", purpose="x"))
    assert ran and not again and obs.startswith(REUSED_PREFIX)


# -----------------------------------------------------------------------------
# Plan nodes
# -----------------------------------------------------------------------------

def test_plan_validation_repairs_node_arguments_before_rejecting():
    reg = _registry([])
    graph = parse_plan({"rationale": "", "nodes": [
        {"id": "n1", "tool": "chembl_drug_profile", "args": {"target_chembl_id": "CHEMBL203", "activity_limit": 500}},
        {"id": "n2", "tool": "primekg_investigate", "args": {"query": "q", "diseases": [{"label": "melanoma"}],
                                                             "organism": "human"}},
        {"id": "n3", "tool": "chembl_drug_profile", "args": {"target_chembl_id": "not-an-id"}},
        {"id": "n4", "tool": "chembl_resolve_drugs", "args": {"names": "$n1.drugs.*.pref_name", "chembl_ids": "CHEMBL941"},
         "depends_on": ["n1"]},
    ]})
    validate_plan(graph, reg, QueryContext(raw_query="q", clean_query="q"))
    reasons = {r["node"]: r["reason"] for r in graph.rejected}
    assert reasons.get("n3", "").startswith("schema: argument target_chembl_id")
    n1, n2, n4 = graph.get("n1"), graph.get("n2"), graph.get("n4")
    assert n1.args["activity_limit"] == 50 and n1.adjusted == ["activity_limit 500→50"]
    assert n2.args == {"query": "q", "diseases": ["melanoma"]}
    assert "dropped unknown argument organism" in n2.adjusted
    assert n4.args["chembl_ids"] == ["CHEMBL941"] and n4.args["names"] == "$n1.drugs.*.pref_name"  # refs untouched
    assert n1.to_artifact()["adjusted"] == ["activity_limit 500→50"]
    # The executor's duplicate check sees the call the registry will make.
    entry = reg.get_entry("chembl_drug_profile")
    assert validated_inputs(entry, {"target_chembl_id": "CHEMBL203", "activity_limit": 80})["activity_limit"] == 50


# -----------------------------------------------------------------------------
# openFDA safety lookup when ChEMBL is down
# -----------------------------------------------------------------------------

class _SafetyRegistry:
    """chembl_resolve_drugs answers with `resolve`; openfda_drug_safety records the ingredient it was asked for."""

    def __init__(self, resolve: ToolResult) -> None:
        self.resolve = resolve
        self.safety_calls: List[str] = []

    async def execute(self, tool_name: str, **kwargs: Any) -> ToolResult:
        if tool_name == "chembl_resolve_drugs":
            return self.resolve
        assert tool_name == "openfda_drug_safety"
        ing = kwargs["ingredient"]
        self.safety_calls.append(ing)
        return ToolResult(success=True, tool_name=tool_name, data={
            "ingredient": ing, "total_reports": 1234, "serious_reports": 800,
            "top_reactions": [{"term": "COLITIS", "count": 90}], "label": None, "label_error": None,
            "approval": {"status": "approved", "ingredient": ing, "application_number": "BLA125514",
                         "applications": [{"application_number": "BLA125514"}]},
            "citation_records": [], "faers_search": f'activesubstancename:"{ing}"',
        })


def _pembro_ctx() -> QueryContext:
    q = "What are the safety warnings for pembrolizumab in melanoma?"
    ctx = QueryContext(raw_query=q, clean_query=q)
    ctx.entities = [ResolvedEntity(type="drug", mention="pembrolizumab", label="PEMBROLIZUMAB",
                                   ids={"chembl_id": "CHEMBL3137343"}, source="OpenTargets", validated=True)]
    return ctx


CHEMBL_500 = ToolResult(success=False, tool_name="chembl_resolve_drugs", error_type=ErrorType.UPSTREAM_API_ERROR,
                        error="ChEMBL HTTP 500 for molecule/CHEMBL3137343.json")


def test_openfda_falls_back_to_the_drug_name_when_chembl_is_down():
    ctx = _pembro_ctx()
    reg = _SafetyRegistry(CHEMBL_500)
    res = run(OpenFdaSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert res.success, res.error
    assert reg.safety_calls == ["pembrolizumab"]  # the user's words, not the ChEMBL id
    entry = res.data["safety"][0]
    assert entry["matched_by_name"] is True and entry["chembl_id"] == "CHEMBL3137343"
    assert res.data["chembl_unavailable"]["drugs"] == ["pembrolizumab"]
    notes = OpenFdaSource().scope_notes(ctx, res)
    assert any("ChEMBL was unavailable" in n and "by the drug name as written" in n for n in notes)
    title, detail = OpenFdaSource().describe(res, ctx)
    assert title == "FAERS: 1,234 reports mention pembrolizumab"
    assert "ChEMBL unavailable: matched by name (pembrolizumab)" in detail
    xml = OpenFdaSource().evidence_xml(res)
    assert "<chembl_unavailable" in xml and 'matched_by="name (ChEMBL unavailable)"' in xml


def test_openfda_still_fails_when_chembl_rejects_the_request():
    ctx = _pembro_ctx()
    bad = ToolResult(success=False, tool_name="chembl_resolve_drugs", error_type=ErrorType.VALIDATION_ERROR, error="bad")
    reg = _SafetyRegistry(bad)
    res = run(OpenFdaSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert not res.success and "could not be identified in ChEMBL first" in res.error
    assert reg.safety_calls == []
    assert chembl_unavailable(CHEMBL_500) and not chembl_unavailable(bad)


def test_openfda_keeps_skipping_unapproved_drugs_when_chembl_answers():
    ctx = _pembro_ctx()
    ok = ToolResult(success=True, tool_name="chembl_resolve_drugs", data={
        "drugs": [{"query_name": "CHEMBL3137343", "chembl_id": "CHEMBL3137343", "pref_name": "PEMBROLIZUMAB",
                   "max_phase": 2.0, "max_phase_meaning": "reached phase 2 clinical trials", "matched_via": "chembl_id"}],
        "unresolved_names": [], "invalid_chembl_ids": [], "lookup_errors": []})
    reg = _SafetyRegistry(ok)
    res = run(OpenFdaSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert res.success and res.data["safety"] == [] and reg.safety_calls == []
    assert "chembl_unavailable" not in res.data


def test_openfda_queries_by_name_only_the_drugs_whose_chembl_lookup_failed():
    q = "Compare the adverse events of imatinib and dasatinib"
    ctx = QueryContext(raw_query=q, clean_query=q)
    partial = ToolResult(success=True, tool_name="chembl_resolve_drugs", data={
        "drugs": [{"query_name": "imatinib", "chembl_id": "CHEMBL941", "pref_name": "IMATINIB", "max_phase": 4.0,
                   "max_phase_meaning": "approved", "matched_via": "pref_name"}],
        "unresolved_names": [], "invalid_chembl_ids": [],
        "lookup_errors": [{"query": "dasatinib", "error": "ChEMBL request timed out"}]})
    reg = _SafetyRegistry(partial)
    res = run(OpenFdaSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert sorted(reg.safety_calls) == ["dasatinib", "imatinib"]
    by_name = [e for e in res.data["safety"] if e.get("matched_by_name")]
    assert [e["ingredient"] for e in by_name] == ["dasatinib"]
    assert res.data["chembl_unavailable"]["drugs"] == ["dasatinib"]


def test_openfda_name_fallback_with_the_real_engine(monkeypatch):
    """ChEMBL answers 500 to everything; FAERS and the label are still fetched for imatinib by name."""
    from tests.test_drug_intel import Upstream, _drug_registry

    class ChemblDown(Upstream):
        def _chembl(self, path, q):  # noqa: ANN001
            import httpx

            return httpx.Response(500, json={"error": "down"})

    monkeypatch.setattr(chembl_client, "RETRY_BACKOFF_S", 0.0)
    chembl_client.clear_release_cache()
    chembl_client.clear_lookup_cache()
    reg, _ = _drug_registry(ChemblDown())
    ctx = QueryContext(raw_query="What are the adverse events of imatinib?", clean_query="What are the adverse events of imatinib?")
    res = run(OpenFdaSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert res.success, res.error
    assert OpenFdaSource().describe(res, ctx)[0] == "FAERS: 46,623 reports mention imatinib"
    assert res.data["chembl_unavailable"]["drugs"] == ["imatinib"]
