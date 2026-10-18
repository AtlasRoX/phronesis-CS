"""
Query-run repairs for the KRAS repurposing + real-world-safety question, fully offline:

  A. A failed ChEMBL approved-drugs-for-target lookup (timeout / HTTP error) is reported as unknown in the step,
     the evidence XML and the artifact, never as "0 approved drugs"; the client retries once with backoff; the
     KRAS (CHEMBL2189121) mechanism query lists sotorasib and adagrasib.
  B. A safety question that names no drug queries openFDA for up to three candidate drugs taken from the ChEMBL
     result (else PrimeKG's approved repurposing candidates), through SourceRunState.source_result, and says that
     FAERS is spontaneous reporting, not EHR data. With no candidate the skip reason says so.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

import phronesis.sources as sources_pkg
from phronesis.agents.query_context import QueryContext, SourceDecision
from phronesis.drugintel import chembl_client as chembl_mod
from phronesis.drugintel.chembl_client import ChemblClient, ChemblError, clear_release_cache
from phronesis.drugintel.engine import DrugIntelEngine
from phronesis.drugintel.openfda_client import OpenFdaClient
from phronesis.drugintel.render import bioactive_summary, chembl_evidence_xml, safety_evidence_xml
from phronesis.drugintel.sources import (
    MAX_SAFETY_CANDIDATES,
    NO_CANDIDATE_REASON,
    ChemblSource,
    OpenFdaSource,
)
from phronesis.sources import EvidenceSource, SourceRunState
from phronesis.sources.base import share_source_run
from phronesis.tools.registry import ToolResult
from phronesis.utils.http_limits import TTLCache
from tests.fakes import DrugToolRegistry

QUESTION = (
    "Design a step-by-step pipeline to identify FDA-approved drugs that could be repurposed for treating "
    "KRAS-mutant pancreatic cancer, incorporating clinical-trial status, in-silico binding predictions, and "
    "real-world EHR safety data."
)
KRAS = "CHEMBL2189121"
SOTORASIB, ADAGRASIB = "CHEMBL4535757", "CHEMBL4594350"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_and_fresh(monkeypatch):
    monkeypatch.setattr(chembl_mod, "RETRY_BACKOFF_S", 0.0)
    clear_release_cache()
    yield
    clear_release_cache()


# -----------------------------------------------------------------------------
# Fake ChEMBL / openFDA
# -----------------------------------------------------------------------------

MOLECULES = {
    SOTORASIB: {"molecule_chembl_id": SOTORASIB, "pref_name": "SOTORASIB", "max_phase": "4.0",
                "molecule_type": "Small molecule", "first_approval": 2021},
    ADAGRASIB: {"molecule_chembl_id": ADAGRASIB, "pref_name": "ADAGRASIB", "max_phase": "4.0",
                "molecule_type": "Small molecule", "first_approval": 2022},
}
# What the live API returns for target_chembl_id=CHEMBL2189121&max_phase=4 (checked 2026-10-16).
KRAS_APPROVED_MECHANISMS = [
    {"molecule_chembl_id": SOTORASIB, "parent_molecule_chembl_id": SOTORASIB, "mechanism_of_action": "GTPase KRas inhibitor",
     "action_type": "INHIBITOR", "target_chembl_id": KRAS, "max_phase": 4},
    {"molecule_chembl_id": ADAGRASIB, "parent_molecule_chembl_id": ADAGRASIB, "mechanism_of_action": "GTPase KRas inhibitor",
     "action_type": "INHIBITOR", "target_chembl_id": KRAS, "max_phase": 4},
]


class Upstream:
    """ChEMBL + openFDA fake. `approved` decides what the approved-mechanisms request does: "ok", "timeout",
    "500", or a list of outcomes consumed one per request ("timeout", "500", "ok")."""

    def __init__(self, approved: Any = "ok", faers_total: int = 1200, target: str = "ok") -> None:
        self.approved = approved
        self.target = target
        self.faers_total = faers_total
        self.requests: List[httpx.Request] = []

    def approved_requests(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/mechanism.json") and r.url.params.get("max_phase") == "4"]

    def openfda_ingredients(self) -> List[str]:
        return sorted({r.url.params.get("search", "") for r in self.requests
                       if r.url.host == "api.fda.gov" and r.url.params.get("count") == "serious"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.fda.gov":
            return self._openfda(request.url.params)
        return self._chembl(request, request.url.path, request.url.params)

    def _approved(self, request: httpx.Request) -> httpx.Response:
        outcome = self.approved.pop(0) if isinstance(self.approved, list) else self.approved
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if outcome == "500":
            return httpx.Response(500, text="<html>Error: 500</html>")
        return httpx.Response(200, json={"mechanisms": KRAS_APPROVED_MECHANISMS, "page_meta": {"total_count": 2}})

    def _chembl(self, request: httpx.Request, path: str, q: httpx.QueryParams) -> httpx.Response:
        if path.endswith("/status.json"):
            return httpx.Response(200, json={"chembl_db_version": "ChEMBL_36"})
        if path.endswith("/mechanism.json"):
            if q.get("max_phase") == "4":
                assert q.get("target_chembl_id") == KRAS
                return self._approved(request)
            return httpx.Response(200, json={"mechanisms": []})
        if path.endswith("/molecule.json"):
            ids = (q.get("molecule_chembl_id__in") or "").split(",")
            return httpx.Response(200, json={"molecules": [MOLECULES[i] for i in ids if i in MOLECULES]})
        if path.endswith("/target.json"):
            if "target_chembl_id__in" in q:
                return httpx.Response(200, json={"targets": [{"target_chembl_id": KRAS, "pref_name": "GTPase KRas"}]})
            if self.target == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            sym = q.get("target_components__target_component_synonyms__component_synonym__iexact")
            return httpx.Response(200, json={"targets": [{"target_chembl_id": KRAS, "pref_name": "GTPase KRas"}] if sym == "KRAS" else []})
        if path.endswith("/activity.json"):
            return httpx.Response(200, json={"activities": [], "page_meta": {"total_count": 0}})
        raise AssertionError(f"unexpected ChEMBL request {path}?{q}")

    def _openfda(self, q: httpx.QueryParams) -> httpx.Response:
        meta = {"disclaimer": "Do not rely on openFDA to make decisions regarding medical care.", "last_updated": "2026-07-30"}
        if q.get("count") == "patient.reaction.reactionmeddrapt.exact":
            return httpx.Response(200, json={"meta": meta, "results": [{"term": "DIARRHOEA", "count": 210}]})
        if q.get("count") == "serious":
            return httpx.Response(200, json={"meta": meta, "results": [{"term": "1", "count": 800}]})
        return httpx.Response(200, json={"meta": {**meta, "results": {"total": self.faers_total}}, "results": [{}]})


def make_engine(upstream: Upstream) -> DrugIntelEngine:
    transport = httpx.MockTransport(upstream)
    return DrugIntelEngine(ChemblClient(transport=transport), OpenFdaClient(transport=transport, api_key="", cache=TTLCache()))




def kras_ctx(query: str = QUESTION, **decisions: bool) -> QueryContext:
    ctx = QueryContext(raw_query=query, clean_query=query, primary_target="KRAS")
    for key, should in decisions.items():
        ctx.decisions[key] = SourceDecision(run=should, reason="test")
    return ctx


class FakePrimeKG(EvidenceSource):
    key = "primekg"
    agent = "Synapta"
    order = 16

    def __init__(self, candidates: List[Dict[str, Any]], delay: float = 0.0) -> None:
        self.candidates = candidates
        self.delay = delay
        self.runs = 0

    async def run(self, ctx, registry, state):
        self.runs += 1
        await asyncio.sleep(self.delay)
        return ToolResult(success=True, tool_name="primekg_investigate", data={"analyses": [{
            "operation": "repurposing", "reason": "repurposing for pancreatic cancer",
            "result": {"disease": {"name": "pancreatic cancer"}, "candidates": self.candidates}}]})


def _install(monkeypatch, *srcs: EvidenceSource) -> None:
    """Register sources for this test only (source_result looks dependencies up in the registry)."""
    for src in srcs:
        share_source_run(src)
        monkeypatch.setitem(sources_pkg._SOURCES, src.key, src)


async def _run_stage1(ctx: QueryContext, registry: Any, srcs: List[EvidenceSource]) -> List[ToolResult]:
    """What the orchestrator does: every active source's run() concurrently with one SourceRunState."""
    state = SourceRunState(registry, ctx)
    return list(await asyncio.gather(*(s.run(ctx, registry, state) for s in srcs)))


