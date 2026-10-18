// Run with: node --test web/tests/agenticLabels.test.mts   (Node 24 strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as labels from "../src/components/agentic/evidenceLabels.ts";
import * as cols from "../src/components/agentic/tableColumns.ts";

const { prettyEvidenceKey, evidenceDisplayNames } = labels;
const { visibleColumns, columnKind, isEmptyCell } = cols;

test("prettyEvidenceKey strips the agent phase prefix and names the tool", () => {
  assert.equal(prettyEvidenceKey("agent.gaps.4.lithoscribe"), "Europe PMC (gap search)");
  assert.equal(prettyEvidenceKey("agent.gaps.5.websurfer"), "web search (gap search)");
  assert.equal(prettyEvidenceKey("agent.plan_execute.2.chembl"), "ChEMBL (plan step)");
  assert.equal(prettyEvidenceKey("synlethdb"), "SynLethDB");
  assert.equal(prettyEvidenceKey("tool:dgidb"), "DGIdb");
  assert.equal(prettyEvidenceKey("PMID:12345"), "PMID:12345");
  assert.equal(prettyEvidenceKey("agent.debate.3.some_tool"), "some tool (debate)");
});

test("evidenceDisplayNames prefers backend labels, falls back to keys and collapses duplicates", () => {
  assert.deepEqual(
    evidenceDisplayNames(["agent.gaps.4.lithoscribe", "agent.gaps.5.websurfer"], ["Europe PMC (gap round 1)", "web search (gap round 1)"]),
    ["Europe PMC (gap round 1)", "web search (gap round 1)"]
  );
  assert.deepEqual(evidenceDisplayNames(["agent.gaps.4.lithoscribe", "agent.gaps.9.lithoscribe"]), ["Europe PMC (gap search)"]);
  assert.deepEqual(evidenceDisplayNames(["synlethdb"], undefined), ["SynLethDB"]);
  assert.deepEqual(evidenceDisplayNames(null), []);
});

test("visibleColumns hides columns whose every value is empty or n/a", () => {
  const rows = [
    { gene: "PARP1", drug: "olaparib", type: "n/a", score: 0.9 },
    { gene: "PARP1", drug: "niraparib", type: "N/A", score: null },
    { gene: "ATR", drug: "ceralasertib", type: "", score: 0.4 },
  ];
  assert.deepEqual(visibleColumns(rows), ["gene", "drug", "score"]);
  // Hidden columns do not use up the column cap.
  assert.deepEqual(visibleColumns([{ a: null, b: "x", c: "y" }], 2), ["b", "c"]);
  assert.equal(isEmptyCell("—"), true);
  assert.equal(isEmptyCell(0), false);
});

test("columnKind gives only long text a wrapping width", () => {
  const rows = [
    { tdl: "Tbio", desc: "studied biology (GO experimental terms, OMIM phenotype or literature) but no small molecule", n: 3 },
    { tdl: "Tclin", desc: "approved drug with a known mechanism", n: 5 },
  ];
  assert.equal(columnKind(rows, "tdl"), "short");
  assert.equal(columnKind(rows, "desc"), "long");
  assert.equal(columnKind(rows, "n"), "num");
});
