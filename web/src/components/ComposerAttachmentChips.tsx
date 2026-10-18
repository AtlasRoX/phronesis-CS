"use client";

import { ShieldCheck, X } from "lucide-react";
import type { MessageAttachment } from "@/types/datasets";
import { KindIcon } from "@/components/ProvenanceForm";
import styles from "./Datasets.module.css";

export interface ComposerAttachmentChipsProps {
  attached: MessageAttachment[];
  onRemove: (id: string) => void;
}

export function ProvenanceBadge({ verified }: { verified?: boolean }) {
  return verified ? (
    <span className={styles.badgeVerified} title="GEO accession confirmed against NCBI GEO">
      <ShieldCheck style={{ width: 11, height: 11 }} aria-hidden="true" />
      GEO verified
    </span>
  ) : (
    <span className={styles.badgeDeclared} title="Provenance declared by the uploader, not verified">
      declared
    </span>
  );
}

export function AttachmentChipBody({ a }: { a: MessageAttachment }) {
  return (
    <>
      <span className={`${styles.chipIcon} ${styles[`kind_${a.kind}`] || ""}`}>
        <KindIcon kind={a.kind} size={13} />
      </span>
      <span className={styles.chipName} title={`${a.display_name} (${a.id})`}>
        {a.display_name}
      </span>
      {a.dims && <span className={styles.chipDims}>{a.dims}</span>}
      <ProvenanceBadge verified={a.geo_verified} />
    </>
  );
}

/** Datasets attached to the next question, shown above the textarea. */
export function ComposerAttachmentChips({ attached, onRemove }: ComposerAttachmentChipsProps) {
  if (!attached.length) return null;
  return (
    <div className={styles.chips} role="list" aria-label="Attached datasets">
      {attached.map((a) => (
        <span key={a.id} className={styles.chip} role="listitem">
          <AttachmentChipBody a={a} />
          <button
            type="button"
            className={styles.chipRemove}
            onClick={() => onRemove(a.id)}
            aria-label={`Remove ${a.display_name}`}
            title="Remove"
          >
            <X style={{ width: 12, height: 12 }} />
          </button>
        </span>
      ))}
    </div>
  );
}
