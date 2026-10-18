// Variant intelligence artifact types (owned by VARIANTS, feature 6). Additive changes only.

export type VariantSourceStatus = {
  name: "clinvar" | "civic" | "cbioportal" | "gnomad";
  status: "ok" | "no_match" | "failed" | "skipped";
  detail?: string;
  elapsed_seconds?: number;
};

export interface ClinVarRecordTS {
  variation_id: string;
  accession: string;
  title: string;
  protein_change: string[];
  rsids: string[];
  germline_classification?: string | null;
  germline_review_status?: string | null;
  oncogenicity_classification?: string | null;
  somatic_clinical_impact?: string | null;
  somatic_clinical_impact_traits?: string[];
  url: string;
  genes?: string[];
  canonical_spdi?: string | null;
  germline_last_evaluated?: string | null;
  germline_traits?: string[];
  oncogenicity_review_status?: string | null;
  somatic_clinical_impact_review_status?: string | null;
}

export interface CivicEvidenceTS {
  eid: string;
  evidence_level: string;
  evidence_type: string;
  evidence_direction: string;
  significance: string;
  evidence_rating?: number | null;
  disease?: string | null;
  therapies: string[];
  pmid?: string | null;
  citation?: string | null;
  description: string;
  url: string;
  variant_origin?: string | null;
  publication_year?: number | null;
}

export interface CivicAssertionTS {
  aid: string;
  amp_level?: string | null;
  assertion_type: string;
  significance: string;
  disease?: string | null;
  therapies: string[];
  fda_companion_test?: boolean | null;
  nccn_guideline?: string | null;
  url: string;
  assertion_direction?: string | null;
  regulatory_approval?: boolean | null;
  status?: string | null;
}

export interface CbioStudyPrevalenceTS {
  study_id: string;
  study_name: string;
  cancer_type?: string | null;
  mutated_samples: number;
  mutated_patients: number;
  profiled_samples: number;
  prevalence_percent: number | null;
  denominator_basis: string;
  url: string;
  gene_mutated_samples?: number;
  reference_genome?: string | null;
  panels?: Record<string, number>;
}

export interface GnomadPopulationTS {
  id: string;
  ac: number;
  an: number;
  af?: number | null;
}

export interface GnomadFrequencyTS {
  variant_id: string;
  dataset: string;
  exome_ac?: number | null;
  exome_an?: number | null;
  exome_af?: number | null;
  genome_ac?: number | null;
  genome_an?: number | null;
  genome_af?: number | null;
  faf95_popmax?: number | null;
  faf95_popmax_population?: string | null;
  url: string;
  rsids?: string[];
  exome_homozygotes?: number | null;
  populations?: GnomadPopulationTS[];
  hgvsp?: string | null;
}

export interface VariantIntelArtifact {
  gene: string;
  protein_change?: string | null;
  hgvs_p?: string | null;
  rsid?: string | null;
  cancer_filter?: string | null;
  clinvar: ClinVarRecordTS[];
  civic?: {
    molecular_profile_name: string;
    accepted_evidence_total: number;
    evidence: CivicEvidenceTS[];
    assertions: CivicAssertionTS[];
    accepted_assertion_total?: number;
    url?: string;
  } | null;
  cbioportal: CbioStudyPrevalenceTS[];
  gnomad?: GnomadFrequencyTS | null;
  sources: VariantSourceStatus[];
  hgvs_c?: string | null;
}
