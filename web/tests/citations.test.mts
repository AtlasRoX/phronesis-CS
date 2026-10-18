// Run with: node --test web/tests/citations.test.mts   (Node 24 strips the TypeScript types)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cite from "../src/lib/citations.ts";

const { CITE_PATTERN, hrefToKey, rehypeCitations, resolveKey, supportFor, toCiteKey } = cite;

type Node = { type: string; tagName?: string; value?: string; properties?: Record<string, unknown>; children?: Node[] };

const text = (value: string): Node => ({ type: "text", value });
const el = (tagName: string, children: Node[] = [], properties: Record<string, unknown> = {}): Node => ({
  type: "element",
  tagName,
  properties,
  children,
});
const root = (...children: Node[]): Node => ({ type: "root", children });

function entry(key: string, url: string, extra: Record<string, unknown> = {}) {
  const [prefix, id] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
  const type = { PMID: "pmid", DOI: "doi", NCT: "nct", URL: "url" }[prefix] || "pmid";
  return { key, id, type, url, source_agent: "Lithoscribe", source_tool: "lithoscribe", passages: [], verified: true, ...extra };
}

// Ids from the trimmed real EuropePMC records in tests/fixtures/citations/live_samples.json.
const MAP = {
  version: 1,
  entries: {
    "PMID:42511798": entry("PMID:42511798", "https://pubmed.ncbi.nlm.nih.gov/42511798/"),
    "NCT:NCT03600883": entry("NCT:NCT03600883", "https://clinicaltrials.gov/study/NCT03600883"),
    "DOI:10.64898/2026.10.05.26364739": entry(
      "DOI:10.64898/2026.10.05.26364739",
      "https://doi.org/10.64898/2026.10.05.26364739"
    ),
  },
  aliases: { "DOI:10.3390/ijms27146455": "PMID:42511798" },
  supports: [
    { key: "PMID:42511798", occurrence: 0, sentence: "a", highlight: null, shared_terms: [], grounded_by_verifier: false },
    { key: "PMID:42511798", occurrence: 1, sentence: "b", highlight: "x", shared_terms: ["x"], grounded_by_verifier: false },
  ],
  unverified: ["PMID:9500320"],
} as any;

function run(tree: Node, map = MAP): Node {
  rehypeCitations({ map })(tree);
  return tree;
}

function anchors(node: Node, out: Node[] = []): Node[] {
  if (node.type === "element" && node.properties && node.properties.dataCiteKey) out.push(node);
  for (const c of node.children || []) anchors(c, out);
  return out;
}

test("a cited PMID inside a paragraph becomes a citation anchor", () => {
  const p = el("p", [text("Covalent inhibitors bind a cryptic GDP-bound pocket (PMID: 42511798).")]);
  run(root(p));
  assert.equal(p.children!.length, 3);
  const [before, a, after] = p.children!;
  assert.equal(before.value, "Covalent inhibitors bind a cryptic GDP-bound pocket (");
  assert.equal(a.tagName, "a");
  assert.deepEqual(a.properties, {
    href: "https://pubmed.ncbi.nlm.nih.gov/42511798/",
    className: ["cite-ref"],
    dataCiteKey: "PMID:42511798",
    dataCiteOccurrence: "0",
  });
  assert.equal(a.children![0].value, "PMID: 42511798");
  assert.equal(after.value, ").");
});

test("text inside code, pre and existing links is never rewritten", () => {
  const code = el("code", [text("PMID: 42511798")]);
  const pre = el("pre", [el("code", [text("NCT03600883")])]);
  const link = el("a", [text("PMID: 42511798")], { href: "https://example.org/x" });
  const kbd = el("kbd", [text("NCT03600883")]);
  const tree = root(el("p", [code, text(" "), link, kbd]), pre);
  run(tree);
  assert.equal(anchors(tree).length, 0);
  assert.equal(code.children![0].value, "PMID: 42511798");
  assert.equal(pre.children![0].children![0].value, "NCT03600883");
  assert.equal(link.children![0].type, "text");
});

test("a table cell with two ids yields two anchors with per-key occurrence counters", () => {
  const td1 = el("td", [text("PMID: 42511798 and NCT03600883")]);
  const td2 = el("td", [text("again PMID 42511798")]);
  const tree = root(el("table", [el("tr", [td1, td2])]));
  run(tree);
  const found = anchors(tree).map((a) => [a.properties!.dataCiteKey, a.properties!.dataCiteOccurrence]);
  assert.deepEqual(found, [
    ["PMID:42511798", "0"],
    ["NCT:NCT03600883", "0"],
    ["PMID:42511798", "1"],
  ]);
});

