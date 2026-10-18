// ClinicalTrials.gov v2 artifact types (owned by TRIALS, feature 1). Additive changes only.
// Result values stay strings exactly as the API returns them ("NA", "<0.0001").

export interface TrialArmValue {
  group_id: string;
  group_title?: string | null;
  n?: string | null;
  value?: string | null;
  lower?: string | null;
  upper?: string | null;
  spread?: string | null;
  class_title?: string | null;
  category_title?: string | null;
  comment?: string | null;
}

export interface TrialAnalysis {
  group_ids: string[];
  param_type?: string | null;
  param_value?: string | null;
  ci_pct?: string | null;
  ci_lower?: string | null;
  ci_upper?: string | null;
  p_value?: string | null;
  method?: string | null;
}

export interface TrialOutcomeResult {
  type: string;
  title: string;
  param_type?: string | null;
  dispersion_type?: string | null;
  unit?: string | null;
  time_frame?: string | null;
  measurements: TrialArmValue[];
  analyses: TrialAnalysis[];
}

export interface TrialRecord {
  nct_id: string;
  url: string;
  brief_title?: string | null;
  acronym?: string | null;
  overall_status?: string | null;
  why_stopped?: string | null;
  phases: string[];
  study_type?: string | null;
  enrollment_count?: number | null;
  enrollment_type?: "ACTUAL" | "ESTIMATED" | null;
  lead_sponsor?: string | null;
  lead_sponsor_class?: string | null;
  conditions: string[];
  interventions: { type?: string | null; name?: string | null }[];
  primary_outcomes: { measure?: string | null; time_frame?: string | null }[];
  start_date?: string | null;
  primary_completion_date?: string | null;
  last_update_posted?: string | null;
  has_results: boolean;
  results_first_posted?: string | null;
  posted_results: TrialOutcomeResult[];
  result_pmids: string[];
}

export interface ClinicalTrialsArtifact {
  source: string;
  query: {
    term?: string | null;
    intervention?: string | null;
    condition?: string | null;
    nct_ids: string[];
    phases: string[];
    statuses: string[];
    strategy: string;
    api_params: Record<string, string>;
  };
  total_count: number;
  trials: TrialRecord[];
  results_fetched_for: string[];
  limitations: string[];
}
