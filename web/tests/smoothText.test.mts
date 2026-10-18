// Run with: node --test web/tests/   (Node 24 strips the TypeScript types)
// Pure stepping logic behind useSmoothText, plus the streaming-Markdown tail stabilizer.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as smooth from "../src/lib/smoothText.ts";
import * as safety from "../src/lib/markdownSafety.ts";

const {
  ANSWER_SMOOTH_OPTIONS: OPTS,
  REASONING_SMOOTH_OPTIONS,
  commonPrefixLength,
  finishArrivals,
  reconcile,
  revealLimit,
  revealSpeed,
  safeBoundary,
  snapToWordEnd,
  step,
} = smooth;
const { stabilizeStreamingTail } = safety;

const FRAME = 1000 / 60;

function words(n: number): string {
  const out: string[] = [];
  for (let i = 0; i < n; i++) out.push(`word${i % 10}`);
  return out.join(" ") + " ";
}

/** Runs frames until the target is fully shown (or `maxMs` passes); returns elapsed ms and the per-frame sizes. */
function simulate(
  target: string,
  opts: { streaming: boolean; arrivals: { end: number; deadline: number }[]; start?: number; shown?: number; maxMs?: number }
) {
  let state = { shownLength: opts.shown ?? 0, pos: opts.shown ?? 0 };
  let now = 0;
  const steps: number[] = [];
  while (state.shownLength < target.length && now < (opts.maxMs ?? 10_000)) {
    now += FRAME;
    const before = state.shownLength;
    state = step(target, state, { streaming: opts.streaming, dtMs: FRAME, now, arrivals: opts.arrivals }, OPTS);
    assert.ok(state.shownLength >= before, "the reveal never moves backwards");
    steps.push(state.shownLength - before);
  }
  return { elapsed: now, steps, state };
}

test("base speed is steady and readable for a small backlog", () => {
  const text = words(20); // ~120 chars
  const { elapsed, state } = simulate(text, { streaming: false, arrivals: [] });
  assert.equal(state.shownLength, text.length);
  // ~220 chars/s base: 120 chars take roughly half a second, not one frame.
  assert.ok(elapsed > 350 && elapsed < 900, `elapsed ${elapsed}`);
});

test("a large backlog speeds the reveal up proportionally", () => {
  const small = revealSpeed(50, 0, [], 0, OPTS);
  const large = revealSpeed(5000, 0, [], 0, OPTS);
  assert.equal(small, OPTS.baseCharsPerSecond);
  assert.ok(large >= 5000 * 1000 / OPTS.catchUpMs);
  assert.ok(large > small * 10);
  assert.equal(revealSpeed(0, 0, [], 0, OPTS), 0);
});

test("a chunk is never on screen later than maxLagMs after it arrived", () => {
  const text = words(900); // ~5,400 chars arriving in one burst
  const { elapsed, state } = simulate(text, { streaming: true, arrivals: [{ end: text.length, deadline: OPTS.maxLagMs }] });
  assert.equal(state.shownLength, text.length);
  assert.ok(elapsed <= OPTS.maxLagMs + 2 * FRAME, `elapsed ${elapsed}`);
});

test("a big burst is spread over many frames, not dumped in one", () => {
  const text = words(900);
  const { steps } = simulate(text, { streaming: true, arrivals: [{ end: text.length, deadline: OPTS.maxLagMs }] });
  assert.ok(steps.length > 40, `frames ${steps.length}`);
  assert.ok(Math.max(...steps) < text.length / 10, `largest step ${Math.max(...steps)}`);
});

test("finishing after the stream ends takes at most finishMs, smoothly", () => {
  const text = words(600);
  const shown = 200;
  const arrivals = finishArrivals([], text.length, 0, OPTS);
  const { elapsed, steps, state } = simulate(text, { streaming: false, arrivals, shown });
  assert.equal(state.shownLength, text.length);
  assert.ok(elapsed <= OPTS.finishMs + 2 * FRAME, `elapsed ${elapsed}`);
  assert.ok(steps.length >= 20, "not revealed instantly");
});

test("finishArrivals pulls every pending deadline in to finishMs", () => {
  const next = finishArrivals([{ end: 100, deadline: 5000 }, { end: 50, deadline: 200 }], 300, 1000, OPTS);
  assert.deepEqual(next, [
    { end: 100, deadline: 1000 + OPTS.finishMs },
    { end: 50, deadline: 200 },
    { end: 300, deadline: 1000 + OPTS.finishMs },
  ]);
});

test("steps end on word boundaries", () => {
  const text = words(40);
  let state = { shownLength: 0, pos: 0 };
  for (let now = FRAME; state.shownLength < text.length; now += FRAME) {
    state = step(text, state, { streaming: false, dtMs: FRAME, now, arrivals: [] }, OPTS);
    if (state.shownLength < text.length) {
      const atBoundary = /\s/.test(text[state.shownLength]) || /\s/.test(text[state.shownLength - 1]);
      assert.ok(atBoundary, `cut mid-word at ${state.shownLength}`);
    }
  }
});