# -----------------------------------------------------------------------------
# A. ChEMBL client robustness
# -----------------------------------------------------------------------------

def test_client_retries_once_on_read_timeout_then_succeeds():
    up = Upstream(approved=["timeout", "ok"])
    client = ChemblClient(transport=httpx.MockTransport(up))
    rows, total = run(client.approved_mechanisms_for_target(KRAS))
    assert {r["parent_molecule_chembl_id"] for r in rows} == {SOTORASIB, ADAGRASIB}
    assert total == 2
    assert len(up.approved_requests()) == 2


def test_client_retries_once_on_5xx_then_succeeds():
    up = Upstream(approved=["500", "ok"])
    rows, _ = run(ChemblClient(transport=httpx.MockTransport(up)).approved_mechanisms_for_target(KRAS))
    assert len(rows) == 2 and len(up.approved_requests()) == 2


@pytest.mark.parametrize("outcome,kind", [("timeout", "timeout"), ("500", "http")])
def test_client_raises_typed_error_after_one_retry(outcome, kind):
    up = Upstream(approved=outcome)
    with pytest.raises(ChemblError) as err:
        run(ChemblClient(transport=httpx.MockTransport(up)).approved_mechanisms_for_target(KRAS))
    assert err.value.kind == kind
    assert len(up.approved_requests()) == 2  # one retry, not more
    if outcome == "timeout":
        assert "timed out" in str(err.value) and "ReadTimeout" in str(err.value)


