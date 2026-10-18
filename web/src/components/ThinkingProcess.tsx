"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { AlertCircle, Check, ChevronRight, CircleDashed, Loader2, MinusCircle } from "lucide-react";
import { ThoughtStep } from "@/types/chat";
import type { QueryPlanExtra } from "@/types/queryPlan";
import { QueryPlanChips } from "@/components/QueryPlanChips";
import { displayStatus, stepLiveness } from "@/components/agentic/thoughts";
import { formatSeconds } from "@/lib/duration";
import { cleanStepText } from "@/lib/entities";
import { REASONING_SMOOTH_OPTIONS } from "@/lib/smoothText";
import { useSmoothText } from "@/lib/useSmoothText";

interface ThinkingProcessProps {
  thoughts: ThoughtStep[];
  /** The model's own reasoning text (streamed live, or saved with the message). */
  reasoning?: string;
  isStreaming?: boolean;
  /** Live elapsed time while streaming. */
  elapsedSeconds?: number;
  /** Total time once finished. */
  durationSeconds?: number;
  /** True once answer text has started arriving; the panel then folds away like Claude/ChatGPT. */
  answerStarted?: boolean;
}

function StepIcon({ status }: { status: ThoughtStep["status"] }) {
  if (status === "running") return <Loader2 className="spin" style={{ width: 14, height: 14 }} aria-label="In progress" />;
  if (status === "error") return <AlertCircle style={{ width: 14, height: 14 }} aria-label="Problem" />;
  if (status === "skipped") return <MinusCircle style={{ width: 14, height: 14, opacity: 0.6 }} aria-label="Skipped" />;
  if (status === "unreported")
    return <CircleDashed style={{ width: 14, height: 14, opacity: 0.7 }} aria-label="Completion not reported" />;
  return <Check style={{ width: 14, height: 14 }} aria-label="Done" />;
}

/** Keeps a scroll container pinned to the bottom while content grows, unless the reader scrolled up. */
function useStickToBottom(dep: unknown, enabled: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && enabled && pinned.current) el.scrollTop = el.scrollHeight;
  }, [dep, enabled]);
  const onScroll = () => {
    const el = ref.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };
  return { ref, onScroll };
}

export function ThinkingProcess({
  thoughts,
  reasoning = "",
  isStreaming = false,
  elapsedSeconds = 0,
  durationSeconds,
  answerStarted = false,
}: ThinkingProcessProps) {
  const panelId = useId();
  // null = follow the automatic behaviour; once the reader toggles, respect their choice.
  const [userExpanded, setUserExpanded] = useState<boolean | null>(null);
  const expanded = userExpanded ?? (isStreaming && !answerStarted);
  // Live reasoning is revealed smoothly while the panel is open; a closed panel or a saved message shows it as is.
  const shownReasoning = useSmoothText(reasoning, isStreaming, REASONING_SMOOTH_OPTIONS, { enabled: expanded }).text;
  const reasoningScroll = useStickToBottom(shownReasoning, isStreaming);

  const steps = thoughts || [];
  const hasReasoning = reasoning.trim().length > 0;
  if (!isStreaming && steps.length === 0 && !hasReasoning) return null;

  const issues = steps.filter((s) => s.status === "error").length;

  // Header: while live, name what is actually happening right now; afterwards, summarize.
  let label: string;
  if (isStreaming) {
    const running = [...steps].reverse().find((s) => s.status === "running");
    if (answerStarted) label = "Writing the answer";
    // An open panel already names the current step in its timeline; the header would only repeat it.
    else if (expanded) label = hasReasoning ? "Thinking" : "Working";
    else if (running?.id === "synthesis") label = hasReasoning ? "Thinking" : running.title;
    else label = running?.title || steps[steps.length - 1]?.title || "Starting";
  } else {
    const verb = hasReasoning ? "Thought" : "Worked";
    label = durationSeconds !== undefined ? `${verb} for ${formatSeconds(durationSeconds)}` : `${verb} through ${steps.length} steps`;
  }

  const reasoningBlock = hasReasoning ? (
    <div
      ref={reasoningScroll.ref}
      onScroll={reasoningScroll.onScroll}
      className={`thinking-reasoning${isStreaming && expanded ? " thinking-reasoning-live" : ""}`}
      aria-label="Model reasoning"
    >
      {shownReasoning.trim()}
    </div>
  ) : null;

  // Show the model's reasoning under the step that produced it (synthesis), else at the end.
  const reasoningAnchor = steps.findIndex((s) => s.id === "synthesis");

  return (
    <div className="thinking">
      <button
        type="button"
        className="thinking-toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setUserExpanded(!expanded)}
      >
        <ChevronRight className="thinking-chevron" style={{ width: 14, height: 14 }} aria-hidden="true" />
        <span className={`thinking-label${isStreaming ? " thinking-label-live" : ""}`} aria-live="polite">
          {label}
        </span>
        {isStreaming ? (
          <span className="thinking-meta">
            {formatSeconds(elapsedSeconds)}
            {expanded && steps.length > 1 && <> &middot; {steps.length} steps</>}
          </span>
        ) : (
          <span className="thinking-meta">
            {steps.length > 0 && <>&middot; {steps.length} step{steps.length === 1 ? "" : "s"}</>}
            {issues > 0 && (
              <span className="thinking-meta-warn">
                {" "}
                &middot; {issues} issue{issues === 1 ? "" : "s"}
              </span>
            )}
          </span>
        )}
      </button>

      {expanded && (
        <div id={panelId} className="thinking-panel">
          <ol className="thinking-timeline">
            {steps.map((th, idx) => {
              // A finished run never shows a spinner, even for a step that was never closed.
              const status = displayStatus(th.status, !isStreaming);
              const time = status === "running" ? "" : formatSeconds(th.duration_seconds);
              const liveness = isStreaming ? stepLiveness({ ...th, status }, elapsedSeconds) : null;
              return (
                <li key={th.id || idx} className="thinking-step" data-status={status}>
                  <span className="thinking-step-icon">
                    <StepIcon status={status} />
                  </span>
                  <div>
                    <div className="thinking-step-head">
                      <span className="thinking-step-title">{th.title}</span>
                      {time && th.duration_seconds ? <span className="thinking-step-time">{time}</span> : null}
                      {status === "unreported" ? <span className="thinking-step-time">not reported as finished</span> : null}
                      {liveness ? <span className="thinking-step-time" data-liveness="true">{liveness}</span> : null}
                    </div>
                    {th.text && <div className="thinking-step-detail">{cleanStepText(th.text)}</div>}
                    {th.id === "plan" && th.extra?.entities ? <QueryPlanChips plan={th.extra as QueryPlanExtra} /> : null}
                    {idx === reasoningAnchor && reasoningBlock}
                  </div>
                </li>
              );
            })}
            {reasoningAnchor === -1 && reasoningBlock && (
              <li className="thinking-step" data-status={isStreaming ? "running" : "done"}>
                <span className="thinking-step-icon">
                  <StepIcon status={isStreaming ? "running" : "done"} />
                </span>
                <div>{reasoningBlock}</div>
              </li>
            )}
            {isStreaming && steps.length === 0 && !hasReasoning && (
              <li className="thinking-step" data-status="running">
                <span className="thinking-step-icon">
                  <StepIcon status="running" />
                </span>
                <span className="thinking-step-title">Connecting</span>
              </li>
            )}
          </ol>
        </div>
      )}
    </div>
  );
}