test("ids not in map.entries stay text; rejected ids get a plain dotted marker", () => {
  const p = el("p", [text("NCT04303780 is unknown; PMID 9500320 was rejected.")]);
  run(root(p));
  assert.equal(anchors(p).length, 0);
  const span = p.children!.find((c) => c.tagName === "span")!;
  assert.equal(span.children![0].value, "PMID 9500320");
  assert.equal(span.properties!.title, "Not verified against the retrieved evidence");
  assert.ok(!("href" in span.properties!));
  assert.ok(p.children!.some((c) => c.type === "text" && c.value!.startsWith("NCT04303780 is unknown")));
});

test("DOIs are normalized (case, trailing punctuation) and resolved through aliases", () => {
  assert.equal(toCiteKey("doi 10.3390/IJMS27146455."), "DOI:10.3390/ijms27146455");
  assert.equal(toCiteKey("(10.1016/s0140-6736(97)11096-0)"), "DOI:10.1016/s0140-6736(97)11096-0");
  assert.equal(resolveKey(MAP, "DOI:10.3390/ijms27146455"), "PMID:42511798");
  const p = el("p", [text("Reviewed in (10.3390/IJMS27146455). Preprint: 10.64898/2026.10.05.26364739.")]);
  run(root(p));
  const found = anchors(p);
  assert.deepEqual(
    found.map((a) => a.properties!.dataCiteKey),
    ["PMID:42511798", "DOI:10.64898/2026.10.05.26364739"]
  );
  assert.equal(found[0].children![0].value, "10.3390/IJMS27146455");
  assert.equal(found[1].children![0].value, "10.64898/2026.10.05.26364739");
});

test("an existing link to a known entry is annotated in place and counted in document order", () => {
  const link = el("a", [text("CodeBreaK 100")], { href: "https://clinicaltrials.gov/study/NCT03600883", className: ["x"] });
  const p = el("p", [text("Trial NCT03600883 "), link, text(" and NCT03600883")]);
  run(root(p));
  assert.equal(link.properties!.dataCiteKey, "NCT:NCT03600883");
  assert.equal(link.properties!.dataCiteOccurrence, "1");
  assert.deepEqual(link.properties!.className, ["x", "cite-ref"]);
  assert.deepEqual(
    anchors(p).map((a) => a.properties!.dataCiteOccurrence),
    ["0", "1", "2"]
  );
});

test("hrefToKey maps database link-outs and plain web links", () => {
  assert.equal(hrefToKey("https://pubmed.ncbi.nlm.nih.gov/42511798/"), "PMID:42511798");
  assert.equal(hrefToKey("https://doi.org/10.3390/IJMS27146455"), "DOI:10.3390/ijms27146455");
  assert.equal(hrefToKey("https://www.biomodels.org/BIOMD0000000010"), "BIOMD:BIOMD0000000010");
  assert.equal(hrefToKey("https://example.org/page/#frag"), "URL:https://example.org/page");
  assert.equal(hrefToKey("javascript:alert(1)"), null);
  assert.equal(hrefToKey("#section"), null);
});

test("supportFor returns the exact occurrence and falls back when counts drift", () => {
  assert.equal(supportFor(MAP, "PMID:42511798", 0)!.sentence, "a");
  assert.equal(supportFor(MAP, "DOI:10.3390/ijms27146455", 1)!.sentence, "b");
  assert.equal(supportFor(MAP, "PMID:42511798", 7)!.sentence, "b");
  assert.equal(supportFor(MAP, "NCT:NCT03600883", 0), null);
});

test("an empty map leaves the tree untouched", () => {
  const p = el("p", [text("PMID: 42511798")]);
  run(root(p), { version: 1, entries: {}, aliases: {}, supports: [], unverified: [] } as any);
  assert.equal(p.children!.length, 1);
});

test("identifier grammar matches the backend scanner (shared fixture)", () => {
  const url = new URL("../../tests/fixtures/citations/scan_parity.json", import.meta.url);
  const { cases } = JSON.parse(readFileSync(url, "utf8"));
  for (const c of cases) {
    const keys = [...String(c.text).matchAll(CITE_PATTERN)].map((m) => toCiteKey(m[0])).filter(Boolean);
    assert.deepEqual(keys, c.keys, c.text);
  }
});
