"use client";

import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Copy, ExternalLink, TriangleAlert, X } from "lucide-react";
import type { CitationEntry, ClaimSupport } from "@/types/citations";
import { safeHref } from "@/lib/citations";
import styles from "@/components/Citations.module.css";

export interface CitationPopoverProps {
  id: string;
  entry: CitationEntry;
  support: ClaimSupport | null;
  anchor: HTMLElement | null;
  /** restoreFocus is true for Esc and the close button, false for an outside click. */
  onClose: (restoreFocus: boolean) => void;
}

const TYPE_LABELS: Record<string, string> = {
  pmid: "PubMed",
  doi: "DOI",
  nct: "Trial",
  chembl: "ChEMBL",
  biomodel: "BioModel",
  ensembl: "Ensembl",
  uniprot: "UniProt",
  kegg: "KEGG",
  clinvar: "ClinVar",
  rsid: "dbSNP",
  civic: "CIViC",
  geo: "GEO",
  url: "Web",
};

const LINK_LABELS: Record<string, string> = {
  pmid: "Open in PubMed",
  doi: "Open DOI",
  nct: "Open in ClinicalTrials.gov",
  chembl: "Open in ChEMBL",
  biomodel: "Open in BioModels",
  ensembl: "Open in Open Targets",
  uniprot: "Open in UniProt",
  kegg: "Open in KEGG",
  clinvar: "Open in ClinVar",
  rsid: "Open in dbSNP",
  civic: "Open in CIViC",
  geo: "Open in GEO",
  url: "Open page",
};

/** Who published a web source; plain "web" pages get no chip. */
const AUTHORITY_LABELS: Record<string, string> = {
  regulatory: "Regulator",
  trial_registry: "Trial registry",
  manufacturer: "Manufacturer / press release",
  literature: "Journal",
};

const LEVEL_LABELS: Record<string, string> = {
  "meta-analysis": "Meta-analysis",
  "systematic-review": "Systematic review",
  "randomized-trial": "Randomized trial",
  "clinical-trial": "Clinical trial",
  observational: "Observational study",
  "case-report": "Case report",
  review: "Review",
  preprint: "Preprint - not peer reviewed",
  "research-article": "Research article",
  "preclinical-in-vivo": "Preclinical in vivo",
  "preclinical-in-vitro": "Preclinical in vitro",
  computational: "Computational",
  "database-record": "Database record",
  simulation: "Simulation output",
  web: "Web source - unreviewed",
};

const KIND_LABELS: Record<string, string> = {
  literature: "Source passage",
  tool_output: "Tool output",
  web_snippet: "Web snippet",
  mention: "Mentioned in passage",
};

function Highlighted({ text, highlight }: { text: string; highlight: string | null }) {
  const idx = highlight ? text.indexOf(highlight) : -1;
  if (!highlight || idx < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className={styles.mark}>{highlight}</mark>
      {text.slice(idx + highlight.length)}
    </>
  );
}

type Placement = { top: number; left: number; width: number };

