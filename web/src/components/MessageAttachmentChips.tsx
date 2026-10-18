"use client";

import type { MessageAttachment } from "@/types/datasets";
import { AttachmentChipBody } from "@/components/ComposerAttachmentChips";
import styles from "./Datasets.module.css";

export interface MessageAttachmentChipsProps {
  attachments: MessageAttachment[];
}

/** Datasets that were attached to a sent question, under its text. */
export function MessageAttachmentChips({ attachments }: MessageAttachmentChipsProps) {
  if (!attachments?.length) return null;
  return (
    <div className={styles.messageChips} role="list" aria-label="Datasets attached to this question">
      {attachments.map((a) => (
        <span key={a.id} className={styles.chip} role="listitem">
          <AttachmentChipBody a={a} />
        </span>
      ))}
    </div>
  );
}
