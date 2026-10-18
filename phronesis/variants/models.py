"""Pydantic models for Synapta variant intelligence (ClinVar, CIViC, cBioPortal, gnomAD)."""

from __future__ import annotations

import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AA1 = r"[ACDEFGHIKLMNPQRSTVWY]"
PROTEIN_CHANGE_PATTERN = rf"^{AA1}\d{{1,5}}(?:{AA1}|\*|fs\*?\d*|del|dup)$"
HGVS_C_PATTERN = r"^(?:NM_\d{4,9}(?:\.\d{1,3})?\([A-Z0-9\-]{1,15}\):)?c\.[0-9_+\-*]{1,24}(?:[ACGT]>[ACGT]|del[ACGT]*|dup[ACGT]*|ins[ACGT]+|delins[ACGT]+)$"

# ClinVar review status -> star rating (https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/). Order matters:
# the first phrase found in the status wins ("criteria provided, conflicting classifications" is one star).
_REVIEW_STARS = (
    ("practice guideline", 4),
    ("expert panel", 3),
    ("multiple submitters, no conflicts", 2),
    ("multiple submitters", 2),
    ("conflicting", 1),
    ("single submitter", 1),
)


def review_stars(status: Optional[str]) -> Optional[int]:
    """ClinVar gold-star rating of a review status (0-4); None when no review status was given. 'no assertion
    criteria provided', 'no classification provided' and similar statuses are 0 stars."""
    low = re.sub(r"\s+", " ", str(status or "")).strip().lower()
    if not low:
        return None
    return next((stars for phrase, stars in _REVIEW_STARS if phrase in low), 0)


SourceName = Literal["clinvar", "civic", "cbioportal", "gnomad"]
SourceState = Literal["ok", "no_match", "failed", "skipped"]


class VariantIntelInput(BaseModel):
    gene: str = Field(..., pattern=r"^[A-Z0-9][A-Z0-9\-]{0,14}$")  # HGNC symbol, alias-normalized (HER2 -> ERBB2)
    protein_change: Optional[str] = Field(None, pattern=PROTEIN_CHANGE_PATTERN)  # 1-letter, e.g. 'G12D'
    rsid: Optional[str] = Field(None, pattern=r"^rs\d{1,12}$")
    cancer_keyword: Optional[str] = Field(None, max_length=60)
    max_studies: int = Field(4, ge=1, le=8)
    max_civic_items: int = Field(10, ge=1, le=25)
    include_gnomad: bool = True
    hgvs_c: Optional[str] = Field(None, max_length=80, pattern=HGVS_C_PATTERN)  # 'NM_004985.5(KRAS):c.35G>A' or 'c.35G>A'
    clinvar_vcv: Optional[str] = Field(None, pattern=r"^VCV\d{9}$")


class VariantMention(BaseModel):
    raw: str
    gene: Optional[str] = None
    protein_change: Optional[str] = None  # 1-letter
    protein_change_3: Optional[str] = None  # 3-letter, e.g. 'Gly12Asp'
    cdna_change: Optional[str] = None  # 'c.35G>A'
    hgvs: Optional[str] = None  # full transcript HGVS when typed, e.g. 'NM_004985.5(KRAS):c.35G>A'
    rsid: Optional[str] = None
    clinvar_vcv: Optional[str] = None

    @property
    def display(self) -> str:
        if self.gene and self.protein_change:
            return f"{self.gene} {self.protein_change}"
        if self.gene and self.cdna_change:
            return f"{self.gene} {self.cdna_change}"
        return " ".join(x for x in (self.gene, self.rsid or self.clinvar_vcv) if x) or self.raw


class SourceStatus(BaseModel):
    name: SourceName
    status: SourceState
    detail: str = ""
    elapsed_seconds: float = 0.0


class ClinVarRecord(BaseModel):
    variation_id: str
    accession: str
    title: str
    protein_change: List[str] = Field(default_factory=list)
    rsids: List[str] = Field(default_factory=list)
    genes: List[str] = Field(default_factory=list)
    molecular_consequences: List[str] = Field(default_factory=list)
    canonical_spdi: Optional[str] = None
    grch38_chr: Optional[str] = None
    grch38_start: Optional[int] = None
    germline_classification: Optional[str] = None
    germline_review_status: Optional[str] = None
    germline_last_evaluated: Optional[str] = None
    # The conditions the germline classification was made for (DAT-006): a ClinVar classification belongs to a
    # variant-condition pair, never to the variant alone.
    germline_traits: List[str] = Field(default_factory=list)
    germline_review_stars: Optional[int] = None  # 0-4, from germline_review_status
    oncogenicity_traits: List[str] = Field(default_factory=list)
    oncogenicity_review_stars: Optional[int] = None
    oncogenicity_classification: Optional[str] = None
    oncogenicity_review_status: Optional[str] = None
    somatic_clinical_impact: Optional[str] = None
    somatic_clinical_impact_review_status: Optional[str] = None
    somatic_clinical_impact_traits: List[str] = Field(default_factory=list)
    somatic_clinical_impact_review_stars: Optional[int] = None
    url: str


