"""
synapta_variant_intelligence: three concurrent chains, each with its own budget so partial data survives
the registry's 30 s timeout.

  ClinVar esearch -> esummary -> gnomAD (GRCh38 SPDI)   14 s
  CIViC gene -> variant -> accepted evidence + assertions 12 s
  cBioPortal gene -> study selection -> per-study prevalence 18 s

cBioPortal prevalence is reported per study only (cohorts overlap, so figures are never pooled).
gnomAD is germline population frequency, never tumour prevalence.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from . import clients
from .clients import CbioPortalClient, CivicClient, ClinVarClient, GnomadClient, UpstreamError, parse_clinvar_record, title_protein_change
from .models import (
    CbioStudyPrevalence,
    CivicAssertion,
    CivicEvidence,
    CivicSummary,
    ClinVarRecord,
    GnomadFrequency,
    GnomadPopulation,
    SourceStatus,
    VariantIntelResult,
    review_stars,
)
from .parse import AA3_TO_1, CANCER_KEYWORDS, disease_matches_keyword, extract_cancer_keyword, protein_change_3letter

TOOL_NAME = "synapta_variant_intelligence"
BUDGETS: Dict[str, float] = {"clinvar": 14.0, "civic": 12.0, "cbioportal": 18.0}
DEFAULT_PAN_CANCER_STUDY = "msk_impact_2017"
_EXCLUDED_STATUSES = {"UNCALLED", "GERMLINE"}
_NON_TISSUE_RE = re.compile(r"cell[\s\-_]?lines?|\bccle\b|ccle_|cellline|nci60|xenograft|\bpdx\b|_pdx|pdx_|cfdna|ctdna|ctdx|cell[\s\-]free|organoid|\bpdo\b|_pdo_|\bpptc\b", re.I)
_CIVIC_DESCRIPTION_CHARS = 600


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------

def explain_spdi(spdi: Optional[str]) -> Tuple[Optional[str], str]:
    """(gnomAD variant id, '') for a GRCh38 SNV SPDI, else (None, reason). SPDI positions are 0-based."""
    m = re.fullmatch(r"NC_0000(\d{2})\.\d+:(\d+):([ACGTN]*):([ACGTN]*)", str(spdi or ""))
    if not m:
        return None, "no GRCh38 chromosome SPDI from ClinVar"
    chrom_num, pos, ref, alt = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    if not 1 <= chrom_num <= 24:
        return None, "not a nuclear chromosome"
    if len(ref) != 1 or len(alt) != 1:
        return None, "not a single-nucleotide variant; gnomAD lookup skipped"
    chrom = {23: "X", 24: "Y"}.get(chrom_num, str(chrom_num))
    return f"{chrom}-{pos + 1}-{ref}-{alt}", ""


def spdi_to_gnomad_id(spdi: Optional[str]) -> Optional[str]:
    return explain_spdi(spdi)[0]


def _cancer_profile(keyword: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    key = keyword.strip().lower()
    canonical = key if key in CANCER_KEYWORDS else extract_cancer_keyword(key)
    if canonical:
        _pattern, terms, ids = CANCER_KEYWORDS[canonical]
        return terms, ids
    return (key,), ()


def select_studies(studies: List[Dict[str, Any]], cancer_keyword: Optional[str], max_studies: int = 4) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Pick cBioPortal studies for a cancer keyword: tissue cohorts only (no cell lines, PDX, organoids or cfDNA),
    one version of each TCGA cohort (PanCancer Atlas preferred, listed first), then the largest by sequenced
    samples. Returns (studies, cancer filter used); with no keyword or no match it is the pan-cancer default.
    """
    tissue = [
        s for s in studies
        if (s.get("sequencedSampleCount") or 0) > 0 and not _NON_TISSUE_RE.search(f"{s.get('studyId', '')} {s.get('name', '')}")
    ]
    matched: List[Dict[str, Any]] = []
    if cancer_keyword and cancer_keyword.strip():
        terms, ids = _cancer_profile(cancer_keyword)
        # Word-start matching: 'glioma' must not match paraganglioma, 'renal' adrenal, 'bladder' gallbladder.
        term_re = re.compile("|".join(rf"\b{re.escape(t)}" for t in terms))
        for s in tissue:
            ct = s.get("cancerType") or {}
            blob = " ".join(str(x or "") for x in (s.get("name"), ct.get("name"), ct.get("parent"))).lower()
            if s.get("cancerTypeId") in ids or term_re.search(blob):
                matched.append(s)
    if matched:
        tcga_best: Dict[str, Dict[str, Any]] = {}
        others: List[Dict[str, Any]] = []
        for s in matched:
            if "tcga" not in str(s.get("studyId")):
                others.append(s)
                continue
            # One version per TCGA cohort code (paad_tcga, paad_tcga_gdc, paad_tcga_pan_can_atlas_2018), not per
            # cancerTypeId: GBM and LGG share 'difg', SARC and PCPG share 'soft_tissue'.
            ct_id = re.split(r"_?tcga", str(s["studyId"]), maxsplit=1)[0] or str(s.get("cancerTypeId"))
            cur = tcga_best.get(ct_id)
            rank = ("pan_can_atlas" in s["studyId"], s.get("sequencedSampleCount") or 0)
            if cur is None or rank > ("pan_can_atlas" in cur["studyId"], cur.get("sequencedSampleCount") or 0):
                tcga_best[ct_id] = s
        size = lambda s: -(s.get("sequencedSampleCount") or 0)  # noqa: E731
        pancan = sorted((s for s in tcga_best.values() if "pan_can_atlas" in s["studyId"]), key=size)
        rest = sorted([s for s in tcga_best.values() if "pan_can_atlas" not in s["studyId"]] + others, key=size)
        return (pancan + rest)[:max_studies], cancer_keyword.strip()
    default = [s for s in studies if s.get("studyId") == DEFAULT_PAN_CANCER_STUDY]
    return default[:1], None


