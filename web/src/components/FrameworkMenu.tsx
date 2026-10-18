"use client";

import React, { useEffect, useRef } from "react";
import { Check, ChevronRight, X } from "lucide-react";
import type { FrameworkOption } from "@/types/chat";
import styles from "./FrameworkMenu.module.css";

type Level = 1 | 2 | 3;

interface GuideEntry {
  summary: string;
  steps: string[];
  bestFor: string[];
  /** How the Auto router uses this architecture (omitted for Auto itself and the classic pipeline). */
  autoPicks?: string;
  note?: string;
  depth: Level;
  speed: Level;
  calls: Level;
}

/** What each architecture does, in the order the backend runs it (phronesis/agentic). Keep in step with the code. */
const GUIDE: Record<string, GuideEntry> = {
  auto: {
    summary:
      "Reads each question, scores the four agent architectures and runs the best fit. Every answer shows which one it chose and why.",
    steps: [
      "Reads the question's shape: how many parts it has, whether they are independent or sequential, the domains it touches, and whether it is safety-critical, contested or asks for hypotheses.",
      "Scores Plan & Execute, Supervisor, Swarm and Debate from those signals, nudged by how similar questions turned out before.",
      "Runs the top choice. When the top two are close, a short model call decides between them.",
      "After the first draft, brings in critics to debate it when the evidence conflicts, the question is safety-critical, or the route was uncertain.",
    ],
    bestFor: ["Any question", "When you are unsure which to pick"],
    note: "A safety-critical question is never sent to the Swarm.",
    depth: 3,
    speed: 2,
    calls: 2,
  },
  "agentic-plan-execute": {
    summary:
      "Breaks the question into sub-questions, plans the tool calls it needs and runs them in parallel, then checks every claim against counter-evidence.",
    steps: [
      "Splits a compound question into up to five sub-questions, simplest first.",
      "Plans up to 12 tool calls (literature, trials, targets, drugs, variants, omics) and runs independent ones in parallel; re-plans once if results fall short.",
      "Searches for evidence that contradicts the draft and fills gaps with follow-up searches.",
      "Verifies each claim against the retrieved sources before the answer is final.",
    ],
    bestFor: ["Multi-part factual questions", "Head-to-head comparisons", "Simulations and dataset analysis"],
    autoPicks: "The default backbone: explicit multi-part questions, computational work and focused factual questions.",
    depth: 3,
    speed: 2,
    calls: 2,
  },
  "multi-agent-supervisor": {
    summary:
      "A manager agent keeps a task ledger and delegates each part to a domain specialist, re-planning as results come in.",
    steps: [
      "The supervisor writes a plan and a ledger of open tasks.",
      "Delegates up to six tasks to specialists (literature, targets, chemistry, omics, simulation), each running up to three tool calls.",
      "Re-plans up to twice when a specialist comes back empty or findings conflict.",
      "Combines the findings; counter-evidence search and claim verification then run as usual.",
    ],
    bestFor: ["Open-ended assessments", "Evaluating a drug target", "Landscapes and overviews"],
    autoPicks: "Open-ended questions spanning several domains without explicit sub-questions.",
    note: "Falls back to a fixed plan if the model cannot produce one.",
    depth: 3,
    speed: 1,
    calls: 3,
  },
  "multi-agent-swarm": {
    summary:
      "Specialists pass the question peer to peer: each adds what its source knows, then hands off to whoever fits the next step.",
    steps: [
      "The first specialist takes the question and searches its own source.",
      "It hands off to the specialist best placed for the next link (up to three hand-offs and eight turns).",
      "Ends when a specialist judges the question answered; counter-evidence search and verification then run.",
    ],
    bestFor: ["How are X and Y connected?", "Low-stakes exploration"],
    autoPicks: "Low-stakes exploratory questions, never safety-critical ones.",
    note: "Mistakes can carry along the chain, so this is the least conservative option.",
    depth: 2,
    speed: 2,
    calls: 2,
  },
  "multi-agent-debate": {
    summary:
      "Runs Plan & Execute, then independent critics cross-examine the draft against the evidence before it is rewritten.",
    steps: [
      "Gathers evidence and writes a draft the Plan & Execute way.",
      "Three critics review it for up to two rounds: a methodologist (study design), a clinical skeptic (human evidence, effect sizes, safety) and a mechanistic biologist (pathway logic).",
      "The answer is rewritten to address their objections; disagreements that remain are reported, not hidden.",
    ],
    bestFor: ["Generating hypotheses", "Contested or disputed claims", "Is X better than Y?"],
    autoPicks: "Hypotheses and contested comparisons. Auto also calls the critics in mid-run when evidence conflicts.",
    depth: 3,
    speed: 1,
    calls: 3,
  },
  "authoritative-react-compiler": {
    summary:
      "A fixed six-stage pipeline with no agent planning: predictable, fast and identical on every run.",
    steps: [
      "Reads the question and picks the evidence sources it needs.",
      "Gathers evidence from those sources, then runs any simulation or analysis.",
      "Writes the answer from the evidence.",
      "Verifies the claims, corrects them once if needed, and records the run for reproducibility.",
    ],
    bestFor: ["Quick lookups", "Reproducible runs"],
    note: "No sub-questions, counter-evidence search or critics, and the run cannot be steered while it streams.",
    depth: 1,
    speed: 3,
    calls: 1,
  },
};

/** One line per architecture in the menu. */
export const FRAMEWORK_TAGLINES: Record<string, string> = {
  auto: "Picks the best approach per question",
  "agentic-plan-execute": "Parallel tool plan with claim checks",
  "multi-agent-supervisor": "A manager delegates to specialists",
  "multi-agent-swarm": "Specialists hand off along a chain",
  "multi-agent-debate": "Critics cross-examine the draft",
  "authoritative-react-compiler": "Fixed six-stage workflow, fastest",
};

