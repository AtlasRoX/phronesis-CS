"use client";

import React, { createContext, useContext, useId, useState } from "react";
import { Check, ChevronRight, Copy } from "lucide-react";
import styles from "./Agentic.module.css";
import { type CellKind, columnKind, isEmptyCell, isScalarList, listText, tableLayout } from "./tableColumns";

export { styles };

/** An array, or [] for anything else (cards never trust the shape of backend data). */
export function arr<T = any>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

export function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export function str(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return "";
  }
}

/** A number with sensible precision for tables; em dash when missing. */
export function fmt(v: unknown, digits = 3): string {
  if (!isNum(v)) return typeof v === "string" && v ? v : "—";
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(2);
  return Number(v.toFixed(digits)).toString();
}

export function pct(v: unknown): string {
  return isNum(v) ? `${Math.round(v * 100)}%` : "—";
}

export function humanize(key: string): string {
  const t = key.replace(/[_-]+/g, " ").trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : key;
}

export type Tone = "ok" | "info" | "warn" | "bad" | "neutral" | "faint" | "accent";

const TONE_CLASS: Record<Tone, string> = {
  ok: styles.toneOk,
  info: styles.toneInfo,
  warn: styles.toneWarn,
  bad: styles.toneBad,
  neutral: styles.toneNeutral,
  faint: styles.toneFaint,
  accent: styles.toneAccent,
};

export function Pill({ tone = "neutral", title, children }: { tone?: Tone; title?: string; children: React.ReactNode }) {
  return (
    <span className={`${styles.pill} ${TONE_CLASS[tone]}`} title={title}>
      {children}
    </span>
  );
}

/** Tone for a generic status word (node, phase, job, sub-question). */
export function statusTone(status: unknown): Tone {
  const s = str(status).toLowerCase();
  if (["done", "ok", "succeeded", "success", "answered", "approved", "completed", "supported", "identifiable", "high"].includes(s)) return "ok";
  if (["running", "queued", "pending", "in_progress", "waiting_approval", "edited", "auto", "medium"].includes(s)) return "info";
  if (["partial", "skipped", "timeout", "interrupted", "low", "practically_non_identifiable", "neutral"].includes(s)) return "warn";
  if (["error", "failed", "missing", "rejected", "cancelled", "contradicts", "structurally_non_identifiable", "exhausted"].includes(s)) return "bad";
  return "neutral";
}

export function dotClass(status: unknown): string {
  const t = statusTone(status);
  if (t === "ok") return `${styles.dot} ${styles.dotOk}`;
  if (t === "bad") return `${styles.dot} ${styles.dotBad}`;
  if (t === "warn") return `${styles.dot} ${styles.dotWarn}`;
  if (t === "info") return `${styles.dot} ${styles.dotInfo}`;
  return styles.dot;
}

/** The open state the disclosure policy chose for the card(s) inside; undefined leaves each card's own default. */
export const CardDisclosureContext = createContext<boolean | undefined>(undefined);

export function Card({
  icon,
  title,
  meta,
  label,
  actions,
  children,
  collapsible = false,
  defaultOpen = true,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  meta?: React.ReactNode;
  label: string;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  // The disclosure policy (MessageArtifacts) overrides the card's own default. Only a reader's click is stored, so an
  // untouched card follows the policy as new data arrives, and a reader's choice survives later updates.
  const policy = useContext(CardDisclosureContext);
  const [manual, setManual] = useState<boolean | null>(null);
  const open = manual ?? policy ?? defaultOpen;
  const setOpen = (next: (prev: boolean) => boolean) => setManual(next(open));
  const bodyId = useId();

  return (
    <section
      className={`${styles.card} ${collapsible ? styles.cardCollapsible : ""} ${collapsible && !open ? styles.cardCollapsed : ""}`}
      aria-label={label}
    >
      {/* The whole header is a mouse target; the toggle button is the one control for keyboard and screen readers. */}
      <header
        className={`${styles.head} ${collapsible ? styles.headClickable : ""} ${collapsible && !open ? styles.headCollapsed : ""}`}
        onClick={collapsible ? () => setOpen((prev) => !prev) : undefined}
      >
        <span className={styles.icon} aria-hidden="true">
          {icon}
        </span>
        <div className={styles.headText}>
          <div className={styles.title}>{title}</div>
          {meta ? <div className={styles.meta}>{meta}</div> : null}
        </div>
        <div className={styles.headActions} onClick={(e) => e.stopPropagation()}>
          {actions}
          {collapsible && (
            <button
              type="button"
              className={styles.collapseToggle}
              onClick={() => setOpen((prev) => !prev)}
              aria-expanded={open}
              aria-controls={bodyId}
              aria-label={open ? "Hide details" : "Show details"}
              title={open ? "Hide details" : "Show details"}
            >
              <ChevronRight
                className={`${styles.chev} ${open ? styles.chevOpen : ""}`}
                style={{ width: 16, height: 16 }}
                aria-hidden="true"
              />
            </button>
          )}
        </div>
      </header>
      {(!collapsible || open) && (
        <div id={bodyId} className={styles.cardBody}>
          {children}
        </div>
      )}
    </section>
  );
}

export function Disclosure({
  summary,
  children,
  defaultOpen = false,
}: {
  summary: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details className={styles.details} open={defaultOpen || undefined}>
      <summary>
        <ChevronRight className={styles.chev} style={{ width: 13, height: 13 }} aria-hidden="true" />
        {summary}
      </summary>
      <div className={styles.detailsBody}>{children}</div>
    </details>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={styles.btn}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1600);
        } catch {
          // clipboard unavailable (insecure context)
        }
      }}
      aria-label={copied ? "Copied" : label}
    >
      {copied ? <Check style={{ width: 12, height: 12 }} aria-hidden="true" /> : <Copy style={{ width: 12, height: 12 }} aria-hidden="true" />}
      {copied ? "Copied" : label}
    </button>
  );
}