def compute_prevalence(
    mutations: Iterable[Dict[str, Any]],
    panel_rows: Iterable[Dict[str, Any]],
    panel_genes: Dict[str, Set[str]],
    *,
    gene: str,
    protein_change: str,
    study: Optional[Dict[str, Any]] = None,
) -> CbioStudyPrevalence:
    """
    Per-study prevalence: distinct profiled samples carrying proteinChange == protein_change (UNCALLED and
    GERMLINE calls excluded) over samples profiled for the gene. A sample counts as profiled when it was
    whole-exome/genome sequenced (no genePanelId) or its panel contains the gene. Matching is on the protein
    change, not genomic position, because cBioPortal coordinates are mostly GRCh37.
    """
    study = study or {}
    profiled: Set[str] = set()
    profiled_patient_ids: Set[str] = set()
    patient_ids_complete = True
    panels: Counter = Counter()
    wes = targeted = 0
    for row in panel_rows:
        if not row.get("profiled", True):
            continue
        pid = row.get("genePanelId")
        if pid and gene not in panel_genes.get(pid, set()):
            continue
        sid = str(row.get("sampleId"))
        if sid in profiled:
            continue
        profiled.add(sid)
        if row.get("patientId"):
            profiled_patient_ids.add(str(row["patientId"]))
        else:
            patient_ids_complete = False
        panels[pid or "WES"] += 1
        if pid:
            targeted += 1
        else:
            wes += 1

    calls = [
        m for m in mutations
        if str(m.get("mutationStatus") or "").upper() not in _EXCLUDED_STATUSES and str(m.get("sampleId")) in profiled
    ]
    hits = [m for m in calls if m.get("proteinChange") == protein_change]
    mutated = {str(m.get("sampleId")) for m in hits}
    patients = {str(m.get("patientId")) for m in hits if m.get("patientId")}
    gene_mutated = {str(m.get("sampleId")) for m in calls}

    if targeted and wes:
        basis = f"whole-exome samples plus samples on gene panels containing {gene}"
    elif targeted:
        basis = f"samples on gene panels containing {gene}"
    else:
        basis = "all sequenced samples (whole-exome)"
    n = len(profiled)
    study_id = str(study.get("studyId") or "")
    return CbioStudyPrevalence(
        study_id=study_id,
        study_name=str(study.get("name") or study_id),
        cancer_type=((study.get("cancerType") or {}).get("name")) or None,
        reference_genome=study.get("referenceGenome") or None,
        mutated_samples=len(mutated),
        mutated_patients=len(patients),
        gene_mutated_samples=len(gene_mutated),
        profiled_samples=n,
        # Patients are counted only when every profiled sample names its patient; otherwise unknown (None).
        profiled_patients=len(profiled_patient_ids) if (n and patient_ids_complete) else (0 if not n else None),
        prevalence_percent=round(100.0 * len(mutated) / n, 1) if n else None,
        denominator_basis=basis,
        panels=dict(panels),
        study_import_date=str(study.get("importDate")) if study.get("importDate") else None,
        url=f"https://www.cbioportal.org/study/summary?id={study_id}",
    )


