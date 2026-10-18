// Collapsed-header line of the variant card (UI-011). Framework-free so `node --test` can import it.
import type { VariantIntelArtifact } from "../types/variants";

function int(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("en-US") : "–";
}

function af(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "–";
  return v === 0 ? "0" : v.toExponential(1);
}

/** ClinVar review status as stars, the way ClinVar shows it ("criteria provided, single submitter" -> 1 star). */
export function reviewStars(status: string | null | undefined): string | null {
  const s = String(status || "").toLowerCase();
  if (!s) return null;
  if (s.includes("practice guideline")) return "4★";
  if (s.includes("expert panel")) return "3★";
  if (s.includes("multiple submitters") && s.includes("no conflicts")) return "2★";
  if (s.includes("conflicting")) return "1★ conflicting";
  if (s.includes("single submitter")) return "1★";
  if (s.includes("no assertion") || s.includes("no classification") || s.includes("no interpretation")) return "0★";
  return null;
}

/** "gnomad_r4" -> "gnomAD v4"; unknown datasets are shown as given. */
export function gnomadLabel(dataset: string | null | undefined): string {
  const d = String(dataset || "").trim();
  if (!d) return "gnomAD (version not reported)";
  const m = /^gnomad_r(\d+)(?:_(\w+))?$/i.exec(d);
  return m ? `gnomAD v${m[1]}${m[2] ? ` (${m[2]})` : ""}` : d;
}

/**
 * One line that makes the collapsed card useful: headline ClinVar call with its review status, tier, CIViC count,
 * and the top cohort prevalence with its study and denominator (a tiny cohort must not read as authoritative).
 */
export function variantCardMeta(data: VariantIntelArtifact): string {
  const parts: string[] = [];
  const rec = (data.clinvar || []).find(
    (r) => r.germline_classification || r.oncogenicity_classification || r.somatic_clinical_impact,
  );
  const germline = rec?.germline_classification;
  const headline = germline || rec?.oncogenicity_classification;
  if (headline) {
    const review = germline ? rec?.germline_review_status : rec?.oncogenicity_review_status;
    const stars = reviewStars(review);
    parts.push(`${headline} (ClinVar${stars ? ` ${stars}` : review ? `: ${review}` : ", review status not reported"})`);
  }
  const tier = rec?.somatic_clinical_impact ? /^Tier\s+[IV]+\b/i.exec(rec.somatic_clinical_impact)?.[0] : null;
  if (tier) parts.push(tier);
  if (data.civic) {
    const n = data.civic.accepted_evidence_total;
    parts.push(`${int(n)} CIViC item${n === 1 ? "" : "s"}`);
  }
  const studies = (data.cbioportal || []).filter((s) => typeof s.prevalence_percent === "number");
  if (studies.length) {
    const top = studies.reduce((a, b) => ((b.prevalence_percent as number) > (a.prevalence_percent as number) ? b : a));
    const where = data.cancer_filter ? `${data.cancer_filter} samples` : "samples";
    const pct = Number((top.prevalence_percent as number).toFixed(1));
    const denom = typeof top.profiled_samples === "number" ? `${int(top.mutated_samples)}/${int(top.profiled_samples)}` : null;
    parts.push(
      `${studies.length > 1 ? "up to " : ""}${pct}% of ${where} (${[denom, top.study_name || top.study_id].filter(Boolean).join(", ")})`,
    );
  }
  if (data.gnomad?.exome_af != null) parts.push(`gnomAD AF ${af(data.gnomad.exome_af)}`);
  return parts.join(" · ") || data.hgvs_p || "";
}
