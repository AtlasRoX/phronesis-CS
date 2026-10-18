"""
Offline regression tests for the fixes from the live tool audit (2026-10-16):

1. Lumicell cluster annotation is vectorised (one pass instead of an AnnData view per cluster x cell type) and gives
   the same labels as the per-pair implementation; the registry timeout fits a real run.
2. The entity resolver tool fails (instead of an empty success) when no upstream answered, and the planner falls
   back to the offline lexicon.
3. Variant intelligence reports the rsID of the ClinVar SNV record that carries the protein change.
4. Open Targets disease targets retries an EFO id that Open Targets replaced (EFO -> MONDO).
5. A GWAS gene + disease query with no hits falls back to the disease alone, labelled as such.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List

import anndata as ad
import httpx
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.tools.registry import ErrorType, ToolResult


# =============================================================================
# 1. Lumicell annotation
# =============================================================================

def _reference_annotation(work: ad.AnnData, symbols: pd.Series, unique_clusters: List[Any]) -> Dict[str, str]:
    """The unvectorised per-cluster reference implementation of hierarchical marker scoring."""
    from phronesis.lumicell import annotation

    symbol_list = symbols.reindex(work.var_names).astype(str).str.upper().tolist()
    return annotation.annotate(work.X, symbol_list, np.asarray(work.obs["leiden"]), unique_clusters).labels



def _synthetic_adata(dense: bool, seed: int = 0) -> ad.AnnData:
    from phronesis.lumicell import engine

    signatures = json.loads((Path(engine.__file__).parent / "markers" / "cell_markers.json").read_text(encoding="utf-8"))
    types = [t for t, m in signatures.items() if m][:60]
    genes = list(dict.fromkeys(g for t in types for g in signatures[t]))[:400] + [f"FILLER{i}" for i in range(50)]
    rng = np.random.default_rng(seed)
    n_cells, n_clusters = 400, 6
    labels = rng.integers(0, n_clusters, size=n_cells)
    x = rng.poisson(0.3, size=(n_cells, len(genes))).astype(np.float64)
    for c in range(n_clusters):  # each cluster over-expresses the markers of one cell type
        cols = [genes.index(g) for g in signatures[types[c * 7]] if g in genes]
        x[np.ix_(labels == c, cols)] += rng.poisson(3.0, size=((labels == c).sum(), len(cols)))
    x = np.log1p(x)
    obs = pd.DataFrame({"leiden": pd.Categorical([str(v) for v in labels])}, index=[f"c{i}" for i in range(n_cells)])
    return ad.AnnData(X=x if dense else sparse.csr_matrix(x), obs=obs, var=pd.DataFrame(index=genes))


@pytest.mark.slow  # ~6-9 s per case: the per-pair reference implementation is the slow part
@pytest.mark.parametrize("dense", [False, True])
def test_vectorised_annotation_matches_per_pair_reference(dense):
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    work = _synthetic_adata(dense)
    symbols = pd.Series(work.var_names.astype(str), index=work.var_names)
    clusters = sorted(work.obs["leiden"].unique().tolist(), key=lambda c: (len(str(c)), str(c)))
    got = LumicellAnalysisEngine._annotate_clusters(work, symbols, clusters)
    assert got == _reference_annotation(work, symbols, clusters)
    assert any(not v.startswith("Cluster ") for v in got.values())


def test_single_cluster_has_no_background_and_still_matches():
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    work = _synthetic_adata(False, seed=3)
    work.obs["leiden"] = pd.Categorical(["0"] * work.n_obs)
    symbols = pd.Series(work.var_names.astype(str), index=work.var_names)
    assert LumicellAnalysisEngine._annotate_clusters(work, symbols, ["0"]) == _reference_annotation(work, symbols, ["0"])


def test_lumicell_registry_timeout_fits_a_real_run():
    from phronesis.tools.registry import get_tool_registry

    entry = get_tool_registry().get_entry("lumicell_analyze_single_cell")
    assert entry is not None and entry.timeout_seconds >= 120.0


# =============================================================================
# 2. Entity resolver: no upstream answered -> failure, planner uses the lexicon
# =============================================================================

class _StubResolver:
    def __init__(self, payload: Dict[str, Any], deadline_s: float) -> None:
        self.payload, self._deadline_s, self.calls = payload, deadline_s, []

    async def resolve(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return json.loads(json.dumps(self.payload))


@pytest.mark.asyncio
async def test_all_upstreams_timing_out_is_a_failed_call(monkeypatch):
    from phronesis.extensions import entities as ext
    from phronesis.tools.registry import BiomedicalToolRegistry
    from phronesis.utils.biocontext import entity_resolver as er

    stub = _StubResolver({"entities": [], "source_status": {"opentargets": "timeout", "hgnc": "timeout"}, "timings_ms": {}},
                         ext.DIRECT_DEADLINE_S)
    monkeypatch.setattr(er, "_DEFAULT_RESOLVER", stub)
    reg = BiomedicalToolRegistry()
    ext.register_tools(reg)
    assert reg.get_entry(ext.RESOLVER_TOOL).timeout_seconds == ext.TOOL_TIMEOUT_S == 12.0
    res = await ext._resolve(query="EGFR T790M", gene_tokens=["EGFR"])
    assert res.success is False and res.error_type == ErrorType.TIMEOUT and "opentargets: timeout" in res.error
    assert res.data["source_status"] == {"opentargets": "timeout", "hgnc": "timeout"}

    stub.payload["source_status"] = {"opentargets": "error", "hgnc": "timeout"}
    res = await ext._resolve(query="EGFR T790M", gene_tokens=["EGFR"])
    assert res.success is False and res.error_type == ErrorType.UPSTREAM_API_ERROR


@pytest.mark.asyncio
async def test_partial_resolution_stays_a_success(monkeypatch):
    from phronesis.extensions import entities as ext
    from phronesis.utils.biocontext import entity_resolver as er

    ent = {"type": "gene", "mention": "EGFR", "label": "EGFR", "ids": {}, "validated": True, "source": "HGNC"}
    stub = _StubResolver({"entities": [ent], "source_status": {"opentargets": "timeout", "hgnc": "ok"}}, ext.DIRECT_DEADLINE_S)
    monkeypatch.setattr(er, "_DEFAULT_RESOLVER", stub)
    out = await ext._resolve(query="EGFR T790M", gene_tokens=["EGFR"])
    assert isinstance(out, dict) and out["entities"][0]["label"] == "EGFR"


def test_budgeted_resolver_scales_timeouts_and_keeps_transport_and_cache(monkeypatch):
    from phronesis.extensions import entities as ext
    from phronesis.utils.biocontext import entity_resolver as er

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    base = er.EntityResolver(transport=transport)
    monkeypatch.setattr(er, "_DEFAULT_RESOLVER", base)
    budgeted = ext._budgeted_resolver(10.0)
    assert budgeted._deadline_s == 10.0 and budgeted._transport is transport and budgeted._cache is base._cache
    assert min(budgeted._timeouts.values()) == pytest.approx(8.0)
    assert budgeted._client().timeout.read == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_planner_passes_its_deadline_and_falls_back_on_failure():
    from phronesis.agents import query_planner as qp

    seen: Dict[str, Any] = {}

    class _Reg:
        async def execute(self, name: str, **kw: Any) -> ToolResult:
            seen.update(kw)
            return ToolResult(success=False, tool_name=name, error="no upstream", error_type=ErrorType.TIMEOUT,
                              data={"entities": [], "source_status": {"opentargets": "timeout"}})

    cands = qp.extract_candidates("KRAS signaling")
    assert await qp._resolve_live(_Reg(), "KRAS signaling", cands, 3.5) is None
    assert seen["deadline_s"] == pytest.approx(3.2)


# =============================================================================
# 3. Variant rsID from the right ClinVar record
# =============================================================================

def _clinvar(vcv: str, rsid: str, title: str, review: str) -> Any:
    from phronesis.variants.models import ClinVarRecord

    return ClinVarRecord(variation_id=str(int(vcv[3:])), accession=vcv, title=title, protein_change=["G12C"], rsids=[rsid],
                         genes=["KRAS"], germline_review_status=review, url=f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{int(vcv[3:])}/")


def test_clinvar_snv_record_ranks_before_delins_and_by_review_then_vcv():
    from phronesis.variants.service import clinvar_record_rank

    delins = _clinvar("VCV001701193", "rs2135806256", "NM_004985.5(KRAS):c.33_34delinsCT (p.Gly12Cys)", "criteria provided, single submitter")
    snv = _clinvar("VCV000012578", "rs121913530", "NM_004985.5(KRAS):c.34G>T (p.Gly12Cys)", "criteria provided, multiple submitters, no conflicts")
    ranked = sorted([delins, snv], key=lambda r: clinvar_record_rank(r, "G12C"))
    assert [r.rsids[0] for r in ranked] == ["rs121913530", "rs2135806256"]

    single = _clinvar("VCV000000100", "rs1", "NM_1.1(KRAS):c.34G>T (p.Gly12Cys)", "criteria provided, single submitter")
    panel = _clinvar("VCV000000900", "rs9", "NM_1.1(KRAS):c.34G>T (p.Gly12Cys)", "reviewed by expert panel")
    older = _clinvar("VCV000000050", "rs5", "NM_1.1(KRAS):c.34G>T (p.Gly12Cys)", "reviewed by expert panel")
    assert [r.rsids[0] for r in sorted([single, panel, older], key=lambda r: clinvar_record_rank(r, "G12C"))] == ["rs5", "rs9", "rs1"]


# =============================================================================
# 4. Open Targets disease targets: EFO -> MONDO retry
# =============================================================================

async def _fake_targets(efo_id: str, target_limit: int = 15, candidate_limit: int = 40) -> Dict[str, Any]:
    if efo_id == "MONDO_0005192":
        return {"disease": {"id": efo_id, "name": "exocrine pancreatic carcinoma"}, "targets": [{"symbol": "KRAS"}], "tiers": {}}
    return {"disease": None, "targets": [], "clinical_candidates": [], "tiers": {}}


@pytest.mark.asyncio
async def test_replaced_efo_id_is_retried_with_its_mondo_id():
    from phronesis.extensions import disease_targets as dt

    async def lookup(efo_id: str):
        return ("MONDO_0005192", "EBI OLS 'term replaced by'") if efo_id == "EFO_0002618" else (None, "")

    res = await dt._run(_fake_targets, "EFO_0002618", 5, 5, lookup=lookup)
    assert res.success and res.canonical_ids == {"ontology_id": "MONDO_0005192"}
    assert res.data["ontology_id_mapping"] == {"requested_id": "EFO_0002618", "resolved_id": "MONDO_0005192", "via": "EBI OLS 'term replaced by'"}
    src = dt.DiseaseTargetsSource()
    assert src.allowed_identifiers(res)["ontology"] == {"MONDO_0005192", "EFO_0002618"}
    assert 'requested_id="EFO_0002618"' in src.evidence_xml(res)

    missing = await dt._run(_fake_targets, "EFO_9999999", 5, 5, lookup=lookup)
    assert missing.success is False and missing.error_type == ErrorType.NOT_FOUND

    async def broken(_efo_id: str):
        raise httpx.ConnectError("down")

    still_missing = await dt._run(_fake_targets, "EFO_0002618", 5, 5, lookup=broken)
    assert still_missing.success is False and still_missing.error_type == ErrorType.NOT_FOUND


@pytest.mark.asyncio
async def test_replacement_lookup_reads_ols_then_searches_open_targets_by_label():
    from phronesis.extensions import disease_targets as dt

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.ebi.ac.uk":
            obo = request.url.params["obo_id"]
            term = {"label": "obsolete_glioblastoma multiforme", "is_obsolete": True,
                    "term_replaced_by": "http://purl.obolibrary.org/obo/MONDO_0018177"}
            if obo == "EFO:0000002":
                term = {"label": "obsolete_crohn disease", "is_obsolete": True, "term_replaced_by": None}
            return httpx.Response(200, json={"_embedded": {"terms": [term]}})
        body = json.loads(request.content)
        assert body["variables"] == {"q": "crohn disease"}
        hits = [{"id": "MONDO_0000001", "name": "crohn disease extra", "entity": "disease"},
                {"id": "MONDO_0005011", "name": "Crohn disease", "entity": "disease"}]
        return httpx.Response(200, json={"data": {"search": {"hits": hits}}})

    transport = httpx.MockTransport(handler)
    assert await dt.replacement_disease_id("EFO_0000519", transport=transport) == ("MONDO_0018177", "EBI OLS 'term replaced by'")
    new_id, how = await dt.replacement_disease_id("EFO_0000002", transport=transport)
    assert new_id == "MONDO_0005011" and "Open Targets search" in how


# =============================================================================
# 5. GWAS gene + disease -> disease-only fallback
# =============================================================================

class _GwasRegistry:
    def __init__(self, gene_hits: int, disease_hits: int) -> None:
        self.gene_hits, self.disease_hits, self.calls = gene_hits, disease_hits, []

    async def execute(self, name: str, **kw: Any) -> ToolResult:
        self.calls.append(kw)
        n = self.gene_hits if kw.get("gene") else self.disease_hits
        items = [{"rsid": f"rs{100 + i}", "trait": "glioblastoma", "mapped_genes": ["TERT"], "p_value": "8e-74"} for i in range(n)]
        subject = ", ".join(x for x in (f"gene {kw['gene']}" if kw.get("gene") else "", f"trait {kw.get('efo_id')}") if x)
        return ToolResult(success=True, tool_name=name, data={"subject": subject, "items": items, "total_associations": n})


def _gwas_ctx() -> QueryContext:
    g = ResolvedEntity(type="gene", mention="MYC", label="MYC", ids={}, validated=True, source="HGNC")
    d = ResolvedEntity(type="disease", mention="glioblastoma", label="glioblastoma", ids={"ontology_id": "MONDO_0018177"},
                       validated=True, source="OpenTargets")
    q = "GWAS associations of MYC in glioblastoma"
    return QueryContext(raw_query=q, clean_query=q, entities=[g, d], primary_gene=g, primary_target="MYC")


@pytest.mark.asyncio
async def test_gwas_gene_disease_empty_falls_back_to_disease_and_says_so():
    from phronesis.sources_extra import gwas

    src, ctx, reg = gwas.GwasSource(), _gwas_ctx(), _GwasRegistry(gene_hits=0, disease_hits=3)
    res = await src.run(ctx, reg, None)
    assert reg.calls == [{"gene": "MYC", "efo_id": "MONDO_0018177"}, {"efo_id": "MONDO_0018177"}]
    assert res.data["fallback"]["kind"] == "disease_only" and len(res.data["items"]) == 3
    assert "no MYC-specific associations for glioblastoma" in res.data["subject"]
    assert "no MYC-specific associations" in src.describe(res, ctx)[0]
    assert "do not attribute them to MYC" in src.scope_notes(ctx, res)[0]
    assert "no MYC-specific associations" in src.evidence_xml(res).splitlines()[0]


@pytest.mark.asyncio
async def test_gwas_fallback_only_when_the_gene_query_is_empty():
    from phronesis.sources_extra import gwas

    src, ctx = gwas.GwasSource(), _gwas_ctx()
    hit = _GwasRegistry(gene_hits=2, disease_hits=9)
    res = await src.run(ctx, hit, None)
    assert len(hit.calls) == 1 and "fallback" not in res.data and len(res.data["items"]) == 2

    nothing = _GwasRegistry(gene_hits=0, disease_hits=0)
    res = await src.run(ctx, nothing, None)
    assert len(nothing.calls) == 2 and "fallback" not in res.data and res.data["items"] == []