def test_client_timeout_is_reasonable():
    client = ChemblClient(transport=httpx.MockTransport(Upstream()))
    timeout = client._client().timeout
    assert 8.0 <= timeout.read <= 15.0
    assert timeout.connect is not None and timeout.connect <= 5.0


def test_kras_mechanism_query_lists_sotorasib_and_adagrasib():
    up = Upstream()
    res = run(_chembl_source_run(kras_ctx(chembl=True), up))
    assert res.success
    data = res.data
    assert data["target_approved_status"] == "ok"
    assert [a["pref_name"] for a in data["target_approved_drugs"]] == ["SOTORASIB", "ADAGRASIB"]
    req = up.approved_requests()[0]
    assert req.url.params["target_chembl_id"] == KRAS and req.url.params["max_phase"] == "4"
    title, detail = ChemblSource().describe(res, kras_ctx())
    assert title.startswith("Found 2 approved drugs acting on KRAS (CHEMBL2189121)")
    assert "SOTORASIB" in detail and "ADAGRASIB" in detail


async def _chembl_source_run(ctx: QueryContext, up: Upstream) -> ToolResult:
    reg = DrugToolRegistry(make_engine(up))
    return await ChemblSource().run(ctx, reg, SourceRunState(reg, ctx))


# -----------------------------------------------------------------------------
# A. a failed approved-drugs lookup is unknown, never 0
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("outcome,phrase", [("timeout", "timed out"), ("500", "failed")])
def test_failed_approved_lookup_is_reported_as_unknown(outcome, phrase):
    ctx = kras_ctx(chembl=True)
    res = run(_chembl_source_run(ctx, Upstream(approved=outcome)))
    assert res.success  # the profile itself still answers (target, compounds)
    data = res.data
    assert data["target_approved_status"] == "failed"
    assert data["target_approved_drugs"] == []

    src = ChemblSource()
    title, _ = src.describe(res, ctx)
    assert title.startswith(f"ChEMBL lookup for KRAS {phrase}") and title.endswith("; approved drugs unknown")
    if outcome == "timeout":
        assert title == "ChEMBL lookup for KRAS timed out; approved drugs unknown"
    assert "Found 0" not in title and "0 approved" not in title

    xml = src.evidence_xml(res)
    assert '<approved_drugs_for_target target_chembl_id="CHEMBL2189121" status="failed"' in xml
    assert "Approved drugs: unknown" in xml
    assert "0 approved" not in xml

    summary = bioactive_summary(data)
    assert "unknown" in summary and not summary.startswith("0 approved")

    key, art = src.artifact(res)
    assert key == "drug_intel"
    assert art["chembl"]["target_approved_status"] == "failed"
    assert art["chembl"]["target_approved_error"]

    notes = " ".join(src.scope_notes(ctx, res))
    assert "unknown" in notes and "never report them as 0" in notes
    assert "0 approved" not in notes
    assert "approved drugs acting on KRAS" not in src.fallback_markdown(res) or "unknown" in src.fallback_markdown(res)


