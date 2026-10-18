"""
Query-run repairs for ChEMBL names and openFDA candidate safety, fully offline:

  1. ChEMBL molecule and target names survive a slow or failing batch request: per-id requests (molecule/{id}.json,
     target/{id}.json) fill in what the batch did not return, names are cached per id, and the target name from the
     resolve step is the profile's fallback. An approved drug whose name is still unknown is shown and described
     as "name not retrieved", never as its mechanism text.
  2. openFDA candidates for a safety question naming no drug: ChEMBL approved drugs win even when the profile lacks
     their names (looked up by id); PrimeKG candidates tagged supportive care / neuroendocrine-tumour agent are
     skipped and approved pathway drugs come first; FAERS is retried under the US name (mesalazine -> mesalamine)
     and on openfda.generic_name, and the name that matched is reported.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx
import pytest

import phronesis.sources as sources_pkg
from phronesis.agents.query_context import QueryContext, SourceDecision
from phronesis.drugintel import chembl_client as chembl_mod
from phronesis.drugintel.chembl_client import ChemblClient, ChemblError, clear_release_cache
from phronesis.drugintel.engine import DrugIntelEngine
from phronesis.drugintel.names import us_drug_name
from phronesis.drugintel.openfda_client import OpenFdaClient
from phronesis.drugintel.render import (
    chembl_evidence_xml,
    chembl_fallback_markdown,
    safety_evidence_xml,
)
from phronesis.drugintel.sources import ChemblSource, OpenFdaSource, _primekg_candidates
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
# How long the "slow" molecule.json batch takes. The hedge (NAME_HEDGE_S, 0.05 s here) starts per-id requests and
# the batch task is cancelled once they answer, so the test never waits this long. Audit TST-009: the margin between
# the hedge and the bound asserted in test_slow_batch_is_hedged_by_per_id_requests (SLOW_BATCH_S / 2) is kept wide
# so a loaded CI runner cannot fail it; only waiting for the batch can.
SLOW_BATCH_S = 30.0
MOLECULES = {
    SOTORASIB: {"molecule_chembl_id": SOTORASIB, "pref_name": "SOTORASIB", "max_phase": "4.0",
                "molecule_type": "Small molecule", "first_approval": 2021},
    ADAGRASIB: {"molecule_chembl_id": ADAGRASIB, "pref_name": "ADAGRASIB", "max_phase": "4.0",
                "molecule_type": "Small molecule", "first_approval": 2022},
}
KRAS_APPROVED_MECHANISMS = [
    {"molecule_chembl_id": cid, "parent_molecule_chembl_id": cid, "mechanism_of_action": "GTPase KRas inhibitor",
     "action_type": "INHIBITOR", "target_chembl_id": KRAS, "max_phase": 4}
    for cid in (SOTORASIB, ADAGRASIB)
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_and_fresh(monkeypatch):
    monkeypatch.setattr(chembl_mod, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(chembl_mod, "NAME_HEDGE_S", 0.05)
    clear_release_cache()
    yield
    clear_release_cache()


# -----------------------------------------------------------------------------
# Fake upstream
# -----------------------------------------------------------------------------

class Upstream:
    """ChEMBL + openFDA fake. batch: what molecule.json?molecule_chembl_id__in= does ("ok", "500", "timeout",
    "slow", "omit_adagrasib"); target_batch: what target.json?target_chembl_id__in= does ("ok", "500");
    single: what molecule/{id}.json and target/{id}.json do ("ok", "500"). faers: {search substring: total}."""

    def __init__(self, batch: str = "ok", target_batch: str = "ok", single: str = "ok",
                 faers: Optional[Dict[str, int]] = None) -> None:
        self.batch = batch
        self.target_batch = target_batch
        self.single = single
        self.faers = faers if faers is not None else {}
        self.requests: List[httpx.Request] = []

    def paths(self, suffix: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def faers_searches(self) -> List[str]:
        return [r.url.params.get("search", "") for r in self.requests
                if r.url.host == "api.fda.gov" and r.url.path == "/drug/event.json"]

    def label_searches(self) -> List[str]:
        return [r.url.params.get("search", "") for r in self.requests
                if r.url.host == "api.fda.gov" and r.url.path == "/drug/label.json"]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.fda.gov":
            return self._openfda(request.url.path, request.url.params)
        return await self._chembl(request, request.url.path, request.url.params)

    async def _chembl(self, request: httpx.Request, path: str, q: httpx.QueryParams) -> httpx.Response:
        if path.endswith("/status.json"):
            return httpx.Response(200, json={"chembl_db_version": "ChEMBL_36"})
        if path.endswith("/mechanism.json"):
            if q.get("max_phase") == "4":
                return httpx.Response(200, json={"mechanisms": KRAS_APPROVED_MECHANISMS, "page_meta": {"total_count": 2}})
            return httpx.Response(200, json={"mechanisms": []})
        if path.endswith("/molecule.json"):
            ids = (q.get("molecule_chembl_id__in") or "").split(",")
            if self.batch == "500":
                return httpx.Response(500, text="<html>Error</html>")
            if self.batch == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if self.batch == "slow":
                await asyncio.sleep(SLOW_BATCH_S)
            rows = [MOLECULES[i] for i in ids if i in MOLECULES]
            if self.batch == "omit_adagrasib":
                rows = [r for r in rows if r["molecule_chembl_id"] != ADAGRASIB]
            return httpx.Response(200, json={"molecules": rows})
        if "/molecule/" in path:
            cid = path.rsplit("/", 1)[-1].removesuffix(".json")
            if self.single == "500":
                return httpx.Response(500, text="<html>Error</html>")
            if cid not in MOLECULES:
                return httpx.Response(404, content=b"")
            return httpx.Response(200, json={**MOLECULES[cid], "molecule_hierarchy": {"parent_chembl_id": cid},
                                             "molecule_synonyms": []})
        if path.endswith("/target.json"):
            if "target_chembl_id__in" in q:
                if self.target_batch == "500":
                    return httpx.Response(500, text="<html>Error</html>")
                return httpx.Response(200, json={"targets": [{"target_chembl_id": KRAS, "pref_name": "GTPase KRas"}]})
            sym = q.get("target_components__target_component_synonyms__component_synonym__iexact")
            return httpx.Response(200, json={"targets": [{"target_chembl_id": KRAS, "pref_name": "GTPase KRas"}] if sym == "KRAS" else []})
        if "/target/" in path:
            if self.single == "500":
                return httpx.Response(500, text="<html>Error</html>")
            return httpx.Response(200, json={"target_chembl_id": KRAS, "pref_name": "GTPase KRas"})
        if path.endswith("/activity.json"):
            return httpx.Response(200, json={"activities": [], "page_meta": {"total_count": 0}})
        raise AssertionError(f"unexpected ChEMBL request {path}?{q}")

    def _openfda(self, path: str, q: httpx.QueryParams) -> httpx.Response:
        meta = {"disclaimer": "Do not rely on openFDA to make decisions regarding medical care.", "last_updated": "2026-07-30"}
        search = q.get("search", "")
        not_found = httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        if path == "/drug/label.json":
            return not_found
        total = next((n for needle, n in self.faers.items() if needle in search), 0)
        if not total:
            return not_found
        if q.get("count") == "patient.reaction.reactionmeddrapt.exact":
            return httpx.Response(200, json={"meta": meta, "results": [{"term": "COLITIS", "count": 321}]})
        if q.get("count") == "serious":
            return httpx.Response(200, json={"meta": meta, "results": [{"term": "1", "count": 900}]})
        return httpx.Response(200, json={"meta": {**meta, "results": {"total": total}}, "results": [{}]})


def make_engine(up: Upstream) -> DrugIntelEngine:
    transport = httpx.MockTransport(up)
    return DrugIntelEngine(ChemblClient(transport=transport), OpenFdaClient(transport=transport, api_key="", cache=TTLCache()))




def kras_ctx(query: str = QUESTION, **decisions: bool) -> QueryContext:
    ctx = QueryContext(raw_query=query, clean_query=query, primary_target="KRAS")
    for key, should in decisions.items():
        ctx.decisions[key] = SourceDecision(run=should, reason="test")
    return ctx


def _install(monkeypatch, *srcs: EvidenceSource) -> None:
    for src in srcs:
        share_source_run(src)
        monkeypatch.setitem(sources_pkg._SOURCES, src.key, src)


async def _stage1(ctx: QueryContext, registry: Any, srcs: List[EvidenceSource]) -> List[ToolResult]:
    state = SourceRunState(registry, ctx)
    return list(await asyncio.gather(*(s.run(ctx, registry, state) for s in srcs)))


def client(up: Upstream, cache: Optional[TTLCache] = None) -> ChemblClient:
    return ChemblClient(transport=httpx.MockTransport(up), cache=cache)


# -----------------------------------------------------------------------------
# 1. robust ChEMBL names
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("batch", ["500", "timeout"])
def test_molecule_names_fall_back_to_per_id_requests_when_the_batch_fails(batch):
    up = Upstream(batch=batch)
    rows = run(client(up).molecule_names_by_ids([SOTORASIB, ADAGRASIB]))
    assert [r["pref_name"] for r in rows] == ["SOTORASIB", "ADAGRASIB"]
    assert len(up.paths("/molecule.json")) == 2  # the batch and its one retry
    assert {r.url.path.rsplit("/", 1)[-1] for r in up.requests if "/molecule/" in r.url.path} == {
        f"{SOTORASIB}.json", f"{ADAGRASIB}.json"}


def test_slow_batch_is_hedged_by_per_id_requests():
    up = Upstream(batch="slow")
    t0 = time.perf_counter()
    rows = run(client(up).molecule_names_by_ids([SOTORASIB, ADAGRASIB]))
    assert [r["pref_name"] for r in rows] == ["SOTORASIB", "ADAGRASIB"]
    assert time.perf_counter() - t0 < SLOW_BATCH_S / 2  # did not wait for the batch (hedge at 0.05 s, then cancelled)


def test_ids_missing_from_the_batch_are_fetched_one_by_one():
    up = Upstream(batch="omit_adagrasib")
    rows = run(client(up).molecule_names_by_ids([SOTORASIB, ADAGRASIB]))
    assert [r["pref_name"] for r in rows] == ["SOTORASIB", "ADAGRASIB"]
    singles = [r.url.path for r in up.requests if "/molecule/" in r.url.path]
    assert singles and all(ADAGRASIB in p for p in singles)


def test_names_raise_only_when_nothing_answered():
    up = Upstream(batch="500", single="500")
    with pytest.raises(ChemblError):
        run(client(up).molecule_names_by_ids([SOTORASIB, ADAGRASIB]))


def test_names_are_cached_per_id():
    up = Upstream()
    c = client(up, cache=TTLCache(maxsize=64, ttl_s=60))
    run(c.molecule_names_by_ids([SOTORASIB, ADAGRASIB]))
    before = len(up.requests)
    rows = run(c.molecule_names_by_ids([ADAGRASIB]))  # a different batch: served from the per-id cache
    assert rows[0]["pref_name"] == "ADAGRASIB" and len(up.requests) == before


def test_target_name_falls_back_to_target_by_id():
    up = Upstream(target_batch="500")
    assert run(client(up).targets_by_ids([KRAS])) == {KRAS: "GTPase KRas"}
    assert any(r.url.path.endswith(f"/target/{KRAS}.json") for r in up.requests)


def test_kras_profile_names_drugs_and_target_when_batch_requests_fail():
    up = Upstream(batch="500", target_batch="500")
    reg = DrugToolRegistry(make_engine(up))
    ctx = kras_ctx(chembl=True)
    res = run(ChemblSource().run(ctx, reg, SourceRunState(reg, ctx)))
    assert res.success
    data = res.data
    assert [a["pref_name"] for a in data["target_approved_drugs"]] == ["SOTORASIB", "ADAGRASIB"]
    assert data["target"]["pref_name"] == "GTPase KRas"
    # The resolve step's target name is passed on, so the profile needs no target-name request of its own.
    profile_kwargs = reg.kwargs[reg.calls.index("chembl_drug_profile")]
    assert profile_kwargs["target_pref_name"] == "GTPase KRas"
    assert not [r for r in up.requests if "target_chembl_id__in" in r.url.params]
    title, detail = ChemblSource().describe(res, ctx)
    assert title.startswith("Found 2 approved drugs acting on KRAS (CHEMBL2189121)")
    assert detail.startswith("SOTORASIB, ADAGRASIB")


def test_unnamed_approved_drug_is_never_named_by_its_mechanism():
    data = {"target": {"target_chembl_id": KRAS, "pref_name": "GTPase KRas", "symbol_query": "KRAS"},
            "target_approved_status": "ok", "target_approved_total": 1,
            "target_approved_drugs": [{"chembl_id": SOTORASIB, "pref_name": None, "mechanism_of_action": "GTPase KRas inhibitor"}]}
    xml = chembl_evidence_xml(data)
    assert f'chembl_id="{SOTORASIB}" name="name not retrieved from ChEMBL"' in xml
    md = chembl_fallback_markdown(data)
    assert "Approved drugs acting on GTPase KRas: name not retrieved from ChEMBL (CHEMBL4535757)" in md
    res = ToolResult(success=True, tool_name="chembl_drug_profile", data=data)
    ctx = kras_ctx()
    _, detail = ChemblSource().describe(res, ctx)
    assert detail == f"{SOTORASIB} (name not retrieved)"
    notes = " ".join(ChemblSource().scope_notes(ctx, res))
    assert f"names of the approved drug(s) {SOTORASIB}" in notes and "is not a drug name" in notes


# -----------------------------------------------------------------------------
# 2. openFDA candidates
# -----------------------------------------------------------------------------

class UnnamedChembl(EvidenceSource):
    """A ChEMBL result whose approved drugs came back without names (the live failure)."""
    key, agent, order = "chembl", "Synapta", 20

    async def run(self, ctx, registry, state):
        return ToolResult(success=True, tool_name="chembl_drug_profile", data={
            "target": {"target_chembl_id": KRAS, "symbol_query": "KRAS", "pref_name": None},
            "target_approved_status": "ok", "target_approved_total": 2,
            "target_approved_drugs": [{"chembl_id": cid, "pref_name": None, "mechanism_of_action": "GTPase KRas inhibitor"}
                                      for cid in (SOTORASIB, ADAGRASIB)]})


class FakePrimeKG(EvidenceSource):
    key, agent, order = "primekg", "Synapta", 16

    def __init__(self, candidates: List[Dict[str, Any]], pathway: Optional[List[Dict[str, Any]]] = None) -> None:
        self.candidates, self.pathway = candidates, pathway

    async def run(self, ctx, registry, state):
        return ToolResult(success=True, tool_name="primekg_investigate", data={"analyses": [{
            "operation": "repurposing",
            "result": {"disease": {"name": "pancreatic cancer"}, "mutant_genes": ["KRAS"],
                       "candidates": self.candidates, "pathway_candidates": self.pathway}}]})


KG_CANDIDATES = [
    {"name": "Dronabinol", "id": "DB00470", "score": 3.1, "approval_groups": ["approved"], "tags": ["supportive care"]},
    {"name": "Lanreotide", "id": "DB06791", "score": 3.0, "approval_groups": ["approved"],
     "tags": ["neuroendocrine-tumour agent"]},
    {"name": "Balsalazide", "id": "DB01014", "score": 2.8, "approval_groups": ["approved"]},
    {"name": "Mesalazine", "id": "DB00244", "score": 2.7, "approval_groups": ["approved"]},
    {"name": "Trametinib", "id": "DB08911", "score": 2.0, "approval_groups": ["approved"], "tags": ["KRAS pathway"]},
]
KG_PATHWAY = [
    {"name": "Binimetinib", "id": "DB11967", "score": 1.1, "approval_groups": ["approved"], "pathway_targets": ["MAP2K1", "MAP2K2"]},
    {"name": "Tool MEK compound", "id": "DB0009", "score": 1.0, "approval_groups": ["investigational"]},
    {"name": "Dabrafenib", "id": "DB08912", "score": 0.9, "approval_groups": ["approved"], "pathway_targets": ["BRAF"]},
]


def test_chembl_approved_drugs_without_names_are_named_by_id_and_win_over_primekg(monkeypatch):
    up = Upstream(faers={'"sotorasib"': 1500, '"adagrasib"': 700})
    reg = DrugToolRegistry(make_engine(up))
    chembl, kg, openfda = UnnamedChembl(), FakePrimeKG(KG_CANDIDATES), OpenFdaSource()
    _install(monkeypatch, kg, chembl, openfda)
    ctx = kras_ctx(chembl=True, primekg=True)
    *_, fda = run(_stage1(ctx, reg, [kg, chembl, openfda]))

    sel = fda.data["candidate_selection"]
    assert sel["source"] == "ChEMBL"
    assert [d["drug"] for d in sel["drugs"]] == ["sotorasib", "adagrasib"]
    assert sel["names_looked_up"] == {SOTORASIB: "SOTORASIB", ADAGRASIB: "ADAGRASIB"}
    assert [e["ingredient"] for e in fda.data["safety"]] == ["sotorasib", "adagrasib"]
    resolve_kwargs = reg.kwargs[reg.calls.index("chembl_resolve_drugs")]
    assert resolve_kwargs["chembl_ids"] == [SOTORASIB, ADAGRASIB]


def test_primekg_fallback_skips_supportive_care_and_prefers_pathway_drugs():
    res = ToolResult(success=True, tool_name="primekg_investigate", data={"analyses": [{
        "operation": "repurposing", "result": {"disease": {"name": "pancreatic cancer"}, "mutant_genes": ["KRAS"],
                                               "candidates": KG_CANDIDATES, "pathway_candidates": KG_PATHWAY}}]})
    found, basis, why, skipped = _primekg_candidates(res)
    assert why is None
    assert [c["drug"] for c in found] == ["binimetinib", "dabrafenib", "trametinib", "balsalazide", "mesalazine"]
    assert {s["drug"] for s in skipped} == {"dronabinol", "lanreotide"}
    assert "pathway" in basis and "pancreatic cancer" in basis
    assert "KRAS-pathway drug" in found[0]["basis"] and "MAP2K1" in found[0]["basis"]


def test_primekg_fallback_without_pathway_list_still_skips_tagged_drugs(monkeypatch):
    up = Upstream(faers={'"balsalazide"': 1454, '"mesalamine"': 63326, '"trametinib"': 9000})
    reg = DrugToolRegistry(make_engine(up))
    kg, openfda = FakePrimeKG(KG_CANDIDATES), OpenFdaSource()
    _install(monkeypatch, kg, openfda)
    ctx = kras_ctx(chembl=False, primekg=True)
    _, fda = run(_stage1(ctx, reg, [kg, openfda]))
    sel = fda.data["candidate_selection"]
    assert sel["source"] == "PrimeKG"
    assert [d["drug"] for d in sel["drugs"]] == ["trametinib", "balsalazide", "mesalazine"]
    assert {s["drug"] for s in sel["skipped"]} == {"dronabinol", "lanreotide"}
    notes = " ".join(openfda.scope_notes(ctx, fda))
    assert "dronabinol (tagged supportive care)" in notes
    # mesalazine has FAERS reports only under its US name.
    mesa = next(e for e in fda.data["safety"] if e["ingredient"] == "mesalazine")
    assert mesa["total_reports"] == 63326 and mesa["faers_matched_name"] == "mesalamine"


def test_primekg_with_only_tagged_candidates_explains_why_none():
    res = ToolResult(success=True, tool_name="primekg_investigate", data={"analyses": [{
        "operation": "repurposing", "result": {"disease": {"name": "pancreatic cancer"}, "candidates": KG_CANDIDATES[:2]}}]})
    found, _, why, _ = _primekg_candidates(res)
    assert not found and "supportive-care or neuroendocrine-tumour agents (dronabinol, lanreotide)" in why


# -----------------------------------------------------------------------------
# 2c. FAERS under the US name
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("inn,usan", [
    ("mesalazine", "mesalamine"), ("paracetamol", "acetaminophen"), ("salbutamol", "albuterol"),
    ("adrenaline", "epinephrine"), ("noradrenaline", "norepinephrine"), ("glibenclamide", "glyburide"),
    ("ciclosporin", "cyclosporine"), ("rifampicin", "rifampin"), ("frusemide", "furosemide"),
    ("lignocaine", "lidocaine"), ("pethidine", "meperidine"), ("aciclovir", "acyclovir"),
    ("colecalciferol", "cholecalciferol"), ("Mesalazine ", "mesalamine"),
])
def test_us_drug_names(inn, usan):
    assert us_drug_name(inn) == usan


def test_us_drug_name_is_none_for_us_names_and_unknown_drugs():
    assert us_drug_name("mesalamine") is None and us_drug_name("sotorasib") is None and us_drug_name(None) is None


def test_faers_retries_with_the_us_name_and_reports_the_match():
    up = Upstream(faers={'activesubstancename:"mesalamine"': 63326})
    res = run(make_engine(up).drug_safety("mesalazine"))
    assert res.success
    d = res.data
    assert d["ingredient"] == "mesalazine"
    assert d["total_reports"] == 63326 and d["serious_reports"] == 900
    assert d["top_reactions"][0]["term"] == "COLITIS"
    assert d["faers_matched_name"] == "mesalamine" and d["faers_match_field"] == "activesubstance"
    assert d["faers_search"] == 'patient.drug.activesubstance.activesubstancename:"mesalamine"'
    assert d["faers_searches_tried"][0].endswith('"mesalazine"')
    # FDA labels use US names too.
    assert up.label_searches() and all("mesalazine" not in s for s in up.label_searches())

    entry = {**d, "candidate": True}
    xml = safety_evidence_xml({"safety": [entry]})
    assert 'drug="mesalazine"' in xml and 'searched_as="mesalamine"' in xml and 'match_field="activesubstance"' in xml
    fda = OpenFdaSource()
    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [entry], "skipped": []})
    title, _ = fda.describe(wrapped, kras_ctx())
    assert title == "FAERS: 63,326 reports mention mesalazine (as mesalamine)"
    notes = " ".join(fda.scope_notes(kras_ctx(), wrapped))
    assert "found under the US name mesalamine" in notes


def test_faers_falls_back_to_openfda_generic_name():
    up = Upstream(faers={'openfda.generic_name:"mesalamine"': 55833})
    d = run(make_engine(up).drug_safety("mesalazine", include_label=False)).data
    assert d["total_reports"] == 55833
    assert d["faers_matched_name"] == "mesalamine" and d["faers_match_field"] == "openfda.generic_name"
    assert d["faers_search"] == 'patient.drug.openfda.generic_name:"mesalamine"'
    assert len(d["faers_searches_tried"]) == 3


def test_faers_zero_everywhere_lists_the_searches_and_says_no_reports():
    up = Upstream(faers={})
    d = run(make_engine(up).drug_safety("mesalazine", include_label=False)).data
    assert d["total_reports"] == 0 and d["faers_matched_name"] is None and d["us_name"] == "mesalamine"
    wrapped = ToolResult(success=True, tool_name="openfda_drug_safety", data={"safety": [d], "skipped": []})
    notes = " ".join(OpenFdaSource().scope_notes(kras_ctx(), wrapped))
    assert "FAERS returned no reports for mesalazine (also searched as mesalamine" in notes


def test_faers_does_not_retry_a_name_that_matched_or_has_no_us_name():
    up = Upstream(faers={'"sotorasib"': 1500})
    d = run(make_engine(up).drug_safety("sotorasib", include_label=False)).data
    assert d["total_reports"] == 1500 and d["faers_matched_name"] == "sotorasib"
    assert all("generic_name" not in s for s in up.faers_searches())
    up = Upstream(faers={})
    d = run(make_engine(up).drug_safety("zzqnotadrug", include_label=False)).data
    assert d["total_reports"] == 0 and len(d["faers_searches_tried"]) == 1
