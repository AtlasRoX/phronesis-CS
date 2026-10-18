/**
 * A run or step duration for display: "4.2s" under 10 s, "42s" under a minute, else "9m 59s" / "10m 0s".
 * The total is rounded to whole seconds before it is split into minutes and seconds, so 599.6 s reads
 * "10m 0s" (never "9m 60s") and 59.6 s reads "1m 0s" (never "60s").
 */
export function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "";
  const value = Math.max(0, s);
  if (value < 9.95) return `${value.toFixed(1)}s`;
  const total = Math.round(value);
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${total % 60}s`;
}