def test_failed_target_lookup_is_reported_as_unknown_not_no_records(monkeypatch):
    up = Upstream(target="timeout")
    reg = DrugToolRegistry(make_engine(up))
    chembl, openfda = ChemblSource(), OpenFdaSource()
    _install(monkeypatch, chembl, openfda)
    ctx = kras_ctx(chembl=True, primekg=False)
    chembl_res, fda_res = run(_run_stage1(ctx, reg, [chembl, openfda]))

    title, detail = chembl.describe(chembl_res, ctx)
    assert title == "ChEMBL target lookup for KRAS timed out; approved drugs unknown"
    assert "ReadTimeout" in detail
    notes = " ".join(chembl.scope_notes(ctx, chembl_res))
    assert "timed out" in notes and "unknown (not 0)" in notes
    f_title, f_detail = openfda.describe(fda_res, ctx)
    assert f_title == f"Skipped the FDA lookup: {NO_CANDIDATE_REASON}"
    assert f_detail == "the ChEMBL target lookup for KRAS timed out"


def test_successful_empty_approved_lookup_still_says_zero():
    data = {"target": {"target_chembl_id": "CHEMBL1250348", "symbol_query": "MYC"}, "target_approved_drugs": [],
            "target_approved_total": 0, "target_approved_status": "ok", "target_activity_records_fetched": None}
    assert bioactive_summary(data).startswith("0 approved drugs act on MYC")
    assert 'status="failed"' not in chembl_evidence_xml(data)


# -----------------------------------------------------------------------------
# B. routing: openFDA for a safety question that names no drug
# -----------------------------------------------------------------------------

def test_openfda_routes_on_safety_intent_with_a_candidate_feeder():
    d = OpenFdaSource().should_run(kras_ctx(chembl=True, primekg=False))
    assert d.run and "candidate" in d.reason and "ChEMBL" in d.reason


def test_openfda_skip_reason_when_no_candidate_source_runs():
    d = OpenFdaSource().should_run(kras_ctx(chembl=False, primekg=False))
    assert not d.run
    assert d.reason.startswith(NO_CANDIDATE_REASON)
    assert d.reason != "no drug named"


def test_openfda_still_skips_a_non_safety_question_naming_no_drug():
    d = OpenFdaSource().should_run(kras_ctx("Which approved drugs target KRAS?", chembl=True))
    assert not d.run and d.reason == "no drug named"


# -----------------------------------------------------------------------------
# B. candidates from ChEMBL, sharing the ChEMBL source's single run
# -----------------------------------------------------------------------------

def test_openfda_uses_chembl_approved_drugs_as_candidates(monkeypatch):
    up = Upstream()
    reg = DrugToolRegistry(make_engine(up))
    chembl, openfda = ChemblSource(), OpenFdaSource()
    _install(monkeypatch, chembl, openfda)
    ctx = kras_ctx(chembl=True, primekg=False, openfda=True)
    chembl_res, fda_res = run(_run_stage1(ctx, reg, [chembl, openfda]))

    assert chembl_res.success and fda_res.success
    # The ChEMBL source ran once although openFDA depended on it.
    assert reg.calls.count("chembl_drug_profile") == 1 and len(up.approved_requests()) == 1
    data = fda_res.data
    sel = data["candidate_selection"]
    assert sel["status"] == "selected" and sel["source"] == "ChEMBL"
    assert [d["drug"] for d in sel["drugs"]] == ["sotorasib", "adagrasib"]
    assert [e["ingredient"] for e in data["safety"]] == ["sotorasib", "adagrasib"]
    assert all(e["candidate"] for e in data["safety"])
    assert {e["chembl_id"] for e in data["safety"]} == {SOTORASIB, ADAGRASIB}

    xml = openfda.evidence_xml(fda_res)
    assert "these candidates' FAERS reports" in xml
    assert "spontaneous adverse-event reporting system, not EHR" in xml
    assert "No EHR source is available" in xml
    assert '<ehr_data available="false">' in xml
    assert 'candidate="true"' in xml

    title, detail = openfda.describe(fda_res, ctx)
    assert title.startswith("Candidate drugs' FAERS: 1,200 reports mention sotorasib")
    assert "not EHR data" in detail
    notes = " ".join(openfda.scope_notes(ctx, fda_res))
    assert "candidates' FAERS reports" in notes and "EHR safety data was not retrieved" in notes
    key, art = openfda.artifact(fda_res)
    assert key == "drug_intel" and art["safety_candidates"]["source"] == "ChEMBL" and art["safety_ehr_note"]


