// Run with: node --test web/tests/agentic.test.mts   (Node 24 strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as agent from "../src/services/agentService.ts";
import * as plan from "../src/lib/planGraph.ts";

const { normalizeAgentOptions, toAgentOptions, readSseEvents, streamJobEvents, listJobs, listProjects, DEFAULT_AGENT_OPTIONS } = agent;
const { layerNodes } = plan;

function sseBody(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
}

test("normalizeAgentOptions fills defaults and clamps budget to the backend's ranges", () => {
  assert.deepEqual(normalizeAgentOptions(null), DEFAULT_AGENT_OPTIONS);
  const n = normalizeAgentOptions({ enable_hypotheses: false, effort_level: "ultra", budgetEnabled: true, budget: { max_tool_calls: 999, max_draft_rounds: -3, max_seconds: "x" } });
  assert.equal(n.enable_hypotheses, false);
  assert.equal(n.effort_level, "ultra");
  assert.equal(n.allow_clarification, true);
  assert.equal(n.budget.max_tool_calls, 200);
  assert.equal(n.budget.max_draft_rounds, 0);
  assert.equal(n.budget.max_seconds, DEFAULT_AGENT_OPTIONS.budget.max_seconds);
});

test("toAgentOptions sends budget only when enabled, with ChatRequest.agent_options field names", () => {
  const off = toAgentOptions(DEFAULT_AGENT_OPTIONS);
  assert.equal("budget" in off, false);
  assert.equal(off.effort_level, "medium");
  assert.deepEqual(Object.keys(off).sort(), [
    "allow_clarification",
    "effort_level",
    "enable_code_analysis",
    "enable_counter_evidence",
    "enable_hypotheses",
    "require_plan_approval",
  ]);
  const on = toAgentOptions({ ...DEFAULT_AGENT_OPTIONS, budgetEnabled: true });
  assert.deepEqual(Object.keys(on.budget ?? {}).sort(), ["max_draft_rounds", "max_llm_calls", "max_seconds", "max_tool_calls"]);
});

test("readSseEvents parses frames split across chunks, skips pings and malformed data, stops on request", async () => {
  const seen: any[] = [];
  await readSseEvents(
    sseBody([
      ': ping\n\ndata: {"type":"run_con',
      'trol","run_id":"run_1"}\n\ndata: not-json\n\n',
      'data: {"type":"content_delta","delta":"a"}\n\ndata: {"type":"done","result":{}}\n\ndata: {"type":"after"}\n\n',
    ]),
    (ev: any) => {
      seen.push(ev);
      return ev.type === "done";
    }
  );
  assert.deepEqual(
    seen.map((e) => e.type),
    ["run_control", "content_delta", "done"]
  );
  assert.equal(seen[0].run_id, "run_1");
});

test("streamJobEvents requests ?after=N with the bearer header and unwraps {seq, event} frames", async () => {
  const orig = globalThis.fetch;
  let url = "";
  let headers: any = {};
  globalThis.fetch = (async (u: string, init: any) => {
    url = u;
    headers = init.headers;
    return new Response(sseBody(['data: {"seq":1,"event":{"type":"thought","data":{"id":"s"}}}\n\n', 'data: {"type":"done","result":{"response":"ok"}}\n\n']), {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }) as typeof fetch;
  try {
    const types: string[] = [];
    await streamJobEvents("http://x", (extra: Record<string, string> = {}) => ({ ...extra, Authorization: "Bearer t" }), "job_1", 3, (ev: any) => {
      types.push(ev.type);
      return ev.type === "done";
    });
    assert.equal(url, "http://x/api/jobs/job_1/events?after=3");
    assert.equal(headers.Authorization, "Bearer t");
    assert.deepEqual(types, ["thought", "done"]);
  } finally {
    globalThis.fetch = orig;
  }
});

test("list helpers accept bare arrays or wrapped lists and normalise project ids", async () => {
  const orig = globalThis.fetch;
  globalThis.fetch = (async (u: string) =>
    new Response(
      JSON.stringify(u.endsWith("/api/jobs") ? { jobs: [{ job_id: "j1", status: "running" }, { nope: 1 }] } : [{ project_id: "prj_abcdef", name: "" }]),
      { status: 200 }
    )) as typeof fetch;
  try {
    const jobs = await listJobs("http://x", () => ({}));
    assert.deepEqual(jobs.map((j: any) => j.job_id), ["j1"]);
    const projects = await listProjects("http://x", () => ({}));
    assert.equal(projects[0].id, "prj_abcdef");
    assert.equal(projects[0].name, "prj_abcdef");
  } finally {
    globalThis.fetch = orig;
  }
});

test("layerNodes layers a DAG by longest dependency chain and survives cycles and unknown deps", () => {
  const d = layerNodes([
    { id: "n1", depends_on: [] },
    { id: "n2", depends_on: ["n1"] },
    { id: "n3", depends_on: ["n1", "n2"] },
    { id: "n4", depends_on: ["missing"] },
  ]);
  assert.deepEqual([...d.entries()].sort(), [
    ["n1", 0],
    ["n2", 1],
    ["n3", 2],
    ["n4", 0],
  ]);
  const cyc = layerNodes([
    { id: "a", depends_on: ["b"] },
    { id: "b", depends_on: ["a"] },
  ]);
  assert.equal(cyc.size, 2);
});
