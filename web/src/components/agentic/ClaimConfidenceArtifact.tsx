"use client";

import React, { useLayoutEffect, useRef, useState } from "react";
import { Gauge } from "lucide-react";
import type { ClaimConfidenceArtifact as ConfidenceData, ClaimConfidenceRow } from "@/types/agentic";
import { Card, Disclosure, arr, fmt, humanize, isNum, str, styles } from "./ui";
import { decodeEntities } from "@/lib/entities";
import { calibrationProvenance, scoreBand, supportScoreText } from "@/lib/confidenceDisplay";

type BandTone = "ok" | "warn" | "bad" | "info";

function bandTone(band: unknown): BandTone {
  const b = str(band).toLowerCase();
  if (b === "high") return "ok";
  if (b === "medium") return "warn";
  if (b === "low") return "bad";
  return "info";
}

const BAND_ORDER = ["low", "medium", "high"];

/** What the NLI check said about a claim, in words; the score beside it is the confidence, not the NLI score. */
function nliText(label: unknown): { text: string; tone: "ok" | "warn" | "bad" | "faint" } {
  const l = str(label).toLowerCase();
  if (!l) return { text: "no NLI check", tone: "faint" };
  if (l.startsWith("entail")) return { text: "evidence supports", tone: "ok" };
  if (l.startsWith("contradict")) return { text: "evidence contradicts", tone: "bad" };
  if (l === "neutral") return { text: "evidence neutral", tone: "warn" };
  return { text: `evidence: ${humanize(l).toLowerCase()}`, tone: "faint" };
}

function featureText(features: Record<string, any> | undefined): string {
  if (!features || typeof features !== "object") return "";
  return Object.entries(features)
    .filter(([, v]) => v != null && (typeof v !== "object" || Array.isArray(v)))
    .map(([k, v]) => `${humanize(k)}: ${typeof v === "number" ? fmt(v, 2) : Array.isArray(v) ? v.length : str(v)}`)
    .join(" · ");
}

/** Claims are lifted from the markdown answer: render **bold** and *italic* (e.g. *p*) instead of showing asterisks. */
function inlineMarkdown(text: string): React.ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g).map((part, i) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (/^\*[^*\s][^*]*\*$/.test(part)) return <em key={i}>{part.slice(1, -1)}</em>;
    return part;
  });
}

/** Whether a line-clamped element hides text, re-measured when the card width changes. */
function useClamped(ref: React.RefObject<HTMLElement | null>, active: boolean): boolean {
  const [clamped, setClamped] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !active) return;
    const measure = () => setClamped(el.scrollHeight > el.clientHeight + 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, active]);
  return clamped;
}