def test_openfda_caps_candidates_at_three(monkeypatch):
    extra = [{"chembl_id": f"CHEMBL90{i}", "pref_name": f"DRUGNAME{i}", "mechanism_of_action": "x"} for i in range(5)]

    class BigChembl(EvidenceSource):
        key, order = "chembl", 20

        async def run(self, ctx, registry, state):
            return ToolResult(success=True, tool_name="chembl_drug_profile", data={
                "target": {"target_chembl_id": KRAS, "symbol_query": "KRAS"}, "target_approved_drugs": extra,
                "target_approved_status": "ok"})

    up = Upstream()
    reg = DrugToolRegistry(make_engine(up))
    openfda = OpenFdaSource()
    _install(monkeypatch, BigChembl(), openfda)
    ctx = kras_ctx(chembl=True, primekg=False)
    res = run(openfda.run(ctx, reg, SourceRunState(reg, ctx)))
    assert len(res.data["safety"]) == MAX_SAFETY_CANDIDATES == 3
    assert reg.calls.count("openfda_drug_safety") == 3


# -----------------------------------------------------------------------------
# B. ChEMBL failed -> PrimeKG candidates; nothing -> explicit skip
# -----------------------------------------------------------------------------

PRIMEKG_CANDIDATES = [
    {"name": "Tool compound X", "id": "DB0001", "score": 2.9, "approval_groups": ["investigational"]},
    {"name": "Trametinib", "id": "DB08911", "score": 2.5, "approval_groups": ["approved"]},
    {"name": "Selumetinib", "id": "DB11689", "score": 2.1, "approval_groups": ["approved", "investigational"]},
    {"name": "Binimetinib", "id": "DB11967", "score": 1.8, "approval_groups": ["approved"]},
    {"name": "Cobimetinib", "id": "DB05239", "score": 1.5, "approval_groups": ["approved"]},
]


def test_openfda_falls_back_to_primekg_when_chembl_approved_lookup_failed(monkeypatch):
    up = Upstream(approved="timeout")
    reg = DrugToolRegistry(make_engine(up))
    chembl, openfda, kg = ChemblSource(), OpenFdaSource(), FakePrimeKG(PRIMEKG_CANDIDATES, delay=0.01)
    _install(monkeypatch, kg, chembl, openfda)
    ctx = kras_ctx(chembl=True, primekg=True)
    kg_res, chembl_res, fda_res = run(_run_stage1(ctx, reg, [kg, chembl, openfda]))

    assert kg.runs == 1  # shared, not re-run for openFDA
    assert chembl_res.data["target_approved_status"] == "failed"
    sel = fda_res.data["candidate_selection"]
    assert sel["source"] == "PrimeKG"
    assert [d["drug"] for d in sel["drugs"]] == ["trametinib", "selumetinib", "binimetinib"]  # approved only, top 3
    assert "pancreatic cancer" in sel["basis"]
    xml = safety_evidence_xml(fda_res.data)
    assert 'source="PrimeKG"' in xml and "not EHR" in xml


def test_no_candidate_gives_explicit_skip(monkeypatch):
    up = Upstream(approved="500")
    reg = DrugToolRegistry(make_engine(up))
    chembl, openfda = ChemblSource(), OpenFdaSource()
    _install(monkeypatch, chembl, openfda)
    ctx = kras_ctx(chembl=True, primekg=False)
    _, fda_res = run(_run_stage1(ctx, reg, [chembl, openfda]))

    assert fda_res.success and fda_res.data["safety"] == []
    assert "openfda_drug_safety" not in reg.calls
    title, detail = openfda.describe(fda_res, ctx)
    assert title == f"Skipped the FDA lookup: {NO_CANDIDATE_REASON}"
    assert "approved drugs acting on KRAS failed" in detail
    xml = openfda.evidence_xml(fda_res)
    assert '<candidate_selection status="none">' in xml and "No EHR source is available" in xml
    notes = " ".join(openfda.scope_notes(ctx, fda_res))
    assert "no drug candidate was found" in notes


# -----------------------------------------------------------------------------
# Source framework: shared runs and dependency ordering
# -----------------------------------------------------------------------------

class _Counting(EvidenceSource):
    def __init__(self, key: str, order: int, body: Optional[Callable[..., Any]] = None) -> None:
        self.key, self.order, self.runs = key, order, 0
        self.body = body

    async def run(self, ctx, registry, state):
        self.runs += 1
        await asyncio.sleep(0.01)
        if self.body:
            return await self.body(ctx, registry, state)
        return ToolResult(success=True, tool_name=self.key, data={"n": self.runs})


