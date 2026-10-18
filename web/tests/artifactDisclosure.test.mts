// Run with: node --test web/tests/artifactDisclosure.test.mts   (Node 24 strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as ad from "../src/lib/artifactDisclosure.ts";

const { artifactDisclosure, questionRequests, exclusionClauses } = ad;

const EXACT_PMID_QUERY =
  "Retrieve PMID 39369609 by exact identifier. If your tool cannot do exact lookup, say so and try one primary-source " +
  "alternative.\nKeep the answer under 400 words.\nNo FDA sweep, simulation, omics, hypothesis generation or debate is " +
  "needed. Do not rewrite the answer without new evidence.";

test("literature, plan review, sandbox analysis and calibration open whenever present", () => {
  const open = artifactDisclosure({
    question: "anything",
    artifacts: { literature: {}, plan_approval: {}, sandbox_analysis: { status: "ok" }, model_calibration: {} },
  });
  assert.deepEqual(open, { literature: true, plan_approval: true, sandbox_analysis: true, model_calibration: true });
});

test("domain cards open only when the question asks for that evidence", () => {
  const arts = { drug_intel: {}, clinical_trials: {}, vivarix: {}, lumicell: {}, omics_intel: {}, primekg: {} };
  const open = artifactDisclosure({ question: "What are the FDA-approved indications and recruiting trials for sotorasib?", artifacts: arts });
  assert.equal(open.drug_intel, true);
  assert.equal(open.clinical_trials, true);
  assert.equal(open.vivarix, false);
  assert.equal(open.lumicell, false);
  assert.equal(open.omics_intel, false);
  assert.equal(open.primekg, false);
});

test("explicit exclusion clauses keep excluded analyses compact", () => {
  assert.equal(questionRequests("vivarix", "Run a kinetic simulation of EGFR signalling."), true);
  assert.equal(questionRequests("vivarix", "Summarize EGFR trials. No simulation or single-cell analysis needed."), false);
  assert.equal(questionRequests("lumicell", "Summarize EGFR trials. No simulation or single-cell analysis needed."), false);
  assert.equal(questionRequests("clinical_trials", "Summarize EGFR trials. No simulation or single-cell analysis needed."), true);
  // The list form of the live diagnostic query excludes FDA, simulation, omics and hypotheses together.
  for (const key of ["drug_intel", "vivarix", "omics_intel", "hypotheses"]) {
    assert.equal(questionRequests(key, EXACT_PMID_QUERY), false, key);
  }
  assert.equal(questionRequests("vivarix", "Simulate the MAPK model without running a parameter scan."), true);
  assert.equal(questionRequests("drug_intel", "Report FDA approvals; do not run a simulation."), true);
  assert.equal(questionRequests("vivarix", "Report FDA approvals; do not run a simulation."), false);
  assert.equal(questionRequests("vivarix", "Simulation is not needed; list the trials."), false);
});

test("a plain 'no' inside other wording is not an exclusion", () => {
  assert.deepEqual(exclusionClauses("If no exact lookup works, try one alternative."), []);
  assert.equal(questionRequests("clinical_trials", "If no trial is recruiting, say so."), true);
});

test("failed analyses open even when the question did not ask for them", () => {
  const open = artifactDisclosure({
    question: "Summarize the literature",
    artifacts: { vivarix: { success: false, error: "solver failed" }, lumicell: { status: "error" }, structure_intel: { error: "" } },
  });
  assert.equal(open.vivarix, true);
  assert.equal(open.lumicell, true);
  assert.equal(open.structure_intel, false);
});

test("counter-evidence opens for contradictions or heuristic-only classification", () => {
  const q = "x";
  assert.equal(artifactDisclosure({ question: q, artifacts: { evidence_conflicts: { classifier: "nli", conflicts: [{ label: "contradicts" }] } } }).evidence_conflicts, true);
  assert.equal(artifactDisclosure({ question: q, artifacts: { evidence_conflicts: { classifier: "heuristic", conflicts: [{ label: "neutral" }] } } }).evidence_conflicts, true);
  assert.equal(artifactDisclosure({ question: q, artifacts: { evidence_conflicts: { classifier: "nli", conflicts: [{ label: "supports" }] } } }).evidence_conflicts, false);
});

test("claim-support scores open for a low-support claim or overall support below 0.6", () => {
  const conf = (c: Record<string, unknown>) => artifactDisclosure({ artifacts: { claim_confidence: c } }).claim_confidence;
  assert.equal(conf({ answer_confidence: 0.55, claims: [] }), true);
  assert.equal(conf({ answer_confidence: 0.8, claims: [{ band: "low" }] }), true);
  assert.equal(conf({ answer_confidence: 0.8, claims: [{ band: "high", confidence: 0.9 }] }), false);
});

test("the agent run and plan open for unfinished or failed work and stay compact otherwise", () => {
  const run = (r: Record<string, unknown>, taskStatus?: Record<string, unknown>) =>
    artifactDisclosure({ taskStatus, artifacts: { agent_run: r } }).agent_run;
  assert.equal(run({ sub_questions: [{ status: "answered" }], trace: [{ success: true }], phases: [{ status: "ok" }] }), false);
  assert.equal(run({ sub_questions: [{ status: "partial" }] }), true);
  assert.equal(run({ budget: { exhausted: true } }), true);
  assert.equal(run({ trace: [{ success: false }] }), true);
  assert.equal(run({ phases: [{ status: "error" }] }), true);
  assert.equal(run({}, { task_status: "partial" }), true);
  assert.equal(run({}, { task_status: "completed", unresolved: ["q2"] }), true);
  const plan = (p: Record<string, unknown>) => artifactDisclosure({ artifacts: { agent_plan: p } }).agent_plan;
  assert.equal(plan({ planner: "llm", nodes: [{ status: "done" }] }), false);
  assert.equal(plan({ planner: "llm", nodes: [{ status: "error" }] }), true);
  assert.equal(plan({ planner: "llm", rejected: [{ reason: "policy" }] }), true);
  assert.equal(plan({ planner: "none" }), true);
});

test("routine dialogue and project memory stay collapsed", () => {
  const open = artifactDisclosure({ question: "Use project memory and show the agent dialogue", artifacts: { agent_dialogue: {}, project_memory: {} } });
  assert.deepEqual(open, { agent_dialogue: false, project_memory: false });
});