function ClaimItem({ claim }: { claim: ClaimConfidenceRow }) {
  const [open, setOpen] = useState(false);
  const textRef = useRef<HTMLParagraphElement>(null);
  const text = claim.text ? decodeEntities(claim.text) : "(claim text missing)";
  const tone = bandTone(claim.band);
  const nli = nliText(claim.nli_label);
  const value = isNum(claim.confidence) ? Math.max(0, Math.min(1, claim.confidence)) : 0;
  // Only offer "more" when the two-line clamp actually cuts the text; once open, keep "less" available.
  const clamped = useClamped(textRef, !open);
  const long = open || clamped;
  return (
    <li className={styles.claimItem} title={featureText(claim.features) || undefined}>
      <span className={styles.claimScore} data-tone={tone}>
        <span className={styles.claimScoreValue}>{supportScoreText(claim.confidence)}</span>
        <span
          className={styles.claimScoreTrack}
          role="meter"
          aria-label="Evidence-support score (not a probability)"
          aria-valuemin={0}
          aria-valuemax={1}
          aria-valuenow={value}
        >
          <span className={styles.claimScoreFill} style={{ width: `${value * 100}%` }} />
        </span>
      </span>
      <div className={styles.claimBody}>
        <p ref={textRef} className={`${styles.claimText}${open ? "" : ` ${styles.claimClamp}`}`}>
          {inlineMarkdown(text)}
        </p>
        <div className={styles.claimMeta}>
          <span className={styles.claimNli} data-tone={nli.tone}>
            {nli.text}
          </span>
          {long && (
            <>
              <span aria-hidden="true">·</span>
              <button type="button" className={styles.linkBtn} onClick={() => setOpen(!open)} aria-expanded={open}>
                {open ? "less" : "more"}
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  );
}

/**
 * Per-claim evidence-support score (logistic features + temperature scaling; optional conformal back-off). Shown as a
 * 0-1 score with its band, with the "not a probability" caveat and calibration provenance visible (UI-004).
 */
export function ClaimConfidenceArtifact({ data }: { data: ConfidenceData }) {
  if (!data || typeof data !== "object") return null;
  const claims = arr<ClaimConfidenceRow>(data.claims).filter((c) => c && typeof c === "object");
  const cal = data.calibration || {};
  // The backend's bands (high >= 0.75, medium >= 0.45 by default), so the answer score matches the claim bands.
  const bands = (cal as any).bands || {};
  const highAt = isNum(bands.high) ? bands.high : 0.75;
  const mediumAt = isNum(bands.medium) ? bands.medium : 0.45;
  const counts = claims.reduce<Record<string, number>>((acc, c) => {
    const b = str(c.band).toLowerCase() || "unbanded";
    acc[b] = (acc[b] || 0) + 1;
    return acc;
  }, {});
  const meta = [
    `${claims.length} claim${claims.length === 1 ? "" : "s"}`,
    BAND_ORDER.filter((b) => counts[b])
      .map((b) => `${counts[b]} ${b}`)
      .join(", "),
  ]
    .filter(Boolean)
    .join(" · ");

  // Weakest first, grouped by band so the band is said once in a heading rather than on every row.
  const sorted = claims.slice(0, 50).sort((a, b) => (a.confidence ?? 0) - (b.confidence ?? 0));
  const groupKeys = [...BAND_ORDER, ...Object.keys(counts).filter((b) => !BAND_ORDER.includes(b))];
  const groups = groupKeys
    .map((band) => ({ band, items: sorted.filter((c) => (str(c.band).toLowerCase() || "unbanded") === band) }))
    .filter((g) => g.items.length > 0);

  const answer = isNum(data.answer_confidence) ? data.answer_confidence : null;
  const answerBand = answer == null ? "" : scoreBand(answer, highAt, mediumAt);

  const calibrationNote = [
    data.method ? `Method: ${data.method}.` : "",
    isNum(cal.temperature)
      ? `Temperature scaling T = ${fmt(cal.temperature, 2)}${cal.temperature === 1 ? " (uncalibrated default)" : ""}.`
      : "No fitted temperature.",
    isNum(cal.conformal_threshold) ? `Claims below the conformal threshold ${fmt(cal.conformal_threshold, 2)} are marked low.` : "",
    `Bands: high ≥ ${fmt(highAt, 2)}, medium ≥ ${fmt(mediumAt, 2)}.`,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <Card
      icon={<Gauge style={{ width: 15, height: 15 }} />}
      title="Claim support scores"
      meta={meta}
      label="Evidence-support scores per claim"
      collapsible
      defaultOpen={false}
      actions={
        answer != null ? (
          <span className={styles.answerScore} data-tone={bandTone(answerBand)}>
            <span className={styles.answerScoreValue}>{supportScoreText(answer)}</span>
            <span className={styles.answerScoreLabel}>overall support · {answerBand} · not a probability</span>
          </span>
        ) : null
      }
    >
      <p className={styles.note} style={{ marginTop: 0, marginBottom: 8 }}>
        Scores (0–1) estimate how well the retrieved evidence backs each claim; they are not probabilities that a claim is
        true. {calibrationProvenance(cal as Record<string, unknown>)}
      </p>
      {claims.length === 0 ? (
        <p className={styles.muted}>No claims were scored.</p>
      ) : (
        groups.map((g) => (
          <div key={g.band} className={styles.section}>
            <div className={styles.sectionHead}>
              <span className={styles.sectionTitle}>
                {g.band === "unbanded" ? "Not banded" : `${humanize(g.band)} support`}
              </span>
              <span className={styles.meta}>{g.items.length}</span>
            </div>
            <ul className={styles.claimList}>
              {g.items.map((c, i) => (
                <ClaimItem key={c.claim_id || i} claim={c} />
              ))}
            </ul>
          </div>
        ))
      )}
      <Disclosure summary="How claims are scored">
        <p className={styles.note} style={{ marginTop: 0 }}>
          {calibrationNote}
        </p>
        {claims.some((c) => c.features && Object.keys(c.features).length) && (
          <ul className={styles.list}>
            {claims.slice(0, 50).map((c, i) => (
              <li key={c.claim_id || i} className={styles.listItem}>
                <span className={styles.mono}>{c.claim_id || `#${i + 1}`}</span>
                <span className={styles.meta}>{featureText(c.features) || "—"}</span>
              </li>
            ))}
          </ul>
        )}
      </Disclosure>
    </Card>
  );
}
