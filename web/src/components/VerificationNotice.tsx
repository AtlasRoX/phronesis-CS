"use client";

import React, { useId, useState } from "react";
import { AlertTriangle, ChevronRight, CircleAlert, CircleCheck, CircleDashed, CircleX, Hourglass } from "lucide-react";
import { decodeEntities } from "@/lib/entities";
import { claimSummary, taskHeadline, verificationState } from "@/lib/verificationState";
import type { ClaimCheck, ClaimStatus, TaskOutcome, TaskStatus, VerificationReport } from "@/types/chat";

interface VerificationNoticeProps {
  report?: VerificationReport | null;
}

const BANNER_RE = /^\s*> ⚠️ \*\*Scientific Verification Notice\*\*[\s\S]*?(?:\n\s*\n|$)/;

/** The API keeps an honest text banner at the top of unverified answers; the UI shows this notice instead. */
export function stripVerificationBanner(content: string): string {
  return content.replace(BANNER_RE, "");
}

const CLAIM_LABEL: Record<ClaimStatus, string> = {
  supported: "supported",
  contradicted: "contradicted",
  unsupported: "unsupported",
  not_checked: "not checked",
};

// Re-exported for existing importers; the implementation lives in a plain module.
export { decodeEntities };

function claimCounts(claims: ClaimCheck[]): Record<ClaimStatus, number> {
  const counts: Record<ClaimStatus, number> = { supported: 0, contradicted: 0, unsupported: 0, not_checked: 0 };
  for (const c of claims) if (c.status in counts) counts[c.status] += 1;
  return counts;
}

/**
 * Evidence grounding for an answer: unverified claims, and the per-claim check when the verifier returns one.
 * This says whether statements match the retrieved sources, not whether the requested task ran (TaskStatusNotice).
 * Every answer gets an explicit state (UI-003): "not checked" (no report, or the placeholder report of direct replies),
 * "checked"/"verified" as a quiet line, or the warning below when claims failed.
 */
/** One checked claim: status, text, why, and the evidence keys it was matched to. */
function ClaimItem({ c }: { c: ClaimCheck }) {
  const miscited = c.cited_source_supported === false;
  return (
    <li data-status={c.status} data-miscited={miscited || undefined}>
      <span className="verify-claim-status">{miscited && c.status === "supported" ? "miscited" : CLAIM_LABEL[c.status] || c.status}</span>
      <span className="verify-claim-text">{decodeEntities(c.claim_text)}</span>
      {c.reason && <span className="verify-claim-reason">{decodeEntities(c.reason)}</span>}
      {miscited && <span className="verify-claim-reason">The cited source did not support this; another retrieved source did.</span>}
      {c.evidence?.length ? (
        <span className="verify-claim-reason">Evidence: {c.evidence.map((e) => e.source_key).filter(Boolean).join(", ")}</span>
      ) : null}
    </li>
  );
}

/**
 * Evidence grounding for an answer: unverified claims, and the per-claim check when the verifier returns one.
 * This says whether statements match the retrieved sources, not whether the requested task ran (TaskStatusNotice).
 * Every answer gets an explicit state (UI-003): "not checked" (no report, or the placeholder report of direct replies),
 * "checked"/"verified" as a quiet line, or the warning below when claims or citations failed. The warning opens by
 * default: claims that lack support or were miscited and every verifier finding are listed; supported claims sit in a
 * separate compact disclosure. A reader's own Hide/Review choice is kept.
 */