def test_shared_source_run_runs_once_per_state(monkeypatch):
    src = _Counting("dep_a", 10)
    _install(monkeypatch, src)
    ctx = kras_ctx(dep_a=True)

    async def main():
        state = SourceRunState(None, ctx)
        a, b = await asyncio.gather(src.run(ctx, None, state), state.source_result("dep_a"))
        c = await src.run(ctx, None, SourceRunState(None, ctx))  # a new inquiry runs again
        return a, b, c

    a, b, c = run(main())
    assert a is b and src.runs == 2 and c.data == {"n": 2}


def test_source_result_refuses_dependencies_on_later_sources_and_inactive_ones(monkeypatch):
    early, late = _Counting("dep_early", 10), _Counting("dep_late", 30)
    _install(monkeypatch, early, late)
    ctx = kras_ctx(dep_early=True, dep_late=True)
    ctx.decisions["dep_off"] = SourceDecision(run=False, reason="test")

    async def main():
        state = SourceRunState(None, ctx)
        backwards = await state.source_result("dep_late", requester=early)
        off = await state.source_result("dep_off", requester=late)
        forwards = await state.source_result("dep_early", requester=late)
        return backwards, off, forwards

    backwards, off, forwards = run(main())
    assert backwards is None and off is None and late.runs == 0
    assert forwards is not None and forwards.success


def test_source_result_timeout_does_not_cancel_the_dependency(monkeypatch):
    async def slow(ctx, registry, state):
        await asyncio.sleep(0.2)
        return ToolResult(success=True, tool_name="slow", data={})

    src = _Counting("dep_slow", 10, body=slow)
    _install(monkeypatch, src)
    ctx = kras_ctx(dep_slow=True)

    async def main():
        state = SourceRunState(None, ctx)
        owner = asyncio.ensure_future(src.run(ctx, None, state))
        await asyncio.sleep(0)
        waited = await state.source_result("dep_slow", timeout_s=0.05)
        return waited, await owner

    waited, owned = run(main())
    assert waited is None and owned.success and src.runs == 1


def test_space_grouped_faers_count_is_accepted_only_when_it_matches():
    import re as _re

    from phronesis.drugintel import checks

    sentence = "Balsalazide: 1 454 total FAERS reports, 797 serious, 22 deaths (openFDA)."
    allowed = {1454, 797, 22}
    m = next(m for m in checks.COUNT_RE.finditer(sentence))
    lead = _re.search(r"(?<![\w.,])(\d{1,3}(?: \d{3})*) $", sentence[: m.start()])
    assert m.group(1) == "454" and lead and int(lead.group(1) + m.group(1)) in allowed
    assert checks._as_int("1\u2007454") == 1454  # figure space groups thousands too


def test_agent_calls_turn_off_nemotron_reasoning(monkeypatch):
    from phronesis.agentic.llm import agent_model_kwargs

    monkeypatch.delenv("PHRONESIS_AGENT_THINKING", raising=False)
    assert agent_model_kwargs("nvidia/nemotron-3-super-120b-a12b") == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }
    assert agent_model_kwargs("gpt-4o") == {}
    monkeypatch.setenv("PHRONESIS_AGENT_THINKING", "on")
    assert agent_model_kwargs("nvidia/nemotron-3-super-120b-a12b") == {}


def test_faers_table_does_not_read_an_approval_year_as_a_report_count():
    from types import SimpleNamespace

    from phronesis.drugintel import checks
    from phronesis.tools.registry import ToolResult

    tr = {"openfda": ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [
        {"ingredient": "olaparib", "total_reports": 21067, "serious_reports": 19165, "deaths_reported": 4736,
         "top_reactions": [{"term": "NAUSEA", "count": 1836}]}]})}
    table = ("| Drug | FDA approval (year) | FAERS total | Serious | Deaths |\n|---|---|---|---|---|\n"
             "| Olaparib | 2014 | 21067 | 19165 | 4736 |\n")
    vctx = SimpleNamespace(response=table, tool_results=tr, sentences=[], evidence_documents=[], hedge_pattern=None)
    defects = checks._check_faers_tables(vctx, checks.faers_counts(tr), [], [])
    assert not any("2014" in d["description"] for d in defects)
    bad = table.replace("| 21067 |", "| 99999 |")
    vctx.response = bad
    assert any("99999" in d["description"] for d in checks._check_faers_tables(vctx, checks.faers_counts(tr), [], []))
