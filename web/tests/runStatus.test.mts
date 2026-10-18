// Run with: node --test web/tests/runStatus.test.mts   (Node 24 strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as vs from "../src/lib/verificationState.ts";
import * as th from "../src/components/agentic/thoughts.ts";

const { claimSummary, taskHeadline } = vs;
const { stepLiveness, STALE_STEP_SECONDS } = th;

test("a budget-stopped run is a partial answer, never 'completed', even when its time course ran", () => {
  const h = taskHeadline({
    task_status: "completed",
    requested_operation: "time_course",
    executed_operation: "time_course",
    incomplete_reasons: ["Agent token budget used up before every sub-question was answered."],
    unresolved: ["What are the exact FDA-approved indications?"],
  });
  assert.ok(h);
  assert.equal(h.partial, true);
  assert.equal(h.status, "partial");
  assert.equal(h.title, "Partial answer");
  assert.deepEqual(h.unresolved, ["What are the exact FDA-approved indications?"]);
  assert.equal(taskHeadline({ task_status: "partial" })?.title, "Partial answer");
  assert.equal(taskHeadline({ task_status: "completed" })?.partial, false);
  assert.equal(taskHeadline(null), null);
  assert.equal(taskHeadline({ task_status: "completed", incomplete_reasons: "not a list" })?.partial, false);
});

test("claim counts name checked claims and explain a different finding count instead of contradicting it", () => {
  const c = { supported: 22, contradicted: 0, unsupported: 8, not_checked: 0 };
  const { summary, note } = claimSummary(c, 30, 9);
  assert.equal(summary, "8 of 30 checked claims lack support in the retrieved evidence");
  assert.match(note, /9 findings/);
  assert.match(note, /counted separately from claims/);
  assert.equal(claimSummary(c, 30, 8).note, "");
  assert.equal(claimSummary({ supported: 29, contradicted: 0, unsupported: 1, not_checked: 0 }, 30).summary, "1 of 30 checked claims lacks support in the retrieved evidence");
  assert.equal(claimSummary({ supported: 3, contradicted: 0, unsupported: 0, not_checked: 0 }, 3).summary, "All 3 checked claims supported by the evidence");
});

test("a heartbeat says 'still working' with time since real progress; silence past the threshold says so", () => {
  const beat = { status: "running", elapsed_seconds: 100, extra: { heartbeat: true, waiting_s: 40 } };
  assert.equal(stepLiveness(beat, 103), "still working · 43s");
  assert.equal(stepLiveness({ status: "running", elapsed_seconds: 100, extra: { heartbeat: true, last_progress_s: 70 } }, 100), "still working · 30s");
  assert.equal(stepLiveness({ status: "running", elapsed_seconds: 10 }, 10 + STALE_STEP_SECONDS + 5), "no update for 25s");
  assert.equal(stepLiveness({ status: "running", elapsed_seconds: 10 }, 15), null);
  assert.equal(stepLiveness({ status: "done", elapsed_seconds: 10 }, 500), null);
  // A heartbeat that itself stopped arriving is silence, not progress.
  assert.equal(stepLiveness({ ...beat }, 100 + STALE_STEP_SECONDS + 1), "no update for 21s");
});
