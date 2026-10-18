// GWAS effect sizes (UI-012). Odds ratios (null value 1) and betas (null value 0, trait units) are different measures,
// so every value is labelled with its measure instead of sharing one "OR / β" column. Framework-free for node tests.

export type EffectMeasure = "OR" | "β" | "OR or β";

export interface GwasEffect {
  measure: EffectMeasure;
  value: number;
  /** "OR 1.2 [1.1-1.3]"; the measure is always named. */
  text: string;
}

function toNum(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() && Number.isFinite(Number(v))) return Number(v);
  return null;
}

function fmt3(v: number): string {
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(2);
  return Number(v.toFixed(3)).toString();
}

const SOURCES: Array<[string, EffectMeasure]> = [
  ["odds_ratio", "OR"],
  ["or", "OR"],
  ["orPerCopyNum", "OR"],
  ["beta", "β"],
  ["betaNum", "β"],
  // Older payloads: the GWAS Catalog "or_beta" field holds either measure and does not say which.
  ["or_beta", "OR or β"],
];

export function gwasEffect(row: Record<string, unknown> | null | undefined): GwasEffect | null {
  if (!row || typeof row !== "object") return null;
  for (const [key, measure] of SOURCES) {
    const value = toNum(row[key]);
    if (value === null) continue;
    const ci = typeof row.ci_95 === "string" && row.ci_95.trim() ? ` ${row.ci_95.trim()}` : "";
    const unspecified = measure === "OR or β" ? " (measure not reported)" : "";
    return { measure, value, text: `${measure === "OR or β" ? "" : `${measure} `}${fmt3(value)}${ci}${unspecified}` };
  }
  return null;
}
