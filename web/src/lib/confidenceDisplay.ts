// Claim-confidence presentation (UI-004). Framework-free so `node --test` can import it.
// The scores are heuristic evidence-support scores (hand-set feature weights, a temperature fitted on few samples),
// not probabilities that a claim is true, so they are shown as a 0-1 score with the band word, never as "82%".

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Score truncated (not rounded) to 2 decimals, so the shown value never crosses a band threshold it did not reach. */
export function supportScoreText(v: unknown): string {
  if (!isNum(v)) return "—";
  const clamped = Math.max(0, Math.min(1, v));
  return (Math.floor(clamped * 100 + 1e-9) / 100).toFixed(2);
}

/** Band with the backend thresholds (high >= 0.75, medium >= 0.45 by default). */
export function scoreBand(v: number, highAt = 0.75, mediumAt = 0.45): "high" | "medium" | "low" {
  return v >= highAt ? "high" : v >= mediumAt ? "medium" : "low";
}

const SOURCE_TEXT: Record<string, string> = {
  default: "no calibration file (uncalibrated defaults)",
  package_default: "the calibration file shipped with Phronesis",
  file: "a site calibration file",
  env: "an environment override",
};

/** One visible sentence on where the temperature came from and on how many labelled claims it was fitted. */
export function calibrationProvenance(cal: Record<string, unknown> | null | undefined): string {
  const c = cal || {};
  const src = typeof c.source === "string" ? c.source : "";
  const srcText = src ? SOURCE_TEXT[src] || src.replace(/\+/g, " + ").replace(/_/g, " ") : "not reported";
  const metrics = c.metrics && typeof c.metrics === "object" ? (c.metrics as Record<string, unknown>) : {};
  const n = isNum(c.n_samples) ? c.n_samples : isNum(metrics.n_samples) ? metrics.n_samples : null;
  const t = isNum(c.temperature) ? c.temperature : null;
  const parts = [`Calibration: ${srcText}`];
  if (t !== null) parts.push(t === 1 ? "temperature 1 (no scaling)" : `temperature ${Number(t.toFixed(2))}`);
  if (n !== null) parts.push(`fitted on ${n} labelled claim${n === 1 ? "" : "s"}${n < 100 ? " (small sample)" : ""}`);
  else if (src && src !== "default") parts.push("sample size not reported");
  return `${parts.join(", ")}.`;
}
