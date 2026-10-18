import { test } from "node:test";
import assert from "node:assert/strict";
import { cleanStepText, decodeEntities } from "../src/lib/entities.ts";

test("decodeEntities decodes abstract entities", () => {
  assert.equal(decodeEntities("p &lt;0.001 &amp; HR&nbsp;0.5"), "p <0.001 & HR 0.5");
  assert.equal(decodeEntities(null), "");
});

test("cleanStepText replaces stored defect tags from older runs", () => {
  assert.equal(
    cleanStepText("[CLINICAL_CLAIM] - Comparative safety studies"),
    "Clinical claim not found in the evidence: Comparative safety studies",
  );
  assert.equal(cleanStepText("[CLAIM_CONTRADICTED] There is no clear rationale"), "Contradicted by a source: There is no clear rationale");
  assert.equal(cleanStepText("Checked 3 claim(s) [not a tag]"), "Checked 3 claim(s) [not a tag]");
});
