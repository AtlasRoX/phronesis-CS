// Run with: node --test web/tests/   (Node 24 strips the TypeScript types)
// The "Worked for …" / step duration formatter.
import { test } from "node:test";
import assert from "node:assert/strict";
import { formatSeconds } from "../src/lib/duration.ts";

test("minutes never show 60 seconds", () => {
  assert.equal(formatSeconds(599.6), "10m 0s");
  assert.equal(formatSeconds(599.61), "10m 0s");
  assert.equal(formatSeconds(119.5), "2m 0s");
  assert.equal(formatSeconds(223.4), "3m 43s");
  assert.equal(formatSeconds(60), "1m 0s");
});

test("under a minute rounds the total, never to 60s", () => {
  assert.equal(formatSeconds(59.6), "1m 0s");
  assert.equal(formatSeconds(59.4), "59s");
  assert.equal(formatSeconds(42.2), "42s");
});

test("under ten seconds keeps one decimal", () => {
  assert.equal(formatSeconds(4.24), "4.2s");
  assert.equal(formatSeconds(0), "0.0s");
  assert.equal(formatSeconds(9.97), "10s");
});

test("missing or invalid values render empty", () => {
  assert.equal(formatSeconds(undefined), "");
  assert.equal(formatSeconds(null), "");
  assert.equal(formatSeconds(Number.NaN), "");
});
