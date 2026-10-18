"use client";

import { useEffect, useRef, useState, type ChangeEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Library, Paperclip, Upload } from "lucide-react";
import type { MessageAttachment } from "@/types/datasets";
import { DatasetLibrary } from "@/components/DatasetLibrary";
import { ProvenanceForm } from "@/components/ProvenanceForm";
import { ACCEPT_ATTR, precheckFile, toAttachment, type AuthHeaders } from "@/services/datasetService";
import styles from "./Datasets.module.css";

export interface ComposerAttachButtonProps {
  attached: MessageAttachment[];
  onAttach: (a: MessageAttachment) => void;
  /** A library delete or provenance edit of a dataset; attached chips are dropped or refreshed. */
  onDeleted?: (id: string) => void;
  onUpdated?: (a: MessageAttachment) => void;
  backendUrl: string;
  authHeaders: (extra?: Record<string, string>) => Record<string, string>;
  disabled?: boolean;
}

const MAX_ATTACHED = 5;

/** Composer pill to upload a dataset/model with provenance, or attach one from the library. */
export function ComposerAttachButton({
  attached,
  onAttach,
  onDeleted,
  onUpdated,
  backendUrl,
  authHeaders,
  disabled,
}: ComposerAttachButtonProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const full = attached.length >= MAX_ATTACHED;
  const headers: AuthHeaders = (extra) => authHeaders(extra);

  useEffect(() => {
    if (!menuOpen && !notice) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setNotice(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMenuOpen(false);
        setNotice(null);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen, notice]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  // Arrow keys move between the enabled menu items (WAI-ARIA menu pattern).
  const moveFocus = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
    if (!items.length) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[next].focus();
  };

  const pickFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const problem = precheckFile(file);
    if (problem) {
      setNotice(`${file.name}: ${problem}`);
      return;
    }
    setPendingFile(file);
  };

  return (
    <div className={styles.attach} ref={rootRef}>
      <button
        type="button"
        className="composer-pill"
        onClick={() => {
          setNotice(null);
          setMenuOpen((o) => !o);
        }}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={attached.length ? `Attach a dataset (${attached.length} attached)` : "Attach a dataset or model"}
        title="Attach a dataset (.h5ad, .csv, .tsv) or SBML model"
        data-active={attached.length > 0 ? "true" : undefined}
        style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
      >
        <Paperclip style={{ width: 13, height: 13, flexShrink: 0 }} aria-hidden="true" />
        <span className="composer-pill-label">{attached.length ? `${attached.length} attached` : "Attach"}</span>
      </button>

      {menuOpen && (
        <div className={styles.menu} role="menu" aria-label="Attach" onKeyDown={moveFocus}>
          <button
            type="button"
            role="menuitem"
            className={styles.menuItem}
            disabled={full}
            autoFocus={!full}
            onClick={() => {
              setMenuOpen(false);
              inputRef.current?.click();
            }}
          >
            <Upload style={{ width: 14, height: 14 }} aria-hidden="true" />
            Upload file…
          </button>
          <button
            type="button"
            role="menuitem"
            className={styles.menuItem}
            autoFocus={full}
            onClick={() => {
              setMenuOpen(false);
              setLibraryOpen(true);
            }}
          >
            <Library style={{ width: 14, height: 14 }} aria-hidden="true" />
            From library…
          </button>
          <div className={styles.menuHint}>
            {full ? `Up to ${MAX_ATTACHED} datasets per question.` : ".h5ad, .csv/.tsv matrices, SBML .xml/.sbml"}
          </div>
        </div>
      )}

      {notice && !menuOpen && (
        <div className={styles.notice} role="alert">
          {notice}
        </div>
      )}

      <input ref={inputRef} type="file" accept={ACCEPT_ATTR} className={styles.hiddenInput} onChange={pickFile} tabIndex={-1} aria-hidden="true" />

      {pendingFile && (
        <ProvenanceForm
          mode="upload"
          file={pendingFile}
          backendUrl={backendUrl}
          authHeaders={headers}
          onClose={() => setPendingFile(null)}
          onSaved={(record) => {
            setPendingFile(null);
            onAttach(toAttachment(record));
          }}
        />
      )}

      {libraryOpen && (
        <DatasetLibrary
          backendUrl={backendUrl}
          authHeaders={headers}
          attachedIds={attached.map((a) => a.id)}
          attachDisabled={full}
          onAttach={(record) => onAttach(toAttachment(record))}
          onDeleted={onDeleted}
          onUpdated={onUpdated ? (record) => onUpdated(toAttachment(record)) : undefined}
          onClose={() => setLibraryOpen(false)}
        />
      )}
    </div>
  );
}
