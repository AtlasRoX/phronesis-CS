// Pure stepping logic for the smooth text reveal (useSmoothText). The stream delivers text in bursts; the
// reveal walks through it at a steady, readable cadence, speeds up when it falls behind, and never lags a
// chunk by more than `maxLagMs`. Keep this module framework-free and erasable-TypeScript-only so
// `node --test` can import it.

export interface SmoothTextOptions {
  /** Steady reveal speed when there is little backlog (characters per second). */
  baseCharsPerSecond: number;
  /** Proportional catch-up: the backlog drains at roughly backlog / catchUpMs (an ease-out). */
  catchUpMs: number;
  /** Every arrived chunk is fully on screen at most this long after it arrived. */
  maxLagMs: number;
  /** Once the stream ends, whatever is left is revealed within this time. */
  finishMs: number;
  /** A reset keeping less than this share of the text on screen is a full rewrite: cross-fade, don't retype. */
  rewritePrefixRatio: number;
  /** Below this many shown characters a reset just retypes; there is nothing worth cross-fading. */
  rewriteMinShown: number;
  /** How far a step may stretch to finish the word it lands in, and how long a trailing word is held back. */
  wordLookahead: number;
  /** Minimum time between committed (rendered) updates: ~30 fps keeps long Markdown answers smooth. */
  commitIntervalMs: number;
}

export const ANSWER_SMOOTH_OPTIONS: SmoothTextOptions = {
  baseCharsPerSecond: 220,
  catchUpMs: 1000,
  maxLagMs: 1500,
  finishMs: 600,
  rewritePrefixRatio: 0.25,
  rewriteMinShown: 40,
  wordLookahead: 16,
  commitIntervalMs: 30,
};

/** The reasoning panel is secondary, small text: reveal it faster, with a shorter lag. */
export const REASONING_SMOOTH_OPTIONS: SmoothTextOptions = {
  ...ANSWER_SMOOTH_OPTIONS,
  baseCharsPerSecond: 520,
  catchUpMs: 600,
  maxLagMs: 1000,
  finishMs: 400,
};

/** A chunk of target text that must be fully revealed by `deadline` (a performance.now() timestamp). */
export interface Arrival {
  end: number;
  deadline: number;
}

export interface RevealState {
  /** Characters of the target currently on screen. */
  shownLength: number;
  /** Fractional reveal position; the shown text runs ahead of it to the end of a word. */
  pos: number;
}

const MAX_STEP_MS = 250;

function isSpace(code: number): boolean {
  return code === 32 || code === 10 || code === 9 || code === 13;
}

/** Never cut between the two halves of a surrogate pair (emoji, some symbols). */
export function safeBoundary(text: string, index: number): number {
  if (index <= 0 || index >= text.length) return Math.max(0, Math.min(index, text.length));
  const code = text.charCodeAt(index - 1);
  return code >= 0xd800 && code <= 0xdbff ? index + 1 : index;
}

export function commonPrefixLength(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i++;
  return i;
}

export interface Reconciled {
  shown: string;
  /** True for a full rewrite: show `target` at once (cross-fade) instead of retyping it. */
  rewrite: boolean;
}

/**
 * The target changed so that it no longer extends what is on screen (a content_reset / revision).
 * Keep the common prefix and continue typing from there; if almost nothing is shared, it is a full rewrite.
 */
export function reconcile(shown: string, target: string, opts: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS): Reconciled {
  if (target.startsWith(shown)) return { shown, rewrite: false };
  const prefix = commonPrefixLength(shown, target);
  if (shown.length >= opts.rewriteMinShown && prefix < shown.length * opts.rewritePrefixRatio) {
    return { shown: target, rewrite: true };
  }
  // Back off to the start of the word the texts diverge in, so a half-word never lingers.
  let cut = prefix;
  while (cut > 0 && !isSpace(target.charCodeAt(cut - 1)) && prefix - cut < opts.wordLookahead) cut--;
  if (cut > 0 && !isSpace(target.charCodeAt(cut - 1))) cut = prefix;
  cut = Math.min(cut, prefix);
  if (cut > 0 && target.charCodeAt(cut - 1) >= 0xd800 && target.charCodeAt(cut - 1) <= 0xdbff) cut--;
  return { shown: target.slice(0, cut), rewrite: false };
}

