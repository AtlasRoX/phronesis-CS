"use client";

import React from "react";
import { AlertTriangle, Check, CircleDashed, MinusCircle, Workflow, XCircle } from "lucide-react";
import type { AgentRunArtifact, AgentTraceRow, FrameworkRoutingArtifact, SubQuestion } from "@/types/agentic";
import { formatSeconds } from "@/lib/duration";
import { evidenceDisplayNames } from "./evidenceLabels";
import { Bar, Card, ChipList, CopyButton, Disclosure, Pill, Section, arr, fmt, humanize, isNum, pct, statusTone, str, styles } from "./ui";

export const FRAMEWORK_NAMES: Record<string, string> = {
  auto: "Auto",
  "authoritative-react-compiler": "Classic pipeline",
  "agentic-plan-execute": "Plan & execute",
  "multi-agent-supervisor": "Supervisor",
  "multi-agent-swarm": "Swarm",
  "multi-agent-debate": "Debate",
};

// budget.limits uses unprefixed keys; used has tokens_estimated for the token count.
const METERS: { key: string; usedKey: string; label: string; seconds?: boolean }[] = [
  { key: "tool_calls", usedKey: "tool_calls", label: "Tool calls" },
  { key: "llm_calls", usedKey: "llm_calls", label: "Model calls" },
  { key: "tokens", usedKey: "tokens_estimated", label: "Tokens (est.)" },
  { key: "seconds", usedKey: "seconds", label: "Time", seconds: true },
];

/** 44757 -> "44.8k", 250000 -> "250k": budget counts read at a glance, never in exponent form. */
function compactCount(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e6) return `${Number((n / 1e6).toFixed(1))}M`;
  if (a >= 1e3) return `${Number((n / 1e3).toFixed(1))}k`;
  return Number(n.toFixed(1)).toString();
}

function meterText(n: number, seconds?: boolean): string {
  return seconds ? formatSeconds(n) : compactCount(n);
}

const PHASE_WORDS: Record<string, string> = { nli: "NLI", llm: "LLM", dag: "DAG", kg: "KG", qa: "QA" };

/** "nli_precompute" -> "NLI precompute". */
function phaseLabel(phase: string): string {
  return humanize(phase)
    .split(" ")
    .map((w) => PHASE_WORDS[w.toLowerCase()] || w)
    .join(" ");
}

interface PhaseGroup {
  phase: string;
  seconds: number | null;
  count: number;
  status: unknown;
  title: string;
}

const TONE_RANK: Record<string, number> = { bad: 3, warn: 2, info: 1 };

/** Pipeline stages (routing, evidence, drafting, rewrites, verification) as the backend times them in agent_run. */
export function isPipelineStage(p: { stage?: unknown } | null | undefined): boolean {
  return str(p?.stage) === "pipeline";
}

/** Repeated phases (a second gap check, an NLI recheck, rewrite_1..rewrite_4) fold into one chip with summed time and a count. */
export function groupPhases(phases: any[]): PhaseGroup[] {
  const groups = new Map<string, PhaseGroup>();
  for (const p of phases) {
    const raw = str(p?.phase) || "phase";
    const phase = isPipelineStage(p) ? raw.replace(/_\d+$/, "") : raw;
    const secs = isNum(p?.seconds) ? p.seconds : null;
    const g = groups.get(phase);
    if (!g) {
      groups.set(phase, { phase, seconds: secs, count: 1, status: p?.status, title: [p?.stage, p?.status].filter(Boolean).join(" · ") });
      continue;
    }
    g.count += 1;
    if (secs != null) g.seconds = (g.seconds ?? 0) + secs;
    // The group's dot shows its worst status.
    if ((TONE_RANK[statusTone(p?.status)] ?? 0) > (TONE_RANK[statusTone(g.status)] ?? 0)) g.status = p?.status;
  }
  return [...groups.values()];
}

/** The framework's own phase (debate, supervisor, swarm, plan & execute) as the backend reports it in agent_run. */
export interface CorePhaseStatus {
  phase?: string;
  label?: string;
  status?: string; // ok | fallback | llm_unavailable | budget | error | skipped | not_run
  reason?: string;
}

