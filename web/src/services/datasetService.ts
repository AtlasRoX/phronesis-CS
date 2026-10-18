// Client for /api/datasets (UPLOADS, feature 8). Uploads use XMLHttpRequest for progress events;
// the browser sets the multipart boundary, so Content-Type is never set by hand.

import type { DatasetKind, DatasetProvenance, DatasetRecord, GeoVerification, MessageAttachment } from "@/types/datasets";
import { ApiError, requestJson, type AuthHeaders } from "./apiClient.ts";

export type { AuthHeaders };

export const ACCEPTED_EXTENSIONS = [".h5ad", ".csv", ".tsv", ".xml", ".sbml"] as const;
export const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(",");

const MB = 1024 * 1024;
// Server defaults (PHRONESIS_UPLOAD_MAX_MB_*); the server re-checks every upload.
export const CLIENT_SIZE_LIMITS: Record<DatasetKind, number> = { h5ad: 500 * MB, table: 100 * MB, sbml: 10 * MB };

export class DatasetApiError extends ApiError {
  reasonCode?: string;
  constructor(status: number, message: string, reasonCode?: string) {
    super(status, message);
    this.name = "DatasetApiError";
    this.reasonCode = reasonCode;
  }
}

const REASON_MESSAGES: Record<string, string> = {
  not_hdf5: "Not an AnnData (.h5ad) file.",
  not_anndata: "Not an AnnData file.",
  hdf5_unreadable: "The file is truncated or corrupt.",
  hdf5_external_link: "The file contains external HDF5 links, which are not allowed.",
  too_few_cells: "The matrix has too few cells (at least 5 are needed).",
  too_few_genes: "The matrix has too few genes (at least 2 are needed).",
  matrix_too_large: "The matrix is too large to analyze.",
  table_unreadable: "The table could not be parsed.",
  non_numeric_values: "Every value in the table must be numeric.",
  non_finite_values: "The table contains empty, NaN or infinite values.",
  xml_unparseable: "The file is not well-formed XML.",
  not_sbml: "The XML file is not an SBML model.",
  sbml_dtd_forbidden: "SBML DTD/entities are not allowed.",
  sbml_external_reference: "External SBML model references are not supported.",
  sbml_unloadable: "COPASI could not load the model.",
  sbml_empty_model: "The SBML model defines no species.",
  empty_file: "The file is empty.",
};

const STATUS_MESSAGES: Record<number, string> = {
  401: "Sign in again: the API token was rejected.",
  404: "Dataset not found.",
  409: "The dataset is in use by a running analysis; try again shortly.",
  411: "The upload was sent without a length; try again.",
  413: "The file is larger than the upload limit.",
  415: "Unsupported file type. Upload .h5ad, .csv, .tsv, .xml or .sbml.",
  507: "Your upload storage is full; delete a dataset first.",
};

function errorFrom(status: number, body: unknown): DatasetApiError {
  const b = (body && typeof body === "object" ? body : {}) as { detail?: unknown; reason_code?: string };
  const detail = typeof b.detail === "string" ? b.detail : undefined;
  const reason = b.reason_code;
  let message: string;
  if (status === 422) {
    const canned = reason ? REASON_MESSAGES[reason] : undefined;
    // The server's detail is specific (which column, which link); keep it next to the short reason.
    message = canned ? (detail && detail !== canned ? `${canned} ${detail}` : canned) : detail || "The file did not pass validation.";
  } else if (status === 413 || status === 507) {
    message = detail || STATUS_MESSAGES[status];
  } else {
    message = STATUS_MESSAGES[status] || detail || `Request failed (${status}).`;
  }
  return new DatasetApiError(status, message, reason);
}

function request<T>(url: string, init: RequestInit): Promise<T> {
  return requestJson<T>(url, init, { toError: errorFrom });
}

export function kindForFile(name: string): DatasetKind | null {
  const ext = name.slice(name.lastIndexOf(".")).toLowerCase();
  if (ext === ".h5ad") return "h5ad";
  if (ext === ".csv" || ext === ".tsv") return "table";
  if (ext === ".xml" || ext === ".sbml") return "sbml";
  return null;
}