/**
 * How far the reveal may go right now. While streaming, a short trailing word fragment is held back until
 * the rest of the word (whitespace after it) arrives, so words never appear half-typed.
 */
export function revealLimit(target: string, streaming: boolean, opts: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS): number {
  const len = target.length;
  if (!streaming || len === 0 || isSpace(target.charCodeAt(len - 1))) return len;
  let i = len;
  while (i > 0 && !isSpace(target.charCodeAt(i - 1)) && len - i < opts.wordLookahead) i--;
  return i > 0 && isSpace(target.charCodeAt(i - 1)) ? i : len;
}

/** Extend `end` to the end of the word it falls in (up to `lookahead` characters), never past `limit`. */
export function snapToWordEnd(target: string, end: number, limit: number, lookahead: number): number {
  if (end >= limit) return limit;
  if (end > 0 && isSpace(target.charCodeAt(end - 1))) return end;
  const stop = Math.min(limit, end + lookahead);
  for (let j = end; j < stop; j++) {
    if (isSpace(target.charCodeAt(j))) return j;
  }
  return stop === limit ? limit : safeBoundary(target, end);
}

/** Reveal speed in characters per second: steady base, proportional catch-up, and chunk deadlines. */
export function revealSpeed(
  backlog: number,
  shownLength: number,
  arrivals: readonly Arrival[],
  now: number,
  opts: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS
): number {
  if (backlog <= 0) return 0;
  let cps = Math.max(opts.baseCharsPerSecond, (backlog * 1000) / opts.catchUpMs);
  for (const a of arrivals) {
    if (a.end <= shownLength) continue;
    const left = a.deadline - now;
    if (left > 0) cps = Math.max(cps, ((a.end - shownLength) * 1000) / left);
  }
  return cps;
}

export interface StepInput {
  streaming: boolean;
  dtMs: number;
  now: number;
  arrivals: readonly Arrival[];
}

/** One animation frame: how much of `target` is on screen afterwards. */
export function step(
  target: string,
  state: RevealState,
  input: StepInput,
  opts: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS
): RevealState {
  const shownLength = Math.min(state.shownLength, target.length);
  let limit = revealLimit(target, input.streaming, opts);
  let overdue = 0;
  for (const a of input.arrivals) {
    if (a.deadline <= input.now && a.end > overdue) overdue = Math.min(a.end, target.length);
  }
  // A chunk past its deadline is revealed in full, held-back word or not.
  limit = Math.max(limit, overdue);
  if (shownLength >= limit) return { shownLength, pos: Math.max(state.pos, shownLength) };

  const dt = Math.min(Math.max(input.dtMs, 0), MAX_STEP_MS);
  const cps = revealSpeed(target.length - shownLength, shownLength, input.arrivals, input.now, opts);
  let pos = Math.max(state.pos, overdue) + (cps * dt) / 1000;
  if (pos <= shownLength) return { shownLength, pos };

  let end = Math.min(limit, Math.floor(pos));
  if (end <= shownLength) end = Math.min(limit, shownLength + 1);
  end = snapToWordEnd(target, end, limit, opts.wordLookahead);
  end = Math.max(end, Math.min(overdue, limit));
  pos = Math.min(pos, target.length);
  return { shownLength: end, pos };
}

/** Drop chunks that are already fully on screen. */
export function pruneArrivals(arrivals: readonly Arrival[], shownLength: number): Arrival[] {
  return arrivals.filter((a) => a.end > shownLength);
}

/** The stream ended: everything still pending must be on screen within `finishMs`. */
export function finishArrivals(
  arrivals: readonly Arrival[],
  targetLength: number,
  now: number,
  opts: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS
): Arrival[] {
  const deadline = now + opts.finishMs;
  const next = arrivals.map((a) => ({ end: a.end, deadline: Math.min(a.deadline, deadline) }));
  next.push({ end: targetLength, deadline });
  return next;
}
