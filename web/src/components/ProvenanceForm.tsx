"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Dna, FileSpreadsheet, GitBranch, LoaderCircle, ShieldCheck, X } from "lucide-react";
import styles from "./Datasets.module.css";
import type { ConsentStatus, DatasetKind, DatasetProvenance, DatasetRecord, GeoVerification } from "@/types/datasets";
import {
  CONSENT_LABELS,
  DatasetApiError,
  SOURCE_LABELS,
  formatBytes,
  kindForFile,
  lookupGeo,
  patchProvenance,
  uploadDataset,
  type AuthHeaders,
  type UploadHandle,
} from "@/services/datasetService";

const GSE_RE = /^GSE\d{1,9}$/;
const IRB_RE = /^[A-Za-z0-9._\-/ ]*$/;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Keeps Tab focus inside `ref` while active, focuses the first control, closes on Escape and restores focus. */
export function useFocusTrap(ref: React.RefObject<HTMLElement | null>, active: boolean, onEscape: () => void) {
  const escapeRef = useRef(onEscape);
  useEffect(() => {
    escapeRef.current = onEscape;
  });
  useEffect(() => {
    if (!active) return;
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const first = node?.querySelector<HTMLElement>("[data-autofocus]") || node?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (!ref.current) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        escapeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      previous?.focus?.();
    };
  }, [active, ref]);
}

export function KindIcon({ kind, size = 14 }: { kind: DatasetKind; size?: number }) {
  const s = { width: size, height: size };
  if (kind === "sbml") return <GitBranch style={s} aria-hidden="true" />;
  if (kind === "table") return <FileSpreadsheet style={s} aria-hidden="true" />;
  return <Dna style={s} aria-hidden="true" />;
}

const KIND_LABELS: Record<DatasetKind, string> = { h5ad: "AnnData (.h5ad)", table: "Expression table", sbml: "SBML model" };

export interface ProvenanceFormProps {
  mode: "upload" | "edit";
  file?: File | null;
  record?: DatasetRecord | null;
  backendUrl: string;
  authHeaders: AuthHeaders;
  onClose: () => void;
  onSaved: (record: DatasetRecord) => void;
}

type Phase = "editing" | "uploading" | "validating" | "done";

/** Server warnings plus a note when a declared GEO accession could not be confirmed. */
function uploadNotes(record: DatasetRecord): string[] {
  const notes = [...(record.validation?.warnings || [])];
  const acc = record.provenance?.geo_accession;
  const status = record.geo_verification?.status;
  if (acc && status === "not_found") notes.push(`${acc} was not found on NCBI GEO; it is stored as declared (unverified).`);
  if (acc && status === "lookup_failed") notes.push(`The NCBI GEO check for ${acc} failed; it is stored as declared (unverified).`);
  return notes;
}

