"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Copy, Database, LoaderCircle, Pencil, ShieldCheck, Trash2, X } from "lucide-react";
import styles from "./Datasets.module.css";
import type { DatasetRecord } from "@/types/datasets";
import { KindIcon, ProvenanceForm, useFocusTrap } from "@/components/ProvenanceForm";
import {
  CLIENT_SIZE_LIMITS,
  CONSENT_LABELS,
  deleteDataset,
  dimsOf,
  formatBytes,
  listDatasets,
  type AuthHeaders,
} from "@/services/datasetService";

export interface DatasetLibraryProps {
  backendUrl: string;
  authHeaders: AuthHeaders;
  attachedIds: string[];
  attachDisabled?: boolean;
  onAttach: (record: DatasetRecord) => void;
  /** Lets the composer drop a deleted dataset or refresh an edited one it has attached. */
  onDeleted?: (id: string) => void;
  onUpdated?: (record: DatasetRecord) => void;
  onClose: () => void;
}

const KIND_SHORT = { h5ad: "h5ad", table: "table", sbml: "SBML" } as const;

function geoCell(r: DatasetRecord) {
  const acc = r.provenance.geo_accession;
  if (!acc) return <span className={styles.hint}>–</span>;
  if (r.geo_verification?.status === "verified") {
    return (
      <span className={styles.badgeVerified} title={r.geo_verification.title || undefined}>
        <ShieldCheck style={{ width: 11, height: 11 }} aria-hidden="true" />
        {acc}
      </span>
    );
  }
  const why = r.geo_verification?.status === "not_found" ? "not found on GEO" : "unverified";
  return (
    <span className={styles.badgeDeclared} title={why}>
      {acc} · {why}
    </span>
  );
}

