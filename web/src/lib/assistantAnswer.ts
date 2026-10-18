// What an assistant message says when the backend result has no answer text (UI-009). Framework-free so `node --test`
// can import it. "No answer" is a failure state, never a fabricated "Clinical evaluation completed." success line.
import type { TaskOutcome } from "../types/chat";

export const EMPTY_ANSWER_TEXT =
  "No answer was returned for this question. The run may have failed or been cut off before it wrote one; try asking again.";

export interface ResolvedAnswer {
  content: string;
  /** True when the result carried no answer text. */
  empty: boolean;
  task_status?: TaskOutcome;
}

/**
 * The answer text and task outcome of a chat result. A null result, or one whose `response` is empty, becomes an
 * explicit "no answer" message with `task_status: failed` (or the backend's own non-completed outcome, if it sent one).
 */
export function resolveAnswer(
  res: { response?: unknown; task_status?: TaskOutcome | null } | null | undefined
): ResolvedAnswer {
  const text = typeof res?.response === "string" ? res.response : "";
  const outcome = res?.task_status || undefined;
  if (text.trim()) return { content: text, empty: false, task_status: outcome };
  const keep = outcome && outcome.task_status !== "completed" ? outcome : undefined;
  return {
    content: EMPTY_ANSWER_TEXT,
    empty: true,
    task_status: keep || {
      ...(outcome || {}),
      task_status: "failed",
      reason: "The server returned no answer text.",
    },
  };
}