/** Client-side pre-check before any network call; returns an error message or null. */
export function precheckFile(file: File): string | null {
  const kind = kindForFile(file.name);
  if (!kind) return STATUS_MESSAGES[415];
  if (file.size === 0) return REASON_MESSAGES.empty_file;
  if (file.size > CLIENT_SIZE_LIMITS[kind]) {
    return `${kind === "table" ? "CSV/TSV" : kind === "sbml" ? "SBML" : ".h5ad"} files are limited to ${CLIENT_SIZE_LIMITS[kind] / MB} MB.`;
  }
  return null;
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n)) return "–";
  if (n < 1024) return `${n} B`;
  if (n < MB) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * MB) return `${(n / MB).toFixed(1)} MB`;
  return `${(n / (1024 * MB)).toFixed(2)} GB`;
}

export function dimsOf(record: DatasetRecord): string | undefined {
  if (record.dims) return record.dims;
  const v = record.validation;
  if (record.kind === "sbml") return v.n_species != null ? `${v.n_species} species` : undefined;
  if (v.n_obs != null && v.n_vars != null) return `${v.n_obs.toLocaleString()} cells × ${v.n_vars.toLocaleString()} genes`;
  return undefined;
}

export function toAttachment(record: DatasetRecord): MessageAttachment {
  return {
    id: record.id,
    kind: record.kind,
    display_name: record.display_name,
    dims: dimsOf(record),
    geo_verified: record.geo_verification?.status === "verified",
  };
}

export function listDatasets(backendUrl: string, authHeaders: AuthHeaders): Promise<DatasetRecord[]> {
  return request<{ datasets: DatasetRecord[] }>(`${backendUrl}/api/datasets`, { headers: authHeaders() }).then(
    (r) => r.datasets || [],
  );
}

export function getDataset(backendUrl: string, authHeaders: AuthHeaders, id: string): Promise<DatasetRecord> {
  return request<DatasetRecord>(`${backendUrl}/api/datasets/${encodeURIComponent(id)}`, { headers: authHeaders() });
}

export function patchProvenance(
  backendUrl: string,
  authHeaders: AuthHeaders,
  id: string,
  provenance: DatasetProvenance,
): Promise<DatasetRecord> {
  return request<DatasetRecord>(`${backendUrl}/api/datasets/${encodeURIComponent(id)}/provenance`, {
    method: "PATCH",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(provenance),
  });
}

export function deleteDataset(backendUrl: string, authHeaders: AuthHeaders, id: string): Promise<{ status: string }> {
  return request<{ status: string }>(`${backendUrl}/api/datasets/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
}

export function lookupGeo(backendUrl: string, authHeaders: AuthHeaders, accession: string): Promise<GeoVerification> {
  return request<GeoVerification>(`${backendUrl}/api/datasets/geo-lookup`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ accession }),
  });
}

export interface UploadHandle {
  promise: Promise<DatasetRecord>;
  abort: () => void;
}

/** Upload with progress (0-1 for the bytes sent; the server then validates before answering). */
export function uploadDataset(
  backendUrl: string,
  authHeaders: AuthHeaders,
  file: File,
  provenance: DatasetProvenance,
  onProgress?: (fraction: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<DatasetRecord>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("provenance", JSON.stringify(provenance));
    xhr.open("POST", `${backendUrl}/api/datasets`);
    for (const [k, v] of Object.entries(authHeaders())) {
      if (k.toLowerCase() !== "content-type") xhr.setRequestHeader(k, v);
    }
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response) resolve(xhr.response as DatasetRecord);
      else reject(errorFrom(xhr.status, xhr.response));
    };
    xhr.onerror = () => reject(new DatasetApiError(0, "The upload failed: the backend is unreachable."));
    xhr.onabort = () => reject(new DatasetApiError(0, "Upload cancelled."));
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

export const CONSENT_LABELS: Record<DatasetProvenance["consent_status"], string> = {
  public_deidentified: "Public, de-identified",
  irb_approved: "IRB-approved",
  consent_obtained_no_irb: "Consent obtained, no IRB",
  not_human_subjects: "Not human subjects",
  unknown: "Unknown",
};

export const SOURCE_LABELS: Record<DatasetProvenance["source"], string> = {
  geo: "GEO",
  cellxgene: "CELLxGENE",
  biomodels: "BioModels",
  in_house: "In-house",
  collaborator: "Collaborator",
  other: "Other",
};