/** Drawer listing the user's uploads with attach, provenance edit and delete. */
export function DatasetLibrary({
  backendUrl,
  authHeaders,
  attachedIds,
  attachDisabled,
  onAttach,
  onDeleted,
  onUpdated,
  onClose,
}: DatasetLibraryProps) {
  const [records, setRecords] = useState<DatasetRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<DatasetRecord | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const headersRef = useRef(authHeaders);
  useEffect(() => {
    headersRef.current = authHeaders;
  });

  const load = useCallback(async () => {
    setError(null);
    try {
      setRecords(await listDatasets(backendUrl, (extra) => headersRef.current(extra)));
    } catch (e) {
      setRecords([]);
      setError(e instanceof Error ? e.message : "Could not load your datasets.");
    }
  }, [backendUrl]);

  useEffect(() => {
    void load();
  }, [load]);

  useFocusTrap(drawerRef, !editing, onClose);

  const copy = async (sha: string) => {
    try {
      await navigator.clipboard.writeText(sha);
      setCopied(sha);
      setTimeout(() => setCopied((c) => (c === sha ? null : c)), 1500);
    } catch {
      // clipboard unavailable
    }
  };

  const remove = async (id: string) => {
    setDeleting(id);
    setError(null);
    try {
      await deleteDataset(backendUrl, authHeaders, id);
      setRecords((rs) => (rs || []).filter((r) => r.id !== id));
      onDeleted?.(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed.");
    } finally {
      setDeleting(null);
      setConfirming(null);
    }
  };

  if (typeof document === "undefined") return null;
  return createPortal(
    <>
      <div className={styles.drawerOverlay} onMouseDown={onClose} />
      <div ref={drawerRef} className={styles.drawer} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className={styles.dialogHead}>
          <div style={{ minWidth: 0 }}>
            <h2 id={titleId} className={styles.dialogTitle}>
              <Database style={{ width: 16, height: 16, marginRight: 8, verticalAlign: "-2px" }} aria-hidden="true" />
              Your datasets
            </h2>
            <p className={styles.dialogSub}>Visible only to you. Files are validated on upload and never served back.</p>
          </div>
          <button type="button" className={styles.iconButton} onClick={onClose} aria-label="Close dataset library">
            <X style={{ width: 16, height: 16 }} />
          </button>
        </div>

        <div className={styles.drawerBody}>
          {error && (
            <div className={styles.error} role="alert" style={{ marginBottom: 12 }}>
              {error}
            </div>
          )}
          {records === null ? (
            <div className={styles.status} role="status">
              <LoaderCircle className={styles.spin} style={{ width: 14, height: 14 }} aria-hidden="true" />
              Loading…
            </div>
          ) : records.length === 0 ? (
            <div className={styles.empty}>
              No uploads yet. Attach an AnnData <code>.h5ad</code> (up to {CLIENT_SIZE_LIMITS.h5ad / 1048576} MB), a numeric{" "}
              <code>.csv</code>/<code>.tsv</code> expression matrix (up to {CLIENT_SIZE_LIMITS.table / 1048576} MB), or an SBML model{" "}
              <code>.xml</code>/<code>.sbml</code> (up to {CLIENT_SIZE_LIMITS.sbml / 1048576} MB) from the paperclip in the composer.
            </div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Dims</th>
                  <th scope="col">Size</th>
                  <th scope="col">sha256</th>
                  <th scope="col">Uploaded</th>
                  <th scope="col">Consent</th>
                  <th scope="col">GEO</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {records.map((r) => {
                  const attached = attachedIds.includes(r.id);
                  const warnings = r.validation?.warnings || [];
                  return (
                    <tr key={r.id}>
                      <td className={styles.nameCell} title={`${r.display_name} (${r.id})${warnings.length ? `\n${warnings.join("\n")}` : ""}`}>
                        <span className={styles[`kind_${r.kind}`]} style={{ marginRight: 6, verticalAlign: "-2px" }}>
                          <KindIcon kind={r.kind} size={13} />
                        </span>
                        {r.display_name}
                      </td>
                      <td data-label="Kind">{KIND_SHORT[r.kind]}</td>
                      <td data-label="Dims">{dimsOf(r) || "–"}</td>
                      <td data-label="Size">{formatBytes(r.size_bytes)}</td>
                      <td data-label="sha256">
                        <span className={styles.mono}>{r.sha256.slice(0, 12)}</span>{" "}
                        <button
                          type="button"
                          className={styles.iconButton}
                          style={{ width: 22, height: 22, verticalAlign: "middle" }}
                          onClick={() => copy(r.sha256)}
                          aria-label={`Copy full sha256 of ${r.display_name}`}
                          title="Copy full sha256"
                        >
                          {copied === r.sha256 ? <Check style={{ width: 12, height: 12 }} /> : <Copy style={{ width: 12, height: 12 }} />}
                        </button>
                      </td>
                      <td data-label="Uploaded">{new Date(r.uploaded_at).toLocaleString()}</td>
                      <td data-label="Consent">{CONSENT_LABELS[r.provenance.consent_status] || r.provenance.consent_status}</td>
                      <td data-label="GEO">{geoCell(r)}</td>
                      <td>
                        <div className={styles.rowActions}>
                          {confirming === r.id ? (
                            <>
                              <button
                                type="button"
                                className={`${styles.buttonDanger} ${styles.small}`}
                                onClick={() => remove(r.id)}
                                disabled={deleting === r.id}
                              >
                                {deleting === r.id ? "Deleting…" : "Delete"}
                              </button>
                              <button type="button" className={`${styles.button} ${styles.small}`} onClick={() => setConfirming(null)}>
                                Keep
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className={`${styles.buttonPrimary} ${styles.small}`}
                                onClick={() => onAttach(r)}
                                disabled={attached || attachDisabled}
                                title={attachDisabled && !attached ? "Up to 5 datasets per question" : undefined}
                              >
                                {attached ? "Attached" : "Attach"}
                              </button>
                              <button
                                type="button"
                                className={`${styles.button} ${styles.small}`}
                                onClick={() => setEditing(r)}
                                aria-label={`Edit provenance of ${r.display_name}`}
                                title="Edit provenance"
                              >
                                <Pencil style={{ width: 12, height: 12 }} />
                              </button>
                              <button
                                type="button"
                                className={`${styles.button} ${styles.small}`}
                                onClick={() => setConfirming(r.id)}
                                aria-label={`Delete ${r.display_name}`}
                                title="Delete"
                              >
                                <Trash2 style={{ width: 12, height: 12 }} />
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
      {editing && (
        <ProvenanceForm
          mode="edit"
          record={editing}
          backendUrl={backendUrl}
          authHeaders={authHeaders}
          onClose={() => setEditing(null)}
          onSaved={(updated) => {
            setRecords((rs) => (rs || []).map((r) => (r.id === updated.id ? updated : r)));
            onUpdated?.(updated);
            setEditing(null);
          }}
        />
      )}
    </>,
    document.body,
  );
}