/** Chip text when the framework's core phase did not run as asked; null when it ran normally. */
export function corePhaseChip(core: CorePhaseStatus | null | undefined): { text: string; tone: "warn" | "bad"; title: string } | null {
  if (!core || typeof core !== "object") return null;
  const status = str(core.status) || "ok";
  if (status === "ok" || status === "not_run") return null;
  const reason = str(core.reason);
  if (status === "fallback") return { text: "fallback plan", tone: "warn", title: reason || "The model gave no usable reply; a fixed plan ran." };
  const why: Record<string, string> = {
    llm_unavailable: "model call failed",
    budget: "agent budget ran out",
    skipped: "agent model unavailable",
    error: "step failed",
  };
  return { text: "did not run", tone: "bad", title: `${str(core.label) || "Core phase"} did not run: ${reason || why[status] || status}` };
}

function limitOf(limits: Record<string, any> | undefined, key: string): number | null {
  if (!limits) return null;
  const v = limits[key] ?? limits[`max_${key}`];
  // 0 means no limit (the server's default for seconds): show usage only, never a full red bar.
  return isNum(v) && v > 0 ? v : null;
}

/** Readable names of a sub-question's evidence: the backend's evidence_labels, else prettified keys. */
function evidenceNames(q: SubQuestion & { evidence_labels?: string[] }): string[] {
  return evidenceDisplayNames(q.evidence_keys, q.evidence_labels);
}

const ICON_STYLE = { width: 13, height: 13 };

/** Status as an icon; the word only appears beside the text when it is not the expected "answered"/"done". */
function StatusIcon({ status }: { status: unknown }) {
  const tone = statusTone(status);
  const label = str(status) || "pending";
  const icon =
    tone === "ok" ? (
      <Check style={ICON_STYLE} />
    ) : tone === "bad" ? (
      <XCircle style={ICON_STYLE} />
    ) : tone === "warn" ? (
      <AlertTriangle style={ICON_STYLE} />
    ) : tone === "info" ? (
      <CircleDashed style={ICON_STYLE} />
    ) : (
      <MinusCircle style={ICON_STYLE} />
    );
  return (
    <span className={styles.statusIcon} data-tone={tone} role="img" aria-label={label}>
      {icon}
    </span>
  );
}

function SubQuestions({ items }: { items: SubQuestion[] }) {
  return (
    <ol className={styles.subqList}>
      {items.map((q, i) => {
        const tone = statusTone(q.status);
        const names = evidenceNames(q);
        return (
          <li key={q.id || i} className={styles.subq}>
            <StatusIcon status={q.status} />
            <div className={styles.subqBody}>
              <span className={styles.subqText}>{q.text || "(empty sub-question)"}</span>
              {q.status && tone !== "ok" && (
                <>
                  {" "}
                  <Pill tone={tone}>{humanize(str(q.status)).toLowerCase()}</Pill>
                </>
              )}
              {arr(q.depends_on).length > 0 && <span className={styles.meta}> · after {arr<string>(q.depends_on).join(", ")}</span>}
              {q.note && <span className={styles.toggleHint}>{q.note}</span>}
              {names.length > 0 && (
                <span className={styles.toggleHint} title={arr<string>(q.evidence_keys).join(", ")}>
                  evidence: {names.slice(0, 6).join(", ")}
                  {names.length > 6 ? " …" : ""}
                </span>
              )}
            </div>
            {q.focus ? <span className={styles.subqFocus}>{q.focus}</span> : <span />}
          </li>
        );
      })}
    </ol>
  );
}

