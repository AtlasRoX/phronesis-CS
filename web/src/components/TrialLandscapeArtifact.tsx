"use client";

import React, { useMemo, useState } from "react";
import { ChevronRight, ClipboardList } from "lucide-react";
import type { ClinicalTrialsArtifact, TrialArmValue, TrialOutcomeResult, TrialRecord } from "@/types/trials";
import { Card, ChipList, Disclosure, Pill, humanize, styles as ui, type Tone } from "@/components/agentic/ui";
import styles from "./TrialLandscapeArtifact.module.css";

export interface TrialLandscapeArtifactProps {
  data: ClinicalTrialsArtifact;
}

/** Pill tone and the word used for the status mix in the card meta. */
const STATUS: Record<string, { tone: Tone; group: string }> = {
  RECRUITING: { tone: "ok", group: "recruiting" },
  ENROLLING_BY_INVITATION: { tone: "ok", group: "recruiting" },
  AVAILABLE: { tone: "ok", group: "recruiting" },
  NOT_YET_RECRUITING: { tone: "ok", group: "not yet recruiting" },
  ACTIVE_NOT_RECRUITING: { tone: "info", group: "active" },
  COMPLETED: { tone: "neutral", group: "completed" },
  APPROVED_FOR_MARKETING: { tone: "neutral", group: "completed" },
  TERMINATED: { tone: "bad", group: "stopped" },
  WITHDRAWN: { tone: "bad", group: "stopped" },
  SUSPENDED: { tone: "bad", group: "stopped" },
  NO_LONGER_AVAILABLE: { tone: "bad", group: "stopped" },
  TEMPORARILY_NOT_AVAILABLE: { tone: "bad", group: "stopped" },
  WITHHELD: { tone: "bad", group: "stopped" },
};
const UNKNOWN_STATUS = { tone: "faint" as Tone, group: "status unknown" };

function phaseLabel(phases: string[] | undefined): string {
  const list = phases ?? [];
  const nums = list.filter((p) => /^PHASE\d$/.test(p)).map((p) => p.replace("PHASE", ""));
  if (list.includes("EARLY_PHASE1")) return `Early Phase 1${nums.length ? `/${nums.join("/")}` : ""}`;
  if (nums.length) return `Phase ${nums.join("/")}`;
  return "N/A";
}

function statusLabel(status?: string | null): string {
  if (!status) return "—";
  if (status === "ACTIVE_NOT_RECRUITING") return "Active, not recruiting";
  if (status === "UNKNOWN") return "Unknown status";
  const text = status.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function enrollmentLabel(t: TrialRecord): string {
  if (t.enrollment_count == null) return "—";
  const suffix = t.enrollment_type === "ACTUAL" ? " actual" : t.enrollment_type === "ESTIMATED" ? " est." : "";
  return `${t.enrollment_count}${suffix}`;
}

/** The `limit` most frequent labels as "5 Phase 3, 4 Phase 2"; ties keep first-seen order. */
function mix(labels: string[], limit = 3): string {
  const counts = new Map<string, number>();
  for (const l of labels) counts.set(l, (counts.get(l) ?? 0) + 1);
  return [...counts]
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([label, n]) => `${n} ${label}`)
    .join(", ");
}

/** A registry value exactly as posted; "NA" becomes an em dash with the registry's comment as a tooltip. */
function Val({ v, comment }: { v?: string | null; comment?: string | null }) {
  if (v == null || v === "") return <span className={styles.na}>—</span>;
  if (v === "NA") {
    return (
      <span className={styles.na} title={comment || "Not available (NA) in the registry record"}>
        —
      </span>
    );
  }
  return <>{v}</>;
}

/** Bounds or spread as posted, labelled (tooltip) with the registry's dispersion type, e.g. "Full Range". */
function interval(m: TrialArmValue, dispersion?: string | null) {
  const title = dispersion || undefined;
  if (m.lower == null && m.upper == null) {
    return m.spread != null ? (
      <span className={styles.ci} title={title}>
        {" "}
        ± {m.spread}
      </span>
    ) : null;
  }
  return (
    <span className={styles.ci} title={title}>
      {" "}
      [<Val v={m.lower} comment={m.comment} /> to <Val v={m.upper} comment={m.comment} />]
    </span>
  );
}

