// Open Targets disease -> target ranking (phronesis/diseaseintel/opentargets.py summarize()).

export interface DiseaseTargetDrug {
  name: string;
  chembl_id?: string | null;
  stage?: string | null;
  outcome?: string | null;
}

export interface DiseaseTarget {
  symbol: string;
  ensembl_id?: string | null;
  name?: string | null;
  overall_score: number;
  /** Open Targets datatype id -> score, e.g. {"genetic_association": 0.81}. */
  evidence?: Record<string, number>;
  evidence_class?: string | null;
  tractability?: unknown;
  drugs_for_disease?: DiseaseTargetDrug[];
}

export interface DiseaseTargetsTiers {
  validated?: { drug: string; chembl_id?: string | null; mechanism?: string | null; targets?: string[] }[];
  clinically_tested?: {
    drug: string;
    chembl_id?: string | null;
    stage?: string | null;
    mechanism?: string | null;
    targets?: string[];
    outcome?: string | null;
    stop_reason?: string | null;
  }[];
  genetic_risk_mechanisms?: string[];
}

export interface DiseaseTargetsArtifact {
  disease?: { id?: string | null; name?: string | null } | null;
  n_associated_targets?: number | null;
  tiers?: DiseaseTargetsTiers;
  targets: DiseaseTarget[];
  clinical_candidates?: unknown[];
  n_clinical_candidates?: number;
  source?: string;
}