export function CitationPopover({ id, entry, support, anchor, onClose }: CitationPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = `${id}-title`;
  const passages = entry.passages || [];
  const supported = support?.passage_id ? passages.findIndex((p) => p.passage_id === support.passage_id) : -1;
  const [tab, setTab] = useState(Math.max(0, supported));
  const [place, setPlace] = useState<Placement | null>(null);
  const [sheet, setSheet] = useState(false);
  const [copied, setCopied] = useState(false);

  useLayoutEffect(() => {
    const update = () => {
      const isSheet = window.matchMedia("(max-width: 639px)").matches;
      setSheet(isSheet);
      if (isSheet || !anchor) {
        setPlace(null);
        return;
      }
      const r = anchor.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const width = Math.min(400, vw - 16);
      const left = Math.min(Math.max(8, r.left), vw - width - 8);
      const height = ref.current?.offsetHeight ?? 320;
      const below = r.bottom + 8;
      const above = r.top - 8 - height;
      const top = below + height > vh - 8 && above >= 8 ? above : Math.max(8, Math.min(below, vh - height - 8));
      setPlace({ top, left, width });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchor, tab]);

  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose(true);
      }
    };
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (target && (ref.current?.contains(target) || anchor?.contains(target))) return;
      onClose(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, [anchor, onClose]);

  if (typeof document === "undefined") return null;

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(entry.id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard can be unavailable (insecure context); the id stays visible for manual copy.
    }
  };

  const passage = passages[tab] || null;
  const isSupportPassage = !!(passage && support && support.passage_id && support.passage_id === passage.passage_id);
  const highlight = (isSupportPassage ? support?.highlight : null) || passage?.highlight || null;
  // A fallback support belongs to another mention of this source: never present it as this sentence's match (UI-017).
  const fallbackSupport = !!support?.fallback;
  const grounded = isSupportPassage && !fallbackSupport && !!support?.grounded_by_verifier;
  let matchLabel = "No specific sentence matched this claim";
  if (grounded) matchLabel = "Matched by Verifier";
  else if (isSupportPassage && fallbackSupport) matchLabel = "Support shown for another mention of this source";
  else if (highlight && passage?.kind === "mention" && !(isSupportPassage && support?.highlight)) {
    matchLabel = "Sentence that mentions this identifier";
  } else if (highlight) matchLabel = "Closest sentence to this claim";

  const meta = [entry.authors, entry.journal, entry.year].filter(Boolean).join(" · ");
  const level = entry.evidence_level ? LEVEL_LABELS[entry.evidence_level] || entry.evidence_level : null;
  const link = safeHref(entry.url);
  const pmcLink = entry.pmcid && /^PMC\d+$/.test(entry.pmcid) ? `https://pmc.ncbi.nlm.nih.gov/articles/${entry.pmcid}/` : null;
  const typeLabel = TYPE_LABELS[entry.type] || String(entry.type || "source").toUpperCase();

  const style: React.CSSProperties | undefined = sheet
    ? undefined
    : place
      ? { top: place.top, left: place.left, width: place.width }
      : { top: 0, left: 0, visibility: "hidden" };

  return createPortal(
    <>
      {sheet && <div className={styles.backdrop} aria-hidden="true" />}
      <div
        ref={ref}
        id={id}
        role="dialog"
        aria-modal="false"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`${styles.popover}${sheet ? ` ${styles.sheet}` : ""}`}
        style={style}
      >
        <div className={styles.header}>
          <span className={styles.typeBadge}>{typeLabel}</span>
          <span className={styles.idText}>{entry.id}</span>
          <span className={styles.headerSpacer} />
          <button type="button" className={styles.iconButton} onClick={copyId} aria-label={`Copy ${entry.id}`}>
            {copied ? <Check size={14} /> : <Copy size={14} />}
            <span>{copied ? "Copied" : "Copy"}</span>
          </button>
          <button type="button" className={styles.iconButton} onClick={() => onClose(true)} aria-label="Close">
            <X size={15} />
          </button>
        </div>

        <h3 id={titleId} className={styles.title}>
          {entry.title || `${typeLabel} ${entry.id}`}
        </h3>
        {meta && <p className={styles.meta}>{meta}</p>}

        <div className={styles.chips}>
          {level && (
            <span className={styles.levelChip} data-level={entry.evidence_level || undefined}>
              {level}
            </span>
          )}
          {entry.source_authority && AUTHORITY_LABELS[entry.source_authority] && (
            <span className={styles.chipMuted}>{AUTHORITY_LABELS[entry.source_authority]}</span>
          )}
          <span className={styles.chipMuted}>via {entry.source_agent}</span>
          {passage?.kind === "literature" && (
            <span className={styles.chipMuted}>{passage.has_full_text ? "Full text" : "Abstract"}</span>
          )}
          {entry.verified && <span className={styles.chipMuted}>Identifier verified</span>}
        </div>

        {entry.retracted === true && (
          <div className={styles.banner} role="note">
            <TriangleAlert size={15} aria-hidden="true" />
            <div>
              <strong>This publication is marked as retracted in its source record.</strong>
              {entry.retraction_notice && <div className={styles.bannerNotice}>{entry.retraction_notice}</div>}
            </div>
          </div>
        )}

        {entry.note && <p className={styles.note}>{entry.note}</p>}

        {passage ? (
          <div className={styles.passageBlock}>
            {passages.length > 1 && (
              <div className={styles.tabs} role="tablist" aria-label="Source passages">
                {passages.map((p, i) => (
                  <button
                    key={p.passage_id || i}
                    type="button"
                    role="tab"
                    aria-selected={i === tab}
                    className={`${styles.tab}${i === tab ? ` ${styles.tabActive}` : ""}`}
                    onClick={() => setTab(i)}
                  >
                    Passage {i + 1}/{passages.length}
                  </button>
                ))}
              </div>
            )}
            <div className={styles.passageHead}>
              <span>
                {KIND_LABELS[passage.kind] || "Source passage"}
                {passage.section ? ` · ${passage.section}` : ""}
              </span>
              <span className={grounded ? styles.matchGrounded : styles.matchLabel}>{matchLabel}</span>
            </div>
            {passage.text || highlight ? (
              <blockquote className={styles.passageText}>
                <Highlighted text={passage.text || highlight || ""} highlight={highlight} />
              </blockquote>
            ) : null}
            {!passage.text && (
              <p className={styles.note}>The full passage was not kept in this browser&apos;s storage.</p>
            )}
          </div>
        ) : (
          !entry.note && <p className={styles.note}>No source passage was stored for this identifier.</p>
        )}

        {(link || pmcLink) && (
          <div className={styles.footer}>
            {link && (
              <a className={styles.primaryLink} href={link} target="_blank" rel="noopener noreferrer">
                {LINK_LABELS[entry.type] || "Open source"}
                <ExternalLink size={13} aria-hidden="true" />
              </a>
            )}
            {pmcLink && (
              <a className={styles.secondaryLink} href={pmcLink} target="_blank" rel="noopener noreferrer">
                Open in PMC
                <ExternalLink size={12} aria-hidden="true" />
              </a>
            )}
          </div>
        )}
      </div>
    </>,
    document.body
  );
}