def parse_civic_evidence(node: Dict[str, Any]) -> CivicEvidence:
    src = node.get("source") or {}
    citation_id = str(src.get("citationId") or "")
    source_type = str(src.get("sourceType") or "")
    year = src.get("publicationYear")
    eid_num = node.get("id")
    desc = str(node.get("description") or "")
    if len(desc) > _CIVIC_DESCRIPTION_CHARS:
        desc = desc[: _CIVIC_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return CivicEvidence(
        eid=str(node.get("name") or f"EID{eid_num}"),
        evidence_level=str(node.get("evidenceLevel") or ""),
        evidence_type=str(node.get("evidenceType") or ""),
        evidence_direction=str(node.get("evidenceDirection") or ""),
        significance=str(node.get("significance") or ""),
        evidence_rating=node.get("evidenceRating"),
        variant_origin=node.get("variantOrigin"),
        disease=(node.get("disease") or {}).get("name"),
        doid=(node.get("disease") or {}).get("doid"),
        therapies=[str(t.get("name")) for t in node.get("therapies") or [] if t.get("name")],
        therapy_interaction_type=node.get("therapyInteractionType"),
        source_type=source_type,
        citation_id=citation_id,
        pmid=citation_id if source_type == "PUBMED" and citation_id.isdigit() else None,
        citation=src.get("citation"),
        publication_year=int(year) if isinstance(year, (int, float)) or str(year or "").isdigit() else None,
        description=desc,
        url=f"https://civicdb.org/evidence/{eid_num}",
    )


def parse_civic_assertion(node: Dict[str, Any]) -> CivicAssertion:
    aid_num = node.get("id")
    return CivicAssertion(
        aid=str(node.get("name") or f"AID{aid_num}"),
        amp_level=node.get("ampLevel"),
        assertion_type=str(node.get("assertionType") or ""),
        assertion_direction=str(node.get("assertionDirection") or ""),
        significance=str(node.get("significance") or ""),
        disease=(node.get("disease") or {}).get("name"),
        therapies=[str(t.get("name")) for t in node.get("therapies") or [] if t.get("name")],
        fda_companion_test=node.get("fdaCompanionTest"),
        regulatory_approval=node.get("regulatoryApproval"),
        nccn_guideline=(node.get("nccnGuideline") or {}).get("name"),
        status=str(node.get("status") or "ACCEPTED"),
        url=f"https://civicdb.org/assertions/{aid_num}",
    )


def parse_gnomad(doc: Dict[str, Any], dataset: str = "gnomad_r4") -> GnomadFrequency:
    exome = doc.get("exome") or {}
    genome = doc.get("genome") or {}
    exome_filters = [str(f) for f in exome.get("filters") or [] if f]
    genome_filters = [str(f) for f in genome.get("filters") or [] if f]
    faf = exome.get("faf95") or {}
    pops = []
    for p in exome.get("populations") or []:
        pid = str(p.get("id") or "")
        if not pid or "_" in pid or pid in ("XX", "XY"):
            continue
        ac, an = int(p.get("ac") or 0), int(p.get("an") or 0)
        pops.append(GnomadPopulation(id=pid, ac=ac, an=an, af=(ac / an) if an else None))
    tcs = doc.get("transcript_consequences") or []
    canonical = next((t for t in tcs if t.get("is_canonical")), tcs[0] if tcs else {})
    vid = str(doc.get("variant_id") or "")
    return GnomadFrequency(
        variant_id=vid,
        dataset=dataset,
        rsids=[str(r) for r in doc.get("rsids") or []],
        caid=doc.get("caid"),
        exome_ac=exome.get("ac"),
        exome_an=exome.get("an"),
        exome_af=exome.get("af"),
        exome_homozygotes=exome.get("homozygote_count"),
        genome_ac=genome.get("ac"),
        genome_an=genome.get("an"),
        genome_af=genome.get("af"),
        faf95_popmax=faf.get("popmax"),
        faf95_popmax_population=faf.get("popmax_population"),
        populations=pops,
        filters=sorted(set(exome_filters) | set(genome_filters)),
        # Per dataset, so the QC status of each frequency is known (DAT-005); flags are variant-level annotations.
        exome_filters=exome_filters,
        genome_filters=genome_filters,
        flags=[str(f) for f in doc.get("flags") or [] if f],
        hgvsp=canonical.get("hgvsp"),
        hgvsc=canonical.get("hgvsc"),
        transcript_id=canonical.get("transcript_id"),
        url=f"https://gnomad.broadinstitute.org/variant/{vid}?dataset={dataset}",
    )


# -----------------------------------------------------------------------------
# Chains
# -----------------------------------------------------------------------------

class _Run:
    """Mutable per-call state the chains fill in, so a chain timeout keeps what already finished."""

    def __init__(self, gene: str, protein_change: Optional[str]) -> None:
        self.gene = gene
        self.protein_change = protein_change
        self.status: Dict[str, SourceStatus] = {}
        self.started: Dict[str, float] = {}
        self.clinvar: List[ClinVarRecord] = []
        self.civic: Optional[CivicSummary] = None
        self.cbio: List[CbioStudyPrevalence] = []
        self.gnomad: Optional[GnomadFrequency] = None
        self.cancer_filter: Optional[str] = None
        self.resolved = asyncio.Event()  # set once the protein change is known (or known to be unknowable)
        if protein_change:
            self.resolved.set()

    def start(self, name: str) -> None:
        self.started.setdefault(name, time.perf_counter())

    def done(self, name: str, status: str, detail: str = "") -> None:
        if name in self.status:
            return
        t0 = self.started.get(name, time.perf_counter())
        self.status[name] = SourceStatus(name=name, status=status, detail=detail, elapsed_seconds=round(time.perf_counter() - t0, 2))

    def fail_pending(self, names: Iterable[str], detail: str) -> None:
        for n in names:
            self.done(n, "failed", detail)


def _clinvar_term(gene: str, protein_change: Optional[str], rsid: Optional[str], hgvs_c: Optional[str]) -> str:
    if hgvs_c:
        return f'"{hgvs_c}"' if hgvs_c.startswith("NM_") else f'{gene}[gene] AND "{hgvs_c}"[variant name]'
    if protein_change:
        three = protein_change_3letter(protein_change)
        if three:
            return f'{gene}[gene] AND "{three}"[variant name]'
        return f"{gene}[gene] AND {protein_change}[All Fields]"
    return f"{rsid}"


def _record_matches(rec: ClinVarRecord, gene: str, protein_change: Optional[str], rsid: Optional[str], vcv: Optional[str]) -> bool:
    if vcv:
        return rec.accession.split(".")[0] == vcv
    if rec.genes and gene not in rec.genes:
        return False
    if protein_change and protein_change not in rec.protein_change:
        return False
    if rsid and rsid not in rec.rsids:
        return False
    return True


_TITLE_SNV_RE = re.compile(r"\):c\.[0-9+\-*]+[ACGT]>[ACGT](?:\s|$)")


def _review_stars(status: Optional[str]) -> int:
    return review_stars(status) or 0


def clinvar_record_rank(rec: ClinVarRecord, protein_change: Optional[str]) -> Tuple[int, int, int]:
    """Sort key, best first: the single-nucleotide variant carrying the queried protein change (KRAS G12C is
    c.34G>T, rs121913530, not the c.33_34delinsCT record), then the best review status, then the oldest VCV."""
    snv = bool(_TITLE_SNV_RE.search(rec.title or "")) and (not protein_change or protein_change in rec.protein_change)
    stars = max(_review_stars(rec.germline_review_status), _review_stars(rec.oncogenicity_review_status),
                _review_stars(rec.somatic_clinical_impact_review_status))
    digits = re.sub(r"\D", "", rec.accession.split(".")[0]) or re.sub(r"\D", "", rec.variation_id or "")
    return (0 if snv else 1, -stars, int(digits) if digits else 10**12)


def _protein_change_from_record(rec: ClinVarRecord) -> Optional[str]:
    three = title_protein_change(rec.title)
    m = re.fullmatch(r"([A-Z][a-z]{2})(\d{1,5})([A-Z][a-z]{2})", three or "")
    if not m or m.group(1) not in AA3_TO_1 or m.group(3) not in AA3_TO_1:
        return None
    one = f"{AA3_TO_1[m.group(1)]}{m.group(2)}{AA3_TO_1[m.group(3)]}"
    return one if not rec.protein_change or one in rec.protein_change else None


async def _clinvar_chain(run: _Run, http: httpx.AsyncClient, *, rsid: Optional[str], hgvs_c: Optional[str], vcv: Optional[str], include_gnomad: bool) -> None:
    run.start("clinvar")
    run.start("gnomad")
    try:
        cv = ClinVarClient(http)
        if vcv:
            ids = [str(int(vcv[3:]))]
        else:
            ids = await cv.esearch(_clinvar_term(run.gene, run.protein_change, rsid, hgvs_c))
        docs = await cv.esummary(ids[:20])
        records = [r for r in (parse_clinvar_record(d) for d in docs) if r is not None]
        records = [r for r in records if _record_matches(r, run.gene, run.protein_change, rsid, vcv)]
        # Best record first: the reported rsid and the record order follow it.
        records = sorted(records, key=lambda r: clinvar_record_rank(r, run.protein_change))[:5]
        run.clinvar = records
        if records:
            run.done("clinvar", "ok", f"{len(records)} ClinVar record{'s' if len(records) != 1 else ''}")
        else:
            label = run.protein_change or hgvs_c or rsid or vcv
            extra = f" ({len(ids)} search hits, none for {label})" if ids else ""
            run.done("clinvar", "no_match", f"no ClinVar record for {run.gene} {label}{extra}")
        if not run.protein_change and len(records) == 1:
            run.protein_change = _protein_change_from_record(records[0])
    except UpstreamError as exc:
        run.done("clinvar", "failed", exc.detail)
    finally:
        run.resolved.set()

    if not include_gnomad:
        run.done("gnomad", "skipped", "gnomAD lookup not requested")
        return
    if not run.clinvar:
        run.done("gnomad", "skipped", "no ClinVar record to derive GRCh38 coordinates")
        return
    if len(run.clinvar) > 1:
        run.done("gnomad", "skipped", "several ClinVar alleles matched; gnomAD needs a single allele")
        return
    variant_id, reason = explain_spdi(run.clinvar[0].canonical_spdi)
    if not variant_id:
        run.done("gnomad", "skipped", reason)
        return
    try:
        doc = await GnomadClient(http).variant(variant_id)
    except UpstreamError as exc:
        run.done("gnomad", "failed", exc.detail)
        return
    if doc is None:
        run.done("gnomad", "no_match", f"{variant_id} not found in gnomAD v4")
        return
    run.gnomad = parse_gnomad(doc)
    run.done("gnomad", "ok", f"gnomAD v4 {variant_id}")


CIVIC_FETCH = 50  # evidence items fetched (rating order) before ranking the question's disease first


def rank_civic_evidence(nodes: List[Dict[str, Any]], cancer_keyword: Optional[str]) -> List[Tuple[Dict[str, Any], Optional[bool]]]:
    """(node, matches_context): items whose disease matches the question's cancer type first, each group keeping
    CIViC's rating order; matches_context is None when no cancer type was named."""
    if not cancer_keyword:
        return [(n, None) for n in nodes]
    flagged = [(n, disease_matches_keyword((n.get("disease") or {}).get("name"), cancer_keyword)) for n in nodes]
    return [x for x in flagged if x[1]] + [x for x in flagged if not x[1]]


async def _civic_chain(run: _Run, http: httpx.AsyncClient, *, max_items: int, cancer_keyword: Optional[str] = None) -> None:
    run.start("civic")
    await run.resolved.wait()
    if not run.protein_change:
        run.done("civic", "skipped", "no protein change to match a CIViC variant")
        return
    civic = CivicClient(http)
    try:
        gene_id = await civic.gene_id(run.gene)
        if not gene_id:
            run.done("civic", "no_match", f"{run.gene} is not curated in CIViC")
            return
        nodes = [n for n in await civic.variants(gene_id, run.protein_change) if n.get("name") == run.protein_change]
        node = nodes[0] if nodes else None
        mp_id = (node or {}).get("singleVariantMolecularProfileId")
        if not node or not mp_id:
            run.done("civic", "no_match", f"no curated CIViC variant named {run.gene} {run.protein_change}")
            return
        (ev_total, ev_nodes), (as_total, as_nodes) = await asyncio.gather(
            civic.evidence(int(mp_id), CIVIC_FETCH), civic.assertions(int(mp_id))
        )
    except UpstreamError as exc:
        run.done("civic", "failed", exc.detail)
        return
    ranked = rank_civic_evidence(ev_nodes, cancer_keyword)
    evidence = []
    for n, matches in ranked[:max_items]:
        ev = parse_civic_evidence(n)
        ev.matches_context = matches
        evidence.append(ev)
    run.civic = CivicSummary(
        variant_id=int(node["id"]),
        molecular_profile_id=int(mp_id),
        molecular_profile_name=f"{run.gene} {node.get('name')}",
        allele_registry_id=node.get("alleleRegistryId"),
        clinvar_ids=[str(x) for x in node.get("clinvarIds") or []],
        accepted_evidence_total=ev_total,
        accepted_assertion_total=as_total,
        evidence=evidence,
        assertions=[parse_civic_assertion(n) for n in as_nodes if str(n.get("status") or "ACCEPTED") == "ACCEPTED"],
        url=f"https://civicdb.org/molecular-profiles/{mp_id}/summary",
        context_disease=cancer_keyword or None,
        context_matched=sum(1 for _, m in ranked if m) if cancer_keyword else None,
    )
    run.done("civic", "ok", f"{ev_total} accepted evidence item{'s' if ev_total != 1 else ''}, {as_total} accepted assertion{'s' if as_total != 1 else ''}")


async def _study_prevalence(cbio: CbioPortalClient, study: Dict[str, Any], gene: Dict[str, Any], protein_change: str) -> CbioStudyPrevalence:
    sid = str(study["studyId"])
    mutations, panel_rows = await asyncio.gather(cbio.mutations(sid, int(gene["entrezGeneId"])), cbio.gene_panel_data(sid))
    panel_ids = sorted({str(r["genePanelId"]) for r in panel_rows if r.get("genePanelId")})
    gene_sets = await asyncio.gather(*(cbio.gene_panel_genes(p) for p in panel_ids))
    return compute_prevalence(
        mutations, panel_rows, dict(zip(panel_ids, gene_sets)),
        gene=str(gene.get("hugoGeneSymbol") or ""), protein_change=protein_change, study=study,
    )


async def _cbio_chain(run: _Run, http: httpx.AsyncClient, *, cancer_keyword: Optional[str], max_studies: int) -> None:
    run.start("cbioportal")
    await run.resolved.wait()
    if not run.protein_change:
        run.done("cbioportal", "skipped", "no protein change to match cBioPortal mutation calls")
        return
    cbio = CbioPortalClient(http)
    try:
        gene = await cbio.gene(run.gene)
        if not gene or not gene.get("entrezGeneId"):
            run.done("cbioportal", "no_match", f"{run.gene} is not a cBioPortal gene symbol")
            return
        studies, used_filter = select_studies(await cbio.studies(), cancer_keyword, max_studies)
    except UpstreamError as exc:
        run.done("cbioportal", "failed", exc.detail)
        return
    run.cancer_filter = used_filter
    if not studies:
        run.done("cbioportal", "no_match", "no cBioPortal study available")
        return
    outcomes = await asyncio.gather(*(_study_prevalence(cbio, s, gene, run.protein_change) for s in studies), return_exceptions=True)
    failed = [s["studyId"] for s, o in zip(studies, outcomes) if isinstance(o, BaseException)]
    run.cbio = [o for o in outcomes if isinstance(o, CbioStudyPrevalence)]
    note = ""
    if cancer_keyword and used_filter is None:
        note = f"no tissue study matched '{cancer_keyword}'; pan-cancer default used"
    if failed:
        note = "; ".join(x for x in (note, f"failed: {', '.join(failed)}") if x)
    if run.cbio:
        n = len(run.cbio)
        run.done("cbioportal", "ok", "; ".join(x for x in (f"{n} stud{'ies' if n != 1 else 'y'}", note) if x))
    else:
        first = next((o for o in outcomes if isinstance(o, BaseException)), None)
        detail = (getattr(first, "detail", None) or type(first).__name__) if first is not None else "no study data"
        run.done("cbioportal", "failed", detail)


async def _bounded(run: _Run, names: Tuple[str, ...], budget: float, coro: Any) -> None:
    try:
        await asyncio.wait_for(coro, timeout=budget)
    except asyncio.TimeoutError:
        run.fail_pending(names, f"timeout after {budget:g} s")
    except UpstreamError as exc:
        run.fail_pending(names, exc.detail)
    except httpx.HTTPError as exc:
        run.fail_pending(names, f"network error: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        run.fail_pending(names, f"internal error: {type(exc).__name__}")
    finally:
        if "clinvar" in names:
            run.resolved.set()


# -----------------------------------------------------------------------------
# Tool entry point
# -----------------------------------------------------------------------------

def _citation_records(result: VariantIntelResult) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in result.clinvar:
        out.append({"type": "clinvar", "id": rec.accession, "title": rec.title, "url": rec.url,
                    "passage": "; ".join(x for x in (
                        rec.germline_classification and f"Germline: {rec.germline_classification} ({rec.germline_review_status})",
                        rec.oncogenicity_classification and f"Oncogenicity: {rec.oncogenicity_classification}",
                        rec.somatic_clinical_impact and f"Somatic clinical impact: {rec.somatic_clinical_impact}",
                    ) if x) or None,
                    "source_agent": "Synapta", "source_tool": TOOL_NAME})
        for rs in rec.rsids:
            out.append({"type": "rsid", "id": rs, "title": f"dbSNP {rs}", "url": f"https://www.ncbi.nlm.nih.gov/snp/{rs}",
                        "source_agent": "Synapta", "source_tool": TOOL_NAME})
    if result.civic:
        for ev in result.civic.evidence:
            out.append({"type": "civic", "id": ev.eid, "title": f"CIViC {ev.eid}: {ev.evidence_type} evidence, level {ev.evidence_level}",
                        "url": ev.url, "passage": ev.description or None, "evidence_level": ev.evidence_level,
                        "year": str(ev.publication_year) if ev.publication_year else None,
                        "source_agent": "Synapta", "source_tool": TOOL_NAME})
        for a in result.civic.assertions:
            out.append({"type": "civic", "id": a.aid, "title": f"CIViC {a.aid}: {a.assertion_type} assertion", "url": a.url,
                        "evidence_level": a.amp_level, "source_agent": "Synapta", "source_tool": TOOL_NAME})
    return out


_TITLE_C_RE = re.compile(r"^(NM_\d{4,9}(?:\.\d{1,3})?\([A-Z0-9\-]{1,15}\):c\.[^\s(]+)")


def consistent_hgvs_c(clinvar: List[ClinVarRecord], gnomad: Optional[GnomadFrequency], hgvs_p: Optional[str]) -> Optional[str]:
    """
    A cDNA change numbered on the same transcript as hgvs_p: the single ClinVar record's title
    ('NM_004333.6(BRAF):c.1799T>A (p.Val600Glu)'), else gnomAD's canonical transcript only when its hgvsp agrees.
    gnomAD's MANE transcript can differ (BRAF c.1919T>A is p.Val640Glu), so it is never paired with another numbering.
    """
    if len(clinvar) == 1:
        m = _TITLE_C_RE.match(clinvar[0].title or "")
        if m:
            return m.group(1)
    if gnomad and gnomad.hgvsc and (hgvs_p is None or gnomad.hgvsp == hgvs_p):
        return gnomad.hgvsc
    return None


def _canonical_ids(result: VariantIntelResult) -> Dict[str, str]:
    ids: Dict[str, str] = {}
    vcv = [r.accession for r in result.clinvar]
    rsids = sorted({rs for r in result.clinvar for rs in r.rsids} | set(result.gnomad.rsids if result.gnomad else []))
    civic_ids = ([e.eid for e in result.civic.evidence] + [a.aid for a in result.civic.assertions]) if result.civic else []
    pmids = sorted({e.pmid for e in result.civic.evidence if e.pmid}) if result.civic else []
    for key, values in (("clinvar_vcv", vcv), ("rsids", rsids), ("civic_ids", civic_ids), ("pmids", pmids)):
        if values:
            ids[key] = ",".join(values)
    return ids


def source_releases(result: VariantIntelResult) -> Dict[str, Any]:
    """Data-release provenance per database, as far as the calls made expose it (DAT-010). The retrieval time is
    stamped on every ToolResult by the registry; no extra version request is made here (ClinVar esummary and the
    CIViC queries carry no release, and an einfo/version probe would add a rate-limited request to every lookup)."""
    return {
        "gnomad": result.gnomad.dataset if result.gnomad else "gnomad_r4",
        "cbioportal": {st.study_id: st.study_import_date for st in result.cbioportal} or None,
        "clinvar": "release not exposed by esummary; per-record last-evaluated dates are in the records",
        "civic": "live CIViC GraphQL (no release identifier exposed by the queries used)",
    }


async def run_variant_intelligence(
    gene: str,
    protein_change: Optional[str] = None,
    rsid: Optional[str] = None,
    cancer_keyword: Optional[str] = None,
    max_studies: int = 4,
    max_civic_items: int = 10,
    include_gnomad: bool = True,
    hgvs_c: Optional[str] = None,
    clinvar_vcv: Optional[str] = None,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    budgets: Optional[Dict[str, float]] = None,
) -> Any:
    """Registry executor for synapta_variant_intelligence; returns a ToolResult."""
    from ..tools.registry import ErrorType, ToolResult

    t0 = time.perf_counter()
    budget = {**BUDGETS, **(budgets or {})}
    run = _Run(gene, protein_change)
    async with clients.new_http_client(transport) as http:
        await asyncio.gather(
            _bounded(run, ("clinvar", "gnomad"), budget["clinvar"],
                     _clinvar_chain(run, http, rsid=rsid, hgvs_c=hgvs_c, vcv=clinvar_vcv, include_gnomad=include_gnomad)),
            _bounded(run, ("civic",), budget["civic"], _civic_chain(run, http, max_items=max_civic_items, cancer_keyword=cancer_keyword)),
            _bounded(run, ("cbioportal",), budget["cbioportal"], _cbio_chain(run, http, cancer_keyword=cancer_keyword, max_studies=max_studies)),
        )

    # The queried protein change names the variant; gnomAD's canonical (MANE) transcript can number it
    # differently (BRAF V600E is p.Val640Glu on ENST00000646891), so its hgvsp stays under data.gnomad.
    hgvs_p = None
    if run.protein_change and protein_change_3letter(run.protein_change):
        hgvs_p = f"p.{protein_change_3letter(run.protein_change)}"
    elif run.gnomad and run.gnomad.hgvsp:
        hgvs_p = run.gnomad.hgvsp
    result = VariantIntelResult(
        gene=gene,
        protein_change=run.protein_change,
        hgvs_p=hgvs_p,
        hgvs_c=hgvs_c or consistent_hgvs_c(run.clinvar, run.gnomad, hgvs_p),
        rsid=rsid or next((rs for r in run.clinvar for rs in r.rsids), None),
        cancer_filter=run.cancer_filter,
        clinvar=run.clinvar,
        civic=run.civic,
        cbioportal=run.cbio,
        gnomad=run.gnomad,
        sources=[run.status[n] for n in ("clinvar", "civic", "cbioportal", "gnomad") if n in run.status],
    )
    data = result.model_dump()
    data["citation_records"] = _citation_records(result)
    elapsed = time.perf_counter() - t0
    statuses = [s.status for s in result.sources]
    meta = {"adapter": "DIRECT_REST", "sources": [s.model_dump() for s in result.sources],
            "source_releases": source_releases(result)}
    if "ok" in statuses:
        return ToolResult(success=True, tool_name=TOOL_NAME, data=data, canonical_ids=_canonical_ids(result),
                          execution_time_seconds=elapsed, metadata=meta)
    summary = "; ".join(f"{s.name}: {s.status}{' (' + s.detail + ')' if s.detail else ''}" for s in result.sources)
    error_type = ErrorType.UPSTREAM_API_ERROR if "failed" in statuses else ErrorType.NOT_FOUND
    return ToolResult(success=False, tool_name=TOOL_NAME, data=data, error=f"No variant database returned data for {result.display}: {summary}",
                      error_type=error_type, execution_time_seconds=elapsed, metadata=meta)
