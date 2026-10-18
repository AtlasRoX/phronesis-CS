"""
Offline tests for Synapta variant intelligence (phronesis.variants): parser, pure prevalence/SPDI helpers,
ClinVar/CIViC/gnomAD/cBioPortal clients over httpx.MockTransport, study selection, orchestrator routing,
verifier checks, evidence XML and memory identifiers. Every fixture under tests/fixtures/variant_intel is a
trimmed live response (captured 2026-10-13) or a live tool result.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from phronesis.agents.prompts import build_evidence_block
from phronesis.agents.query_context import QueryContext, ResolvedEntity
from phronesis.extensions import hooks
from phronesis.phronesis.agents.verifier import ClinicalVerifier
from phronesis.tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult
from phronesis.utils.memory.clinical_memory import ClinicalMemoryEngine
from phronesis.variants import clients, service
from phronesis.variants.checks import check_clinvar_classification, check_variant_frequencies
from phronesis.variants.models import VariantIntelInput
from phronesis.variants.parse import extract_cancer_keyword, parse_variant_mentions
from phronesis.variants.render import compact_artifact, describe_variant_result, render_variant_evidence, variant_markdown_summary
from phronesis.variants.service import compute_prevalence, explain_spdi, run_variant_intelligence, select_studies, spdi_to_gnomad_id
from phronesis.variants.source import VariantIntelSource
from tests.fakes import called, make_offline_orchestrator

FIX = Path(__file__).parent / "fixtures" / "variant_intel"
TOOL = "synapta_variant_intelligence"
PAAD = "paad_tcga_pan_can_atlas_2018"


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def tool_result(name: str = "tool_result_kras_g12d_pancreatic.json") -> ToolResult:
    raw = fx(name)
    return ToolResult(**raw)


@pytest.fixture(autouse=True)
def _offline_clients(monkeypatch):
    """No real sleeping or limiter spacing, and empty caches, for every test."""
    sleeps: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    class _NoLimit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(clients, "_sleep", fake_sleep)
    monkeypatch.setattr(clients, "_limiter", lambda name, interval: _NoLimit())
    clients.clear_caches()
    yield sleeps
    clients.clear_caches()


# -----------------------------------------------------------------------------
# Fixture-backed upstream (httpx.MockTransport)
# -----------------------------------------------------------------------------

def _json_response(obj: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, json=obj, headers=headers)


class Upstream:
    """Routes ClinVar, CIViC, gnomAD and cBioPortal requests to captured fixtures; tests override per service."""

    ESEARCH = {
        'KRAS[gene] AND "Gly12Asp"[variant name]': "clinvar_esearch_kras_g12d.json",
        'BRAF[gene] AND "Val600Glu"[variant name]': "clinvar_esearch_braf_v600e.json",
    }

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.overrides: Dict[str, Callable[[httpx.Request], Any]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        kind = {"eutils.ncbi.nlm.nih.gov": "clinvar", "civicdb.org": "civic", "gnomad.broadinstitute.org": "gnomad",
                "www.cbioportal.org": "cbioportal"}[host]
        if kind in self.overrides:
            out = self.overrides[kind](request)
            if asyncio.iscoroutine(out):
                out = await out
            if out is not None:
                return out
        return getattr(self, f"_{kind}")(request)

    def bodies(self, kind_host: str) -> List[Dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.host == kind_host and r.method == "POST"]

    def _clinvar(self, request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(request.url)).query)
        if request.url.path.endswith("esearch.fcgi"):
            name = self.ESEARCH.get(q["term"][0])
            return _json_response(fx(name)) if name else httpx.Response(404, text="not in fixtures")
        ids = q["id"][0].split(",")
        doc = fx("clinvar_esummary_13961.json" if ids == ["13961"] else "clinvar_esummary_12582_12583.json")
        uids = [u for u in doc["result"]["uids"] if u in ids]  # esummary returns only the requested uids
        doc["result"] = {"uids": uids, **{u: doc["result"][u] for u in uids}}
        return _json_response(doc)

    def _civic(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, var = body["query"], body["variables"]
        if "gene(entrezSymbol" in query:
            return _json_response(fx({"KRAS": "civic_gene_kras.json", "BRAF": "civic_gene_braf.json"}.get(var["s"], "civic_gene_null.json")))
        if "variants(" in query:
            return _json_response(fx({30: "civic_variants_kras_g12d.json"}.get(var["g"], "civic_variants_braf_v600e.json")))
        if "evidenceItems" in query:
            return _json_response(fx({79: "civic_evidence_mp79.json", 12: "civic_evidence_mp12_first6.json"}[var["mp"]]))
        return _json_response(fx({79: "civic_assertions_mp79.json", 12: "civic_assertions_mp12.json"}[var["mp"]]))

    def _gnomad(self, request: httpx.Request) -> httpx.Response:
        vid = json.loads(request.content)["variables"]["id"]
        return _json_response(fx("gnomad_12-25245350-C-T.json" if vid == "12-25245350-C-T" else "gnomad_not_found.json"))

    def _cbioportal(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/genes/KRAS":
            return _json_response(fx("cbio_genes_kras.json"))
        if path.startswith("/api/genes/"):
            nf = fx("cbio_genes_notfound.json")
            return _json_response(nf["body"], status=nf["status"])
        if path == "/api/studies":
            return _json_response(fx("cbio_studies_subset.json"))
        if path == f"/api/molecular-profiles/{PAAD}_mutations/mutations":
            return _json_response(fx(f"cbio_mutations_{PAAD}_kras.json"))
        if path == f"/api/molecular-profiles/{PAAD}_mutations/gene-panel-data/fetch":
            return _json_response(fx(f"cbio_gene_panel_data_{PAAD}.json"))
        return _json_response({"message": f"Molecular profile not found: {path}"}, status=404)


def _html_429(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, text="<html><body><h1>429 Too Many Requests</h1></body></html>", headers={"content-type": "text/html"})


async def _run(upstream: Upstream, **kwargs: Any) -> ToolResult:
    kwargs.setdefault("gene", "KRAS")
    kwargs.setdefault("protein_change", "G12D")
    kwargs.setdefault("cancer_keyword", "pancreatic")
    kwargs.setdefault("max_studies", 1)
    return await run_variant_intelligence(**kwargs, transport=upstream.transport())


def _status(res: ToolResult) -> Dict[str, Dict[str, Any]]:
    return {s["name"]: s for s in res.data["sources"]}


# -----------------------------------------------------------------------------
# 1. Parser
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query, expected",
    [
        ("KRAS G12D", [("KRAS", "G12D", None, None)]),
        ("KRAS p.G12D", [("KRAS", "G12D", None, None)]),
        ("KRAS p.Gly12Asp", [("KRAS", "G12D", None, None)]),
        ("BRAF V600E", [("BRAF", "V600E", None, None)]),
        ("EGFR L858R and T790M", [("EGFR", "L858R", None, None), ("EGFR", "T790M", None, None)]),
        ("HER2 S310F", [("ERBB2", "S310F", None, None)]),
        ("rs121913529", [(None, None, "rs121913529", None)]),
        ("NM_004985.5(KRAS):c.35G>A", [("KRAS", None, None, "c.35G>A")]),
        ("What is the prevalence of KRAS G12D in pancreatic cancer?", [("KRAS", "G12D", None, None)]),
        ("kras g12d in pancreatic cancer", [("KRAS", "G12D", None, None)]),
        ("EGFR L858R, c797s", [("EGFR", "L858R", None, None), ("EGFR", "C797S", None, None)]),
        ("TP53 p.Arg248fsTer5", [("TP53", "R248fs*5", None, None)]),
    ],
)
def test_parser_positives(query, expected):
    got = [(m.gene, m.protein_change, m.rsid, m.cdna_change) for m in parse_variant_mentions(query)]
    assert got == expected


def test_parser_details():
    (m,) = parse_variant_mentions("KRAS G12D")
    assert m.protein_change_3 == "Gly12Asp"
    (h,) = parse_variant_mentions("NM_004985.5(KRAS):c.35G>A")
    assert h.hgvs == "NM_004985.5(KRAS):c.35G>A"
    (v,) = parse_variant_mentions("VCV000012582")
    assert v.clinvar_vcv == "VCV000012582" and v.gene is None


@pytest.mark.parametrize(
    "query",
    ["A549 cells", "K562 leukemia cells", "COVID-19 outcomes", "CD8 T cells", "IL6 levels", "H3K27M glioma",
     "simulate BIOMD0000000010", "Analyze EGFR signaling", "pembrolizumab 200 mg IV Q3W in NSCLC",
     "dose PO Q12H for 5 days", "abc d12e"],
)
def test_parser_negatives(query):
    assert parse_variant_mentions(query) == []


def test_cancer_keyword():
    assert extract_cancer_keyword("KRAS G12D in pancreatic cancer") == "pancreatic"
    assert extract_cancer_keyword("EGFR T790M in non-small cell lung cancer") == "nsclc"
    assert extract_cancer_keyword("BRAF V600E") is None
    # Substrings of other organs are not the cancer type: gallbladder is biliary, paraganglioma is not a glioma.
    assert extract_cancer_keyword("KRAS G12D in gallbladder cancer") == "cholangiocarcinoma"
    assert extract_cancer_keyword("SDHB R46Q in paraganglioma") is None
    assert extract_cancer_keyword("CDC73 in parathyroid carcinoma") is None


def test_input_schema():
    assert VariantIntelInput(gene="KRAS", protein_change="G12D").max_studies == 4
    VariantIntelInput(gene="KRAS", hgvs_c="NM_004985.5(KRAS):c.35G>A")
    for bad in ({"gene": "kras"}, {"gene": "KRAS", "protein_change": "Gly12Asp"}, {"gene": "KRAS", "rsid": "121913529"},
                {"gene": "KRAS", "max_studies": 9}, {"gene": "KRAS", "cancer_keyword": "x" * 61}):
        with pytest.raises(Exception):
            VariantIntelInput(**bad)


# -----------------------------------------------------------------------------
# 2. Pure helpers
# -----------------------------------------------------------------------------

def test_spdi_to_gnomad_id():
    assert spdi_to_gnomad_id("NC_000012.12:25245349:C:T") == "12-25245350-C-T"
    assert spdi_to_gnomad_id("NC_000023.11:100:G:A") == "X-101-G-A"
    variant_id, reason = explain_spdi("NC_000012.12:25245349:CA:C")
    assert variant_id is None and "single-nucleotide" in reason
    assert spdi_to_gnomad_id(None) is None


def _paad_inputs():
    return fx(f"cbio_mutations_{PAAD}_kras.json"), fx(f"cbio_gene_panel_data_{PAAD}.json")


def test_compute_prevalence_paad_pancan():
    muts, rows = _paad_inputs()
    st = compute_prevalence(muts, rows, {}, gene="KRAS", protein_change="G12D", study={"studyId": PAAD, "name": "PAAD"})
    assert (st.mutated_samples, st.profiled_samples, st.prevalence_percent) == (49, 179, 27.4)
    assert st.mutated_patients == 49 and st.gene_mutated_samples == 117
    assert st.denominator_basis == "all sequenced samples (whole-exome)" and st.panels == {"WES": 179}


def test_compute_prevalence_duplicates_and_excluded_statuses():
    muts, rows = _paad_inputs()
    g12d = [m for m in muts if m["proteinChange"] == "G12D"]
    duplicated = muts + [copy.deepcopy(g12d[0])]  # the same call reported twice for one sample
    assert compute_prevalence(duplicated, rows, {}, gene="KRAS", protein_change="G12D").mutated_samples == 49
    relabelled = copy.deepcopy(muts)
    changed = 0
    for m in relabelled:
        if m["proteinChange"] == "G12D" and changed < 2:
            m["mutationStatus"] = "UNCALLED" if changed == 0 else "Germline"
            changed += 1
    st = compute_prevalence(relabelled, rows, {}, gene="KRAS", protein_change="G12D")
    assert st.mutated_samples == 47 and st.profiled_samples == 179


def test_compute_prevalence_panel_lacking_gene():
    rows = fx("cbio_gene_panel_data_msk_impact_2017_subset.json")
    muts = fx("cbio_mutations_msk_impact_2017_subset_fyn.json")
    panels = {p: {g["hugoGeneSymbol"] for g in fx(f"cbio_gene_panel_{p}.json")["genes"]} for p in ("IMPACT341", "IMPACT410")}
    assert "FYN" not in panels["IMPACT341"] and "FYN" in panels["IMPACT410"]
    st = compute_prevalence(muts, rows, panels, gene="FYN", protein_change="S124F")
    assert st.profiled_samples == 20 and st.panels == {"IMPACT410": 20}  # the 20 IMPACT341 samples were not profiled for FYN
    assert st.gene_mutated_samples == 10 and st.mutated_samples == 1 and st.prevalence_percent == 5.0
    assert st.denominator_basis == "samples on gene panels containing FYN"
    # An unknown panel is treated as not covering the gene; no profiled samples gives None, never 0.
    empty = compute_prevalence(muts, rows, {}, gene="FYN", protein_change="S124F")
    assert empty.profiled_samples == 0 and empty.prevalence_percent is None


def test_select_studies():
    studies = fx("cbio_studies_subset.json")
    picked, used = select_studies(studies, "pancreatic", 4)
    ids = [s["studyId"] for s in picked]
    assert used == "pancreatic" and ids[0] == PAAD and len(ids) == 4
    for dropped in ("paad_tcga", "paad_tcga_gdc", "pancreas_ctdna_msk_2025"):
        assert dropped not in ids
    sizes = [s["sequencedSampleCount"] for s in picked[1:]]
    assert sizes == sorted(sizes, reverse=True)
    assert [s["studyId"] for s in select_studies(studies, None, 4)[0]] == ["msk_impact_2017"]
    assert select_studies(studies, None, 4)[1] is None
    assert select_studies(studies, "unicorn cancer", 4)[1] is None
    lung = [s["studyId"] for s in select_studies(studies, "lung", 8)[0]]
    assert "lung_pdx_msk_2021" not in lung and "nsclc_ctdx_msk_2022" not in lung


def test_hgvs_c_is_numbered_on_the_protein_change_transcript():
    from phronesis.variants.models import ClinVarRecord, GnomadFrequency

    braf = tool_result("tool_result_braf_v600e_melanoma.json").data
    records, gnomad = [ClinVarRecord(**r) for r in braf["clinvar"]], GnomadFrequency(**braf["gnomad"])
    assert gnomad.hgvsc == "c.1919T>A" and gnomad.hgvsp == "p.Val640Glu"  # gnomAD canonical (MANE) transcript numbering
    assert service.consistent_hgvs_c(records, gnomad, "p.Val600Glu") == "NM_004333.6(BRAF):c.1799T>A"
    assert service.consistent_hgvs_c([], gnomad, "p.Val600Glu") is None
    kras = tool_result().data
    assert service.consistent_hgvs_c([], GnomadFrequency(**kras["gnomad"]), "p.Gly12Asp") == "c.35G>A"


def test_select_studies_word_start_and_tcga_cohort_dedupe():
    studies = fx("cbio_studies_word_boundary.json")
    ids = lambda kw: [s["studyId"] for s in select_studies(studies, kw, 8)[0]]  # noqa: E731
    # Not Pheochromocytoma and Paraganglioma; GBM and LGG share cancerTypeId difg but are distinct TCGA cohorts,
    # while gbm_tcga (Firehose Legacy) is another version of the GBM cohort.
    assert ids("glioma") == ["lgg_tcga_pan_can_atlas_2018", "gbm_tcga_pan_can_atlas_2018"]
    assert ids("kidney") == ["kirc_tcga_pan_can_atlas_2018"]  # not adrenocortical (parent adrenal_gland)
    assert ids("bladder") == ["blca_tcga_pan_can_atlas_2018"]  # not gallbladder carcinoma
    assert ids("sarcoma") == ["sarc_tcga_pan_can_atlas_2018"]  # PCPG shares cancerTypeId soft_tissue
    assert ids("gallbladder") == ["gbc_shanghai_2014"]


# -----------------------------------------------------------------------------
# 3. Clients and the tool (MockTransport)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_success_all_sources():
    up = Upstream()
    res = await _run(up)
    assert res.success and res.tool_name == TOOL
    st = _status(res)
    assert {k: v["status"] for k, v in st.items()} == {"clinvar": "ok", "civic": "ok", "cbioportal": "ok", "gnomad": "ok"}
    data = res.data
    (rec,) = data["clinvar"]
    assert rec["accession"] == "VCV000012582" and rec["rsids"] == ["rs121913529"]
    assert rec["germline_classification"] == "Pathogenic/Likely pathogenic"
    assert rec["oncogenicity_classification"] == "Oncogenic" and rec["somatic_clinical_impact"] == "Tier I - Strong"
    assert data["civic"]["accepted_evidence_total"] == 21 and len(data["civic"]["evidence"]) == 10
    assert data["civic"]["evidence"][0]["url"].startswith("https://civicdb.org/evidence/")
    assert all(len(e["description"]) <= 600 for e in data["civic"]["evidence"])
    (study,) = data["cbioportal"]
    assert (study["study_id"], study["mutated_samples"], study["profiled_samples"], study["prevalence_percent"]) == (PAAD, 49, 179, 27.4)
    g = data["gnomad"]
    assert g["variant_id"] == "12-25245350-C-T" and (g["exome_ac"], g["exome_an"]) == (4, 1460562)
    assert {p["id"] for p in g["populations"]} >= {"nfe", "afr"} and all("_" not in p["id"] for p in g["populations"])
    assert data["hgvs_p"] == "p.Gly12Asp" and data["cancer_filter"] == "pancreatic"
    assert res.canonical_ids["clinvar_vcv"] == "VCV000012582" and res.canonical_ids["rsids"] == "rs121913529"
    assert "EID1300" in res.canonical_ids["civic_ids"] and "27010960" in res.canonical_ids["pmids"]
    assert {c["type"] for c in data["citation_records"]} == {"clinvar", "rsid", "civic"}
    # ClinVar etiquette parameters, never a user email
    q = parse_qs(urlparse(str(next(r for r in up.requests if r.url.host.startswith("eutils")).url)).query)
    assert q["tool"] == ["phronesis"] and "email" not in q


@pytest.mark.asyncio
async def test_clinvar_filters_other_alleles_by_protein_change():
    up = Upstream()
    up.overrides["clinvar"] = lambda r: (
        _json_response(fx("clinvar_esearch_kras_loose.json")) if r.url.path.endswith("esearch.fcgi") else None
    )
    res = await _run(up)
    assert [r["variation_id"] for r in res.data["clinvar"]] == ["12582"]  # 12583 (G12V) is dropped


@pytest.mark.asyncio
async def test_clinvar_429_retry_after(_offline_clients):
    sleeps = _offline_clients
    up = Upstream()
    hits = {"n": 0}

    def once_limited(request):
        if request.url.path.endswith("esearch.fcgi") and hits["n"] == 0:
            hits["n"] += 1
            return _json_response({"error": "API rate limit exceeded", "limit": "3"}, status=429, headers={"retry-after": "2"})
        return None

    up.overrides["clinvar"] = once_limited
    res = await _run(up)
    assert _status(res)["clinvar"]["status"] == "ok" and 2.0 in sleeps

    up2 = Upstream()
    up2.overrides["clinvar"] = lambda r: _json_response({"error": "API rate limit exceeded"}, status=429, headers={"retry-after": "2"})
    res2 = await _run(up2)
    st = _status(res2)
    assert st["clinvar"]["status"] == "failed" and "429" in st["clinvar"]["detail"]
    assert st["gnomad"]["status"] == "skipped"
    assert res2.success and st["civic"]["status"] == "ok"  # other sources still return data


@pytest.mark.asyncio
async def test_civic_errors_and_null_gene():
    up = Upstream()
    up.overrides["civic"] = lambda r: _json_response(fx("civic_error_bad_field.json"))
    res = await _run(up)
    assert _status(res)["civic"]["status"] == "failed" and "GraphQL error" in _status(res)["civic"]["detail"]
    assert res.success and res.data["civic"] is None

    up2 = Upstream()
    res2 = await _run(up2, gene="NRAS", protein_change="Q61K", include_gnomad=False)
    assert _status(res2)["civic"]["status"] == "no_match"


@pytest.mark.asyncio
async def test_civic_exact_name_filter_and_accepted_status():
    up = Upstream()
    up.overrides["cbioportal"] = lambda r: httpx.Response(503, text="not in fixtures")
    res = await _run(up, gene="BRAF", protein_change="V600E", cancer_keyword=None, include_gnomad=False)
    civic = res.data["civic"]
    assert civic["variant_id"] == 12 and civic["molecular_profile_id"] == 12  # not 'V600E+V600K' or 'p61BRAF(V600E)'
    assert [a["aid"] for a in civic["assertions"]] == ["AID20", "AID7", "AID10"]
    assert civic["assertions"][1]["fda_companion_test"] is True
    bodies = [b["query"] for b in up.bodies("civicdb.org")]
    assert all("status:ACCEPTED" in q for q in bodies if "evidenceItems" in q or "assertions" in q)
    assert sum(1 for q in bodies if "evidenceItems" in q or "assertions" in q) == 2
    (rec,) = res.data["clinvar"]
    assert rec["germline_classification"] == "Conflicting classifications of pathogenicity"
    assert rec["protein_change"][0] == "V600E" and len(rec["protein_change"]) > 1


@pytest.mark.asyncio
async def test_gnomad_not_found_and_rate_limit(_offline_clients):
    up = Upstream()
    up.overrides["gnomad"] = lambda r: _json_response(fx("gnomad_not_found.json"))
    res = await _run(up)
    assert _status(res)["gnomad"]["status"] == "no_match" and res.data["gnomad"] is None

    clients.clear_caches()  # the not-found answer above is cached, as intended
    sleeps = _offline_clients
    up2 = Upstream()
    up2.overrides["gnomad"] = _html_429
    res2 = await _run(up2)
    st = _status(res2)
    assert st["gnomad"] == {**st["gnomad"], "status": "failed", "detail": "rate limited"}
    assert 3.0 in sleeps and sum(1 for r in up2.requests if r.url.host.startswith("gnomad")) == 2  # one retry only
    assert res2.success and all(st[n]["status"] == "ok" for n in ("clinvar", "civic", "cbioportal"))
    xml = render_variant_evidence(res2.data)
    assert '<source_status name="gnomad" status="failed"' in xml and ">rate limited</source_status>" in xml


@pytest.mark.asyncio
async def test_all_sources_failing_is_upstream_error():
    up = Upstream()
    for kind in ("clinvar", "civic", "gnomad", "cbioportal"):
        up.overrides[kind] = lambda r: httpx.Response(503, text="Service Unavailable")
    res = await _run(up)
    assert not res.success and res.error_type == ErrorType.UPSTREAM_API_ERROR
    assert {s["status"] for s in res.data["sources"]} <= {"failed", "skipped"}


@pytest.mark.asyncio
async def test_per_source_timeout_keeps_other_sources():
    up = Upstream()

    async def slow(_request):
        await asyncio.sleep(1.0)  # outlasts the 0.2 s CIViC budget, which cancels it
        return None

    up.overrides["civic"] = slow
    res = await run_variant_intelligence(
        gene="KRAS", protein_change="G12D", cancer_keyword="pancreatic", max_studies=1,
        transport=up.transport(), budgets={"civic": 0.2},
    )
    st = _status(res)
    assert st["civic"]["status"] == "failed" and st["civic"]["detail"] == "timeout after 0.2 s"
    assert res.success and res.data["cbioportal"][0]["mutated_samples"] == 49 and res.data["clinvar"]


@pytest.mark.asyncio
async def test_cbioportal_unknown_gene_and_hgvs_resolution():
    up = Upstream()
    res = await _run(up, gene="NOTAGENE", protein_change="G12D", include_gnomad=False)
    assert _status(res)["cbioportal"]["status"] == "no_match"

    up2 = Upstream()
    up2.overrides["clinvar"] = lambda r: (
        _json_response(fx("clinvar_esearch_kras_g12d.json")) if r.url.path.endswith("esearch.fcgi") else None
    )
    res2 = await _run(up2, protein_change=None, hgvs_c="NM_004985.5(KRAS):c.35G>A")
    term = parse_qs(urlparse(str(up2.requests[0].url)).query)["term"][0]
    assert term == '"NM_004985.5(KRAS):c.35G>A"'
    assert res2.data["protein_change"] == "G12D"  # resolved from the ClinVar title before CIViC/cBioPortal ran
    assert res2.data["cbioportal"][0]["mutated_samples"] == 49


@pytest.mark.asyncio
async def test_registry_entry_and_validation():
    from phronesis.extensions import variants as ext

    reg = BiomedicalToolRegistry()
    with hooks.isolated_hooks():
        ext.register_tools(reg)
        assert "Synapta variant intelligence" in "\n".join(hooks.get_prompt_components())
    entry = reg.get_entry(TOOL)
    assert entry.domain == "synapta" and entry.timeout_seconds == 30.0 and entry.input_schema is VariantIntelInput
    bad = await reg.execute(TOOL, gene="KRAS", protein_change="p.G12D")
    assert not bad.success and bad.error_type == ErrorType.VALIDATION_ERROR


# -----------------------------------------------------------------------------
# 4. Evidence source, orchestrator routing
# -----------------------------------------------------------------------------

def _ctx(query: str, **kw: Any) -> QueryContext:
    return QueryContext(raw_query=query, clean_query=query, **kw)


def test_source_routing_and_plan():
    src = VariantIntelSource()
    ctx = _ctx("What is the prevalence of KRAS G12D in pancreatic cancer?")
    assert src.should_run(ctx).run
    assert src.plan_fact(ctx) == "Variant: KRAS p.G12D"
    assert src.running_title(ctx) == "Looking up KRAS G12D in ClinVar, CIViC, cBioPortal and gnomAD"
    sim = _ctx("Simulate BIOMD0000000010 with KRAS G12D", biomodel_id="BIOMD0000000010", is_simulation_intent=True)
    assert not src.should_run(sim).run
    ent = _ctx("prevalence in pancreatic cancer", entities=[
        ResolvedEntity(type="variant", mention="G12D", label="KRAS G12D", gene="KRAS", protein_change="G12D")])
    assert src.should_run(ent).run and src.plan_fact(ent) == "Variant: KRAS p.G12D"
    rs_only = _ctx("rs121913529 frequency")
    decision = src.should_run(rs_only)
    assert not decision.run and "gene" in decision.reason
    assert "without a gene symbol" in src.scope_notes(rs_only, None)[0]
    two = _ctx("EGFR L858R and T790M")
    assert src.should_run(two).run
    res = tool_result()
    assert any("EGFR T790M" in n for n in src.scope_notes(two, res))
    same = _ctx("KRAS G12D (rs121913529) prevalence in pancreatic cancer")
    assert src.should_run(same).run
    assert not [n for n in src.scope_notes(same, res) if "rs121913529" in n]


def test_source_outputs_from_real_result():
    src = VariantIntelSource()
    ctx = _ctx("KRAS G12D prevalence in pancreatic cancer")
    res = tool_result()
    title, detail = src.describe(res, ctx)
    assert title == "Looked up KRAS G12D in 4 of 4 variant databases"
    # DAT-006: the germline classification carries its conditions and review stars, never a bare label.
    assert ("ClinVar VCV000012582: Pathogenic/Likely pathogenic (germline; for KRAS-related disorder, Congenital "
            "Pulmonary Airway Malformations +4 more; 2/4 stars)") in detail
    assert "CIViC: 21 accepted evidence items" in detail
    assert "cBioPortal: 49/179 samples (27.4%) in Pancreatic Adenocarcinoma (TCGA, PanCancer Atlas)" in detail
    assert "gnomAD AF 2.7e-06 (exome, germline)" in detail
    ids = src.allowed_identifiers(res)
    assert ids["clinvar"] == {"VCV000012582"} and ids["rsid"] == {"rs121913529"} and "EID1300" in ids["civic"]
    cites = src.citations(res)
    assert {c.type for c in cites} == {"clinvar", "rsid", "civic"} and cites[0].source_agent == "Synapta"
    key, art = src.artifact(res)
    assert key == "variant_intel" and "citation_records" not in art
    assert all(len(e["description"]) <= 280 for e in art["civic"]["evidence"])
    assert len(json.dumps(art)) < 16000
    md = src.fallback_markdown(res)
    assert md.startswith("#### Variant & Biomarker Evidence") and "49/179 samples (27.4%)" in md
    assert src.scope_notes(ctx, res) == []


@pytest.mark.asyncio
async def test_orchestrator_routes_variant_question(monkeypatch, tmp_path):
    def fake(kwargs: Dict[str, Any]) -> ToolResult:
        return tool_result()

    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools={TOOL: (VariantIntelInput, fake)})
    events: List[Dict[str, Any]] = []

    async def cb(ev):
        events.append(ev)

    res = await orch.execute_inquiry(
        "What is the prevalence of KRAS G12D in pancreatic cancer?", thread_id="t-var", enable_web_search=False, event_callback=cb
    )
    (kw,) = called(calls, TOOL)
    assert (kw["gene"], kw["protein_change"], kw["cancer_keyword"]) == ("KRAS", "G12D", "pancreatic")
    steps = [e["data"] for e in events if e["type"] == "thought" and e["data"]["id"] == "variant_intel"]
    assert [s["status"] for s in steps] == ["running", "done"]
    assert steps[0]["agent"] == "Synapta" and steps[-1]["title"] == "Looked up KRAS G12D in 4 of 4 variant databases"
    assert "Synapta" in res.selected_agents
    art = res.execution_artifacts["variant_intel"]
    assert art["cbioportal"][0]["mutated_samples"] == 49 and "citation_records" not in art
    plan = next(t for t in res.thoughts if t["id"] == "plan")
    assert "Variant: KRAS p.G12D" in plan["text"]
    assert "#### Variant & Biomarker Evidence" in res.response  # deterministic fallback (LLM disabled offline)
    defects = (res.verification_report or {}).get("defects") or []
    assert not [d for d in defects if d["category"] in ("NUMERICAL", "CLASSIFICATION", "IDENTIFIER")]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Analyze EGFR signaling", "Simulate BIOMD0000000010 over 60 seconds"])
async def test_orchestrator_skips_without_variant(monkeypatch, tmp_path, query):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools={TOOL: (VariantIntelInput, lambda kw: tool_result())})
    res = await orch.execute_inquiry(query, thread_id="t-novar", enable_web_search=False)
    assert called(calls, TOOL) == []
    assert "variant_intel" not in res.execution_artifacts


@pytest.mark.asyncio
async def test_orchestrator_without_tool_registered_is_unchanged(monkeypatch, tmp_path):
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path)
    res = await orch.execute_inquiry("Analyze KRAS G12D signaling", thread_id="t-noreg", enable_web_search=False)
    assert called(calls, TOOL) == [] and "variant_intel" not in res.execution_artifacts
    assert all(t["id"] != "variant_intel" for t in res.thoughts)


# -----------------------------------------------------------------------------
# 5. Verifier
# -----------------------------------------------------------------------------

def _verify(response: str, results: Optional[Dict[str, ToolResult]] = None, query: str = "KRAS G12D prevalence in pancreatic cancer"):
    import phronesis.extensions.variants  # noqa: F401  (registers the checks)

    results = results if results is not None else {"variant_intel": tool_result()}
    return ClinicalVerifier().verify_response(query, response, results)


def _cats(report) -> List[tuple]:
    return [(d.category, d.severity.value) for d in report.defects]


def test_verifier_accepts_returned_figures_and_ids():
    rep = _verify(
        "In KRAS G12D pancreatic cancer data, Synapta found KRAS G12D in 49/179 profiled samples (27.4%) in Pancreatic "
        "Adenocarcinoma (TCGA, PanCancer Atlas) and 889/2336 samples (38.1%) in the MSK 2024 cohort. ClinVar "
        "VCV000012582 (rs121913529) lists Pathogenic/Likely pathogenic germline and Oncogenic classifications. "
        "CIViC EID1300 is prognostic evidence. The gnomAD exome allele frequency is 2.7e-06 (4 of 1,460,562 alleles)."
    )
    assert rep.passed, rep.defects
    assert {"CLINVAR:VCV000012582", "RSID:rs121913529", "CIVIC:EID1300"} <= set(rep.verified_identifiers)


def test_verifier_flags_unreturned_ids():
    rep = _verify("KRAS G12D in pancreatic cancer: see CIViC EID9999 and dbSNP rs999123 for prevalence.")
    assert {d.claim for d in rep.defects if d.category == "IDENTIFIER" and d.severity.value == "CRITICAL"} == {"EID9999", "rs999123"}


@pytest.mark.parametrize(
    "sentence",
    [
        "KRAS G12D prevalence in pancreatic cancer cohorts is 35% of samples.",
        "Pooling the cohorts, KRAS G12D was found in 938/2515 pancreatic cancer samples.",
        "The gnomAD allele frequency of KRAS G12D in pancreatic cancer context is 4.1e-05.",
    ],
)
def test_verifier_flags_wrong_or_pooled_frequencies(sentence):
    rep = _verify(sentence)
    assert ("NUMERICAL", "CRITICAL") in _cats(rep) and not rep.passed


def test_pooled_figure_is_named_as_pooled():
    defects = [d for d in _verify("KRAS G12D was mutated in 938/2515 pancreatic cancer samples.").defects if d.category == "NUMERICAL"]
    assert "pooled" in defects[0].description


def test_verifier_hedged_sentence_and_literature_percent():
    assert _verify("KRAS G12D prevalence in pancreatic cancer is not 35% in any returned cohort.").passed
    lit = ToolResult(
        success=True,
        tool_name="lithoscribe_literature_synthesis",
        data={"ranked_passages": [{"pmid": "27010960", "passage_text": "KRAS G12D mutations were found in 41% of pancreatic cancer samples in this cohort."}]},
    )
    rep = _verify("KRAS G12D was found in 41% of pancreatic cancer samples in one literature cohort (PMID: 27010960).",
                  {"variant_intel": tool_result(), "lithoscribe": lit})
    assert not [d for d in rep.defects if d.category == "NUMERICAL"]


def test_verifier_civic_description_figure_passes():
    data = tool_result().data
    ev = next(e for e in data["civic"]["evidence"] if e["eid"] == "EID1300")
    assert "N=73/219" in ev["description"]
    rep = _verify("CIViC EID1300 reports KRAS G12D in 73/219 metastatic pancreatic cancer samples with poorer prognosis.")
    assert not [d for d in rep.defects if d.category == "NUMERICAL"]
    assert ("NUMERICAL", "CRITICAL") in _cats(_verify("CIViC EID1300 reports KRAS G12D in 74/219 pancreatic cancer samples."))


def test_verifier_pair_counts_must_share_a_document():
    lit = ToolResult(
        success=True,
        tool_name="lithoscribe_literature_synthesis",
        data={"ranked_passages": [
            {"passage_id": "p1", "passage_text": "KRAS G12D was detected in 41 tumours."},
            {"passage_id": "p2", "passage_text": "The validation cohort included 600 resected tumours."},
        ]},
    )
    rep = _verify("KRAS G12D was found in 41/600 pancreatic cancer samples.", {"variant_intel": tool_result(), "lithoscribe": lit})
    assert ("NUMERICAL", "CRITICAL") in _cats(rep)


def test_verifier_clinvar_classification_mislabel():
    braf = {"variant_intel": tool_result("tool_result_braf_v600e_melanoma.json")}
    q = "BRAF V600E in melanoma"
    rep = _verify("ClinVar classifies BRAF V600E in melanoma as pathogenic.", braf, query=q)
    assert ("CLASSIFICATION", "MAJOR") in _cats(rep)
    ok = _verify(
        "ClinVar lists BRAF V600E in melanoma with Conflicting classifications of pathogenicity (germline) and Oncogenic "
        "(oncogenicity), and a Tier I - Strong somatic clinical impact.", braf, query=q)
    assert not [d for d in ok.defects if d.category == "CLASSIFICATION"]


def test_checks_are_inert_without_variant_result():
    class V:
        tool_results: Dict[str, Any] = {}
        sentences = ["KRAS G12D prevalence is 35%."]
    assert check_variant_frequencies(V()) == [] and check_clinvar_classification(V()) == []


# -----------------------------------------------------------------------------
# 6. Evidence XML, memory
# -----------------------------------------------------------------------------

def test_evidence_block_renders_variant_xml():
    import phronesis.extensions.variants  # noqa: F401

    block = build_evidence_block({"variant_intel": tool_result()}, "")
    assert "<variant_intelligence " in block and 'gene="KRAS"' in block and 'hgvs_p="p.Gly12Asp"' in block
    assert '<cbioportal_prevalence mutated_samples="49" profiled_samples="179" percent="27.4"' in block
    assert '<clinvar_record accession="VCV000012582"' in block
    assert 'germline_classification="Pathogenic/Likely pathogenic"' in block and 'oncogenicity_classification="Oncogenic"' in block
    assert "<gnomad_frequency " in block and "not tumour prevalence" in block
    assert '<source_status name="gnomad" status="ok"' in block
    assert block.index("<variant_intelligence") < block.index("<biodiscovery_ranking>")


def test_evidence_xml_escapes_values():
    data = tool_result("tool_result_braf_v600e_melanoma.json").data
    real = next(e for e in data["civic"]["evidence"] if "<" in e["description"])
    xml = render_variant_evidence(data)
    snippet = real["description"][real["description"].index("<") - 1: real["description"].index("<") + 5]
    assert snippet.replace("<", "&lt;") in xml and snippet not in xml
    hostile = copy.deepcopy(data)
    hostile["civic"]["evidence"][0]["description"] = 'R&D <script>"x"</script>'
    hostile["cbioportal"][0]["study_name"] = 'A "quoted" & <tagged> study'
    xml2 = render_variant_evidence(hostile)
    assert "R&amp;D &lt;script&gt;" in xml2 and 'study="A &quot;quoted&quot; &amp; &lt;tagged&gt; study"' in xml2
    assert real["eid"] in xml


def test_describe_failure_and_markdown_for_partial_result():
    data = tool_result().data
    partial = copy.deepcopy(data)
    partial["gnomad"] = None
    partial["sources"][-1] = {"name": "gnomad", "status": "failed", "detail": "rate limited", "elapsed_seconds": 6.1}
    title, detail = describe_variant_result(partial, "KRAS G12D")
    assert title == "Looked up KRAS G12D in 3 of 4 variant databases" and detail.endswith("gnomAD: rate limited")
    assert "- gnomAD failed: rate limited" in variant_markdown_summary(partial)
    assert compact_artifact(partial)["gnomad"] is None


def test_memory_collects_variant_identifiers():
    ids = ClinicalMemoryEngine.collect_identifiers(tool_result().data, tool_key="variant_intel")
    assert ids["clinvar"] == ["VCV000012582"] and ids["rsid"] == ["rs121913529"] and "EID1300" in ids["civic"]
