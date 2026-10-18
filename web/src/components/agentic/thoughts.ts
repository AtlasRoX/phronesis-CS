/**
 * Thought-step helpers shared by the chat page and the thinking panel (plain TypeScript, no React, so the node
 * tests can import it).
 */

export interface FinalizableThought {
  status?: string;
  duration_seconds?: number | null;
  elapsed_seconds?: number;
}

/**
 * Once a run is done no step may keep spinning. A step still marked "running" never reported its end (cut off by a
 * budget or timeout, or its closing event was lost), so it becomes "unreported", never "done" (UI-013): a check mark
 * would claim the step completed. Steps that finished keep their own status (an "error" stays an error).
 */
export function finalizeThoughts<T extends FinalizableThought>(thoughts: T[] | null | undefined): T[] {
  return (thoughts || []).map((th) => (th && th.status === "running" ? { ...th, status: "unreported" } : th));
}

/**
 * The status a step is shown with: a step of a finished run is never "running"; one left running is "unreported".
 * Older saved messages carry no status at all and are shown as done, as they were when saved.
 */
export function displayStatus<S extends string>(status: S | undefined, runFinished: boolean): S | "done" | "unreported" {
  const s: S | "done" = status || "done";
  return runFinished && s === "running" ? "unreported" : s;
}

export interface LiveThought extends FinalizableThought {
  started_at?: number;
  extra?: Record<string, unknown> | null;
}

/** Seconds of silence after which a running step is flagged as not updating. */
export const STALE_STEP_SECONDS = 20;

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * A short liveness note for a running step, or null. `runElapsedSeconds` is the client's live run clock; a step's
 * `elapsed_seconds` is the run time of its latest update. A heartbeat only says the worker is alive, so it is shown as
 * "still working" with the time since the step last made real progress, never as progress; a step with no update
 * (heartbeat or otherwise) for STALE_STEP_SECONDS says so. No percentages or ETAs are invented.
 */
export function stepLiveness(step: LiveThought | null | undefined, runElapsedSeconds: number, staleAfter = STALE_STEP_SECONDS): string | null {
  if (!step || step.status !== "running") return null;
  const lastUpdate = num(step.elapsed_seconds) ?? num(step.started_at);
  const silence = lastUpdate != null ? Math.max(0, runElapsedSeconds - lastUpdate) : 0;
  if (silence > staleAfter) return `no update for ${Math.round(silence)}s`;
  const extra = step.extra || {};
  if (extra.heartbeat === true) {
    const waiting = num(extra.waiting_s);
    const lastProgress = num(extra.last_progress_s);
    const since = waiting ?? (lastProgress != null && lastUpdate != null ? Math.max(0, lastUpdate - lastProgress) : null);
    return since != null ? `still working · ${Math.round(since + silence)}s` : "still working";
  }
  return null;
}
