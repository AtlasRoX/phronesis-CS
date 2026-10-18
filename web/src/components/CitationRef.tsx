"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { ExternalLink } from "lucide-react";
import type { CitationMap } from "@/types/citations";
import { CitationPopover } from "@/components/CitationPopover";
import { entryFor, resolveKey, safeHref, supportFor } from "@/lib/citations";
import styles from "@/components/Citations.module.css";

export interface CitationRefProps {
  citeKey: string;
  /** 0-based occurrence of citeKey in the answer, in document order. */
  occurrence: number;
  map: CitationMap;
  href?: string;
  children: React.ReactNode;
}

// One citation popover is open at a time; opening another closes the current one.
let closeActive: (() => void) | null = null;

/**
 * A citation chip for an identifier in the answer. It stays a real link (middle-click and modified
 * clicks open the source), and a plain click or Space toggles the source-passage popover.
 */
export function CitationRef({ citeKey, occurrence, map, href, children }: CitationRefProps) {
  const anchorRef = useRef<HTMLAnchorElement>(null);
  const [open, setOpen] = useState(false);
  const dialogId = useId();
  const key = resolveKey(map, citeKey);
  const entry = entryFor(map, key);
  const link = safeHref(entry?.url) ?? safeHref(href);

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) anchorRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const mine = () => setOpen(false);
    closeActive = mine;
    return () => {
      if (closeActive === mine) closeActive = null;
    };
  }, [open]);

  if (!entry) {
    return (
      <a href={link} target="_blank" rel="noopener noreferrer" className="external-link">
        {children}
        <ExternalLink style={{ width: 11, height: 11, display: "inline", marginLeft: 3 }} />
      </a>
    );
  }

  const toggle = () => {
    if (open) {
      close(false);
      return;
    }
    closeActive?.();
    setOpen(true);
  };

  const onClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    toggle();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLAnchorElement>) => {
    if (e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };

  const retracted = entry.retracted === true;
  return (
    <>
      <a
        ref={anchorRef}
        href={link}
        target="_blank"
        rel="noopener noreferrer"
        className={`${styles.chip}${retracted ? ` ${styles.chipRetracted}` : ""}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? dialogId : undefined}
        title={retracted ? "Retracted publication: show source" : "Show source passage"}
        onClick={onClick}
        onKeyDown={onKeyDown}
      >
        {children}
        {retracted && <span className={styles.retractedSuffix}>Retracted</span>}
      </a>
      {open && (
        <CitationPopover
          id={dialogId}
          entry={entry}
          support={supportFor(map, key, occurrence)}
          anchor={anchorRef.current}
          onClose={close}
        />
      )}
    </>
  );
}