/** One labelled horizontal bar; `value` in [0, max]. */
export function Bar({
  label,
  value,
  max = 1,
  display,
  tone = "info",
  title,
}: {
  label: React.ReactNode;
  value: number;
  max?: number;
  display?: React.ReactNode;
  tone?: "info" | "ok" | "warn" | "bad";
  title?: string;
}) {
  const ratio = max > 0 && isNum(value) ? Math.max(0, Math.min(1, value / max)) : 0;
  const fill =
    tone === "ok" ? styles.barFillOk : tone === "warn" ? styles.barFillWarn : tone === "bad" ? styles.barFillBad : "";
  return (
    <div className={styles.barRow} title={title}>
      <span className={styles.barLabel}>{label}</span>
      <span
        className={styles.barTrack}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={isNum(value) ? value : 0}
        aria-label={typeof label === "string" ? label : undefined}
      >
        <span className={`${styles.barFill} ${fill}`} style={{ width: `${ratio * 100}%` }} />
      </span>
      <span className={styles.barValue}>{display ?? fmt(value, 2)}</span>
    </div>
  );
}

/**
 * Rows of unknown shape as a table: scalar fields and scalar lists (joined), empty / "n/a" columns hidden. At most
 * `maxCols` columns show at first; the rest are counted in a "N more columns" toggle rather than silently dropped
 * (UI-022). `priority` columns (significance, confidence) are placed first. Only long text columns get a minimum
 * width, so the table wraps inside the card rather than scrolling for one long column.
 */
