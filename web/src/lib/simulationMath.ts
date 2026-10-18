// Pure helpers behind the simulation card (SCI-013, SCI-015). Framework-free and erasable-TypeScript-only so
// `node --test` can import it.

/** Below this magnitude a start value counts as zero (same threshold as phronesis/vivarix engine.py). */
export const ZERO_ABS = 1e-12;

/**
 * Steady-state agreement tolerance: |end − ss| ≤ RTOL·|ss| + ATOL_REL·scale, where `scale` is the largest
 * magnitude of any species in the run (start, end or steady state). The relative part (5 %) is a display
 * heuristic, not a convergence criterion of COPASI; the absolute part (one millionth of the model's largest
 * value) stops a species whose steady state is ~0 from being reported "not reached" because it ends at 1e-7.
 */
export const STEADY_RTOL = 0.05;
export const STEADY_ATOL_REL = 1e-6;

/** Ranking floor for "largest change": changes are relative to max(|start|, SCALE_FLOOR_REL·scale). */
export const SCALE_FLOOR_REL = 1e-3;

type Values = Record<string, unknown> | null | undefined;

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Largest finite magnitude across the given value maps (0 when none). */
export function modelScale(...maps: Values[]): number {
  let scale = 0;
  for (const m of maps) {
    if (!m) continue;
    for (const v of Object.values(m)) {
      const n = num(v);
      if (n !== null && Math.abs(n) > scale) scale = Math.abs(n);
    }
  }
  return scale;
}

export interface SteadyStateCheck {
  /** reached: every species agrees; not_reached: at least one disagrees; unknown: none disagree but some could not be compared. */
  status: "reached" | "not_reached" | "unknown";
  /** Species that ended away from COPASI's steady state. */
  notReached: string[];
  /** Species with no comparable end or steady-state value (missing from one of the two maps). */
  unmatched: string[];
  /** Species actually compared. */
  compared: number;
}

/** Did the time course end at COPASI's steady state? Missing species are "unknown", never a silent pass (SCI-013). */
export function checkSteadyState(
  species: string[],
  finals: Values,
  steady: Values,
  initials?: Values
): SteadyStateCheck {
  const scale = modelScale(finals, steady, initials);
  const atol = STEADY_ATOL_REL * scale;
  const notReached: string[] = [];
  const unmatched: string[] = [];
  let compared = 0;
  for (const sp of species) {
    const end = num(finals?.[sp]);
    const ss = num(steady?.[sp]);
    if (end === null || ss === null) {
      unmatched.push(sp);
      continue;
    }
    compared++;
    if (Math.abs(end - ss) > STEADY_RTOL * Math.abs(ss) + atol) notReached.push(sp);
  }
  const status = notReached.length ? "not_reached" : unmatched.length || !compared ? "unknown" : "reached";
  return { status, notReached, unmatched, compared };
}

function fmtNumber(v: number): string {
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e5)) return v.toExponential(2);
  return v.toPrecision(4);
}

/**
 * Start → end change. Relative (sign from end − start, magnitude over |start|) when the start is non-zero;
 * the absolute change when the start is below `zeroBelow` (a relative change from ~0 is undefined or meaningless).
 */
export function changeText(start: unknown, end: unknown, zeroBelow = ZERO_ABS): string {
  const s = num(start);
  const e = num(end);
  if (s === null || e === null) return "–";
  const delta = e - s;
  const sign = delta > 0 ? "+" : delta < 0 ? "-" : "";
  if (Math.abs(s) < Math.max(zeroBelow, ZERO_ABS)) return `${sign}${fmtNumber(Math.abs(delta))}`;
  const rel = (Math.abs(delta) / Math.abs(s)) * 100;
  return `${sign}${rel >= 1e5 ? rel.toExponential(2) : rel.toFixed(1)}%`;
}

export interface LargestChange {
  species: string;
  /** |end − start| / max(|start|, floor): the ranking score. */
  score: number;
  /** Floor used for this run; starts below it are shown as an absolute change. */
  floor: number;
}

/**
 * The species that moved most, relative to its own start but never relative to less than a thousandth of the run's
 * largest value. Zero-start species (often the activated readout) can be selected, and a near-zero start that moves
 * by a numerically trivial amount cannot dominate the headline (SCI-015).
 */
export function largestChange(species: string[], initials: Values, finals: Values): LargestChange | null {
  const scale = modelScale(initials, finals);
  const floor = Math.max(ZERO_ABS, SCALE_FLOOR_REL * scale);
  let best: LargestChange | null = null;
  for (const sp of species) {
    const s = num(initials?.[sp]);
    const e = num(finals?.[sp]);
    if (s === null || e === null) continue;
    const score = Math.abs(e - s) / Math.max(Math.abs(s), floor);
    if (!best || score > best.score) best = { species: sp, score, floor };
  }
  return best;
}
