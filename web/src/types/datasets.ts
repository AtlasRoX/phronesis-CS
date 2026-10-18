// Dataset upload types (owned by UPLOADS, feature 8). Additive changes only.

export type DatasetKind = "h5ad" | "table" | "sbml";

export type ConsentStatus =
  | "public_deidentified"
  | "irb_approved"
  | "consent_obtained_no_irb"
  | "not_human_subjects"
  | "unknown";

export interface DatasetProvenance {
  source: "geo" | "cellxgene" | "biomodels" | "in_house" | "collaborator" | "other";
  geo_accession?: string | null;
  organism?: string | null;
  consent_status: ConsentStatus;
  irb_protocol_id?: string | null;
  description?: string | null;
  attests_no_direct_identifiers: boolean;
  table_orientation?: "cells_by_genes" | "genes_by_cells" | null;
}

export interface GeoVerification {
  status: "verified" | "not_found" | "lookup_failed" | "not_requested";
  accession?: string;
  title?: string;
  taxon?: string;
  n_samples?: number;
  pdat?: string;
  pubmedids?: string[];
  organism_matches?: boolean | null;
  bioproject?: string | null;
  samples?: string[];
  checked_at?: string | null;
}

export interface DatasetRecord {
  id: string;
  kind: DatasetKind;
  display_name: string;
  size_bytes: number;
  sha256: string;
  uploaded_at: string;
  declared_by: "uploader";
  provenance: DatasetProvenance;
  geo_verification: GeoVerification;
  validation: {
    format: string;
    n_obs?: number;
    n_vars?: number;
    n_species?: number;
    sbml_model_id?: string;
    sbml_model_name?: string;
    units?: Record<string, string>;
    warnings: string[];
  };
  original_extension?: string;
  derived_sha256?: string | null;
  /** Server-formatted dimensions, e.g. "2,700 cells × 1,838 genes" or "8 species". */
  dims?: string | null;
  provenance_history?: { changed_at: string; previous: Record<string, any> }[];
}

export interface MessageAttachment {
  id: string;
  kind: DatasetKind;
  display_name: string;
  dims?: string;
  geo_verified?: boolean;
}

/** One entry of execution_artifacts.datasets, i.e. DatasetRef.to_public(); it never contains server paths. */
export interface DatasetPublic {
  id: string;
  kind: DatasetKind;
  display_name: string;
  public_provenance: Record<string, any>;
  dims?: string | null;
  geo_accession?: string | null;
  geo_verified?: boolean;
  pmids?: string[];
  sbml_model_id?: string | null;
}