function TraceTable({ rows }: { rows: AgentTraceRow[] }) {
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Phase</th>
            <th scope="col">Tool</th>
            <th scope="col">Purpose</th>
            <th scope="col">Result</th>
            <th scope="col" className={styles.num}>
              Seconds
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.phase || "—"}</td>
              <td className={styles.mono}>{r.tool || "—"}</td>
              <td className={styles.wrapCell}>{r.purpose || "—"}</td>
              <td>
                {r.success ? <Pill tone="ok">ok</Pill> : <Pill tone="bad">{r.error_type ? humanize(String(r.error_type).toLowerCase()) : "failed"}</Pill>}
              </td>
              <td className={styles.num}>{fmt(r.seconds, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** How the router reached its choice, for the section meta. */
function routingMethod(routing: FrameworkRoutingArtifact): string {
  const parts = [routing.semantic?.used ? "semantic + keyword signals" : "keyword signals"];
  if ((routing.semantic?.valid_samples ?? 0) > 1) parts.push(`${routing.semantic?.valid_samples} samples`);
  if (routing.tie_break === "llm") parts.push("model adjudication");
  return parts.join(" · ");
}

/** Auto routing: the chosen architecture, why, the factors and probabilities behind it, and any escalation. */
function RoutingSection({ routing }: { routing: FrameworkRoutingArtifact }) {
  const probs = routing.probabilities || {};
  const ranked = (Object.keys(probs) as (keyof typeof probs)[])
    .filter((fw) => isNum(probs[fw]))
    .sort((a, b) => (probs[b] ?? 0) - (probs[a] ?? 0));
  const factors = arr<{ factor: string; label: string; weight: number }>(routing.factors).filter((f) => isNum(f.weight));
  const learned = Object.entries(routing.learned || {}).filter(([, v]) => v && isNum(v.delta) && v.delta !== 0);
  const kind = routing.semantic?.question_kind;
  const esc = routing.escalation;
  return (
    <Section
      title="Why this approach"
      meta={isNum(routing.confidence) ? `${pct(routing.confidence)} relative score` : undefined}
      hint={[routing.rule ? `Rule: ${routing.rule}` : "", routingMethod(routing), routing.scores_note || ""]
        .filter(Boolean)
        .join(" · ")}
    >
      <p className={styles.routingReason}>{routing.reason || "Auto chose this architecture for the question."}</p>
      {routing.cost_note && <p className={styles.routingReason}>{routing.cost_note}</p>}
      {factors.length > 0 && (
        <ChipList
          items={factors.map((f) => ({
            key: f.factor,
            label: `${f.label} ${f.weight > 0 ? "+" : "−"}${Math.abs(f.weight).toFixed(1)}`,
            highlight: f.weight > 0,
            title: f.weight > 0 ? "Raised this architecture's score" : "Lowered this architecture's score",
          }))}
          limit={6}
        />
      )}
      {ranked.length > 1 && (
        <div className={styles.routingProbs} aria-label="Relative suitability score of each architecture">
          {ranked.map((fw) => (
            <Bar
              key={fw}
              label={FRAMEWORK_NAMES[fw] || fw}
              value={probs[fw] ?? 0}
              max={1}
              display={pct(probs[fw])}
              tone={fw === routing.chosen ? "ok" : "info"}
            />
          ))}
        </div>
      )}
      {(kind || learned.length > 0 || routing.guardrail) && (
        <p className={styles.note}>
          {[
            kind ? `Read as a ${kind.replace(/_/g, " ")} question (${routingMethod(routing)}).` : "",
            learned.length
              ? `Past outcomes on similar questions: ${learned
                  .map(([fw, v]) => `${FRAMEWORK_NAMES[fw] || fw} ${v!.delta > 0 ? "+" : "−"}${Math.abs(v!.delta).toFixed(2)} (${v!.runs} runs)`)
                  .join(", ")}.`
              : "",
            routing.guardrail || "",
          ]
            .filter(Boolean)
            .join(" ")}
        </p>
      )}
      {esc && (
        <p className={styles.routingEscalation} data-status={esc.status}>
          <Pill tone={esc.status === "escalated" ? "warn" : "faint"}>
            {esc.status === "escalated" ? "escalated to critic debate" : "escalation skipped"}
          </Pill>{" "}
          {[...arr<string>(esc.triggers), esc.reason].filter(Boolean).join("; ")}
        </p>
      )}
    </Section>
  );
}

/** How the agentic run went: framework, sub-questions, budget, phases, tool trace and draft rounds. */
export function AgentRunPanel({ data, routing }: { data: AgentRunArtifact; routing?: FrameworkRoutingArtifact | null }) {
  if (!data || typeof data !== "object") return null;
  const subs = arr<SubQuestion>(data.sub_questions);
  const phases = arr(data.phases);
  const trace = arr<AgentTraceRow>(data.trace);
  const decisions = arr<Record<string, any>>(data.decisions);
  const steering = arr(data.steering);
  const budget = data.budget || {};
  const used = budget.used || {};
  const failures = trace.filter((t) => !t.success).length;
  const fw = data.framework ? FRAMEWORK_NAMES[data.framework] || data.framework : "Agentic run";
  const coreChip = corePhaseChip((data as AgentRunArtifact & { core_phase?: CorePhaseStatus | null }).core_phase);

  const pipelineGroups = groupPhases(phases.filter(isPipelineStage));
  const agentGroups = groupPhases(phases.filter((p) => !isPipelineStage(p)));
  const groups = [...pipelineGroups, ...agentGroups];
  const phaseMax = Math.max(0, ...groups.map((g) => g.seconds ?? 0));
  const sumOf = (gs: PhaseGroup[]) => gs.reduce((sum, g) => sum + (g.seconds ?? 0), 0);
  const pipelineTotal = sumOf(pipelineGroups);
  const agentTotal = sumOf(agentGroups);
  const runSeconds = isNum(used.seconds) ? used.seconds : null;
  // Agent phases run inside pipeline stages, so the two sums are shown apart and never added up.
  const timing = [
    pipelineGroups.length && pipelineTotal > 0 ? `pipeline ${formatSeconds(pipelineTotal)}` : "",
    agentGroups.length && agentTotal > 0 ? `agent phases ${formatSeconds(agentTotal)}` : "",
    runSeconds != null ? `run time ${formatSeconds(runSeconds)}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const untimed =
    runSeconds != null && runSeconds > 0 && Math.max(pipelineTotal, agentTotal) < 0.8 * runSeconds
      ? `Timed stages cover ${formatSeconds(Math.max(pipelineTotal, agentTotal))} of ${formatSeconds(runSeconds)}; the rest (model streaming, waiting, untimed steps) is not broken down.`
      : "";
  const answered = subs.filter((q) => statusTone(q.status) === "ok").length;

  const meta = [
    subs.length ? `${answered} of ${subs.length} sub-question${subs.length === 1 ? "" : "s"} answered` : "",
    trace.length ? `${trace.length} tool call${trace.length === 1 ? "" : "s"}${failures ? ` (${failures} failed)` : ""}` : "",
    isNum(data.draft_rounds) ? `${data.draft_rounds} draft round${data.draft_rounds === 1 ? "" : "s"}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<Workflow style={{ width: 15, height: 15 }} />}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          Agent run{" "}
          <Pill tone="accent" title={routing?.reason || undefined}>
            {routing?.requested === "auto" ? `Auto · ${fw}` : fw}
          </Pill>
          {routing?.escalation?.status === "escalated" && (
            <Pill tone="warn" title={arr<string>(routing.escalation.triggers).join("; ")}>
              + critic debate
            </Pill>
          )}
          {coreChip && (
            <Pill tone={coreChip.tone} title={coreChip.title}>
              {coreChip.text}
            </Pill>
          )}
          {budget.exhausted && (
            <Pill tone="bad" title={str(budget.exhausted_reason) || undefined}>
              budget exhausted
            </Pill>
          )}
        </span>
      }
      meta={meta}
      label="Agent run summary"
      collapsible
      defaultOpen={false}
    >
      {routing?.requested === "auto" && <RoutingSection routing={routing} />}

      {subs.length > 0 && (
        <div className={styles.section}>
          <div className={styles.sectionHead}>
            <span className={styles.sectionTitle}>Sub-questions</span>
            <span className={styles.meta}>
              {answered} of {subs.length} answered
            </span>
          </div>
          <SubQuestions items={subs} />
        </div>
      )}

      {(budget.limits || budget.used) && (
        <div className={styles.section}>
          <div className={styles.sectionTitle}>Budget</div>
          <div className={styles.budgetRow}>
            {METERS.map((m) => {
              const u = used[m.usedKey];
              const lim = limitOf(budget.limits, m.key);
              if (!isNum(u) && lim == null) return null;
              const ratio = lim && isNum(u) ? u / lim : 0;
              const tone = ratio >= 1 ? styles.barFillBad : ratio >= 0.8 ? styles.barFillWarn : styles.barFillOk;
              return (
                <div key={m.key} className={styles.budgetStat} title={isNum(u) ? `${fmt(u, 1)}${lim != null ? ` of ${lim}` : " (no limit)"}` : undefined}>
                  <span className={styles.budgetLabel}>{m.label}</span>
                  <span className={styles.budgetValue}>
                    {isNum(u) ? meterText(u, m.seconds) : "—"}
                    <span className={styles.budgetLimit}>{lim != null ? ` / ${meterText(lim, m.seconds)}` : " · no limit"}</span>
                  </span>
                  {lim != null && (
                    <span
                      className={`${styles.barTrack} ${styles.budgetBar}`}
                      role="meter"
                      aria-label={`${m.label} used`}
                      aria-valuemin={0}
                      aria-valuemax={lim}
                      aria-valuenow={isNum(u) ? u : 0}
                    >
                      <span className={`${styles.barFill} ${tone}`} style={{ width: `${Math.min(1, ratio) * 100}%` }} />
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          {budget.exhausted_reason && <p className={styles.warnNote}>Stopped early: {str(budget.exhausted_reason)}</p>}
        </div>
      )}

      {groups.length > 0 && (
        <div className={styles.section}>
          <div className={styles.sectionHead}>
            <span className={styles.sectionTitle}>Timing</span>
            <span className={styles.meta}>in run order{timing ? ` · ${timing}` : ""}</span>
          </div>
          {[
            { label: "Pipeline", items: pipelineGroups },
            { label: "Agent phases", items: agentGroups },
          ]
            .filter((g) => g.items.length > 0)
            .map((g) => (
              <div key={g.label}>
                {pipelineGroups.length > 0 && <div className={styles.meta}>{g.label}</div>}
                <ol className={styles.phaseGrid}>
                  {g.items.map((p) => {
                    const tone = statusTone(p.status);
                    const secs = p.seconds ?? 0;
                    return (
                      <li key={p.phase} className={styles.phaseRow} title={p.title || undefined}>
                        <span className={styles.phaseName}>
                          {tone !== "ok" && tone !== "neutral" && <StatusIcon status={p.status} />}
                          {phaseLabel(p.phase)}
                          {p.count > 1 && <span className={styles.phaseCount}>×{p.count}</span>}
                        </span>
                        <span className={styles.phaseTrack} aria-hidden="true">
                          <span className={styles.phaseFill} data-tone={tone} style={{ width: `${phaseMax > 0 ? Math.max(2, (secs / phaseMax) * 100) : 0}%` }} />
                        </span>
                        <span className={styles.phaseTime}>{secs >= 0.05 ? formatSeconds(secs) : "<0.1s"}</span>
                      </li>
                    );
                  })}
                </ol>
              </div>
            ))}
          {untimed && <p className={styles.meta}>{untimed}</p>}
        </div>
      )}

      {(decisions.length > 0 || steering.length > 0) && (
        <div className={styles.section}>
          <div className={styles.sectionTitle}>Your input</div>
          <ul className={styles.list}>
            {decisions.map((d, i) => (
              <li key={`d${i}`} className={styles.listItem}>
                <Pill tone={d?.approved === false ? "bad" : d?.source === "user" ? "ok" : "neutral"}>
                  {humanize(str(d?.kind) || "decision")}: {d?.approved === false ? "rejected" : "approved"}
                  {d?.source && d.source !== "user" ? ` (${d.source})` : ""}
                </Pill>
                {d?.message ? <span className={styles.meta}>{str(d.message)}</span> : <span />}
              </li>
            ))}
            {steering.map((s, i) => (
              <li key={`s${i}`} className={styles.listItem}>
                <Pill tone="info">steer</Pill>
                <span>{typeof s === "string" ? s : str((s as any)?.message ?? s)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {trace.length > 0 && (
        <Disclosure summary={`Tool trace (${trace.length})`}>
          <TraceTable rows={trace} />
        </Disclosure>
      )}

      {data.run_id && (
        <div className={styles.runIdRow}>
          <span className={styles.meta}>
            Run ID <span className={styles.mono}>{data.run_id}</span>
          </span>
          <CopyButton text={data.run_id} label="Copy ID" />
        </div>
      )}
    </Card>
  );
}