function Meter({ label, level, words }: { label: string; level: Level; words: [string, string, string] }) {
  return (
    <div className={styles.meter}>
      <span className={styles.meterLabel}>{label}</span>
      <span className={styles.meterDots} aria-hidden="true">
        {[1, 2, 3].map((n) => (
          <span key={n} className={styles.meterDot} data-on={n <= level || undefined} />
        ))}
      </span>
      <span className={styles.meterWord}>{words[level - 1]}</span>
    </div>
  );
}

/** The compact architecture menu above the composer's selector pill. */
export function FrameworkMenu({
  options,
  selected,
  onSelect,
  onOpenGuide,
}: {
  options: FrameworkOption[];
  selected: string;
  onSelect: (id: string) => void;
  onOpenGuide: () => void;
}) {
  return (
    <div className={styles.menu} role="listbox" aria-label="Orchestration architecture">
      {options.map((fw) => {
        const isSelected = selected === fw.id;
        const Icon = fw.icon;
        return (
          <React.Fragment key={fw.id}>
            <button
              type="button"
              role="option"
              aria-selected={isSelected}
              disabled={!fw.available}
              className={styles.option}
              data-selected={isSelected || undefined}
              onClick={() => fw.available && onSelect(fw.id)}
              title={fw.available ? fw.description : "Not offered by this backend"}
            >
              <span className={styles.optionIcon} style={{ color: fw.badgeColor }}>
                <Icon style={{ width: 15, height: 15 }} aria-hidden="true" />
              </span>
              <span className={styles.optionText}>
                <span className={styles.optionTitle}>
                  {fw.title}
                  {fw.recommended && <span className={styles.recommended}>Recommended</span>}
                </span>
                <span className={styles.optionTagline}>
                  {fw.available ? FRAMEWORK_TAGLINES[fw.id] || fw.description : "Not offered by this backend"}
                </span>
              </span>
              {isSelected && <Check className={styles.check} aria-hidden="true" />}
            </button>
            {fw.recommended && <div className={styles.divider} role="separator" />}
          </React.Fragment>
        );
      })}
      <button type="button" className={styles.guideLink} onClick={onOpenGuide}>
        How each one works
        <ChevronRight style={{ width: 13, height: 13 }} aria-hidden="true" />
      </button>
    </div>
  );
}

/** Right-side panel: what every architecture does, step by step, with its trade-offs. */
export function FrameworkGuide({
  open,
  options,
  selected,
  onSelect,
  onClose,
}: {
  open: boolean;
  options: FrameworkOption[];
  selected: string;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className={styles.overlay} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside ref={panelRef} className={styles.panel} role="dialog" aria-modal="true" aria-labelledby="framework-guide-title">
        <header className={styles.panelHead}>
          <div>
            <h2 id="framework-guide-title" className={styles.panelTitle}>
              How Phronesis answers
            </h2>
            <p className={styles.panelLead}>
              Every architecture searches the same sources and verifies claims before answering. They differ in how they
              plan the research and how hard they challenge the draft.
            </p>
          </div>
          <button ref={closeRef} type="button" className={styles.close} onClick={onClose} aria-label="Close">
            <X style={{ width: 16, height: 16 }} aria-hidden="true" />
          </button>
        </header>

        <div className={styles.panelBody}>
          {options.map((fw) => {
            const g = GUIDE[fw.id];
            if (!g) return null;
            const Icon = fw.icon;
            const isSelected = selected === fw.id;
            return (
              <section key={fw.id} className={styles.entry} data-selected={isSelected || undefined} aria-label={fw.title}>
                <div className={styles.entryHead}>
                  <span className={styles.entryIcon} style={{ color: fw.badgeColor }}>
                    <Icon style={{ width: 17, height: 17 }} aria-hidden="true" />
                  </span>
                  <h3 className={styles.entryTitle}>
                    {fw.title}
                    {fw.recommended && <span className={styles.recommended}>Recommended</span>}
                  </h3>
                  {isSelected ? (
                    <span className={styles.inUse}>
                      <Check style={{ width: 13, height: 13 }} aria-hidden="true" /> In use
                    </span>
                  ) : (
                    <button
                      type="button"
                      className={styles.useBtn}
                      disabled={!fw.available}
                      onClick={() => onSelect(fw.id)}
                    >
                      Use
                    </button>
                  )}
                </div>
                <p className={styles.summary}>{g.summary}</p>

                <div className={styles.meters}>
                  <Meter label="Depth" level={g.depth} words={["Light", "Moderate", "Deep"]} />
                  <Meter label="Speed" level={g.speed} words={["Slower", "Moderate", "Fastest"]} />
                  <Meter label="Model calls" level={g.calls} words={["Fewest", "Moderate", "Most"]} />
                </div>

                <h4 className={styles.subhead}>How it works</h4>
                <ol className={styles.steps}>
                  {g.steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>

                <h4 className={styles.subhead}>Best for</h4>
                <ul className={styles.chips}>
                  {g.bestFor.map((b) => (
                    <li key={b} className={styles.chip}>
                      {b}
                    </li>
                  ))}
                </ul>

                {g.autoPicks && (
                  <p className={styles.autoNote}>
                    <span className={styles.autoLabel}>When Auto picks it</span> {g.autoPicks}
                  </p>
                )}
                {g.note && <p className={styles.note}>{g.note}</p>}
              </section>
            );
          })}
        </div>
      </aside>
    </div>
  );
}
