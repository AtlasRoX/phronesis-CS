"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Download, FileCode2, FileText, FileType, Loader2 } from "lucide-react";

import styles from "@/components/Export.module.css";
import { exportMessage } from "@/services/exportService";
import type { Message } from "@/types/chat";
import type { ExportFormat } from "@/types/runs";

export interface ExportMenuProps {
  message: Message;
  question: string;
  backendUrl: string;
  authHeaders: (extra?: Record<string, string>) => Record<string, string>;
  disabled?: boolean;
}

type ItemKey = Exclude<ExportFormat, "bundle">;

// The reproducibility bundle stays available from the API (POST /api/export/bundle) for scripted use.
const ITEMS: { key: ItemKey; label: string; hint?: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { key: "pdf", label: "PDF", hint: ".pdf", Icon: FileType },
  { key: "docx", label: "Word document", hint: ".docx", Icon: FileText },
  { key: "md", label: "Markdown", hint: ".md", Icon: FileCode2 },
];

/** "Export" action: a small menu that downloads the answer as a report rendered by the backend. */
export function ExportMenu({ message, question, backendUrl, authHeaders, disabled = false }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<ItemKey | null>(null);
  const [errors, setErrors] = useState<Partial<Record<ItemKey, string>>>({});
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();

  const close = useCallback((focusTrigger = false) => {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) close();
    };
    document.addEventListener("mousedown", onPointer);
    const first = itemRefs.current.find((el) => el && !el.disabled);
    first?.focus();
    return () => document.removeEventListener("mousedown", onPointer);
  }, [open, close]);

  const run = async (key: ItemKey) => {
    if (busy) return;
    setBusy(key);
    setErrors((prev) => ({ ...prev, [key]: undefined }));
    try {
      await exportMessage(key, { question, message }, backendUrl, authHeaders());
    } catch (err) {
      const text = err instanceof Error ? err.message : "Export failed";
      setErrors((prev) => ({ ...prev, [key]: text }));
    } finally {
      setBusy(null);
    }
  };

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    const items = itemRefs.current.filter((el): el is HTMLButtonElement => !!el && !el.disabled);
    const idx = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      items[(idx + 1) % items.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items[(idx - 1 + items.length) % items.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      items[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      items[items.length - 1]?.focus();
    } else if (e.key === "Tab") {
      close();
    }
  };

  return (
    <div className={styles.menuWrap} ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        className="msg-action-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        title={disabled ? "Nothing to export for this message" : "Export this answer"}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <Download style={{ width: 13, height: 13 }} aria-hidden="true" /> Export
        <ChevronDown className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <div id={menuId} role="menu" aria-label="Export answer" className={styles.menu} onKeyDown={onMenuKeyDown}>
          {ITEMS.map(({ key, label, hint, Icon }, i) => (
            <React.Fragment key={key}>
              <button
                ref={(el) => {
                  itemRefs.current[i] = el;
                }}
                type="button"
                role="menuitem"
                className={styles.menuItem}
                disabled={busy !== null && busy !== key}
                aria-busy={busy === key}
                onClick={() => run(key)}
              >
                {busy === key ? (
                  <Loader2 className={`${styles.menuIcon} ${styles.spin}`} aria-hidden="true" />
                ) : (
                  <Icon className={styles.menuIcon} aria-hidden="true" />
                )}
                <span>{label}</span>
                {hint && <span className={styles.menuHint}>{hint}</span>}
              </button>
              {errors[key] && (
                <div className={styles.menuError} role="alert">
                  {errors[key]}
                </div>
              )}
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
