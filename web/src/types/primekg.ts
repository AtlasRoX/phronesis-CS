// PrimeKG knowledge-graph artifact (phronesis/extensions/primekg.py). Additive changes only.

export interface PrimeKGNode {
  index: number;
  id: string;
  name: string;
  type: string;
  curie?: string | null;
  url?: string | null;
  [k: string]: any;
}

export interface PrimeKGSection {
  total: number;
  items: PrimeKGNode[];
}

export interface PrimeKGAnalysis {
  operation:
    | "drug_profile"
    | "disease_profile"
    | "gene_profile"
    | "paths"
    | "drug_disease"
    | "repurposing"
    | "phenotype_diagnosis"
    | "polypharmacy"
    | "enrichment"
    | "similar"
    | string;
  reason?: string;
  arguments?: Record<string, unknown>;
  result?: Record<string, any>;
  error?: string;
}

export interface PrimeKGArtifact {
  analyses: PrimeKGAnalysis[];
  provenance?: {
    source?: string;
    dataverse_doi?: string;
    dataverse_version?: string;
    store_built_at?: string;
    citation?: string;
  };
  entities?: { drugs?: string[]; diseases?: string[]; genes?: string[] };
  unresolved?: string[];
}
