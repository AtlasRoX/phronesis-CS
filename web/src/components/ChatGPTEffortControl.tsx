"use client";

import React, { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import type { EffortLevel } from "@/types/agentic";
import type { FrameworkOption } from "@/types/chat";
import { FRAMEWORK_TAGLINES } from "@/components/FrameworkMenu";
import styles from "./EffortControl.module.css";

export interface EffortStepConfig {
  level: EffortLevel;
  label: string;
  sublabel: string;
  tokens: string;
  articles: string;
  rounds: string;
}

export const EFFORT_STEPS: EffortStepConfig[] = [
  {
    level: "low",
    label: "Low",
    sublabel: "Fast & focused",
    tokens: "1k tokens",
    articles: "4 passages",
    rounds: "1 round",
  },
  {
    level: "medium",
    label: "Medium",
    sublabel: "Standard deliberation",
    tokens: "4k tokens",
    articles: "8 passages",
    rounds: "2 rounds",
  },
  {
    level: "high",
    label: "High",
    sublabel: "Deep deliberation",
    tokens: "16k tokens",
    articles: "14 passages",
    rounds: "3 rounds",
  },
  {
    level: "ultra",
    label: "Ultra",
    sublabel: "Exhaustive frontier",
    tokens: "32k tokens",
    articles: "25 passages",
    rounds: "5 rounds",
  },
];

const DEFAULT_EFFORT: EffortLevel = "medium";

/** Circle filled clockwise in quarters (1 of 4 for Low ... full for Ultra), like Claude Code's effort glyph. */
function EffortGauge({ index, className }: { index: number; className?: string }) {
  // A 6px-wide stroke on a radius-3 circle paints a pie wedge; the dash length sets how much of it shows.
  const circumference = 2 * Math.PI * 3;
  const filled = ((index + 1) / EFFORT_STEPS.length) * circumference;
  return (
    <svg viewBox="0 0 16 16" className={className} aria-hidden="true">
      <circle cx="8" cy="8" r="6.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle
        cx="8"
        cy="8"
        r="3"
        fill="none"
        stroke="currentColor"
        strokeWidth="6"
        strokeDasharray={`${filled} ${circumference}`}
        transform="rotate(-90 8 8)"
      />
    </svg>
  );
}

export interface ChatGPTEffortControlProps {
  effortLevel: EffortLevel;
  onEffortChange: (effort: EffortLevel) => void;
  selectedFramework: string;
  onFrameworkChange: (frameworkId: string) => void;
  frameworkOptions: FrameworkOption[];
  onOpenGuide?: () => void;
}

/** Composer picker: the orchestration architecture and the reasoning effort for the next question. */
export function ChatGPTEffortControl({
  effortLevel,
  onEffortChange,
  selectedFramework,
  onFrameworkChange,
  frameworkOptions,
  onOpenGuide,
}: ChatGPTEffortControlProps) {
  const [isOpen, setIsOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const activeIndex = Math.max(0, EFFORT_STEPS.findIndex((s) => s.level === (effortLevel || DEFAULT_EFFORT)));
  const currentStep = EFFORT_STEPS[activeIndex];
  const currentFramework =
    frameworkOptions.find((fw) => fw.id === selectedFramework) || frameworkOptions[0] || { title: "Auto" };

  const close = (returnFocus: boolean) => {
    setIsOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  };

  const stepEffort = (delta: number) => {
    const next = Math.max(0, Math.min(EFFORT_STEPS.length - 1, activeIndex + delta));
    if (next !== activeIndex) onEffortChange(EFFORT_STEPS[next].level);
  };

  // Outside click closes; Escape closes and hands focus back to the trigger.
  useEffect(() => {
    if (!isOpen) return;
    const handleDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close(false);
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(true);
      }
    };
    document.addEventListener("mousedown", handleDown);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleDown);
      document.removeEventListener("keydown", handleKey);
    };
  }, [isOpen]);

  // On open, focus the selected architecture so arrows and Enter work straight away.
  useEffect(() => {
    if (!isOpen) return;
    const list = listRef.current;
    const target =
      list?.querySelector<HTMLButtonElement>('[aria-selected="true"]:not(:disabled)') ||
      list?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    target?.focus();
  }, [isOpen]);

  // Up/down moves through architectures; left/right adjusts effort from anywhere in the menu.
  const handleMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      stepEffort(e.key === "ArrowRight" ? 1 : -1);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const items = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || []);
    if (!items.length) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home" ? 0
      : e.key === "End" ? items.length - 1
      : e.key === "ArrowDown" ? (at + 1) % items.length
      : (at - 1 + items.length) % items.length;
    items[next].focus();
  };

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        onClick={() => (isOpen ? close(false) : setIsOpen(true))}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-label={`Architecture ${currentFramework.title}, ${currentStep.label} effort. Change`}
        title="Architecture and reasoning effort"
      >
        <span className={styles.triggerFramework}>{currentFramework.title}</span>
        <span className={styles.triggerSep} aria-hidden="true">·</span>
        <span>{currentStep.label}</span>
        <ChevronDown className={styles.triggerChevron} aria-hidden="true" />
      </button>

      {isOpen && (
        <div
          className={styles.menu}
          role="dialog"
          aria-label="Architecture and reasoning effort"
          onKeyDown={handleMenuKeyDown}
        >
          <div ref={listRef} className={styles.list} role="listbox" aria-label="Architecture">
            {frameworkOptions.map((fw) => {
              const isSelected = fw.id === selectedFramework;
              return (
                <React.Fragment key={fw.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    disabled={!fw.available}
                    className={styles.item}
                    onClick={() => {
                      onFrameworkChange(fw.id);
                      close(true);
                    }}
                    title={fw.available ? fw.description : "Not offered by this backend"}
                  >
                    <span className={styles.itemText}>
                      <span className={styles.itemTitle}>
                        {fw.title}
                        {fw.recommended && <span className={styles.tag}>(recommended)</span>}
                      </span>
                      <span className={styles.itemDesc}>
                        {fw.available ? FRAMEWORK_TAGLINES[fw.id] || fw.description : "Not offered by this backend"}
                      </span>
                    </span>
                    {isSelected && <Check className={styles.check} aria-hidden="true" />}
                  </button>
                  {fw.recommended && <div className={styles.divider} role="separator" />}
                </React.Fragment>
              );
            })}
          </div>

          <div className={styles.divider} role="separator" />

          <div className={styles.effort}>
            <EffortGauge index={activeIndex} className={styles.gauge} />
            <div className={styles.effortText} aria-live="polite">
              <span className={styles.effortTitle}>
                {currentStep.label} effort
                {currentStep.level === DEFAULT_EFFORT && <span className={styles.effortDefault}> (default)</span>}
              </span>
              <span className={styles.effortSpecs}>
                {currentStep.articles} · {currentStep.tokens} · {currentStep.rounds}
              </span>
            </div>
            <div className={styles.steppers}>
              <button
                type="button"
                className={styles.step}
                onClick={() => stepEffort(-1)}
                disabled={activeIndex === 0}
                aria-label="Less effort"
                title="Less effort"
              >
                <ChevronLeft style={{ width: 15, height: 15 }} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={styles.step}
                onClick={() => stepEffort(1)}
                disabled={activeIndex === EFFORT_STEPS.length - 1}
                aria-label="More effort"
                title="More effort"
              >
                <ChevronRight style={{ width: 15, height: 15 }} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className={styles.footer}>
            <span>
              <kbd className={styles.key}>←</kbd> <kbd className={styles.key}>→</kbd> to adjust effort
            </span>
            {onOpenGuide && (
              <button
                type="button"
                className={styles.footerLink}
                onClick={() => {
                  close(false);
                  onOpenGuide();
                }}
              >
                How each one works
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