const OUTCOME_TYPE: Record<string, string> = {
  PRIMARY: "Primary",
  SECONDARY: "Secondary",
  OTHER_PRE_SPECIFIED: "Other pre-specified",
  POST_HOC: "Post hoc",
};

function armLabel(m: TrialArmValue): string {
  return [m.group_title || m.group_id, m.class_title, m.category_title].filter(Boolean).join(" · ");
}

function analysisLine(r: TrialOutcomeResult): string[] {
  const titles = new Map((r.measurements ?? []).map((m) => [m.group_id, m.group_title || m.group_id]));
  return (r.analyses ?? []).map((a) => {
    const groups = (a.group_ids ?? []).map((g) => titles.get(g) || g).join(" vs ");
    const ci =
      a.ci_lower != null || a.ci_upper != null
        ? ` (${a.ci_pct ? `${a.ci_pct}% CI ` : "CI "}${a.ci_lower ?? "NA"}–${a.ci_upper ?? "NA"})`
        : "";
    const p = a.p_value != null ? `, p${/^[<>≤≥=]/.test(a.p_value) ? "" : "="}${a.p_value}` : "";
    const head = [a.param_type, a.param_value].filter(Boolean).join(" ");
    return [head + ci + p, a.method, groups].filter(Boolean).join(" · ");
  });
}

// Several conditions are searched one by one and merged; api_params shows only the first search.
function searchedConditions(q: ClinicalTrialsArtifact["query"]): string[] {
  return (q as { conditions?: string[] }).conditions ?? [];
}

function strategyTerms(q: ClinicalTrialsArtifact["query"]): string {
  if (q.strategy === "nct_ids") return (q.nct_ids ?? []).join(", ");
  const p = q.api_params || {};
  const conditions = searchedConditions(q);
  const parts = [
    p["query.intr"] && `intervention: ${p["query.intr"]}`,
    p["query.term"] && `term: ${p["query.term"]}`,
    conditions.length > 1
      ? `conditions: ${conditions.join("; ")} (one search each)`
      : p["query.cond"] && `condition: ${p["query.cond"]}`,
    q.phases?.length ? phaseLabel(q.phases) : null,
    q.statuses?.length ? q.statuses.map(statusLabel).join(", ") : null,
  ];
  return parts.filter(Boolean).join(" · ");
}

/** True when a registry name contains one of the searched terms (case-insensitive). */
function matchesSearch(name: string, terms: string[]): boolean {
  const n = name.toLowerCase();
  return terms.some((t) => n.includes(t));
}

