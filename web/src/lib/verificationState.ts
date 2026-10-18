// Trust state of an assistant answer (UI-003). Framework-free so `node --test` can import it.
//
// Every assistant message gets an explicit state: an answer with no report, or with the placeholder report the backend
// attaches to direct replies and clarification stops ({passed: true, verification_score: 1.0, defects: []} with none of
// the fields a real verifier run carries), is "not checked", never silently treated as verified.

export type VerificationKind = "not_checked" | "verified" | "checked" | "problems";

interface ClaimLike {
  status?: string;
  claim_type?: string;
}

export interface VerificationLike {
  passed?: boolean;
  verification_score?: number;
  defects?: unknown[];
  claims?: ClaimLike[];
  fully_verified?: boolean;
  [k: string]: unknown;
}

export interface VerificationState {
  kind: VerificationKind;
  /** One short line for quiet states; problem summaries are built by the notice itself. */
  text: string;
  counts: { supported: number; contradicted: number; unsupported: number; not_checked: number };
}

/** Fields only a real verifier run produces (phronesis VerificationReport); the placeholder has none of them. */
const VERIFIER_FIELDS = [
  "fully_verified",
  "claims",
  "verified_identifiers",
  "claim_support",
  "numerical_checks_passed",
  "numerical_checks_failed",
  "contradicted_claims",
  "unsupported_claims",
  "unchecked_claims",
];

/** True when the report is the backend's placeholder (or explicitly says no check ran). */
export function isPlaceholderReport(report: VerificationLike): boolean {
  if (report.checked === false || report.synthetic === true || report.skipped === true || report.not_checked === true) {
    return true;
  }
  const defects = Array.isArray(report.defects) ? report.defects : [];
  if (defects.length) return false;
  return !VERIFIER_FIELDS.some((k) => k in report);
}

export function verificationState(report: VerificationLike | null | undefined): VerificationState {
  const counts = { supported: 0, contradicted: 0, unsupported: 0, not_checked: 0 };
  if (!report || typeof report !== "object") {
    return { kind: "not_checked", text: "Not checked against the evidence", counts };
  }
  // Run-status statements ("the lookup failed") are checked against the tool log; they are not evidence claims.
  const claims = (Array.isArray(report.claims) ? report.claims : []).filter((c) => c.claim_type !== "operational");
  for (const c of claims) {
    const s = String(c?.status || "");
    if (s in counts) counts[s as keyof typeof counts] += 1;
  }
  const defects = Array.isArray(report.defects) ? report.defects : [];
  if (isPlaceholderReport(report)) {
    return { kind: "not_checked", text: "No evidence check ran for this reply", counts };
  }
  const problems = counts.contradicted + counts.unsupported;
  if (!report.passed || problems > 0 || counts.not_checked > 0 || defects.length > 0) {
    return { kind: "problems", text: "", counts };
  }
  if (report.fully_verified === true && claims.length > 0) {
    return {
      kind: "verified",
      text: `All ${claims.length} claim${claims.length === 1 ? "" : "s"} checked and supported by the retrieved evidence`,
      counts,
    };
  }
  return {
    kind: "checked",
    text: claims.length
      ? `${counts.supported} of ${claims.length} claim${claims.length === 1 ? "" : "s"} checked and supported`
      : "Checked: no checkable claims; cited identifiers match the tool outputs",
    counts,
  };
}

// ---------------------------------------------------------------------------------------------------------------------
// Claim-count wording and the task headline, kept here so they can be unit tested without React.

export interface ClaimCountsLike {
  supported: number;
  contradicted: number;
  unsupported: number;
  not_checked: number;
}

/**
 * One summary line over the checked claims ("2 of 30 checked claims lack support"), plus a note when the verifier's
 * finding list has a different length: findings also cover citations, identifiers and format, so they are a different
 * measure from claims and are never shown as a second, conflicting claim count.
 */
export function claimSummary(counts: ClaimCountsLike, total: number, defectCount = 0): { summary: string; note: string } {
  const kinds = [
    counts.contradicted ? { n: counts.contradicted, short: "contradicted", long: "contradicted by the sources" } : null,
    counts.unsupported ? { n: counts.unsupported, short: "unsupported", long: "lack support in the retrieved evidence" } : null,
    counts.not_checked ? { n: counts.not_checked, short: "not checked", long: "could not be checked" } : null,
  ].filter((k): k is { n: number; short: string; long: string } => k !== null);
  const of = `${total} checked claim${total === 1 ? "" : "s"}`;
  let summary: string;
  if (kinds.length === 0) summary = `All ${of} supported by the evidence`;
  else if (kinds.length === 1) {
    const k = kinds[0];
    const verb = k.n === 1 ? k.long.replace(/^lack/, "lacks") : k.long;
    summary = `${k.n} of ${of} ${verb}`;
  } else summary = `${of}: ${kinds.map((k) => `${k.n} ${k.short}`).join(", ")}`;
  const problems = counts.contradicted + counts.unsupported + counts.not_checked;
  const note =
    defectCount > 0 && defectCount !== problems
      ? `The verifier also lists ${defectCount} finding${defectCount === 1 ? "" : "s"}; findings include citation, identifier and format checks, so they are counted separately from claims.`
      : "";
  return { summary, note };
}

export interface TaskOutcomeLike {
  task_status?: string;
  status?: string;
  requested_operation?: string | null;
  executed_operation?: string | null;
  request_match?: boolean;
  reason?: string;
  incomplete_reasons?: unknown;
  unresolved?: unknown;
}

export interface TaskHeadline {
  status: string;
  partial: boolean;
  title: string;
  reasons: string[];
  unresolved: string[];
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((v) => String(v ?? "").trim()).filter(Boolean) : [];
}

/**
 * The headline for whether the request finished. A run that stopped early (budget, open sub-questions) is a
 * "Partial answer" with its reasons, even when one operation inside it (a time course) completed: tool completion is
 * never shown as request completion.
 */
export function taskHeadline(outcome: TaskOutcomeLike | null | undefined): TaskHeadline | null {
  if (!outcome || typeof outcome !== "object") return null;
  const raw = String(outcome.task_status || outcome.status || "");
  if (!raw) return null;
  const reasons = stringList(outcome.incomplete_reasons);
  const unresolved = stringList(outcome.unresolved);
  const partial = raw === "partial" || (raw === "completed" && (reasons.length > 0 || unresolved.length > 0));
  const status = partial ? "partial" : raw;
  return { status, partial, title: partial ? "Partial answer" : "", reasons, unresolved };
}