class CivicEvidence(BaseModel):
    eid: str
    evidence_level: str
    evidence_type: str
    evidence_direction: str
    significance: str
    evidence_rating: Optional[int] = None
    variant_origin: Optional[str] = None
    disease: Optional[str] = None
    doid: Optional[str] = None
    therapies: List[str] = Field(default_factory=list)
    therapy_interaction_type: Optional[str] = None
    source_type: str = ""
    citation_id: str = ""
    pmid: Optional[str] = None
    citation: Optional[str] = None
    publication_year: Optional[int] = None
    description: str = ""
    url: str
    matches_context: Optional[bool] = None  # disease matches the question's cancer type; None when none was named


class CivicAssertion(BaseModel):
    aid: str
    amp_level: Optional[str] = None
    assertion_type: str
    assertion_direction: str = ""
    significance: str = ""
    disease: Optional[str] = None
    therapies: List[str] = Field(default_factory=list)
    fda_companion_test: Optional[bool] = None
    regulatory_approval: Optional[bool] = None
    nccn_guideline: Optional[str] = None
    status: str = "ACCEPTED"
    url: str


class CivicSummary(BaseModel):
    variant_id: int
    molecular_profile_id: int
    molecular_profile_name: str
    allele_registry_id: Optional[str] = None
    clinvar_ids: List[str] = Field(default_factory=list)
    accepted_evidence_total: int = 0
    accepted_assertion_total: int = 0
    evidence: List[CivicEvidence] = Field(default_factory=list)
    assertions: List[CivicAssertion] = Field(default_factory=list)
    url: str = ""
    context_disease: Optional[str] = None  # cancer keyword used to rank evidence (matching disease first)
    context_matched: Optional[int] = None  # fetched evidence items whose disease matches context_disease


class CbioStudyPrevalence(BaseModel):
    study_id: str
    study_name: str
    cancer_type: Optional[str] = None
    reference_genome: Optional[str] = None
    mutated_samples: int  # distinct profiled samples with proteinChange == query (not UNCALLED/GERMLINE)
    mutated_patients: int  # distinct patients among the mutated samples
    gene_mutated_samples: int  # distinct profiled samples with any call in the gene
    profiled_samples: int  # panel-aware denominator
    # Distinct patients among the profiled samples (DAT-015): the denominator for mutated_patients. None when the
    # gene-panel rows carry no patient ids; a patient numerator is then never paired with a sample denominator.
    profiled_patients: Optional[int] = None
    prevalence_percent: Optional[float] = None  # None when profiled_samples == 0
    denominator_basis: str
    panels: Dict[str, int] = Field(default_factory=dict)
    study_import_date: Optional[str] = None  # cBioPortal importDate of the study (data release provenance)
    url: str


class GnomadPopulation(BaseModel):
    id: str
    ac: int
    an: int
    af: Optional[float] = None


class GnomadFrequency(BaseModel):
    variant_id: str
    dataset: str = "gnomad_r4"
    rsids: List[str] = Field(default_factory=list)
    caid: Optional[str] = None
    exome_ac: Optional[int] = None
    exome_an: Optional[int] = None
    exome_af: Optional[float] = None
    exome_homozygotes: Optional[int] = None
    genome_ac: Optional[int] = None
    genome_an: Optional[int] = None
    genome_af: Optional[float] = None
    faf95_popmax: Optional[float] = None
    faf95_popmax_population: Optional[str] = None
    populations: List[GnomadPopulation] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)  # exome and genome filters merged (kept for compatibility)
    # gnomAD QC (DAT-005): a non-empty filter list means that dataset's call FAILED QC (e.g. AC0, AS_VQSR,
    # InbreedingCoeff), so its AC/AN/AF are unreliable. flags are variant-level annotations (e.g. lcr, segdup).
    exome_filters: List[str] = Field(default_factory=list)
    genome_filters: List[str] = Field(default_factory=list)
    flags: List[str] = Field(default_factory=list)
    hgvsp: Optional[str] = None
    hgvsc: Optional[str] = None
    transcript_id: Optional[str] = None
    url: str


class VariantIntelResult(BaseModel):
    gene: str
    protein_change: Optional[str] = None
    hgvs_p: Optional[str] = None
    hgvs_c: Optional[str] = None
    rsid: Optional[str] = None
    cancer_filter: Optional[str] = None  # keyword used for cBioPortal study selection, None for the pan-cancer default
    clinvar: List[ClinVarRecord] = Field(default_factory=list)
    civic: Optional[CivicSummary] = None
    cbioportal: List[CbioStudyPrevalence] = Field(default_factory=list)
    gnomad: Optional[GnomadFrequency] = None
    sources: List[SourceStatus] = Field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.gene} {self.protein_change or self.hgvs_c or self.rsid or ''}".strip()
