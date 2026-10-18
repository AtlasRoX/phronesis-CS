// ChEMBL + openFDA artifact types (owned by DRUGS, feature 2). Additive changes only.

export interface DrugMechanism {
  mechanism_of_action: string;
  action_type?: string | null;
  target_chembl_id?: string | null;
  target_pref_name?: string | null;
  form_chembl_id: string;
}

export interface DrugBioactivity {
  activity_id: number;
  assay_chembl_id: string;
  assay_description?: string | null;
  standard_type: string;
  standard_relation?: string | null;
  standard_value?: number | null;
  standard_units?: string | null;
  pchembl_value?: number | null;
  data_validity_comment?: string | null;
  target_chembl_id: string;
}

export interface DrugRecord {
  query_name: string;
  chembl_id: string;
  pref_name?: string | null;
  max_phase?: number | null;
  max_phase_meaning: string;
  first_approval?: number | null;
  molecule_type?: string | null;
  withdrawn_flag?: boolean | null;
  trade_names: string[];
  mechanisms: DrugMechanism[];
  bioactivities: DrugBioactivity[];
  bioactivity_total?: number | null;
}

export interface DrugSafety {
  ingredient: string;
  disclaimer: string;
  last_updated?: string | null;
  faers_search: string;
  total_reports: number;
  serious_reports?: number | null;
  top_reactions: { term: string; count: number }[];
  label?: {
    set_id: string;
    brand_name?: string | null;
    application_number?: string | null;
    effective_time?: string | null;
    harmonized: boolean;
    boxed_warning?: string | null;
    warnings?: string | null;
    contraindications?: string | null;
    dailymed_url: string;
  } | null;
  caveats: string[];
}

export interface DrugIntelArtifact {
  chembl?: {
    chembl_release?: string | null;
    target?: { target_chembl_id: string; pref_name: string } | null;
    drugs: DrugRecord[];
    unresolved_names: string[];
  };
  safety?: DrugSafety | DrugSafety[];
}

// Additional fields the backend emits (drugintel/schemas.py); all optional so older artifacts still render.
export interface DrugMechanism {
  max_phase?: number | null;
}

export interface DrugBioactivity {
  assay_type?: string | null;
  document_chembl_id?: string | null;
  molecule_chembl_id?: string;
  target_organism?: string | null;
}

export interface DrugRecord {
  forms?: string[];
  matched_via?: "pref_name" | "synonym" | "chembl_id";
  chembl_black_box_flag?: number | null;
}

export interface DrugApprovedForTarget {
  chembl_id: string;
  pref_name?: string | null;
  mechanism_of_action?: string | null;
  first_approval?: number | null;
  molecule_type?: string | null;
}

export interface DrugSafetyLabelDetail {
  generic_name?: string | null;
  /** null when openFDA did not harmonize the label, so the ingredient list is unknown. */
  combination_product?: boolean | null;
  manufacturer_name?: string | null;
  boxed_warning_truncated?: boolean;
  warnings_section?: "warnings_and_cautions" | "warnings" | null;
  warnings_truncated?: boolean;
  contraindications_truncated?: boolean;
}

export interface DrugSafety {
  chembl_id?: string | null;
  label_search?: string | null;
  label_error?: string | null;
}

export interface DrugSafetySkipped {
  drug: string;
  chembl_id?: string | null;
  max_phase?: number | null;
  reason: string;
}

export interface DrugChemblSection {
  chembl_release?: string | null;
  target?: { target_chembl_id: string; pref_name?: string | null; symbol_query?: string | null } | null;
  drugs: DrugRecord[];
  unresolved_names: string[];
  invalid_chembl_ids?: string[];
  target_approved_drugs?: DrugApprovedForTarget[];
  target_approved_total?: number | null;
  target_lookup?: "resolved" | "not_found" | "skipped" | "error" | null;
  // "failed" when the approved-drug lookup timed out or errored: the list is unknown, not empty.
  target_approved_status?: "ok" | "failed" | null;
  target_approved_error?: string | null;
  target_approved_error_kind?: string | null;
}

export interface DrugIntelArtifact {
  safety_skipped?: DrugSafetySkipped[];
}
