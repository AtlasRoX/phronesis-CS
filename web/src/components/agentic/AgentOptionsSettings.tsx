"use client";

import React from "react";
import { BUDGET_LIMITS, EFFORT_LEVELS } from "@/services/agentService";
import type { AgentOptionsSettings as Settings, EffortLevel } from "@/types/agentic";
import { Brain, Rocket, Scale, Zap } from "lucide-react";
import { styles } from "./ui";

const ICONS: Record<string, React.ComponentType<{ style?: React.CSSProperties }>> = {
  Zap,
  Scale,
  Brain,
  Rocket,
};

const TOGGLES: { key: keyof Omit<Settings, "budget" | "budgetEnabled" | "effort_level">; label: string; hint: string }[] = [
  { key: "require_plan_approval", label: "Ask me to approve the plan", hint: "Pause before sources and expensive tool steps run (auto-continues after 5 minutes)." },
  { key: "allow_clarification", label: "Allow clarifying questions", hint: "Ask once when the question is ambiguous instead of guessing." },
  { key: "enable_counter_evidence", label: "Search for counter-evidence", hint: "Look for passages that contradict the main claims." },
  { key: "enable_hypotheses", label: "Generate and test hypotheses", hint: "Rank hypotheses and, with a model, run in-silico perturbations." },
  { key: "enable_code_analysis", label: "Allow code analysis on datasets", hint: "Run sandboxed Python on attached tables (needs PHRONESIS_SANDBOX=on on the server)." },
];

const BUDGET_KEYS = Object.keys(BUDGET_LIMITS) as (keyof typeof BUDGET_LIMITS)[];

/** Agentic settings for every request: HITL, optional phases, effort level and budget limits. */
export function AgentOptionsSettings({
  value,
  onChange,
  agentic,
}: {
  value: Settings;
  onChange: (next: Settings) => void;
  /** False when the classic framework is selected: the options are kept but not sent. */
  agentic: boolean;
}) {
  const currentEffort = value.effort_level || "medium";

  return (
    <fieldset style={{ border: "1px solid var(--border-subtle)", borderRadius: 8, padding: "12px 14px", margin: 0 }}>
      <legend style={{ fontSize: 12, color: "var(--text-muted)", padding: "0 4px" }}>Agent options</legend>
      {!agentic && <p className={styles.note} style={{ marginTop: 0 }}>The classic pipeline ignores these; they apply to the agentic frameworks.</p>}

      {/* Effort Level & Thinking Engine */}
      <div style={{ marginBottom: "14px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px" }}>
          <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-main)" }}>
            Thinking & Deliberation Effort
          </span>
          <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>
            Governs reasoning budget, retrieval depth & revision rounds
          </span>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
            gap: "8px",
          }}
        >
          {EFFORT_LEVELS.map((lvl) => {
            const isSelected = currentEffort === lvl.id;
            const Icon = ICONS[lvl.icon] || Brain;
            return (
              <button
                key={lvl.id}
                type="button"
                onClick={() => onChange({ ...value, effort_level: lvl.id as EffortLevel })}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-start",
                  gap: "6px",
                  padding: "8px 10px",
                  border: isSelected
                    ? `1.5px solid ${lvl.color}`
                    : "1px solid var(--border-subtle)",
                  borderRadius: "8px",
                  background: isSelected
                    ? `color-mix(in srgb, ${lvl.color} 10%, var(--bg-surface))`
                    : "var(--bg-surface)",
                  cursor: "pointer",
                  textAlign: "left",
                  transition: "all 0.15s ease",
                  boxShadow: isSelected ? `0 2px 8px color-mix(in srgb, ${lvl.color} 20%, transparent)` : "none",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%" }}>
                  <div
                    style={{
                      width: "22px",
                      height: "22px",
                      borderRadius: "6px",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      background: `color-mix(in srgb, ${lvl.color} 18%, transparent)`,
                      color: lvl.color,
                    }}
                  >
                    <Icon style={{ width: 13, height: 13 }} />
                  </div>
                  <span
                    style={{
                      fontSize: "10px",
                      fontWeight: 600,
                      padding: "1px 5px",
                      borderRadius: "6px",
                      background: `color-mix(in srgb, ${lvl.color} 15%, transparent)`,
                      color: lvl.color,
                    }}
                  >
                    {lvl.badge}
                  </span>
                </div>
                <div>
                  <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-main)" }}>
                    {lvl.name}
                  </div>
                  <div style={{ fontSize: "10px", color: "var(--text-muted)", lineHeight: 1.25, marginTop: "2px" }}>
                    {lvl.tagline}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      <div style={{ borderTop: "1px solid var(--border-subtle)", paddingTop: "10px" }}>
        {TOGGLES.map((t) => (
          <label key={t.key} className={styles.toggleRow}>
            <input type="checkbox" checked={value[t.key]} onChange={(e) => onChange({ ...value, [t.key]: e.target.checked })} />
            <span>
              {t.label}
              <span className={styles.toggleHint}>{t.hint}</span>
            </span>
          </label>
        ))}
      </div>
      <label className={styles.toggleRow} style={{ marginTop: 6 }}>
        <input type="checkbox" checked={value.budgetEnabled} onChange={(e) => onChange({ ...value, budgetEnabled: e.target.checked })} />
        <span>
          Limit the run budget
          <span className={styles.toggleHint}>Off uses the server&apos;s limits. Your limits can only make a run smaller.</span>
        </span>
      </label>
      {value.budgetEnabled && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4, paddingLeft: 22 }}>
          {BUDGET_KEYS.map((k) => {
            const lim = BUDGET_LIMITS[k];
            const id = `agent-budget-${k}`;
            const display = k === "max_seconds" ? (value.budget[k] > 0 ? `${Math.round(value.budget[k] / 60)} min` : "no limit") : String(value.budget[k]);
            return (
              <div key={k} className={styles.sliderRow}>
                <label htmlFor={id}>{lim.label}</label>
                <input
                  id={id}
                  type="range"
                  min={k === "max_seconds" ? 0 : lim.min}
                  max={k === "max_seconds" ? 3600 : lim.max}
                  step={k === "max_seconds" ? 60 : lim.step}
                  value={value.budget[k]}
                  onChange={(e) => onChange({ ...value, budget: { ...value.budget, [k]: Number(e.target.value) } })}
                  aria-valuetext={display}
                />
                <span className={styles.barValue}>{display}</span>
              </div>
            );
          })}
        </div>
      )}
    </fieldset>
  );
}
