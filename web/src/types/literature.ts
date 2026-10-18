// Literature grading and integrity types (owned by GRADING, feature 5). Additive changes only.

export type StudyType =
  | "meta_analysis"
  | "network_meta_analysis"
  | "systematic_review"
  | "rct"
  | "clinical_trial"
  | "indirect_comparison"
  | "observational"
  | "case_report"
  | "preclinical_in_vivo"
  | "in_vitro"
  | "computational"
  | "review"
  | "editorial"
  | "conference_abstract"
  | "trial_protocol"
  | "unclassified";

export interface EvidenceGrade {
  study_type: StudyType;
  label: string;
  tier: number;
  weight: number;
  basis: "europepmc_pubtype" | "mesh_heading" | "text_heuristic" | "none";
  cues: string[];
  confidence: "high" | "medium" | "low";
  is_preprint: boolean;
  /** Raw indexed publication types, kept apart from the design the article reports. */
  indexed_pub_types?: string[];
  /** The design the indexed tags alone gave, when the reported analysis overrode it (e.g. "rct" for a MAIC). */
  indexed_design?: string | null;
  mixed_design?: boolean;
  design_note?: string;
}

export interface IntegrityNotice {
  kind: string;
  source: "europepmc" | "crossref_publisher" | "crossref_retraction_watch";
  notice_pmid?: string | null;
  notice_doi?: string | null;
  date?: string | null;
  reference?: string | null;
  record_id?: string | null;
}

export interface IntegrityStatus {
  status: "retracted" | "withdrawn" | "expression_of_concern" | "corrected" | "none" | "unchecked";
  notices: IntegrityNotice[];
  sources_checked: string[];
  crossref_error?: string | null;
}

export interface GradedArticle {
  article_key: string;
  source: string;
  pmid?: string | null;
  doi?: string | null;
  pmcid?: string | null;
  title: string;
  journal: string;
  year: string;
  grade: EvidenceGrade;
  integrity: IntegrityStatus;
  is_integrity_notice: boolean;
  excluded_reason?: string | null;
}

export interface LiteratureArtifact {
  graded_articles: GradedArticle[];
  excluded_articles: GradedArticle[];
  integrity_check: Record<string, string>;
  grade_summary: Record<string, number>;
}
