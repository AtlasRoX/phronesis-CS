"use client";

import React, { useId, useState } from "react";
import { ChevronRight, Loader2, Play } from "lucide-react";

import styles from "@/components/Export.module.css";
import { rerun } from "@/services/exportService";
import type { FieldChange, RerunMode, RerunReport, ToolDiff } from "@/types/runs";

export interface RerunDiffPanelProps {
  runId: string;
  backendUrl: string;
  authHeaders: (extra?: Record<string, string>) => Record<string, string>;
}

const MODES: { key: RerunMode; label: string; hint: string }[] = [
  {
    key: "tools",
    label: "Replay tool calls",
    hint: "Re-executes the recorded tool calls with the same inputs. No model call; differences come from upstream data or code.",
  },
  {
    key: "full",
    label: "Full re-run",
    hint: "Runs the whole question again, including the model and verification, in an isolated thread that does not use or save memory.",
  },
];

const OVERALL: Record<RerunReport["overall"], { label: string; className: string }> = {
  identical: { label: "Identical", className: styles.chipIdentical },
  drifted: { label: "Upstream drift", className: styles.chipDrifted },
  changed: { label: "Changed", className: styles.chipChanged },
};

const ID_SET_PATHS = new Set(["pmids", "dois", "urls", "ensembl_ids", "kegg_ids"]);

const TOOL_LABELS: Record<string, string> = {
  vivarix_simulate_time_course: "Vivarix time course",
  vivarix_compute_steady_state: "Vivarix steady state",
  lithoscribe_literature_synthesis: "Lithoscribe literature",
  synapta_opentargets_search: "Synapta target search",
  synapta_opentargets_associations: "Synapta associations",
  synapta_kegg_pathways: "Synapta KEGG pathways",
  websurfer_search: "WebSurfer search",
  lumicell_analyze_single_cell: "Lumicell single-cell",
};

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(+value.toPrecision(6));
  if (typeof value === "string") return value.length > 80 ? `${value.slice(0, 79)}…` : value;
  const text = JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

export function describeChange(c: FieldChange): string {
  if (c.tier === "added") return `${c.path}: +${show(c.after)}`;
  if (c.tier === "removed") return `${c.path}: −${show(c.before)}`;
  let text = `${c.path} ${show(c.before)} → ${show(c.after)}`;
  if (typeof c.rel_change === "number") {
    const before = typeof c.before === "number" ? c.before : null;
    const after = typeof c.after === "number" ? c.after : null;
    const sign = before !== null && after !== null && after < before ? "−" : "+";
    text += ` (${sign}${(c.rel_change * 100).toFixed(2)}%`;
    text += c.tier === "within_tolerance" ? ", within tolerance)" : ")";
  }
  return text;
}

function tierClass(tier: FieldChange["tier"]): string {
  if (tier === "changed" || tier === "removed") return styles.bad;
  if (tier === "within_tolerance" || tier === "added") return styles.warn;
  return "";
}

function idSetChanges(diffs: ToolDiff[]) {
  const added: string[] = [];
  const removed: string[] = [];
  for (const d of diffs) {
    for (const c of d.changes ?? []) {
      if (!ID_SET_PATHS.has(c.path)) continue;
      if (c.tier === "added" && c.after != null) added.push(`${c.path === "pmids" ? "PMID " : ""}${c.after}`);
      if (c.tier === "removed" && c.before != null) removed.push(`${c.path === "pmids" ? "PMID " : ""}${c.before}`);
    }
  }
  return { added, removed };
}

