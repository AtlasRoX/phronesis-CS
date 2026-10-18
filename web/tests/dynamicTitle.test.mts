import { test } from "node:test";
import assert from "node:assert/strict";
import { generateDynamicTitle } from "../src/lib/dynamicTitle.ts";

test("generateDynamicTitle extracts clean titles from conversational prompts", () => {
  // Preamble stripping & capitalization
  assert.equal(
    generateDynamicTitle("Can you please explain what are the common resistance mutations for osimertinib?"),
    "Common Resistance Mutations for Osimertinib"
  );

  // Biomedical acronyms & genes preserved
  assert.equal(
    generateDynamicTitle("What is the role of KRAS G12D in pancreatic cancer?"),
    "Role of KRAS G12D in Pancreatic Cancer"
  );

  assert.equal(
    generateDynamicTitle("Tell me about BRCA1 and TP53 mutations in NSCLC"),
    "BRCA1 and TP53 Mutations in NSCLC"
  );

  // Direct questions
  assert.equal(
    generateDynamicTitle("How does pembrolizumab compare to nivolumab in melanoma?"),
    "Pembrolizumab Compare to Nivolumab in Melanoma"
  );

  // Short prompts
  assert.equal(
    generateDynamicTitle("EGFR T790M"),
    "EGFR T790M"
  );

  // Empty or invalid inputs
  assert.equal(generateDynamicTitle(""), "Clinical Investigation");
  assert.equal(generateDynamicTitle("   ??? "), "Clinical Investigation");
});

test("generateDynamicTitle drops punctuation left at the cut", () => {
  const title = generateDynamicTitle(
    "For KRAS G12C-mutant non-small cell lung cancer, compare sotorasib and adagrasib: summarize their pivotal trials"
  );
  assert.ok(!/[,.:;]$/.test(title), title);
});
