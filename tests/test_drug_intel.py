"""
Offline tests for drug and safety intelligence (ChEMBL + openFDA): clients, engine, evidence sources,
orchestrator routing, evidence XML and verifier checks. Every HTTP response is a trimmed live capture
from tests/fixtures/drugintel/ served through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from phronesis.agents.prompts import build_evidence_block, build_tool_status_block, get_synthesis_system_prompt
from phronesis.agents.query_context import QueryContext
from phronesis.drugintel import checks, names
from phronesis.drugintel.chembl_client import ChemblClient, clear_release_cache
from phronesis.drugintel.engine import DrugIntelEngine
from phronesis.drugintel.openfda_client import OpenFdaClient
from phronesis.drugintel.schemas import (
    FAERS_CAVEAT,
    ChemblProfileInput,
    ChemblResolveInput,
    OpenFdaSafetyInput,
)
from phronesis.drugintel.sources import ChemblSource, OpenFdaSource
from phronesis.extensions import hooks, load_extensions
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.sources import SourceRunState
from phronesis.tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult
from phronesis.utils.http_limits import TTLCache
from tests.fakes import build_recording_registry, called, make_offline_orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "drugintel"


def fx(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# MockTransport serving the captured responses
# -----------------------------------------------------------------------------

class Upstream:
    """Routes ChEMBL/openFDA requests to fixtures and records every request it served."""

    def __init__(self, overrides: Optional[Dict[str, Tuple[int, str]]] = None) -> None:
        self.requests: List[httpx.Request] = []
        self.overrides = overrides or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path, q = request.url.host, request.url.path, request.url.params
        if host == "api.fda.gov":
            return self._openfda(path, q)
        return self._chembl(path, q)

    @staticmethod
    def _json(name: str, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=fx(name))

    def _chembl(self, path: str, q: httpx.QueryParams) -> httpx.Response:
        if path.endswith("/status.json"):
            return self._json("status")
        if path.endswith("/molecule/CHEMBL999999999.json"):
            return httpx.Response(404, content=b"")
        if path.endswith("/molecule.json"):
            if q.get("pref_name__iexact", "").lower() == "imatinib":
                return self._json("molecule_pref_imatinib")
            if q.get("pref_name__iexact", "").lower() == "tivantinib":
                return self._json("molecule_pref_tivantinib")
            if q.get("molecule_synonyms__molecule_synonym__iexact", "").lower() == "gleevec":
                return self._json("molecule_syn_gleevec")
            ids = q.get("molecule_chembl_id__in", "")
            if ids == "CHEMBL941":
                return self._json("molecule_in_imatinib")
            if ids.startswith("CHEMBL1201827,"):
                return self._json("molecule_in_egfr_approved")
            if "pref_name__iexact" in q or "molecule_synonyms__molecule_synonym__iexact" in q:
                return self._json("molecule_empty")
        if path.endswith("/mechanism.json"):
            if q.get("parent_molecule_chembl_id") == "CHEMBL941":
                return self._json("mechanism_parent_imatinib")
            if q.get("molecule_chembl_id") == "CHEMBL941":
                return self._json("mechanism_by_molecule_imatinib_empty")
            if q.get("target_chembl_id") == "CHEMBL203" and q.get("max_phase") == "4":
                return self._json("mechanism_target_egfr_approved")
        if path.endswith("/target.json"):
            sym = q.get("target_components__target_component_synonyms__component_synonym__iexact")
            if sym == "ABL1":
                return self._json("target_sym_abl1")
            if sym == "EGFR":
                return self._json("target_sym_egfr")
            ids = q.get("target_chembl_id__in")
            if ids == "CHEMBL1862,CHEMBL1913,CHEMBL1936,CHEMBL2096618":
                return self._json("target_in_imatinib_mechanisms")
            if ids == "CHEMBL203":
                return self._json("target_in_egfr")
        if path.endswith("/activity.json") and q.get("parent_molecule_chembl_id") == "CHEMBL941" and q.get("target_chembl_id") == "CHEMBL1862":
            return self._json("activity_imatinib_abl1")
        raise AssertionError(f"unexpected ChEMBL request {path}?{q}")

    def _openfda(self, path: str, q: httpx.QueryParams) -> httpx.Response:
        search = q.get("search", "")
        assert int(q.get("limit", "1")) < 1000, "openFDA limit must stay below 1000 without an API key"
        for needle, (status, name) in self.overrides.items():
            if needle in search:
                return self._json(name, status)
        if path == "/drug/event.json":
            assert "openfda.generic_name" not in search
            for drug in ("imatinib", "osimertinib"):
                if f'activesubstancename:"{drug}"' in search:
                    if q.get("count") == "patient.reaction.reactionmeddrapt.exact":
                        return self._json(f"faers_counts_{drug}")
                    if q.get("count") == "serious":
                        return self._json(f"faers_serious_{drug}")
                    return self._json(f"faers_total_{drug}")
            return self._json("openfda_404_not_found", 404)
        if path == "/drug/label.json":
            if q.get("count") == "openfda.generic_name.exact":
                for drug in ("imatinib", "metformin", "aspirin"):
                    if search.startswith(f'openfda.generic_name:"{drug}"'):
                        return self._json(f"label_generic_count_{drug}")
                return self._json("openfda_404_not_found", 404)
            if search.startswith('openfda.generic_name.exact:"IMATINIB MESYLATE"'):
                return self._json("label_exact_imatinib")
            if search.startswith('openfda.generic_name.exact:"METFORMIN HYDROCHLORIDE"'):
                return self._json("label_exact_metformin")
            if search.startswith('openfda.generic_name:"aspirin"'):
                return self._json("label_nda_aspirin")
            if search.startswith('openfda.generic_name:"imatinib"'):
                return self._json("label_nda_imatinib")
            if search == "spl_product_data_elements:osimertinib":
                return self._json("label_fallback_osimertinib")
            if search == "spl_product_data_elements:warfarin":
                return self._json("label_fallback_warfarin")
            return self._json("openfda_404_not_found", 404)
        raise AssertionError(f"unexpected openFDA request {path}?{q}")


def make_engine(upstream: Optional[Upstream] = None) -> Tuple[DrugIntelEngine, Upstream]:
    upstream = upstream or Upstream()
    transport = httpx.MockTransport(upstream)
    engine = DrugIntelEngine(
        ChemblClient(transport=transport),
        OpenFdaClient(transport=transport, api_key="", cache=TTLCache()),
    )
    return engine, upstream


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_release_cache():
    clear_release_cache()
    yield
    clear_release_cache()


def _requests_to(upstream: Upstream, path_end: str) -> List[httpx.Request]:
    return [r for r in upstream.requests if r.url.path.endswith(path_end)]


# -----------------------------------------------------------------------------
# (a) Resolution
# -----------------------------------------------------------------------------

def test_resolve_by_pref_name():
    engine, up = make_engine()
    res = run(engine.resolve_drugs(names=["imatinib"], target_symbol="ABL1"))
    assert res.success, res.error
    drug = res.data["drugs"][0]
    assert (drug["chembl_id"], drug["pref_name"], drug["matched_via"]) == ("CHEMBL941", "IMATINIB", "pref_name")
    assert drug["max_phase"] == 4.0 and drug["first_approval"] == 2001 and drug["forms"] == []
    assert drug["max_phase_meaning"].startswith("approved by at least one regulator")
    assert res.data["target"] == {"target_chembl_id": "CHEMBL1862", "pref_name": "Tyrosine-protein kinase ABL1", "symbol_query": "ABL1"}
    assert res.data["chembl_release"] == "ChEMBL_37"
    assert not any("molecule_synonyms__molecule_synonym__iexact" in r.url.params for r in up.requests)
    assert not any("search.json" in r.url.path for r in up.requests)  # fuzzy search is never used
    assert set(res.canonical_ids["chembl_ids"].split(",")) == {"CHEMBL941", "CHEMBL1862"}


def test_resolve_trade_name_collapses_salt_to_parent():
    engine, _ = make_engine()
    res = run(engine.resolve_drugs(names=["Gleevec"]))
    drug = res.data["drugs"][0]
    assert drug["chembl_id"] == "CHEMBL941" and drug["matched_via"] == "synonym"
    assert drug["forms"] == ["CHEMBL1642"]
    assert drug["query_name"] == "Gleevec"


def test_resolve_unknown_name_and_invalid_id():
    engine, _ = make_engine()
    res = run(engine.resolve_drugs(names=["notadrugxyz"], chembl_ids=["CHEMBL999999999"]))
    assert res.success
    assert res.data["drugs"] == []
    assert res.data["unresolved_names"] == ["notadrugxyz"]
    assert res.data["invalid_chembl_ids"] == ["CHEMBL999999999"]


def test_resolve_input_schema_rejects_bad_ids():
    with pytest.raises(Exception):
        ChemblResolveInput(chembl_ids=["CHEMBL94x"])
    with pytest.raises(Exception):
        OpenFdaSafetyInput(ingredient='imatinib" OR x:(y)')
    assert ChemblProfileInput(parent_chembl_ids=["CHEMBL941"]).activity_limit == 10


# -----------------------------------------------------------------------------
# (b, c) Mechanisms by parent; activity casting
# -----------------------------------------------------------------------------

def test_mechanisms_are_queried_by_parent_and_found_on_the_salt():
    engine, up = make_engine()
    res = run(engine.drug_profile(parent_chembl_ids=["CHEMBL941"], target_chembl_id="CHEMBL1862", target_symbol="ABL1"))
    assert res.success, res.error
    mech_reqs = _requests_to(up, "/mechanism.json")
    assert [r.url.params.get("parent_molecule_chembl_id") for r in mech_reqs] == ["CHEMBL941"]
    assert all("molecule_chembl_id" not in r.url.params for r in mech_reqs)
    drug = res.data["drugs"][0]
    assert len(drug["mechanisms"]) == 4
    assert {m["form_chembl_id"] for m in drug["mechanisms"]} == {"CHEMBL1642"}
    assert {m["target_chembl_id"] for m in drug["mechanisms"]} == {"CHEMBL1862", "CHEMBL1913", "CHEMBL1936", "CHEMBL2096618"}
    assert drug["mechanisms"][0]["target_pref_name"] == "Tyrosine-protein kinase ABL1"
    assert "CHEMBL1642" in drug["forms"]


def test_salt_regression_querying_the_parent_as_molecule_finds_nothing():
    # The captured by-molecule response for the parent is empty; the mechanisms live on CHEMBL1642.
    assert fx("mechanism_by_molecule_imatinib_empty")["mechanisms"] == []
    assert all(m["molecule_chembl_id"] == "CHEMBL1642" for m in fx("mechanism_parent_imatinib")["mechanisms"])


def test_activity_values_are_cast_from_standard_fields():
    engine, up = make_engine()
    res = run(engine.drug_profile(parent_chembl_ids=["CHEMBL941"], target_chembl_id="CHEMBL1862"))
    acts = res.data["drugs"][0]["bioactivities"]
    raw = fx("activity_imatinib_abl1")["activities"]
    assert len(acts) == 10 and res.data["drugs"][0]["bioactivity_total"] == 106
    assert acts[0]["standard_value"] == float(raw[0]["standard_value"]) == 1.0
    assert acts[0]["pchembl_value"] == float(raw[0]["pchembl_value"]) == 9.0
    ki = next(a for a in acts if a["standard_type"] == "Ki")
    raw_ki = next(r for r in raw if r["standard_type"] == "Ki")
    assert (ki["standard_value"], ki["standard_units"]) == (13.0, "nM")
    assert raw_ki["units"] == "uM"  # the raw field differs; only standard_* is kept
    assert "value" not in ki and "units" not in ki
    assert any("Q252H" in (a["assay_description"] or "") for a in acts)
    act_req = _requests_to(up, "/activity.json")[0].url.params
    assert act_req["standard_type__in"] == "IC50,Ki,Kd" and act_req["order_by"] == "-pchembl_value"


def test_approved_drugs_for_target():
    engine, _ = make_engine()
    res = run(engine.drug_profile(target_chembl_id="CHEMBL203", target_symbol="EGFR"))
    approved = res.data["target_approved_drugs"]
    names_ = {a["pref_name"] for a in approved}
    assert {"GEFITINIB", "ERLOTINIB", "OSIMERTINIB"} <= names_
    assert all(a["chembl_id"].startswith("CHEMBL") for a in approved)
    assert res.data["target"]["pref_name"] == "Epidermal growth factor receptor"


# -----------------------------------------------------------------------------
# (d) openFDA
# -----------------------------------------------------------------------------

def test_faers_counts_total_serious_and_label():
    engine, up = make_engine()
    res = run(engine.drug_safety("imatinib"))
    assert res.success, res.error
    d = res.data
    counts = fx("faers_counts_imatinib")
    assert d["total_reports"] == fx("faers_total_imatinib")["meta"]["results"]["total"] == 46623
    assert d["serious_reports"] == 36826
    # DEATH (the fixture's top term) is an outcome, not a reaction: it is reported separately (plan item 2.4).
    assert counts["results"][0]["term"] == "DEATH"
    assert d["deaths_reported"] == counts["results"][0]["count"] == 10162
    assert d["top_reactions"][0] == {"term": "NAUSEA", "count": 2570}
    assert all(r["term"] != "DEATH" for r in d["top_reactions"])
    assert d["disclaimer"] == counts["meta"]["disclaimer"]
    assert d["last_updated"] == counts["meta"]["last_updated"]
    assert d["caveats"] == [FAERS_CAVEAT]
    assert d["faers_search"] == 'patient.drug.activesubstance.activesubstancename:"imatinib"'
    label = d["label"]
    assert (label["brand_name"], label["application_number"], label["harmonized"]) == ("Gleevec", "NDA021588", True)
    assert label["boxed_warning"] is None
    assert label["dailymed_url"] == "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=211ef2da-2868-4a77-8055-1cb2cd78e24b"
    assert res.canonical_ids == {"set_ids": "211ef2da-2868-4a77-8055-1cb2cd78e24b"}
    assert d["citation_records"][0]["type"] == "dailymed"
    for r in _requests_to(up, "/drug/event.json"):
        assert "activesubstancename" in r.url.params["search"]
        assert int(r.url.params.get("limit", "1")) < 1000


def test_label_truncation_keeps_a_verbatim_prefix():
    engine, _ = make_engine()
    label = run(engine.drug_safety("imatinib")).data["label"]
    source = " ".join(fx("label_nda_imatinib")["results"][0]["warnings_and_cautions"][0].split())
    assert len(source) > 10000
    assert label["warnings_truncated"] is True and len(label["warnings"]) <= 1500
    assert source.startswith(label["warnings"])
    assert label["warnings_section"] == "warnings_and_cautions"


def test_label_fallback_is_marked_unharmonized():
    engine, up = make_engine()
    res = run(engine.drug_safety("osimertinib"))
    assert res.success, res.error
    label = res.data["label"]
    assert label["harmonized"] is False
    assert label["brand_name"] is None and label["application_number"] is None
    assert label["set_id"] == "5e81b4a7-b971-45e1-9c31-29cea8c87ce7"
    assert res.data["label_search"] == "spl_product_data_elements:osimertinib"
    searches = [r.url.params["search"] for r in _requests_to(up, "/drug/label.json")]
    assert searches[0].startswith('openfda.generic_name:"osimertinib" AND openfda.application_number:(NDA* OR BLA*)')
    assert res.data["total_reports"] == 32521


def test_boxed_warning_kept_and_contraindications_truncated():
    engine, _ = make_engine(Upstream(overrides={'activesubstancename:"warfarin"': (404, "openfda_404_not_found")}))
    label = run(engine.drug_safety("warfarin")).data["label"]
    raw = " ".join(fx("label_fallback_warfarin")["results"][0]["boxed_warning"][0].split())
    assert label["boxed_warning"] == raw and label["boxed_warning_truncated"] is False
    assert label["boxed_warning"].startswith("WARNING: BLEEDING RISK")
    assert label["contraindications_truncated"] is True and len(label["contraindications"]) <= 600


def test_openfda_404_is_success_with_zero_reports():
    engine, _ = make_engine()
    res = run(engine.drug_safety("zzqnotadrug", include_label=False))
    assert res.success
    assert res.data["total_reports"] == 0 and res.data["top_reactions"] == []


def test_openfda_403_and_500_are_failures():
    engine, _ = make_engine(Upstream(overrides={"imatinib": (403, "openfda_403_api_key_missing")}))
    res = run(engine.drug_safety("imatinib"))
    assert not res.success and res.error_type == ErrorType.UPSTREAM_API_ERROR
    assert "API key" in res.error and "403" in res.error

    engine, _ = make_engine(Upstream(overrides={"imatinib": (500, "openfda_500_parse_exception")}))
    res = run(engine.drug_safety("imatinib"))
    assert not res.success and "parse_exception" in res.error


def test_limit_is_clamped_below_1000_without_a_key():
    client = OpenFdaClient(api_key="", cache=TTLCache())
    assert client.clamp_limit(1000) == 999
    assert OpenFdaClient(api_key="k", cache=TTLCache()).clamp_limit(1000) == 1000


# -----------------------------------------------------------------------------
# Names and routing decisions
# -----------------------------------------------------------------------------

def test_drug_mention_detection():
    assert names.extract_drug_mentions("What are the adverse events of imatinib?") == ["imatinib"]
    assert names.extract_drug_mentions("Compare pembrolizumab, nivolumab and ipilimumab") == ["pembrolizumab", "nivolumab"]
    assert names.extract_drug_mentions("Is CHEMBL941 potent?") == ["CHEMBL941"]
    assert names.extract_drug_mentions("aspirin and warfarin bleeding") == ["aspirin", "warfarin"]
    assert names.extract_drug_mentions("KRAS signaling in pancreatic cancer") == []
    assert names.extract_drug_mentions("platinum resistance in ovarian cancer") == []
    assert names.has_drug_intent("Which approved drugs target EGFR?")
    assert not names.has_drug_intent("KRAS signaling in pancreatic cancer")
    assert names.normalize_target_symbol("KRAS G12C") == "KRAS"


def _ctx(query: str, target: Optional[str] = None) -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query, primary_target=target)


def test_source_routing_decisions():
    chembl, openfda = ChemblSource(), OpenFdaSource()
    assert chembl.should_run(_ctx("adverse events of imatinib")).run
    assert openfda.should_run(_ctx("adverse events of imatinib")).run
    assert chembl.should_run(_ctx("approved drugs targeting EGFR", "EGFR")).run
    assert not openfda.should_run(_ctx("approved drugs targeting EGFR", "EGFR")).run
    assert not chembl.should_run(_ctx("KRAS signaling in pancreatic cancer", "KRAS")).run
    assert chembl.running_title(_ctx("adverse events of imatinib")) == "Looking up imatinib in ChEMBL"
    assert openfda.running_title(_ctx("adverse events of imatinib")) == "Checking FDA adverse-event reports and label for imatinib"


# -----------------------------------------------------------------------------
# Sources against a real registry wired to the fixtures
# -----------------------------------------------------------------------------

def _drug_registry(upstream: Optional[Upstream] = None) -> Tuple[BiomedicalToolRegistry, Upstream]:
    from phronesis.extensions import drugs as drugs_ext

    engine, up = make_engine(upstream)
    reg = BiomedicalToolRegistry()
    with hooks.isolated_hooks():
        drugs_ext.register_tools(reg)
    for name, fn in (
        ("chembl_resolve_drugs", engine.resolve_drugs),
        ("chembl_drug_profile", engine.drug_profile),
        ("openfda_drug_safety", engine.drug_safety),
    ):
        reg._executors[name] = fn
    return reg, up


async def _run_sources(query: str, target: Optional[str] = None, upstream: Optional[Upstream] = None):
    reg, up = _drug_registry(upstream)
    ctx = _ctx(query, target)
    state = SourceRunState(reg, ctx)
    chembl, openfda = ChemblSource(), OpenFdaSource()
    c_res, o_res = await asyncio.gather(chembl.run(ctx, reg, state), openfda.run(ctx, reg, state))
    return ctx, c_res, o_res, up


def test_sources_share_one_resolve_and_describe_from_data():
    ctx, c_res, o_res, up = run(_run_sources("What are the adverse events of imatinib?"))
    assert len([r for r in up.requests if r.url.params.get("pref_name__iexact")]) == 1
    title, detail = ChemblSource().describe(c_res, ctx)
    assert title == "Resolved imatinib to CHEMBL941 (max phase 4)"
    assert detail == "4 mechanisms"
    title, detail = OpenFdaSource().describe(o_res, ctx)
    assert title == "FAERS: 46,623 reports mention imatinib"
    assert detail == "top: NAUSEA (2570) · boxed warning: none on Gleevec NDA021588"

    assert ChemblSource().allowed_identifiers(c_res)["chembl"] >= {"CHEMBL941", "CHEMBL1642", "CHEMBL1862"}
    cites = ChemblSource().citations(c_res) + OpenFdaSource().citations(o_res)
    assert [(c.type, c.id, c.source_agent) for c in cites] == [
        ("chembl", "CHEMBL941", "Synapta"),
        ("chembl", "CHEMBL1862", "Synapta"),
        ("chembl", "CHEMBL1913", "Synapta"),
        ("chembl", "CHEMBL1936", "Synapta"),
        ("chembl", "CHEMBL2096618", "Synapta"),
        ("dailymed", "211ef2da-2868-4a77-8055-1cb2cd78e24b", "Lithoscribe"),
    ]
    assert cites[0].url == "https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL941"
    # Mechanism targets link to ChEMBL target pages, named as ChEMBL names them.
    assert (cites[1].title, cites[1].url) == (
        "Tyrosine-protein kinase ABL1",
        "https://www.ebi.ac.uk/chembl/explore/target/CHEMBL1862",
    )
    key, art = OpenFdaSource().artifact(o_res)
    assert key == "drug_intel" and art["safety"][0]["ingredient"] == "imatinib"
    notes = ChemblSource().scope_notes(ctx, c_res)
    assert any("no ChEMBL IC50/Ki/Kd values were retrieved" in n for n in notes)


def test_target_potency_detail():
    ctx, c_res, _, _ = run(_run_sources("imatinib potency (IC50, Kd) against ABL1", target="ABL1"))
    assert ChemblSource().describe(c_res, ctx) == (
        "Resolved imatinib to CHEMBL941 (max phase 4)",
        "4 mechanisms · 10 IC50/Ki/Kd records vs ABL1",
    )


def test_openfda_skips_drugs_that_are_not_approved():
    ctx, c_res, o_res, up = run(_run_sources("tivantinib safety"))
    assert o_res.success and o_res.data["safety"] == []
    assert o_res.data["skipped"][0]["drug"] == "TIVANTINIB"
    assert not [r for r in up.requests if r.url.host == "api.fda.gov"]
    notes = OpenFdaSource().scope_notes(ctx, o_res)
    assert notes and "max phase 3" in notes[0] and "not queried" in notes[0]
    assert OpenFdaSource().describe(o_res, ctx)[0] == "Skipped the FDA lookup for TIVANTINIB"


# -----------------------------------------------------------------------------
# (e) Orchestrator routing with fixture-backed fake tools
# -----------------------------------------------------------------------------

def _fake_drug_tools() -> Dict[str, Any]:
    def fake(method: str):
        def call(kwargs: Dict[str, Any]) -> ToolResult:
            engine, _ = make_engine()
            return asyncio.run(getattr(engine, method)(**kwargs))

        return call

    return {
        "chembl_resolve_drugs": (ChemblResolveInput, fake("resolve_drugs")),
        "chembl_drug_profile": (ChemblProfileInput, fake("drug_profile")),
        "openfda_drug_safety": (OpenFdaSafetyInput, fake("drug_safety")),
    }


async def _inquire(monkeypatch, tmp_path, query: str):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=_fake_drug_tools())
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(query, thread_id="t-drug", enable_web_search=False, event_callback=cb)
    steps = {e["data"]["id"]: e["data"] for e in events if e["type"] == "thought"}
    return res, calls, steps


@pytest.mark.asyncio
async def test_orchestrator_adverse_events_runs_both(monkeypatch, tmp_path):
    res, calls, steps = await _inquire(monkeypatch, tmp_path, "What are the adverse events of imatinib?")
    assert len(called(calls, "chembl_resolve_drugs")) == 1
    assert called(calls, "chembl_resolve_drugs")[0]["names"] == ["imatinib"]
    assert len(called(calls, "chembl_drug_profile")) == 1
    assert called(calls, "openfda_drug_safety")[0]["ingredient"] == "imatinib"
    assert steps["chembl"]["title"] == "Resolved imatinib to CHEMBL941 (max phase 4)"
    assert steps["chembl"]["agent"] == "Synapta"
    assert steps["openfda"]["title"] == "FAERS: 46,623 reports mention imatinib"
    assert steps["openfda"]["agent"] == "Lithoscribe"
    art = res.execution_artifacts["drug_intel"]
    assert art["chembl"]["drugs"][0]["chembl_id"] == "CHEMBL941"
    assert art["safety"][0]["total_reports"] == 46623
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Drugs: imatinib" in plan["text"] and "Safety: FAERS + label" in plan["text"]
    assert "#### Drug Pharmacology" in res.response and "#### FDA Adverse-Event Reports" in res.response


@pytest.mark.asyncio
async def test_orchestrator_target_drug_question_runs_chembl_only(monkeypatch, tmp_path):
    res, calls, steps = await _inquire(monkeypatch, tmp_path, "Which approved drugs targeting EGFR exist?")
    resolve = called(calls, "chembl_resolve_drugs")
    assert resolve and resolve[0]["target_symbol"] == "EGFR" and resolve[0]["names"] == []
    assert not called(calls, "openfda_drug_safety")
    assert steps["chembl"]["title"].startswith("Found ") and "approved drugs acting on EGFR (CHEMBL203)" in steps["chembl"]["title"]
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Drug lookup: approved drugs for EGFR" in plan["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["KRAS signaling in pancreatic cancer", "Simulate BIOMD0000000010 for 120 seconds"])
async def test_orchestrator_non_drug_questions_run_neither(monkeypatch, tmp_path, query):
    res, calls, steps = await _inquire(monkeypatch, tmp_path, query)
    assert not called(calls, "chembl_resolve_drugs")
    assert not called(calls, "openfda_drug_safety")
    assert "chembl" not in steps and "openfda" not in steps
    assert "drug_intel" not in res.execution_artifacts


@pytest.mark.asyncio
async def test_sources_skipped_without_tools(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry("adverse events of imatinib", thread_id="t-none", enable_web_search=False)
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert {"name": "chembl", "run": False, "reason": "tool not available"} in plan["extra"]["sources"]
    assert "drug_intel" not in res.execution_artifacts


# -----------------------------------------------------------------------------
# (f) Evidence XML and prompt
# -----------------------------------------------------------------------------

def test_evidence_block_renders_drug_sections():
    _, c_res, o_res, _ = run(_run_sources("imatinib potency against ABL1", target="ABL1"))
    block = build_evidence_block({"chembl": c_res, "openfda": o_res}, "")
    assert '<drug_intelligence source="ChEMBL REST ChEMBL_37">' in block
    assert '<drug chembl_id="CHEMBL941" pref_name="IMATINIB"' in block
    assert '<mechanism target_chembl_id="CHEMBL1862"' in block and 'form_chembl_id="CHEMBL1642"' in block
    assert 'type="Kd" relation="=" value="1" units="nM" pchembl="9"' in block
    assert '<drug_safety source="openFDA" last_updated="2026-07-30">' in block
    assert f"<disclaimer>{fx('faers_counts_imatinib')['meta']['disclaimer']}</disclaimer>" in block
    assert '<faers drug="imatinib"' in block and 'total_reports="46623"' in block
    assert '<reaction term="NAUSEA" count="2570"/>' in block
    assert 'reports_listing_death="10162"' in block
    assert '<reaction term="DEATH"' not in block
    assert '<label set_id="211ef2da-2868-4a77-8055-1cb2cd78e24b" effective_time="20260713" brand="Gleevec" application_number="NDA021588"' in block
    assert '<boxed_warning present="false"/>' in block
    assert block.index("<drug_intelligence") < block.index("<drug_safety") < block.index("</evidence>")


def test_evidence_xml_escapes_label_text_and_omits_failures():
    engine, _ = make_engine(Upstream(overrides={'activesubstancename:"warfarin"': (404, "openfda_404_not_found")}))
    safety = run(engine.drug_safety("warfarin"))
    xml = OpenFdaSource().evidence_xml(ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [safety.data]}))
    assert "FD&amp;C Yellow" in xml and "FD&C" not in xml
    assert '<boxed_warning truncated="false">WARNING: BLEEDING RISK' in xml
    assert 'brand="unknown (not harmonized by openFDA)"' in xml

    failed = ToolResult(success=False, tool_name="openfda_drug_safety", error="openFDA HTTP 500", error_type=ErrorType.UPSTREAM_API_ERROR)
    block = build_evidence_block({"openfda": failed}, "")
    assert "<drug_safety" not in block
    assert "openfda" in build_tool_status_block({"openfda": failed}, [])


def test_prompt_rules_registered_with_tools():
    from phronesis.extensions import drugs as drugs_ext

    with hooks.isolated_hooks():
        drugs_ext.register_prompt_hooks()
        prompt = get_synthesis_system_prompt()
    assert "- Synapta (ChEMBL):" in prompt and "- Lithoscribe (openFDA):" in prompt
    assert "FAERS counts are spontaneous reports" in prompt
    assert "Do not call a drug \"FDA-approved\"" in prompt


# -----------------------------------------------------------------------------
# (g) Verifier checks through the foundation
# -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def drug_results() -> Dict[str, ToolResult]:
    _, c_res, o_res, _ = asyncio.run(_run_sources("imatinib potency and adverse events against ABL1", target="ABL1"))
    return {"chembl": c_res, "openfda": o_res}


def _defects(drug_results, response: str, category: Optional[str] = None, **kwargs):
    load_extensions()
    report = ClinicalVerifier().verify_response("imatinib potency and FAERS adverse events", response, drug_results, **kwargs)
    return report, [d for d in report.defects if category is None or d.category == category]


@pytest.mark.parametrize(
    "sentence",
    [
        "Imatinib binds ABL1 with a Kd of 1.1 nM in assay CHEMBL1908688.",
        "Imatinib inhibits recombinant c-Abl with a Ki of 0.013 µM in assay CHEMBL1908688.",
        "The top Kd record has a pChEMBL value of 8.96 in assay CHEMBL1908688.",
    ],
)
def test_pharmacology_values_that_match_pass(drug_results, sentence):
    _, numerical = _defects(drug_results, sentence, "NUMERICAL")
    assert numerical == []


def test_pharmacology_value_mismatch_is_critical(drug_results):
    _, numerical = _defects(drug_results, "Imatinib binds ABL1 with a Kd of 1.3 nM in assay CHEMBL1908688.", "NUMERICAL")
    assert numerical and numerical[0].severity.value == "CRITICAL"
    _, numerical = _defects(drug_results, "Its pChEMBL value is 7.5 for ABL1.", "NUMERICAL")
    assert numerical and numerical[0].severity.value == "CRITICAL"


def test_faers_counts(drug_results):
    _, ok = _defects(drug_results, "FAERS lists 10162 reports of death and 46,623 reports in total that mention imatinib.", "NUMERICAL")
    assert ok == []
    _, bad = _defects(drug_results, "FAERS lists 10,200 reports of death mentioning imatinib.", "NUMERICAL")
    assert bad and bad[0].severity.value == "CRITICAL"
    _, pct = _defects(drug_results, "About 22% of FAERS reports for imatinib mention death.", "NUMERICAL")
    assert pct and pct[0].severity.value == "MAJOR"


def test_faers_causation_language(drug_results):
    _, bad = _defects(drug_results, "According to FAERS, imatinib causes nausea.", "CLINICAL_CLAIM")
    assert any(d.severity.value == "MAJOR" and "causation" in d.description for d in bad)
    _, ok = _defects(drug_results, "FAERS reports for imatinib do not establish causation.", "CLINICAL_CLAIM")
    assert not [d for d in ok if "causation" in d.description]


def test_chembl_identifier_family(drug_results):
    report, ids = _defects(drug_results, "Imatinib (CHEMBL941) is recorded on its salt CHEMBL1642.", "IDENTIFIER")
    assert ids == [] and "CHEMBL:CHEMBL941" in report.verified_identifiers
    _, ids = _defects(drug_results, "Aspirin is CHEMBL25.", "IDENTIFIER")
    assert ids and ids[0].severity.value == "CRITICAL" and ids[0].claim == "CHEMBL25"
    _, ids = _defects(drug_results, "Aspirin is CHEMBL25.", "IDENTIFIER", historical_identifiers={"chembl": ["CHEMBL25"]})
    assert ids == []


def test_checks_are_registered_and_quiet_without_drug_sentences(drug_results):
    load_extensions()
    keys = [k for k, _ in hooks.get_verification_checks()]
    assert "drug_pharmacology" in keys and "drug_faers" in keys
    assert checks.check_faers(type("V", (), {"sentences": ["KRAS signaling drives growth."], "tool_results": {}, "evidence_documents": [], "hedge_pattern": None})()) == []


def test_fda_application_and_dailymed_set_id_families(drug_results):
    # label_exact_imatinib.json: openfda.application_number NDA021588, set_id 211ef2da-2868-4a77-8055-1cb2cd78e24b.
    allowed = OpenFdaSource().allowed_identifiers(drug_results["openfda"])
    assert allowed == {"fda_application": {"NDA021588"}, "dailymed_setid": {"211ef2da-2868-4a77-8055-1cb2cd78e24b"}}
    good = (
        "The openFDA label for Gleevec (imatinib) lists application number NDA 021588 "
        "(DailyMed set id 211ef2da-2868-4a77-8055-1cb2cd78e24b)."
    )
    report, ids = _defects(drug_results, good, "IDENTIFIER")
    assert ids == [] and "FDA_APPLICATION:NDA021588" in report.verified_identifiers
    assert "DAILYMED_SETID:211ef2da-2868-4a77-8055-1cb2cd78e24b" in report.verified_identifiers

    _, ids = _defects(drug_results, "The openFDA label for Gleevec (imatinib) lists application number NDA099999.", "IDENTIFIER")
    assert ids and ids[0].severity.value == "CRITICAL" and ids[0].claim == "NDA099999"
    made_up = "The DailyMed set id for the imatinib label is 00000000-1111-2222-3333-444444444444."
    _, ids = _defects(drug_results, made_up, "IDENTIFIER")
    assert ids and ids[0].severity.value == "CRITICAL" and ids[0].claim == "00000000-1111-2222-3333-444444444444"


def test_label_lookup_failure_is_not_reported_as_no_label():
    up = Upstream({"openfda.generic_name": (500, "openfda_500_parse_exception")})
    ctx, _, o_res, _ = run(_run_sources("What are the adverse events of imatinib?", upstream=up))
    entry = o_res.data["safety"][0]
    assert entry["label"] is None and entry["label_error"]
    notes = OpenFdaSource().scope_notes(ctx, o_res)
    assert any("FDA label lookup for imatinib failed" in n for n in notes)
    assert not any(n.startswith("No FDA label was found") for n in notes)
    _, detail = OpenFdaSource().describe(o_res, ctx)
    assert "FDA label lookup failed" in detail and "no FDA label found" not in detail


# -----------------------------------------------------------------------------
# Review regressions
# -----------------------------------------------------------------------------

def test_label_prefers_single_ingredient_product_over_combination():
    # Live: the generic_name phrase search ranks ZITUVIMET (sitagliptin and metformin) first for "metformin".
    engine, up = make_engine()
    res = run(engine.drug_safety("metformin"))
    label = res.data["label"]
    assert (label["brand_name"], label["application_number"]) == ("Riomet", "NDA021591")
    assert label["generic_name"] == "METFORMIN HYDROCHLORIDE" and label["combination_product"] is False
    raw = " ".join(fx("label_exact_metformin")["results"][0]["boxed_warning"][0].split())
    assert label["boxed_warning"] == raw[: len(label["boxed_warning"])]
    assert res.data["label_search"].startswith('openfda.generic_name.exact:"METFORMIN HYDROCHLORIDE"')
    searches = [r.url.params["search"] for r in _requests_to(up, "/drug/label.json") if "count" not in r.url.params]
    assert searches == [res.data["label_search"]]


def test_imatinib_label_found_by_exact_generic_name():
    engine, _ = make_engine()
    data = run(engine.drug_safety("imatinib")).data
    assert data["label_search"].startswith('openfda.generic_name.exact:"IMATINIB MESYLATE"')
    assert data["label"]["combination_product"] is False


def test_combination_only_label_is_flagged():
    engine, _ = make_engine()
    res = run(engine.drug_safety("aspirin"))
    label = res.data["label"]
    assert label["brand_name"] == "Excedrin Migraine"
    assert label["generic_name"] == "ACETAMINOPHEN, ASPIRIN, AND CAFFEINE" and label["combination_product"] is True
    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [res.data], "skipped": [], "errors": []})
    notes = OpenFdaSource().scope_notes(_ctx("aspirin safety"), wrapped)
    assert any("combination product" in n and "do not attribute" in n for n in notes)
    assert 'combination_product="true"' in OpenFdaSource().evidence_xml(wrapped)


def test_numbers_render_exactly():
    from phronesis.drugintel.render import fmt_num

    assert [fmt_num(v) for v in (38.0, 1.1, 31622.78, 1234567.0, 1e-05, None)] == ["38", "1.1", "31622.78", "1234567", "0.00001", ""]


def test_approved_total_counts_distinct_parents_when_all_rows_fetched():
    engine, up = make_engine()
    res = run(engine.drug_profile(target_chembl_id="CHEMBL203", target_symbol="EGFR"))
    parents = {m["parent_molecule_chembl_id"] for m in fx("mechanism_target_egfr_approved")["mechanisms"]}
    assert res.data["target_approved_total"] == len(parents)
    assert _requests_to(up, "/mechanism.json")[0].url.params["limit"] == "500"


def _vctx(sentences: List[str], tool_results: Dict[str, Any], passages: List[str], response: str = "") -> Any:
    from phronesis.phronesis.agents.verifier import _HEDGE_PATTERN

    return type("V", (), {
        "sentences": sentences,
        "response": response or "\n".join(sentences),
        "tool_results": tool_results,
        "evidence_documents": [p.lower() for p in passages],
        "evidence_keys": [f"passage:{i}" for i in range(len(passages))],
        "hedge_pattern": _HEDGE_PATTERN,
    })()


def test_unrelated_passage_number_does_not_validate_a_potency_value():
    sentence = "Imatinib inhibits ABL1 with an IC50 of 38 nM."
    unrelated = "The cohort enrolled 38 patients with chronic-phase CML."
    assert checks.check_pharmacology(_vctx([sentence], {}, [unrelated]))
    assert checks.check_pharmacology(_vctx([sentence], {}, ["Imatinib inhibited ABL1 kinase activity (IC50 = 38 nM)."])) == []


def test_meddra_term_is_not_a_causal_claim():
    assert not checks.CAUSAL_RE.search(checks._without_terms("FAERS lists DRUG-INDUCED LIVER INJURY reports.", {"DRUG-INDUCED LIVER INJURY"}))
    assert checks.CAUSAL_RE.search(checks._without_terms("FAERS shows the drug induced liver injury.", {"DRUG-INDUCED LIVER INJURY"}))


def test_faers_counts_in_tables_are_checked(drug_results):
    ok = "| Reaction (FAERS) | Reports |\n|---|---|\n| DEATH | 10,162 |\n| NAUSEA | 2570 |"
    assert checks.check_faers(_vctx(ok.splitlines(), drug_results, [], ok)) == []
    bad = "| Rank | Reaction | Reports |\n|---|---|---|\n| 1 | DEATH | 10,200 |\n| 2 | NAUSEA | 12% |"
    found = checks.check_faers(_vctx(bad.splitlines(), drug_results, [], bad))
    assert [(d["severity"], d["description"].split(" ")[0]) for d in found] == [("CRITICAL", "FAERS"), ("MAJOR", "Percentage")]
    unrelated = "| Study | Patients |\n|---|---|\n| IRIS | 553 |"
    assert checks.check_faers(_vctx(unrelated.splitlines(), drug_results, [], unrelated)) == []


def test_faers_count_from_a_literature_passage_is_accepted():
    sentence = "A published FAERS analysis identified 1,234 reports of QT prolongation."
    assert checks.check_faers(_vctx([sentence], {}, ["Disproportionality analysis of FAERS: 1,234 reports of QT prolongation."])) == []
    assert checks.check_faers(_vctx([sentence], {}, []))
