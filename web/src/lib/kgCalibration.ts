// How the PrimeKG repurposing "calibration" check is presented (KG-002). Framework-free so `node --test` can import it.
//
// The backend scores the disease's own known indications like any other drug and reports how well they rank (AUROC,
// top-100 recall). Unless ontology relatives are held out, the similar-disease component can find those indications on
// the disease's MONDO parents/children, so the check is in-sample and inflated. The UI therefore never calls it a
// calibrated "high signal": it names it a known-indication recall check, says when it is in-sample, gives n, and only
// uses a positive tone when relatives were held out and enough indications exist.

export const MIN_KNOWN_INDICATIONS = 5;

type Tone = "ok" | "info" | "warn" | "bad" | "neutral" | "faint" | "accent";

export interface RepurposingCheck {
  /** Pill text. */
  label: string;
  tone: Tone;
  /** One visible sentence: the numbers, n and whether they are in-sample. */
  summary: string;
  /** Visible caveat (empty when the check was done with relatives held out). */
  caveat: string;
  /** True when the backend reports that ontology relatives were held out of the similar-disease signal. */
  heldOut: boolean;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/** Band of an AUROC (same thresholds as the backend: 0.85 / 0.7). */
export function aurocBand(auc: number): "high" | "moderate" | "low" {
  return auc >= 0.85 ? "high" : auc >= 0.7 ? "moderate" : "low";
}

export function repurposingCheck(calibration: unknown): RepurposingCheck {
  const cal = obj(calibration) || {};
  // Newer backends may report a strict (relatives held out) computation alongside or instead of the in-sample one.
  const strict = obj(cal.strict);
  const heldOut =
    cal.relatives_held_out === true || cal.ontology_relatives_held_out === true || cal.strict === true || !!strict;
  const src = strict || cal;
  const known = num(src.known_indications) ?? num(cal.known_indications);
  const inTop = num(src.in_top_100);
  const auc = num(src.auroc_known_indications) ?? num(src.auroc);
  const ci = Array.isArray(src.auroc_ci) ? src.auroc_ci.map(num) : Array.isArray(cal.auroc_ci) ? (cal.auroc_ci as unknown[]).map(num) : [];
  const ciText = ci.length === 2 && ci[0] !== null && ci[1] !== null ? ` (95% CI ${ci[0].toFixed(2)}–${ci[1].toFixed(2)})` : "";
  const inSampleCaveat =
    "In-sample check: the similar-disease signal can borrow these very indications from the disease's ontology parents and children, so it overstates how well the ranking finds new uses.";

  if (!known) {
    return {
      label: "not checked",
      tone: "faint",
      summary: "No known indications to check the ranking against.",
      caveat: "Without known indications the candidates' ranking is unchecked.",
      heldOut,
    };
  }
  const numbers = `${inTop ?? 0}/${known} known indications rank in the top 100${auc !== null ? ` · AUROC ${auc.toFixed(2)}${ciText}` : ""} (n = ${known})`;
  if (auc === null) {
    return { label: "not checked", tone: "faint", summary: numbers, caveat: heldOut ? "" : inSampleCaveat, heldOut };
  }
  const band = aurocBand(auc);
  if (known < MIN_KNOWN_INDICATIONS) {
    return {
      label: `inconclusive (n = ${known})`,
      tone: "faint",
      summary: numbers,
      caveat: `Too few known indications (fewer than ${MIN_KNOWN_INDICATIONS}) to judge the ranking.${heldOut ? "" : ` ${inSampleCaveat}`}`,
      heldOut,
    };
  }
  if (heldOut) {
    return {
      label: `recall ${band} (relatives held out)`,
      tone: band === "high" ? "ok" : band === "moderate" ? "warn" : "bad",
      summary: numbers,
      caveat: "",
      heldOut,
    };
  }
  return {
    label: `in-sample recall ${band}`,
    // An in-sample figure is never shown as a green "good" signal.
    tone: band === "low" ? "bad" : "warn",
    summary: numbers,
    caveat: inSampleCaveat,
    heldOut,
  };
}