/** Modal collecting uploader-declared provenance, then uploading (or PATCHing) with inline server reasons. */
export function ProvenanceForm({ mode, file, record, backendUrl, authHeaders, onClose, onSaved }: ProvenanceFormProps) {
  const initial = record?.provenance;
  const kind: DatasetKind | null = record?.kind ?? (file ? kindForFile(file.name) : null);
  const [source, setSource] = useState<DatasetProvenance["source"]>(initial?.source ?? (kind === "sbml" ? "biomodels" : "in_house"));
  const [geoAccession, setGeoAccession] = useState(initial?.geo_accession ?? "");
  const [organism, setOrganism] = useState(initial?.organism ?? "");
  const [consent, setConsent] = useState<ConsentStatus>(initial?.consent_status ?? (kind === "sbml" ? "not_human_subjects" : "unknown"));
  const [irbId, setIrbId] = useState(initial?.irb_protocol_id ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [orientation, setOrientation] = useState<"cells_by_genes" | "genes_by_cells">(initial?.table_orientation ?? "cells_by_genes");
  const [attest, setAttest] = useState(mode === "edit" ? Boolean(initial?.attests_no_direct_identifiers) : false);
  const [geo, setGeo] = useState<GeoVerification | null>(record?.geo_verification?.status !== "not_requested" ? record?.geo_verification ?? null : null);
  const [geoBusy, setGeoBusy] = useState(false);
  const [phase, setPhase] = useState<Phase>("editing");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<DatasetRecord | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const uploadRef = useRef<UploadHandle | null>(null);
  const ids = useId();

  const busy = phase === "uploading" || phase === "validating";
  const locked = phase !== "editing";
  const close = () => {
    uploadRef.current?.abort();
    onClose();
  };
  useFocusTrap(dialogRef, true, () => {
    if (!busy) close();
  });

  const geoValid = !geoAccession || GSE_RE.test(geoAccession.trim().toUpperCase());
  const irbValid = IRB_RE.test(irbId);
  const canSubmit = attest && geoValid && irbValid && !busy && (mode === "edit" || Boolean(file && kind));

  const verifyGeo = async () => {
    const acc = geoAccession.trim().toUpperCase();
    if (!GSE_RE.test(acc)) return;
    setGeoBusy(true);
    try {
      const result = await lookupGeo(backendUrl, authHeaders, acc);
      setGeo(result);
      if (result.status === "verified" && result.taxon && !organism.trim()) setOrganism(result.taxon);
    } catch (e) {
      setGeo({ status: "lookup_failed", accession: acc });
      if (e instanceof DatasetApiError && e.status === 401) setError(e.message);
    } finally {
      setGeoBusy(false);
    }
  };

  const provenance = (): DatasetProvenance => ({
    source,
    geo_accession: geoAccession.trim() ? geoAccession.trim().toUpperCase() : null,
    organism: organism.trim() || null,
    consent_status: consent,
    irb_protocol_id: consent === "irb_approved" && irbId.trim() ? irbId.trim() : null,
    description: description.trim() || null,
    attests_no_direct_identifiers: attest,
    table_orientation: kind === "table" ? orientation : null,
  });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    // The modal is portaled, but React still bubbles submit to the composer form around the attach button.
    e.stopPropagation();
    if (!canSubmit) return;
    setError(null);
    try {
      let result: DatasetRecord;
      if (mode === "edit" && record) {
        setPhase("validating");
        result = await patchProvenance(backendUrl, authHeaders, record.id, provenance());
      } else {
        setPhase("uploading");
        setProgress(0);
        const handle = uploadDataset(backendUrl, authHeaders, file as File, provenance(), (f) => {
          setProgress(f);
          if (f >= 1) setPhase("validating");
        });
        uploadRef.current = handle;
        result = await handle.promise;
      }
      uploadRef.current = null;
      // Validation warnings (e.g. a possible identifier column) are shown before the dataset is attached.
      if (mode === "upload" && uploadNotes(result).length) {
        setSaved(result);
        setPhase("done");
      } else {
        onSaved(result);
      }
    } catch (err) {
      uploadRef.current = null;
      setPhase("editing");
      setError(err instanceof Error ? err.message : "The upload failed.");
    }
  };

  const geoNote = (() => {
    if (!geo || geo.status === "not_requested") return null;
    if (geo.status === "verified") {
      return (
        <div className={styles.geoResult} data-status="verified" role="status">
          <ShieldCheck style={{ width: 12, height: 12, verticalAlign: "-1px" }} aria-hidden="true" /> {geo.accession} verified on NCBI GEO:{" "}
          {geo.title}
          {[geo.taxon, geo.n_samples != null ? `${geo.n_samples} samples` : null, geo.pdat ? `released ${geo.pdat}` : null]
            .filter(Boolean)
            .map((t) => ` · ${t}`)
            .join("")}
          {geo.organism_matches === false ? " · differs from the declared organism" : ""}
        </div>
      );
    }
    return (
      <div className={styles.geoResult} data-status={geo.status} role="status">
        {geo.status === "not_found"
          ? `${geo.accession || "This accession"} was not found on NCBI GEO; it will be stored as declared.`
          : "GEO lookup failed; the accession will be stored as declared (unverified)."}
      </div>
    );
  })();

  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      className={styles.overlay}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) close();
      }}
    >
      <div ref={dialogRef} className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby={`${ids}-title`}>
        <div className={styles.dialogHead}>
          <div style={{ minWidth: 0 }}>
            <h2 id={`${ids}-title`} className={styles.dialogTitle}>
              {mode === "edit" ? "Edit provenance" : "Dataset provenance"}
            </h2>
            <p className={styles.dialogSub}>
              Provenance is declared by you and shown to the model as uploader-declared. Only a GEO accession can be verified.
            </p>
          </div>
          <button type="button" className={styles.iconButton} onClick={close} aria-label="Close" disabled={phase === "validating"}>
            <X style={{ width: 16, height: 16 }} />
          </button>
        </div>

        <form className={styles.form} onSubmit={submit} noValidate>
          <div className={styles.fileSummary}>
            {kind && <KindIcon kind={kind} size={16} />}
            <strong>{record?.display_name || file?.name}</strong>
            <span className={styles.hint}>
              {[kind ? KIND_LABELS[kind] : null, file ? formatBytes(file.size) : record ? formatBytes(record.size_bytes) : null]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </div>

          <fieldset className={styles.field} disabled={locked}>
            <legend className={styles.label}>Source</legend>
            <div className={styles.segmented}>
              {(Object.keys(SOURCE_LABELS) as DatasetProvenance["source"][]).map((s) => (
                <button key={s} type="button" className={styles.segment} aria-pressed={source === s} onClick={() => setSource(s)}>
                  {SOURCE_LABELS[s]}
                </button>
              ))}
            </div>
          </fieldset>

          {(source === "geo" || geoAccession) && (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${ids}-geo`}>
                GEO series accession
              </label>
              <div className={styles.inline}>
                <input
                  id={`${ids}-geo`}
                  className={styles.input}
                  value={geoAccession}
                  onChange={(e) => {
                    setGeoAccession(e.target.value);
                    setGeo(null);
                  }}
                  placeholder="GSE132465"
                  aria-invalid={!geoValid}
                  aria-describedby={`${ids}-geo-hint`}
                  disabled={locked}
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  type="button"
                  className={styles.button}
                  onClick={verifyGeo}
                  disabled={locked || geoBusy || !geoAccession.trim() || !geoValid}
                >
                  {geoBusy ? <LoaderCircle className={styles.spin} style={{ width: 14, height: 14 }} aria-hidden="true" /> : null}
                  Verify
                </button>
              </div>
              <span id={`${ids}-geo-hint`} className={styles.hint}>
                {geoValid ? "Checked against NCBI GEO on upload; a failed check never blocks the upload." : "Use the form GSE followed by digits."}
              </span>
              {geoNote}
            </div>
          )}

          <div className={styles.fieldRow}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${ids}-org`}>
                Organism
              </label>
              <input
                id={`${ids}-org`}
                className={styles.input}
                value={organism}
                onChange={(e) => setOrganism(e.target.value)}
                placeholder="Homo sapiens"
                maxLength={120}
                disabled={locked}
              />
            </div>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${ids}-consent`}>
                Consent / IRB status
              </label>
              <select
                id={`${ids}-consent`}
                className={styles.select}
                value={consent}
                onChange={(e) => setConsent(e.target.value as ConsentStatus)}
                disabled={locked}
              >
                {(Object.keys(CONSENT_LABELS) as ConsentStatus[]).map((c) => (
                  <option key={c} value={c}>
                    {CONSENT_LABELS[c]}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {consent === "irb_approved" && (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${ids}-irb`}>
                IRB protocol id
              </label>
              <input
                id={`${ids}-irb`}
                className={styles.input}
                value={irbId}
                onChange={(e) => setIrbId(e.target.value)}
                maxLength={64}
                aria-invalid={!irbValid}
                disabled={locked}
              />
              {!irbValid && <span className={styles.hint}>Letters, digits, spaces and . _ - / only.</span>}
            </div>
          )}

          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${ids}-desc`}>
              Description
            </label>
            <textarea
              id={`${ids}-desc`}
              className={styles.textarea}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={4000}
              placeholder="Tissue, assay, processing steps…"
              disabled={locked}
            />
          </div>

          {kind === "table" && mode === "upload" && (
            <fieldset className={styles.field} disabled={locked}>
              <legend className={styles.label}>Table orientation</legend>
              <div className={styles.radioRow}>
                <label>
                  <input
                    type="radio"
                    name={`${ids}-orient`}
                    checked={orientation === "cells_by_genes"}
                    onChange={() => setOrientation("cells_by_genes")}
                  />
                  Rows are cells, columns are genes
                </label>
                <label>
                  <input
                    type="radio"
                    name={`${ids}-orient`}
                    checked={orientation === "genes_by_cells"}
                    onChange={() => setOrientation("genes_by_cells")}
                  />
                  Rows are genes, columns are cells
                </label>
              </div>
            </fieldset>
          )}

          <label className={styles.checkbox}>
            <input type="checkbox" checked={attest} onChange={(e) => setAttest(e.target.checked)} disabled={locked} required />
            <span>I confirm this file contains no directly identifying patient information (names, dates of birth, record numbers, addresses).</span>
          </label>

          {phase === "uploading" && (
            <div>
              <div className={styles.progress} role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
                <div className={styles.progressBar} style={{ width: `${Math.round(progress * 100)}%` }} />
              </div>
              <div className={styles.status} style={{ marginTop: 6 }}>
                Uploading… {Math.round(progress * 100)}%
              </div>
            </div>
          )}
          {phase === "validating" && (
            <div className={styles.status} role="status">
              <LoaderCircle className={styles.spin} style={{ width: 14, height: 14 }} aria-hidden="true" />
              {mode === "edit" ? "Saving…" : "Validating the file on the server…"}
            </div>
          )}
          {error && (
            <div className={styles.error} role="alert">
              {error}
            </div>
          )}
          {phase === "done" && saved && (
            <div role="status">
              <div className={styles.status}>Uploaded as {saved.id}. The server noted:</div>
              <ul className={styles.warnings}>
                {uploadNotes(saved).map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          <div className={styles.actions}>
            {phase === "done" && saved ? (
              <button type="button" className={styles.buttonPrimary} onClick={() => onSaved(saved)} autoFocus>
                Attach
              </button>
            ) : (
              <>
                <button type="button" className={styles.button} onClick={close} disabled={phase === "validating"}>
                  Cancel
                </button>
                <button type="submit" className={styles.buttonPrimary} disabled={!canSubmit}>
                  {mode === "edit" ? "Save provenance" : "Upload"}
                </button>
              </>
            )}
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