function PostedResults({ trial }: { trial: TrialRecord }) {
  const posted = (trial.posted_results ?? []).filter((r) => (r.measurements ?? []).length > 0);
  if (!posted.length) {
    return (
      <p className={ui.muted}>
        {trial.has_results ? (
          <>
            Results are posted on ClinicalTrials.gov but were not retrieved here.{" "}
            <a href={`${trial.url}?tab=results`} target="_blank" rel="noopener noreferrer" className={ui.link}>
              View results
            </a>
          </>
        ) : (
          "No results posted on ClinicalTrials.gov."
        )}
      </p>
    );
  }
  return (
    <div className={styles.resultsWrap}>
      <div className={ui.tableScroll}>
        <table className={`${ui.table} ${styles.results}`}>
          <thead>
            <tr>
              <th scope="col">Endpoint</th>
              <th scope="col">Arm</th>
              <th scope="col" className={ui.num}>
                N
              </th>
              <th scope="col" className={ui.num}>
                Value
              </th>
            </tr>
          </thead>
          <tbody>
            {posted.map((r, ri) =>
              r.measurements.map((m, mi) => (
                <tr key={`${ri}-${mi}`}>
                  {mi === 0 && (
                    <td rowSpan={r.measurements.length} className={styles.endpoint}>
                      <div>
                        <Pill tone={r.type === "PRIMARY" ? "info" : "faint"}>{OUTCOME_TYPE[r.type] ?? r.type}</Pill>
                      </div>
                      <div className={styles.endpointTitle}>{r.title}</div>
                      {r.time_frame && (
                        <div className={styles.timeFrame} title={r.time_frame}>
                          {r.time_frame}
                        </div>
                      )}
                    </td>
                  )}
                  <td>{armLabel(m)}</td>
                  <td className={ui.num}>
                    <Val v={m.n} />
                  </td>
                  <td className={ui.num}>
                    <Val v={m.value} comment={m.comment} />
                    {interval(m, r.dispersion_type)}
                    {r.unit && m.value && m.value !== "NA" ? <span className={styles.unit}> {r.unit}</span> : null}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
      {posted.some((r) => (r.analyses ?? []).length > 0) && (
        <ul className={styles.analyses}>
          {posted.flatMap((r, ri) =>
            analysisLine(r).map((line, ai) => (
              <li key={`${ri}-${ai}`}>
                <span className={styles.analysisTitle}>{r.title}:</span> {line}
              </li>
            )),
          )}
        </ul>
      )}
    </div>
  );
}

function TrialRow({ trial, terms }: { trial: TrialRecord; terms: string[] }) {
  const status = STATUS[trial.overall_status || ""] ?? UNKNOWN_STATUS;
  const conditions = (trial.conditions ?? []).map((c, i) => ({
    key: `${i}-${c}`,
    label: c,
    highlight: matchesSearch(c, terms),
  }));
  const interventions = (trial.interventions ?? [])
    .filter((iv) => iv.name || iv.type)
    .map((iv, i) => ({
      key: `${i}-${iv.name ?? iv.type}`,
      label: iv.name || humanize((iv.type ?? "").toLowerCase()),
      title: iv.type ? humanize(iv.type.toLowerCase()) : undefined,
      highlight: !!iv.name && matchesSearch(iv.name, terms),
    }));
  return (
    <details className={styles.trial}>
      <summary className={styles.row}>
        <span className={styles.cellId} data-label="NCT ID">
          <a href={trial.url} target="_blank" rel="noopener noreferrer" className={styles.nct}>
            {trial.nct_id}
          </a>
          {trial.acronym && <span className={styles.acronym}>{trial.acronym}</span>}
        </span>
        <span className={styles.cell} data-label="Phase">
          {phaseLabel(trial.phases)}
        </span>
        <span className={styles.cell} data-label="Status">
          <Pill tone={status.tone} title={trial.why_stopped || undefined}>
            {statusLabel(trial.overall_status)}
          </Pill>
        </span>
        <span className={`${styles.cell} ${styles.num}`} data-label="Enrollment">
          {enrollmentLabel(trial)}
        </span>
        <span className={`${styles.cell} ${styles.sponsor}`} data-label="Lead sponsor" title={trial.lead_sponsor || undefined}>
          {trial.lead_sponsor || "—"}
        </span>
        <span className={styles.cellBadge}>
          {trial.has_results && <span className={styles.resultsBadge}>Results</span>}
          <ChevronRight className={styles.rowChev} style={{ width: 14, height: 14 }} aria-hidden="true" />
        </span>
      </summary>
      <div className={styles.detail}>
        {trial.brief_title && <p className={styles.briefTitle}>{trial.brief_title}</p>}
        <dl className={styles.facts}>
          {conditions.length > 0 && (
            <>
              <dt>Conditions</dt>
              <dd>
                <ChipList items={conditions} limit={6} highlightHint="Matches the search" />
              </dd>
            </>
          )}
          {interventions.length > 0 && (
            <>
              <dt>Interventions</dt>
              <dd>
                <ChipList items={interventions} limit={8} highlightHint="Matches the search" />
              </dd>
            </>
          )}
          {trial.primary_outcomes?.length > 0 && (
            <>
              <dt>Primary outcomes</dt>
              <dd>
                <ul className={styles.outcomes}>
                  {trial.primary_outcomes.map((po, i) => (
                    <li key={i}>
                      {po.measure}
                      {po.time_frame && <span className={styles.timeFrameInline}> ({po.time_frame})</span>}
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          )}
          {trial.why_stopped && (
            <>
              <dt>Why stopped</dt>
              <dd>{trial.why_stopped}</dd>
            </>
          )}
          {(trial.start_date || trial.primary_completion_date) && (
            <>
              <dt>Dates</dt>
              <dd>
                {[
                  trial.start_date && `start ${trial.start_date}`,
                  trial.primary_completion_date && `primary completion ${trial.primary_completion_date}`,
                  trial.results_first_posted && `results posted ${trial.results_first_posted}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </dd>
            </>
          )}
          {trial.result_pmids?.length > 0 && (
            <>
              <dt>Result publications</dt>
              <dd>
                {trial.result_pmids.map((p, i) => (
                  <React.Fragment key={p}>
                    {i > 0 && ", "}
                    <a href={`https://pubmed.ncbi.nlm.nih.gov/${p}/`} target="_blank" rel="noopener noreferrer" className={ui.link}>
                      PMID {p}
                    </a>
                  </React.Fragment>
                ))}
              </dd>
            </>
          )}
        </dl>
        <PostedResults trial={trial} />
      </div>
    </details>
  );
}

/** Registered clinical trials behind an answer, rendered from the ClinicalTrials.gov tool output (never from model text). */
export function TrialLandscapeArtifact({ data }: TrialLandscapeArtifactProps) {
  const [resultsOnly, setResultsOnly] = useState(false);
  const trials = useMemo(() => data?.trials ?? [], [data]);
  const terms = useMemo(() => {
    const q = data?.query;
    if (!q) return [];
    const raw = [q.condition, q.intervention, q.term, ...searchedConditions(q)];
    // Very short terms ("CF", "MS") would match inside unrelated words.
    return Array.from(new Set(raw.filter((t): t is string => !!t && t.trim().length >= 3).map((t) => t.trim().toLowerCase())));
  }, [data]);
  if (!data?.query) return null;

  const withResults = trials.filter((t) => t.has_results).length;
  const shown = resultsOnly ? trials.filter((t) => t.has_results) : trials;
  const search = strategyTerms(data.query);
  const count =
    trials.length === data.total_count || data.total_count == null
      ? `${trials.length} trial${trials.length === 1 ? "" : "s"}`
      : `${trials.length} of ${data.total_count} registered`;
  const meta = [
    count,
    mix(trials.map((t) => phaseLabel(t.phases)).map((p) => (p === "N/A" ? "no phase" : p))),
    mix(trials.map((t) => (STATUS[t.overall_status || ""] ?? UNKNOWN_STATUS).group)),
    trials.length > 0 ? `${withResults} with results` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<ClipboardList style={{ width: 15, height: 15 }} />}
      title="Registered clinical trials"
      meta={meta}
      label="Registered clinical trials"
      collapsible
      defaultOpen={false}
    >
      {trials.length === 0 ? (
        <p className={ui.muted}>No registered trials matched this search.</p>
      ) : (
        <>
          {withResults > 0 && withResults < trials.length && (
            <div className={styles.toolbar}>
              <button
                type="button"
                className={styles.toggle}
                aria-pressed={resultsOnly}
                onClick={() => setResultsOnly((v) => !v)}
              >
                With posted results only ({withResults})
              </button>
            </div>
          )}
          <div className={styles.list} role="list">
            <div className={`${styles.row} ${styles.headerRow}`} aria-hidden="true">
              <span>NCT ID</span>
              <span>Phase</span>
              <span>Status</span>
              <span className={styles.num}>Enrollment</span>
              <span>Lead sponsor</span>
              <span />
            </div>
            {shown.map((t) => (
              <div role="listitem" key={t.nct_id}>
                <TrialRow trial={t} terms={terms} />
              </div>
            ))}
          </div>
        </>
      )}

      <Disclosure summary="About these data">
        <p className={ui.note} style={{ marginTop: 0 }}>
          Registry records from ClinicalTrials.gov; results are shown exactly as posted.
          {search && ` Search: ${search}.`}
          {data.total_count != null && ` ${data.total_count} registered match${data.total_count === 1 ? "" : "es"}, ${trials.length} shown.`}
        </p>
        {data.limitations?.length > 0 && (
          <ul className={styles.notes}>
            {data.limitations.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}
      </Disclosure>
    </Card>
  );
}
