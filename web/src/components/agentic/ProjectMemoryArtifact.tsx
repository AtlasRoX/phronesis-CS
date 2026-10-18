"use client";

import React from "react";
import { Brain, ListChecks } from "lucide-react";
import type { MemoryFact, PlanApprovalArtifact as PlanApprovalData, ProjectMemoryArtifact as ProjectMemoryData } from "@/types/agentic";
import { Card, ChipList, Pill, Section, arr, fmt, isNum, statusTone, str, styles } from "./ui";

export function factText(f: MemoryFact): React.ReactNode {
  return (
    <>
      <span style={{ color: "var(--text-heading)" }}>{str(f.subject) || "?"}</span>{" "}
      <span className={styles.mono} style={{ color: "var(--text-faint)" }}>
        {str(f.relation).replace(/_/g, " ") || "→"}
      </span>{" "}
      <span style={{ color: "var(--text-heading)" }}>{str(f.object) || "?"}</span>
    </>
  );
}

/** A fact's origin: the backend sends {thread_id, run_id, tool} (or {origin}); older facts carry a plain string. */
export function sourceText(v: unknown): string {
  if (v && typeof v === "object") return str((v as Record<string, unknown>).tool ?? (v as Record<string, unknown>).origin);
  return str(v);
}

/** Whether a recalled fact came from a verified turn: the backend's verified flag, else the "unverified" tag. */
export function factVerified(f: MemoryFact): boolean | null {
  if (typeof f.verified === "boolean") return f.verified;
  if (typeof f.verified === "number") return f.verified !== 0;
  if (Array.isArray(f.tags)) return !f.tags.includes("unverified");
  return null;
}

export function dateText(v: unknown): string {
  if (v == null || v === "") return "";
  const d = typeof v === "number" ? new Date(v < 1e12 ? v * 1000 : v) : new Date(String(v));
  return Number.isNaN(d.getTime()) ? str(v) : d.toLocaleDateString();
}

/** Facts recalled from the research project's memory (bi-temporal fact graph) for this turn. */
export function ProjectMemoryArtifact({ data }: { data: ProjectMemoryData }) {
  if (!data || typeof data !== "object") return null;
  const recalled = arr<MemoryFact>(data.recalled).filter((f) => f && typeof f === "object").slice(0, 50);
  const written = isNum(data.written) ? data.written : 0;
  const invalidated = isNum(data.invalidated) ? data.invalidated : 0;
  if (!recalled.length && !written && !invalidated) return null;
  const unverified = recalled.filter((f) => factVerified(f) !== true).length;
  const meta = [
    recalled.length ? `${recalled.length} fact${recalled.length === 1 ? "" : "s"} recalled${unverified ? ` (${unverified} unverified)` : ""}` : "nothing recalled",
    written ? `${written} new fact${written === 1 ? "" : "s"} saved` : "",
    invalidated ? `${invalidated} superseded` : "",
    data.project_id ? `project ${data.project_id}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Card
      icon={<Brain style={{ width: 15, height: 15 }} />}
      title="Project memory"
      meta={meta}
      label="Project memory"
      collapsible
      defaultOpen={false}
    >
      {recalled.length > 0 ? (
        <ul className={styles.divList}>
          {recalled.map((f, i) => (
            <li key={f.id || i} className={`${styles.divItem} ${styles.listItem}`}>
              <span>
                {factText(f)}{" "}
                {factVerified(f) === true ? (
                  <Pill tone="ok" title="Recorded from a turn whose claims were all checked and supported">verified</Pill>
                ) : (
                  <Pill tone="warn" title="Recorded from a turn that left claims unsupported or unchecked: background only">unverified</Pill>
                )}
              </span>
              <span className={styles.meta} style={{ marginLeft: "auto", flexShrink: 0 }}>
                {[isNum(f.confidence) ? `confidence ${fmt(f.confidence, 2)}` : "", sourceText(f.source), dateText(f.valid_from)].filter(Boolean).join(" · ")}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>Nothing relevant was recalled from this project.</p>
      )}
      <p className={styles.note}>
        Recalled facts come from earlier answers in this project and are background only: the answer cites this turn&apos;s evidence. Facts
        marked unverified came from turns that left claims unsupported or unchecked and are treated as leads, not evidence. Edit them from
        the project&apos;s memory panel.
      </p>
    </Card>
  );
}

const APPROVAL_LABEL: Record<string, string> = {
  approved: "approved by you",
  edited: "approved with your edits",
  rejected: "rejected by you",
  auto: "approved automatically (non-interactive run)",
  timeout: "continued after 5 minutes without an answer",
};

/** What happened to the plan approval request (F15). It records a decision the user made, so it stays open. */
export function PlanApprovalArtifact({ data }: { data: PlanApprovalData }) {
  if (!data || typeof data !== "object") return null;
  const sources = arr<{ key?: string; label?: string; run?: boolean; reason?: string }>(data.sources).filter((s) => s && typeof s === "object");
  const off = sources.filter((s) => s.run === false);
  const ran = sources.filter((s) => s.run !== false);
  const status = str(data.status) || "unknown";
  const outcome = APPROVAL_LABEL[status] || status;
  return (
    <Card
      icon={<ListChecks style={{ width: 15, height: 15 }} />}
      title="Plan approval"
      meta={
        <>
          <span className={styles.toneText} data-tone={statusTone(status)}>
            {outcome.charAt(0).toUpperCase() + outcome.slice(1)}
          </span>
          {sources.length ? ` · ${ran.length} of ${sources.length} sources ran` : ""}
        </>
      }
      label="Plan approval"
      collapsible
      defaultOpen
    >
      {sources.length === 0 && <p className={styles.muted}>No source list was recorded for this plan.</p>}
      {ran.length > 0 && (
        <Section title="Ran" meta={ran.length}>
          <ChipList
            items={ran.map((s, i) => ({ key: s.key || String(i), label: s.label || s.key || "source", title: s.reason || undefined }))}
            limit={12}
          />
        </Section>
      )}
      {off.length > 0 && (
        <Section title="Switched off" meta={off.length}>
          <ul className={styles.list}>
            {off.map((s, i) => (
              <li key={s.key || i} className={styles.listItem}>
                <span>
                  {s.label || s.key}
                  {s.reason ? <span className={styles.meta}> · {s.reason}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </Card>
  );
}
