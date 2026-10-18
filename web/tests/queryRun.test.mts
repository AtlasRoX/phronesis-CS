// Run with: node --test web/tests/queryRun.test.mts   (Node strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as thoughts from "../src/components/agentic/thoughts.ts";

const { finalizeThoughts, displayStatus } = thoughts;

// UI-013: a step left "running" when the run ended never reported completion; it must not be shown as done (✓).
test("finalizeThoughts marks steps left running as unreported when the run is done, keeping errors", () => {
  const out = finalizeThoughts([
    { id: "agent_plan", status: "running", duration_seconds: null },
    { id: "agent_gaps_0", status: "error" },
    { id: "synthesis", status: "done", duration_seconds: 3.2 },
    { id: "legacy" },
  ]);
  assert.deepEqual(
    out.map((t: { status?: string }) => t.status),
    ["unreported", "error", "done", undefined]
  );
  assert.equal(finalizeThoughts(null).length, 0);
});

test("finalizeThoughts does not mutate the input", () => {
  const input = [{ id: "a", status: "running" }];
  finalizeThoughts(input);
  assert.equal(input[0].status, "running");
});

test("displayStatus never shows a spinner (nor a false check mark) for a finished run", () => {
  // UI-013: a step still running at the end is "unreported", not "done".
  assert.equal(displayStatus("running", true), "unreported");
  assert.equal(displayStatus("running", false), "running");
  assert.equal(displayStatus(undefined, true), "done");
  assert.equal(displayStatus("error", true), "error");
});
