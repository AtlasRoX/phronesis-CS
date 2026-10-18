"use client";

import React, { useState } from "react";
import { Target } from "lucide-react";
import type { DiseaseTargetsArtifact as DiseaseTargetsData } from "@/types/diseaseTargets";
import { Card, ChipList, Disclosure, ExtLink, Pill, Section, type Tone, styles as ui } from "@/components/agentic/ui";
import styles from "./DiseaseTargetsArtifact.module.css";

export interface DiseaseTargetsArtifactProps {
  data: DiseaseTargetsData;
}

const EVIDENCE_LABEL: Record<string, string> = {
  genetic_association: "genetics",
  somatic_mutation: "somatic",
  known_drug: "drugs",
  affected_pathway: "pathways",
  rna_expression: "expression",
  literature: "literature",
  animal_model: "animal models",
};

const CLASS_LABEL: Record<string, string> = {
  human_genetics: "Human genetics",
  clinical_precedent: "Clinical precedent",
  somatic: "Somatic",
};

const CLASS_TONE: Record<string, Tone> = {
  clinical_precedent: "ok",
  human_genetics: "info",
  somatic: "accent",
};

/** Rows shown per drug tier before "+N more". */
const TIER_LIMIT = 6;

function label(v: string | null | undefined, table: Record<string, string>): string {
  if (!v) return "";
  return table[v] || v.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Evidence types for a target, strongest first, e.g. "genetics 0.81 · drugs 0.62". */
function evidenceText(evidence: Record<string, number> | undefined, limit?: number): string {
  return Object.entries(evidence || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([k, v]) => `${label(k, EVIDENCE_LABEL)} ${v.toFixed(2)}`)
    .join(" · ");
}

/** ChEMBL names arrive in capitals; long all-caps names read better in sentence case. */
function tidyName(name: string): string {
  return name.length > 12 && !/[a-z]/.test(name) && /[A-Z]/.test(name) ? name.charAt(0) + name.slice(1).toLowerCase() : name;
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;
}

/** A tier's rows, the first TIER_LIMIT shown with a "+N more" toggle. */
function TierList<T>({ items, render }: { items: T[]; render: (item: T, i: number) => React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const shown = open ? items : items.slice(0, TIER_LIMIT);
  return (
    <>
      <ul className={styles.tierList}>{shown.map(render)}</ul>
      {items.length > TIER_LIMIT && (
        <button type="button" className={ui.chipMore} onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "show less" : `+${items.length - TIER_LIMIT} more`}
        </button>
      )}
    </>
  );
}

/** Open Targets ranking of the targets associated with a disease, with drugs already tested against them. */
export function DiseaseTargetsArtifact({ data }: DiseaseTargetsArtifactProps) {
  const targets = (data.targets || []).slice(0, 10);
  if (!targets.length) return null;
  const disease = data.disease || {};
  const validated = data.tiers?.validated || [];
  const tested = data.tiers?.clinically_tested || [];
  const shownCandidates = (data.clinical_candidates || []).length;
  const totalCandidates = data.n_clinical_candidates ?? shownCandidates;
  const source = data.source || "Open Targets Platform";
  const nAssociated = typeof data.n_associated_targets === "number" ? data.n_associated_targets : null;

  const meta = [
    `Top: ${targets
      .slice(0, 3)
      .map((t) => `${t.symbol} ${t.overall_score.toFixed(2)}`)
      .join(", ")}`,
    nAssociated != null ? `${targets.length} of ${plural(nAssociated, "target")}` : plural(targets.length, "target"),
    validated.length ? plural(validated.length, "approved drug") : null,
    tested.length ? `${tested.length} clinically tested` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<Target className={styles.icon} style={{ width: 15, height: 15 }} />}
      title={`Targets associated with ${disease.name || "this disease"}`}
      meta={meta}
      label="Disease targets"
      collapsible
      defaultOpen={false}
    >
      <Section
        title="Ranked targets"
        meta={
          disease.id ? (
            <ExtLink href={`https://platform.opentargets.org/disease/${encodeURIComponent(disease.id)}`}>{disease.id}</ExtLink>
          ) : undefined
        }
      >
        <div className={ui.tableScroll}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th scope="col">Target</th>
                <th scope="col" className={ui.num}>
                  Score
                </th>
                <th scope="col">Strongest evidence</th>
                <th scope="col">Drugs tested for this disease</th>
              </tr>
            </thead>
            <tbody>
              {targets.map((t) => {
                const score = Math.max(0, Math.min(1, t.overall_score));
                const drugs = t.drugs_for_disease || [];
                return (
                  <tr key={t.ensembl_id || t.symbol}>
                    <td>
                      <div className={styles.target}>
                        <span className={styles.symbol} title={t.name || undefined}>
                          {t.ensembl_id ? (
                            <ExtLink href={`https://platform.opentargets.org/target/${encodeURIComponent(t.ensembl_id)}`}>{t.symbol}</ExtLink>
                          ) : (
                            t.symbol
                          )}
                        </span>
                        {t.evidence_class ? (
                          <Pill tone={CLASS_TONE[t.evidence_class] || "neutral"}>{label(t.evidence_class, CLASS_LABEL)}</Pill>
                        ) : null}
                      </div>
                    </td>
                    <td className={ui.num}>
                      <span className={styles.score}>
                        <span className={styles.scoreTrack} aria-hidden="true">
                          <span className={styles.scoreFill} style={{ width: `${score * 100}%` }} />
                        </span>
                        {t.overall_score.toFixed(2)}
                      </span>
                    </td>
                    <td className={styles.evidence} title={evidenceText(t.evidence) || undefined}>
                      {evidenceText(t.evidence, 2) || "–"}
                    </td>
                    <td className={styles.drugs}>
                      <ChipList
                        limit={3}
                        empty="–"
                        items={drugs.map((d, i) => ({
                          key: `${d.chembl_id || d.name}-${i}`,
                          label: (
                            <>
                              {tidyName(d.name)}
                              {d.stage ? <span className={styles.stage}> {d.stage}</span> : null}
                            </>
                          ),
                          title: [d.name, d.stage, d.outcome].filter(Boolean).join(" · "),
                        }))}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className={ui.note}>Scores rank strength of evidence, not effect size or causality.</p>
      </Section>

      {validated.length > 0 && (
        <Section title="Approved for this disease" meta={validated.length}>
          <TierList
            items={validated}
            render={(v) => (
              <li key={`${v.drug}-${v.chembl_id}`} className={styles.tierItem} title={v.targets?.length ? `Targets: ${v.targets.join(", ")}` : undefined}>
                <span className={styles.drug} title={v.drug}>
                  {tidyName(v.drug)}
                </span>
                {v.mechanism ? <span className={styles.detail}>{v.mechanism}</span> : null}
              </li>
            )}
          />
        </Section>
      )}

      {tested.length > 0 && (
        <Section title="Clinically tested" meta={tested.length} hint="Phase 2 or later, not approved for this disease">
          <TierList
            items={tested}
            render={(v) => (
              <li
                key={`${v.drug}-${v.chembl_id}-${v.stage}`}
                className={styles.tierItem}
                title={[v.mechanism, v.targets?.length ? `Targets: ${v.targets.join(", ")}` : ""].filter(Boolean).join(" · ") || undefined}
              >
                <span className={styles.drug} title={v.drug}>
                  {tidyName(v.drug)}
                </span>
                {v.stage ? <Pill tone="neutral">{v.stage}</Pill> : null}
                {(v.outcome || v.stop_reason) && (
                  <span className={styles.detail}>
                    {v.outcome}
                    {v.outcome && v.stop_reason ? " · " : ""}
                    {v.stop_reason}
                  </span>
                )}
              </li>
            )}
          />
        </Section>
      )}

      <Disclosure summary="About these data">
        <p className={ui.note} style={{ marginTop: 0 }}>
          Source: {source}
          {disease.id ? ` (${disease.id})` : ""}.{" "}
          {nAssociated != null ? `Top ${targets.length} of ${nAssociated.toLocaleString("en-US")} associated targets. ` : ""}
          Association scores combine genetic, somatic, drug, pathway, expression, literature and animal-model evidence
          into a 0–1 ranking; they are not effect sizes or proof of causality. Clinically tested drugs reached phase 2 or
          later without approval for this disease.
          {totalCandidates > shownCandidates ? ` The backend returned ${shownCandidates} of ${totalCandidates} clinical candidates.` : ""}
        </p>
      </Disclosure>
    </Card>
  );
}
