// Run manifest, sources and re-run diff types (owned by EXPORT, feature 9). Additive changes only.

export interface ToolCallRecord {
  seq: number;
  tool_name: string;
  adapter?: string;
  inputs: Record<string, any>;
  started_at: string;
  elapsed_seconds: number;
  success: boolean;
  error_type?: string | null;
  error?: string | null;
  canonical_ids: Record<string, string>;
  output_sha256?: string | null;
  output_digest: Record<string, any>;
}

export interface RunManifest {
  schema_version: "phronesis.run_manifest/1";
  run_id: string;
  thread_id: string;
  query: string;
  model_id: string;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  routing: Record<string, any>;
  tool_calls: ToolCallRecord[];
  biomodels: {
    accession: string;
    latest_revision?: number | null;
    main_file_sha256?: string | null;
    local_cache_sha256?: string | null;
    error?: string | null;
  }[];
  datasets: { path_basename: string; sha256?: string | null; size_bytes?: number | null; note?: string | null }[];
  source_versions: Record<string, Record<string, any>>;
  verification: Record<string, any>;
  response_sha256: string;
  environment: {
    app_version: string;
    source_fingerprint: string;
    python: string;
    platform: string;
    packages: Record<string, string>;
  };
}

export interface SourceRecord {
  /** "literature" | "web", or the CitationRecord type of a plugin source (e.g. "nct", "chembl"). */
  kind: "literature" | "web" | (string & {});
  pmid?: string | null;
  doi?: string | null;
  pmcid?: string | null;
  id?: string | null;
  id_type?: string | null;
  section?: string | null;
  has_full_text?: boolean | null;
  /** null when no integrity source answered. */
  retracted?: boolean | null;
  title: string;
  authors: string;
  journal: string;
  year: string;
  url?: string | null;
  tool: string;
}

export interface FieldChange {
  path: string;
  before: any;
  after: any;
  rel_change?: number | null;
  tier: "identical" | "within_tolerance" | "changed" | "added" | "removed";
}

export interface ToolDiff {
  key: string;
  tool_name: string;
  inputs?: Record<string, any>;
  status_before: string;
  status_after: string;
  output_hash_equal: boolean;
  changes: FieldChange[];
}

export interface RerunReport {
  original_run_id: string;
  new_run_id: string;
  mode: "tools" | "full";
  provenance?: "server_store" | "client_supplied";
  started_at?: string;
  finished_at?: string;
  overall: "identical" | "drifted" | "changed";
  tool_diffs: ToolDiff[];
  missing_in_new?: string[];
  new_only?: string[];
  environment_changes: FieldChange[];
  source_version_changes: FieldChange[];
  biomodel_changes: FieldChange[];
  verification_delta?: Record<string, any> | null;
  answer_unified_diff?: string | null;
  notes: string[];
}

export type ExportFormat = "docx" | "pdf" | "md" | "json" | "bundle";

export type RerunMode = "tools" | "full";