test("while streaming, a trailing partial word is held back until it completes", () => {
  const target = "The answer is ready and compl";
  assert.equal(revealLimit(target, true, OPTS), "The answer is ready and ".length);
  assert.equal(revealLimit(target, false, OPTS), target.length);
  assert.equal(revealLimit(target + "ete ", true, OPTS), target.length + 4);
  // A single long token (URL) is not held back forever.
  const url = "See https://example.org/a/very/long/path/that/keeps/going";
  assert.equal(revealLimit(url, true, OPTS), url.length);
  // ...and a held word is revealed once its chunk's deadline passes.
  const held = step(target, { shownLength: 24, pos: 24 }, { streaming: true, dtMs: FRAME, now: 2000, arrivals: [{ end: target.length, deadline: 1500 }] }, OPTS);
  assert.equal(held.shownLength, target.length);
});

test("snapToWordEnd and safeBoundary never split words or surrogate pairs", () => {
  assert.equal(snapToWordEnd("hello world", 2, 11, 16), 5);
  assert.equal(snapToWordEnd("hello world", 6, 11, 16), 6);
  assert.equal(snapToWordEnd("hello world", 8, 11, 16), 11);
  const emoji = "ok \u{1F600}\u{1F600}";
  assert.equal(safeBoundary(emoji, 4), 5);
  assert.equal(safeBoundary(emoji, 5), 5);
});

test("a revision keeps the common prefix instead of retyping from scratch", () => {
  const head = words(400); // ~2,400 chars shared
  const shown = head + "The old conclusion was that";
  const target = head + "The revised conclusion is that the evidence is weak.";
  assert.equal(commonPrefixLength(shown, target), head.length + "The ".length);
  const r = reconcile(shown, target, OPTS);
  assert.equal(r.rewrite, false);
  // Backs off to the start of the diverging word; nothing on screen is thrown away beyond it.
  assert.equal(r.shown, head + "The ");
  assert.ok(target.startsWith(r.shown));
});

test("a revision whose text only extends what is shown changes nothing", () => {
  const r = reconcile("Hello wor", "Hello world, again", OPTS);
  assert.deepEqual(r, { shown: "Hello wor", rewrite: false });
});

test("a shorter revision truncates to the shared prefix", () => {
  const r = reconcile("Alpha beta gamma delta epsilon zeta eta theta", "Alpha beta gamma.", OPTS);
  assert.equal(r.rewrite, false);
  assert.equal(r.shown, "Alpha beta ");
});

test("a full rewrite is detected and shown at once (cross-fade), not retyped", () => {
  const shown = words(800);
  const target = "Completely different answer. " + words(500);
  const r = reconcile(shown, target, OPTS);
  assert.equal(r.rewrite, true);
  assert.equal(r.shown, target);
  // With little on screen, a change just retypes from the shared prefix.
  const short = reconcile("Draft", "Final text", OPTS);
  assert.equal(short.rewrite, false);
  assert.equal(short.shown, "");
});

test("the reasoning preset is faster than the answer preset", () => {
  assert.ok(REASONING_SMOOTH_OPTIONS.baseCharsPerSecond > OPTS.baseCharsPerSecond);
  assert.ok(REASONING_SMOOTH_OPTIONS.finishMs <= OPTS.finishMs);
});

test("streaming tail: a table appears only once header and delimiter rows are in", () => {
  assert.equal(stabilizeStreamingTail("Intro\n| Gene | Score |"), "Intro");
  assert.equal(stabilizeStreamingTail("| Gene | Score |\n| --- | -"), "");
  const ready = "| Gene | Score |\n| --- | --- |";
  assert.equal(stabilizeStreamingTail(ready), ready);
  // A half-typed row is held back; complete rows stay.
  const rows = "| Gene | Score |\n| --- | --- |\n| TP53 | 0.9 |\n| EGF";
  assert.equal(stabilizeStreamingTail(rows), "| Gene | Score |\n| --- | --- |\n| TP53 | 0.9 |");
});

test("streaming tail: lone markers wait for their text; open fences are untouched", () => {
  assert.equal(stabilizeStreamingTail("Some text\n-"), "Some text");
  assert.equal(stabilizeStreamingTail("Para\n##"), "Para");
  assert.equal(stabilizeStreamingTail("1. one\n2."), "1. one");
  const fence = "```python\nx = 1\n| not a table";
  assert.equal(stabilizeStreamingTail(fence), fence);
});

test("streaming tail: unclosed bold and inline code are closed provisionally", () => {
  assert.equal(stabilizeStreamingTail("The **key finding "), "The **key finding**");
  assert.equal(stabilizeStreamingTail("Done **bold** and more"), "Done **bold** and more");
  assert.equal(stabilizeStreamingTail("Run `npm te"), "Run `npm te`");
  assert.equal(stabilizeStreamingTail("Next **"), "Next");
});