export function VerificationNotice({ report }: VerificationNoticeProps) {
  const [manual, setManual] = useState<boolean | null>(null);
  const listId = useId();
  const state = verificationState(report);
  if (state.kind !== "problems" || !report) {
    const Icon = state.kind === "not_checked" ? CircleDashed : CircleCheck;
    return (
      <div className="task-status verify-state" data-verification={state.kind} role="status">
        <Icon style={{ width: 13, height: 13, flexShrink: 0 }} aria-hidden={true} />
        <span>{state.text}</span>
      </div>
    );
  }
  const allClaims: ClaimCheck[] = Array.isArray(report.claims) ? report.claims : [];
  // Statements about the run itself ("the lookup failed") are checked against its tool log, not against papers.
  const operational = allClaims.filter((c) => c.claim_type === "operational");
  const claims = allClaims.filter((c) => c.claim_type !== "operational");
  const defects: any[] = report.defects || [];
  const counts = claimCounts(claims);
  // The per-claim summary already gives the share; the score is only shown when there are no claim checks.
  const score = !claims.length && typeof report.verification_score === "number" ? `grounding ${report.verification_score.toFixed(2)}` : null;
  const flagged = claims.filter((c) => c.status !== "supported" || c.cited_source_supported === false);
  const supported = claims.filter((c) => c.status === "supported" && c.cited_source_supported !== false);

  let summary: string;
  let note = "";
  if (claims.length) {
    ({ summary, note } = claimSummary(counts, claims.length, defects.length));
  } else if (defects.length > 0) {
    summary = `${defects.length} verifier finding${defects.length === 1 ? "" : "s"}: parts of this answer could not be verified against the evidence`;
  } else {
    summary = "Parts of this answer could not be verified against the evidence";
  }
  const expandable = claims.length > 0 || defects.length > 0 || operational.length > 0;
  const open = expandable && (manual ?? true);

  return (
    <div className="verify-notice" role="note" data-severity={counts.contradicted ? "bad" : "warn"}>
      <button
        type="button"
        className="verify-toggle"
        aria-expanded={expandable ? open : undefined}
        aria-controls={expandable ? listId : undefined}
        onClick={() => expandable && setManual(!open)}
        disabled={!expandable}
      >
        <AlertTriangle style={{ width: 14, height: 14, flexShrink: 0 }} aria-hidden="true" />
        <span className="verify-summary">{summary}</span>
        {score && <span className="verify-score" title="Share of checked statements matched to evidence; not a probability that the answer is correct">{score}</span>}
        {expandable && (
          <span className="verify-action">
            {open ? "Hide" : "Review"}
            <ChevronRight className="verify-chevron" style={{ width: 13, height: 13 }} aria-hidden="true" />
          </span>
        )}
      </button>
      {note && <div className="verify-claim-reason" style={{ display: "block", padding: "0 12px 8px" }}>{note}</div>}
      {open && (
        <div id={listId}>
          {flagged.length > 0 && (
            <ul className="verify-claims" aria-label="Claims that lack support or were miscited">
              {flagged.map((c) => (
                <ClaimItem key={c.claim_id} c={c} />
              ))}
            </ul>
          )}
          {defects.length > 0 && (
            <ul className="verify-list" aria-label="Verifier findings">
              {defects.map((d, i) => (
                <li key={i}>{d.description || d.claim}</li>
              ))}
            </ul>
          )}
          {operational.length > 0 && (
            <details className="verify-supported" style={{ padding: "0 12px 8px" }}>
              <summary className="verify-claim-reason" style={{ display: "list-item", cursor: "pointer" }}>
                {operational.length} statement{operational.length === 1 ? "" : "s"} about this run, checked against its tool log
                rather than sources
              </summary>
              <ul className="verify-claims">
                {operational.map((c) => (
                  <ClaimItem key={c.claim_id} c={c} />
                ))}
              </ul>
            </details>
          )}
          {supported.length > 0 && (
            <details className="verify-supported" style={{ padding: "0 12px 8px" }}>
              <summary className="verify-claim-reason" style={{ display: "list-item", cursor: "pointer" }}>
                {supported.length} supported claim{supported.length === 1 ? "" : "s"}
              </summary>
              <ul className="verify-claims">
                {supported.map((c) => (
                  <ClaimItem key={c.claim_id} c={c} />
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

const TASK_LABEL: Record<TaskStatus, string> = {
  completed: "Requested operation completed",
  partial: "Requested operation partly completed",
  needs_input: "More input needed",
  failed: "Requested operation did not run",
  cancelled: "Cancelled",
};

const TASK_ICON: Record<TaskStatus, React.ComponentType<{ style?: React.CSSProperties; "aria-hidden"?: boolean }>> = {
  completed: CircleCheck,
  partial: CircleDashed,
  needs_input: Hourglass,
  failed: CircleX,
  cancelled: CircleAlert,
};

function opName(op?: string | null): string {
  return op ? op.replace(/_/g, " ") : "";
}

/** "Evidence synthesis partly completed" rather than a generic title over a bare operation name. */
function taskTitle(status: TaskStatus, op: string): string {
  const Op = op ? op.charAt(0).toUpperCase() + op.slice(1) : "";
  if (Op && status === "partial") return `${Op} partly completed`;
  if (Op && status === "failed") return `${Op} did not run`;
  if (Op && status === "cancelled") return `${Op} cancelled`;
  return TASK_LABEL[status] || status;
}

/** Whether the operation the user asked for actually ran (audit F02); shown apart from evidence grounding. */
export function TaskStatusNotice({ outcome }: { outcome?: TaskOutcome | null }) {
  const headline = taskHeadline(outcome);
  if (!outcome || !headline) return null;
  const status = headline.status as TaskStatus;
  const mismatch = outcome.request_match === false;
  const Icon = TASK_ICON[status] || CircleAlert;
  const operations =
    outcome.requested_operation || outcome.executed_operation
      ? mismatch
        ? `Requested ${opName(outcome.requested_operation) || "an operation"}; ran ${opName(outcome.executed_operation) || "nothing"}.`
        : opName(outcome.requested_operation || outcome.executed_operation)
      : "";

  if (status === "completed" && !mismatch) {
    return (
      <div className="task-status" data-status="completed" role="status">
        <Icon style={{ width: 13, height: 13, flexShrink: 0 }} aria-hidden={true} />
        <span>{TASK_LABEL.completed}</span>
        {operations && <span className="task-status-op">{operations}</span>}
      </div>
    );
  }

  if (headline.partial) {
    // A run that stopped early is a partial answer even when one operation inside it (a time course) completed.
    return (
      <div className="task-status task-status-alert" data-status="partial" role="note">
        <Icon style={{ width: 14, height: 14, flexShrink: 0 }} aria-hidden={true} />
        <div style={{ minWidth: 0 }}>
          <span className="task-status-title">{headline.title}</span>
          {outcome.reason && <span className="task-status-reason"> — {outcome.reason}</span>}
          {headline.reasons.length > 0 && (
            <ul className="task-status-reason" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {headline.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
          {headline.unresolved.length > 0 && (
            <div className="task-status-reason">
              Not fully answered:
              <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>
                {headline.unresolved.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}
          {operations && <div className="task-status-op">Ran within this request: {operations}</div>}
        </div>
      </div>
    );
  }

  const title = mismatch ? (status === "completed" ? TASK_LABEL.failed : TASK_LABEL[status] || status) : taskTitle(status, operations);
  return (
    <div className="task-status task-status-alert" data-status={status} role="note">
      <Icon style={{ width: 14, height: 14, flexShrink: 0 }} aria-hidden={true} />
      <div style={{ minWidth: 0 }}>
        <span className="task-status-title">{title}</span>
        {mismatch && operations && <div className="task-status-op">{operations}</div>}
        {outcome.reason &&
          (mismatch && operations ? (
            <div className="task-status-reason">{outcome.reason}</div>
          ) : (
            <span className="task-status-reason"> — {outcome.reason}</span>
          ))}
      </div>
    </div>
  );
}