function DiffText({ text }: { text: string }) {
  return (
    <pre className={styles.diff}>
      {text.split("\n").map((line, i) => (
        <span
          key={i}
          className={
            line.startsWith("+") && !line.startsWith("+++")
              ? styles.diffAdd
              : line.startsWith("-") && !line.startsWith("---")
                ? styles.diffDel
                : undefined
          }
        >
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

function ChangeRows({ title, rows }: { title: string; rows: FieldChange[] }) {
  if (!rows.length) return null;
  return (
    <div className={styles.section}>
      <h4 className={styles.sectionTitle}>{title}</h4>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Original</th>
              <th scope="col">Re-run</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => (
              <tr key={`${c.path}-${i}`}>
                <td className={styles.mono}>{c.path}</td>
                <td className={styles.mono}>{show(c.before)}</td>
                <td className={`${styles.mono} ${tierClass(c.tier)}`}>{show(c.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ReportView({ report }: { report: RerunReport }) {
  const [diffOpen, setDiffOpen] = useState(false);
  const overall = OVERALL[report.overall] ?? OVERALL.changed;
  const toolDiffs = report.tool_diffs ?? [];
  const literature = idSetChanges(toolDiffs);
  const toolRows = toolDiffs.filter(
    (d) => d.status_before !== d.status_after || !d.output_hash_equal || (d.changes ?? []).length > 0,
  );
  const unchanged = toolDiffs.length - toolRows.length;
  const versionRows = [...(report.source_version_changes ?? []), ...(report.biomodel_changes ?? [])];
  const notes = report.notes ?? [];
  const verification = report.verification_delta;
  const scoreDelta = typeof verification?.score_delta === "number" ? verification.score_delta : null;

  return (
    <>
      <div className={styles.summary}>
        <span className={`${styles.chip} ${overall.className}`}>{overall.label}</span>
        <span>
          {toolDiffs.length} tool call{toolDiffs.length === 1 ? "" : "s"} compared
          {unchanged > 0 && toolRows.length > 0 ? `, ${unchanged} unchanged` : ""}
        </span>
        {report.provenance === "client_supplied" && <span className={styles.warn}>client-supplied manifest</span>}
      </div>

      {toolRows.length > 0 && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Tool changes</h4>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Tool</th>
                  <th scope="col">Status</th>
                  <th scope="col">Output hash</th>
                  <th scope="col">Changes</th>
                </tr>
              </thead>
              <tbody>
                {toolRows.map((d, rowIdx) => {
                  const changes = d.changes ?? [];
                  const fieldChanges = changes.filter((c) => !ID_SET_PATHS.has(c.path));
                  return (
                    <tr key={`${d.key}-${rowIdx}`}>
                      <td>{TOOL_LABELS[d.tool_name] ?? d.tool_name}</td>
                      <td className={d.status_before !== d.status_after ? styles.bad : undefined}>
                        {d.status_before === d.status_after ? d.status_after : `${d.status_before} → ${d.status_after}`}
                      </td>
                      <td className={d.output_hash_equal ? styles.ok : styles.warn}>
                        {d.output_hash_equal ? "equal" : "differs"}
                      </td>
                      <td>
                        {fieldChanges.length === 0 ? (
                          <span className={styles.mono}>{changes.length ? "identifier sets (below)" : "—"}</span>
                        ) : (
                          fieldChanges.slice(0, 6).map((c, i) => (
                            <div key={i} className={`${styles.mono} ${tierClass(c.tier)}`}>
                              {describeChange(c)}
                            </div>
                          ))
                        )}
                        {fieldChanges.length > 6 && <div className={styles.mono}>+{fieldChanges.length - 6} more</div>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {(report.missing_in_new?.length || report.new_only?.length) ? (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Calls that did not match</h4>
          <ul className={styles.idList}>
            {(report.missing_in_new ?? []).map((k, i) => (
              <li key={`m-${i}`} className={styles.bad}>
                −{k}
              </li>
            ))}
            {(report.new_only ?? []).map((k, i) => (
              <li key={`n-${i}`} className={styles.warn}>
                +{k}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(literature.added.length > 0 || literature.removed.length > 0) && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Literature and web sources</h4>
          <ul className={styles.idList}>
            {literature.added.map((id, i) => (
              <li key={`a-${i}`} className={styles.warn}>
                +{id}
              </li>
            ))}
            {literature.removed.map((id, i) => (
              <li key={`r-${i}`} className={styles.bad}>
                −{id}
              </li>
            ))}
          </ul>
        </div>
      )}

      <ChangeRows title="Data versions" rows={versionRows} />
      <ChangeRows title="Environment" rows={report.environment_changes ?? []} />

      {verification && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Verification</h4>
          <span className={verification.passed_changed ? styles.bad : undefined}>
            Score {show(verification.before?.score)} → {show(verification.after?.score)}
            {scoreDelta !== null && scoreDelta !== 0 ? ` (${scoreDelta > 0 ? "+" : ""}${scoreDelta.toFixed(2)})` : ""}
            {" · "}
            {verification.before?.passed ? "passed" : "not passed"} → {verification.after?.passed ? "passed" : "not passed"}
          </span>
        </div>
      )}

      {report.mode === "full" && typeof report.answer_unified_diff === "string" && (
        <div className={styles.section}>
          {report.answer_unified_diff ? (
            <>
              <button type="button" className={styles.diffToggle} aria-expanded={diffOpen} onClick={() => setDiffOpen((v) => !v)}>
                <ChevronRight
                  style={{ width: 13, height: 13, transform: diffOpen ? "rotate(90deg)" : undefined }}
                  aria-hidden="true"
                />
                Answer diff
              </button>
              {diffOpen && <DiffText text={report.answer_unified_diff} />}
            </>
          ) : (
            <span className={styles.ok}>The answer text is identical.</span>
          )}
        </div>
      )}

      {notes.length > 0 && (
        <ul className={styles.notes}>
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </>
  );
}

/** Inline panel that re-runs a stored run and shows what changed. */
export function RerunDiffPanel({ runId, backendUrl, authHeaders }: RerunDiffPanelProps) {
  const [mode, setMode] = useState<RerunMode>("tools");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<RerunReport | null>(null);
  const titleId = useId();

  const start = async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await rerun(runId, mode, backendUrl, authHeaders()));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Re-run failed");
    } finally {
      setLoading(false);
    }
  };

  const onModeKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      setMode((m) => (m === "tools" ? "full" : "tools"));
    }
  };

  const current = MODES.find((m) => m.key === mode) ?? MODES[0];

  return (
    <section className={styles.panel} aria-labelledby={titleId}>
      <div className={styles.panelHeader}>
        <h3 id={titleId} className={styles.panelTitle}>
          Re-run &amp; compare
        </h3>
        <div role="radiogroup" aria-label="Re-run mode" className={styles.segmented} onKeyDown={onModeKeyDown}>
          {MODES.map((m) => (
            <button
              key={m.key}
              type="button"
              role="radio"
              aria-checked={mode === m.key}
              tabIndex={mode === m.key ? 0 : -1}
              className={styles.segment}
              disabled={loading}
              onClick={() => setMode(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
        <button type="button" className={styles.runButton} onClick={start} disabled={loading}>
          {loading ? (
            <Loader2 className={styles.spin} style={{ width: 13, height: 13 }} aria-hidden="true" />
          ) : (
            <Play style={{ width: 12, height: 12 }} aria-hidden="true" />
          )}
          {loading ? "Running…" : "Run"}
        </button>
      </div>
      <p className={styles.modeHint}>{current.hint}</p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div aria-live="polite">{report && !loading && <ReportView report={report} />}</div>
    </section>
  );
}