export function GenericTable({
  rows,
  maxRows = 20,
  maxCols = 6,
  priority = [],
}: {
  rows: Record<string, any>[];
  maxRows?: number;
  maxCols?: number;
  priority?: string[];
}) {
  const [allCols, setAllCols] = useState(false);
  const valid = rows.filter((r) => r && typeof r === "object");
  const list = valid.slice(0, maxRows);
  if (!list.length) return null;
  const layout = tableLayout(list, maxCols, priority);
  const cols = allCols ? [...layout.columns, ...layout.hidden] : layout.columns;
  if (!cols.length) return null;
  const kinds = Object.fromEntries(cols.map((c) => [c, columnKind(list, c)]));
  const cellClass: Record<CellKind, string | undefined> = {
    num: styles.num,
    short: styles.shortCell,
    text: styles.textCell,
    long: styles.wrapCell,
  };
  const cell = (v: unknown) => {
    if (isNum(v)) return fmt(v);
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (isScalarList(v)) return listText(v) || "—";
    return isEmptyCell(v) ? "—" : str(v);
  };
  return (
    <>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c} scope="col" className={kinds[c] === "num" ? styles.num : undefined}>
                  {humanize(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.map((r, i) => (
              <tr key={i}>
                {cols.map((c) => (
                  <td key={c} className={isNum(r[c]) ? styles.num : cellClass[kinds[c] === "num" ? "short" : kinds[c]]}>
                    {cell(r[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(layout.hidden.length > 0 || valid.length > list.length) && (
        <p className={styles.note}>
          {layout.hidden.length > 0 && (
            <button type="button" className={styles.chipMore} onClick={() => setAllCols((v) => !v)} aria-expanded={allCols}>
              {allCols
                ? "show fewer columns"
                : `${layout.hidden.length} more column${layout.hidden.length === 1 ? "" : "s"}: ${layout.hidden.map(humanize).join(", ")}`}
            </button>
          )}
          {valid.length > list.length && ` ${list.length} of ${valid.length} rows shown.`}
        </p>
      )}
    </>
  );
}

/** First present value among `keys` on `obj`. */
export function pick(obj: unknown, ...keys: string[]): any {
  if (!obj || typeof obj !== "object") return undefined;
  for (const k of keys) {
    const v = (obj as Record<string, any>)[k];
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return undefined;
}

/** Only http(s) links are rendered as anchors. */
export function safeUrl(v: unknown): string | null {
  if (typeof v !== "string") return null;
  try {
    const u = new URL(v);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

export function ExtLink({ href, children }: { href: unknown; children: React.ReactNode }) {
  const url = safeUrl(href);
  if (!url) return <>{children}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className={styles.link}>
      {children}
    </a>
  );
}

/**
 * A titled block inside a Card: uppercase heading, optional right-aligned meta. `hint` is a mouse tooltip for minor
 * detail (timings, sources) that is also exposed as screen-reader text; `caveat` is shown as visible text under the
 * heading, for anything a reader must not miss (UI-019: safety text is never hover-only).
 */
export function Section({
  title,
  meta,
  hint,
  caveat,
  children,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  hint?: string;
  caveat?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead} title={hint}>
        <span className={styles.sectionTitle}>
          {title}
          {hint ? <span className="sr-only"> ({hint})</span> : null}
        </span>
        {meta != null && meta !== "" ? <span className={styles.meta}>{meta}</span> : null}
      </div>
      {caveat ? <p className={styles.sectionCaveat}>{caveat}</p> : null}
      {children}
    </div>
  );
}

export interface ChipItem {
  key: string;
  label: React.ReactNode;
  href?: string | null;
  title?: string;
  /** Emphasised (e.g. matches the question's disease); explain it with `highlightHint` on the list. */
  highlight?: boolean;
}

/**
 * Names (drugs, genes, diseases, pathways) as tinted chips. Shows `limit` and a "+N more" toggle; `extra` counts
 * items the backend reported but did not send (shown as plain text, nothing to expand).
 */
export function ChipList({
  items,
  limit = 8,
  extra = 0,
  highlightHint,
  empty = "none recorded",
}: {
  items: ChipItem[];
  limit?: number;
  extra?: number;
  highlightHint?: string;
  empty?: string;
}) {
  const [open, setOpen] = useState(false);
  if (!items.length && !extra) return <span className={styles.muted}>{empty}</span>;
  const shown = open ? items : items.slice(0, limit);
  const hidden = items.length - shown.length;
  return (
    <span className={styles.chipList}>
      {shown.map((it) => {
        const url = safeUrl(it.href);
        const title = it.highlight && highlightHint ? [it.title, highlightHint].filter(Boolean).join(" · ") : it.title;
        return url ? (
          <a key={it.key} className={styles.chipItem} data-highlight={it.highlight || undefined} href={url} target="_blank" rel="noopener noreferrer" title={title}>
            {it.label}
          </a>
        ) : (
          <span key={it.key} className={styles.chipItem} data-highlight={it.highlight || undefined} title={title}>
            {it.label}
          </span>
        );
      })}
      {hidden > 0 && (
        <button type="button" className={styles.chipMore} onClick={() => setOpen(true)}>
          +{hidden} more
        </button>
      )}
      {open && items.length > limit && (
        <button type="button" className={styles.chipMore} onClick={() => setOpen(false)}>
          show less
        </button>
      )}
      {extra > 0 && <span className={styles.chipExtra}>+{extra} not shown</span>}
    </span>
  );
}

/** A row of headline numbers (label above, value below), wrapping on narrow screens. */
export function Stats({ items }: { items: { label: React.ReactNode; value: React.ReactNode; sub?: React.ReactNode; title?: string }[] }) {
  return (
    <div className={styles.budgetRow}>
      {items.map((s, i) => (
        <div key={i} className={styles.budgetStat} title={s.title}>
          <span className={styles.budgetLabel}>{s.label}</span>
          <span className={styles.budgetValue}>
            {s.value}
            {s.sub != null && <span className={styles.budgetLimit}> {s.sub}</span>}
          </span>
        </div>
      ))}
    </div>
  );
}
