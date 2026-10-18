"use client";

import React, { useId } from "react";
import { Dna, TriangleAlert } from "lucide-react";
import type {
  CbioStudyPrevalenceTS,
  CivicAssertionTS,
  CivicEvidenceTS,
  ClinVarRecordTS,
  GnomadFrequencyTS,
  VariantIntelArtifact,
  VariantSourceStatus,
} from "@/types/variants";
import { Card, ChipList, Disclosure, Pill, Section, type Tone, styles as ui } from "@/components/agentic/ui";
import { gnomadLabel, variantCardMeta } from "@/lib/variantMeta";
import styles from "./VariantArtifact.module.css";

export interface VariantArtifactProps {
  data: VariantIntelArtifact;
}

/** Fields added by the multi-variant lookup and CIViC disease ranking; optional, so older saved chats still render. */
type CivicEvidenceCtx = CivicEvidenceTS & { matches_context?: boolean | null };
type CivicCtx = NonNullable<VariantIntelArtifact["civic"]> & { context_disease?: string | null; context_matched?: number | null };
type VariantData = VariantIntelArtifact & { additional_variants?: VariantIntelArtifact[] };

const SOURCE_LABEL: Record<VariantSourceStatus["name"], string> = {
  clinvar: "ClinVar",
  civic: "CIViC",
  cbioportal: "cBioPortal",
  gnomad: "gnomAD",
};

const STATUS_WORD: Record<VariantSourceStatus["status"], string> = {
  ok: "",
  no_match: "no match",
  failed: "unavailable",
  skipped: "skipped",
};

/** CIViC evidence levels; C–E are preliminary, so they are drawn faint. */
const LEVEL: Record<string, { tone: Tone; text: string }> = {
  A: { tone: "ok", text: "validated association" },
  B: { tone: "info", text: "clinical evidence" },
  C: { tone: "faint", text: "case study (preliminary)" },
  D: { tone: "faint", text: "preclinical evidence (preliminary)" },
  E: { tone: "faint", text: "inferential association (preliminary)" },
};

function int(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("en-US") : "–";
}

/** Allele frequency with two significant figures ("2.7e-6"). */
function af(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "–";
  return v === 0 ? "0" : v.toExponential(1);
}

/** "TIER_I_LEVEL_A" -> "Tier I, Level A". */
function ampLabel(v: string): string {
  const m = /^TIER_([IV]+)(?:_LEVEL_([A-D]))?$/.exec(v);
  return m ? `Tier ${m[1]}${m[2] ? `, Level ${m[2]}` : ""}` : humanize(v);
}

const ENUM_LABEL: Record<string, string> = {
  SENSITIVITYRESPONSE: "Sensitivity/response",
  REDUCED_SENSITIVITY: "Reduced sensitivity",
  NA: "Not applicable",
};

function humanize(v: string | null | undefined): string {
  if (!v) return "";
  if (ENUM_LABEL[v]) return ENUM_LABEL[v];
  return v
    .toLowerCase()
    .split("_")
    .join(" ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

/** Tone for a ClinVar classification or AMP/ASCO/CAP tier: red for (likely) pathogenic or oncogenic, amber for
 * conflicts, green for benign, blue for actionable tiers (I/II). */
function classTone(v: string): Tone {
  const s = v.toLowerCase();
  if (s.includes("conflicting")) return "warn";
  const tier = /^tier\s+(iv|iii|ii|i)\b/.exec(s);
  if (tier) return tier[1] === "iv" ? "ok" : tier[1] === "iii" ? "neutral" : "info";
  if (s.includes("benign")) return "ok";
  if (/pathogenic|oncogenic/.test(s)) return "bad";
  return "neutral";
}

/** Per-source timings live in the section tooltip, not in the layout. */
function timing(s?: VariantSourceStatus): string | undefined {
  return s && typeof s.elapsed_seconds === "number" ? `${SOURCE_LABEL[s.name]} answered in ${s.elapsed_seconds.toFixed(1)} s` : undefined;
}

function SourceLine({ status }: { status: VariantSourceStatus }) {
  const word = STATUS_WORD[status.status];
  return (
    <p className={status.status === "failed" ? ui.warnNote : ui.note}>
      {SOURCE_LABEL[status.name]} {word}
      {status.detail ? `: ${status.detail}` : ""}
    </p>
  );
}

// ---------------------------------------------------------------------------
// ClinVar
// ---------------------------------------------------------------------------

function ClassRow({ kind, value, review }: { kind: string; value?: string | null; review?: string | null }) {
  if (!value) return null;
  const conflicting = /conflicting/i.test(value);
  return (
    <>
      <dt>{kind}</dt>
      <dd className={styles.classValue}>
        <Pill tone={classTone(value)}>
          {conflicting && <TriangleAlert aria-hidden="true" style={{ width: 12, height: 12 }} />}
          {value}
        </Pill>
        {review && <span className={styles.review}>{review}</span>}
      </dd>
    </>
  );
}

function ClinVarRecord({ r, context }: { r: ClinVarRecordTS; context?: string | null }) {
  const traits = r.somatic_clinical_impact_traits || [];
  // Highlight traits naming the question's cancer type (e.g. "lung adenocarcinoma"); short codes like "nsclc" never match.
  const ctx = context && context.length > 3 ? context.toLowerCase() : null;
  const classified = r.germline_classification || r.oncogenicity_classification || r.somatic_clinical_impact;
  return (
    <div className={styles.record}>
      <div className={styles.recordTitle}>
        <a href={r.url} target="_blank" rel="noopener noreferrer" className={ui.link}>
          {r.accession}
        </a>
        <span className={styles.recordName} title={r.title}>
          {r.title}
        </span>
      </div>
      {(classified || traits.length > 0) && (
        <dl className={styles.classGrid}>
          <ClassRow kind="Germline" value={r.germline_classification} review={r.germline_review_status} />
          <ClassRow kind="Oncogenicity" value={r.oncogenicity_classification} review={r.oncogenicity_review_status} />
          <ClassRow kind="Somatic impact" value={r.somatic_clinical_impact} review={r.somatic_clinical_impact_review_status} />
          {traits.length > 0 && (
            <>
              <dt>Somatic traits</dt>
              <dd>
                <ChipList
                  items={traits.map((t, i) => ({ key: `${t}-${i}`, label: t, title: t, highlight: !!ctx && t.toLowerCase().includes(ctx) }))}
                  limit={4}
                  highlightHint="Matches the question's cancer type"
                />
              </dd>
            </>
          )}
        </dl>
      )}
    </div>
  );
}

function ClinVarSection({ records, status, context }: { records: ClinVarRecordTS[]; status?: VariantSourceStatus; context?: string | null }) {
  if (!records.length) return status ? <SourceLine status={status} /> : null;
  const shown = records.slice(0, 2);
  const meta =
    records.length > shown.length ? `${shown.length} of ${records.length} alleles` : records.length > 1 ? `${records.length} alleles` : undefined;
  return (
    <Section title="ClinVar" meta={meta} hint={timing(status)}>
      {shown.map((r) => (
        <ClinVarRecord key={r.accession} r={r} context={context} />
      ))}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// CIViC
// ---------------------------------------------------------------------------

function LevelPill({ level }: { level: string }) {
  const l = LEVEL[level];
  return (
    <Pill tone={l?.tone ?? "neutral"} title={l ? `Level ${level}: ${l.text}` : undefined}>
      {level}
    </Pill>
  );
}

function EvidenceRow({ ev }: { ev: CivicEvidenceTS }) {
  return (
    <tr>
      <td data-label="EID">
        <a href={ev.url} target="_blank" rel="noopener noreferrer" title={ev.description} className={ui.link}>
          {ev.eid}
        </a>
      </td>
      <td data-label="Level">
        <LevelPill level={ev.evidence_level} />
      </td>
      <td data-label="Type">{humanize(ev.evidence_type)}</td>
      <td data-label="Significance">{humanize(ev.significance)}</td>
      <td data-label="Disease">{ev.disease || "–"}</td>
      <td data-label="Therapy">{ev.therapies?.length ? ev.therapies.join(", ") : "–"}</td>
      <td data-label="PMID">
        {ev.pmid ? (
          <a href={`https://pubmed.ncbi.nlm.nih.gov/${ev.pmid}/`} target="_blank" rel="noopener noreferrer" className={ui.link}>
            {ev.pmid}
          </a>
        ) : (
          ev.citation || "–"
        )}
      </td>
    </tr>
  );
}

function AssertionRow({ a }: { a: CivicAssertionTS }) {
  const amp = a.amp_level ? ampLabel(a.amp_level) : null;
  return (
    <li className={styles.assertion}>
      <a href={a.url} target="_blank" rel="noopener noreferrer" className={ui.link}>
        {a.aid}
      </a>
      {amp && <Pill tone={classTone(amp)}>{amp}</Pill>}
      <span>
        {humanize(a.assertion_type)} · {humanize(a.significance)}
        {a.disease ? ` · ${a.disease}` : ""}
        {a.therapies?.length ? ` · ${a.therapies.join(" + ")}` : ""}
      </span>
      {a.fda_companion_test === true && <Pill tone="ok">FDA companion test</Pill>}
      {a.regulatory_approval === true && <Pill tone="ok">Regulatory approval</Pill>}
      {a.nccn_guideline && <Pill>NCCN: {a.nccn_guideline}</Pill>}
    </li>
  );
}

function CivicSection({ civic: rawCivic, status }: { civic: VariantIntelArtifact["civic"]; status?: VariantSourceStatus }) {
  if (!rawCivic) return status ? <SourceLine status={status} /> : null;
  const civic = rawCivic as CivicCtx;
  const assertions = civic.assertions || [];
  const disease = civic.context_disease || null;
  // Items for the question's disease first (when one was named), then by CIViC rating; the sort is stable.
  const top = [...((civic.evidence || []) as CivicEvidenceCtx[])]
    .sort(
      (a, b) =>
        (disease ? Number(Boolean(b.matches_context)) - Number(Boolean(a.matches_context)) : 0) ||
        (b.evidence_rating ?? 0) - (a.evidence_rating ?? 0),
    )
    .slice(0, 6);
  const total = `${int(civic.accepted_evidence_total)} accepted evidence items`;
  return (
    <Section
      title="CIViC"
      hint={timing(status)}
      meta={
        <>
          {civic.url ? (
            <a href={civic.url} target="_blank" rel="noopener noreferrer" className={ui.link}>
              {total}
            </a>
          ) : (
            total
          )}
          {top.length < civic.accepted_evidence_total
            ? disease
              ? ` · top ${top.length} for ${disease} first, then by rating`
              : ` · top ${top.length} by rating`
            : ""}
        </>
      }
    >
      {/* Assertions are CIViC's curated summaries (AMP tier, approvals), so they lead the individual evidence items. */}
      {assertions.length > 0 && (
        <ul className={styles.assertions} aria-label="Accepted CIViC assertions">
          {assertions.map((a) => (
            <AssertionRow key={a.aid} a={a} />
          ))}
        </ul>
      )}
      {top.length > 0 && (
        <div className={styles.civicTable}>
          <div className={ui.tableScroll}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">EID</th>
                  <th scope="col">Level</th>
                  <th scope="col">Type</th>
                  <th scope="col">Significance</th>
                  <th scope="col">Disease</th>
                  <th scope="col">Therapy</th>
                  <th scope="col">PMID</th>
                </tr>
              </thead>
              <tbody>
                {top.map((ev) => (
                  <EvidenceRow key={ev.eid} ev={ev} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// cBioPortal per-study prevalence bars
// ---------------------------------------------------------------------------

function StudyBar({ st }: { st: CbioStudyPrevalenceTS }) {
  const tipId = useId();
  const pct = st.prevalence_percent;
  const width = pct == null ? 0 : Math.min(100, Math.max(0, pct));
  const label = `${int(st.mutated_samples)} / ${int(st.profiled_samples)} samples${pct != null ? ` (${pct}%)` : ""}`;
  const panels = Object.entries(st.panels || {})
    .map(([k, n]) => `${k} ${int(n)}`)
    .join(", ");
  return (
    <li className={styles.study} tabIndex={0} aria-describedby={tipId}>
      <a href={st.url} target="_blank" rel="noopener noreferrer" className={styles.studyName}>
        {st.study_name}
      </a>
      <span className={styles.track} aria-hidden="true">
        {width > 0 && <span className={styles.fill} style={{ width: `${width}%` }} />}
      </span>
      <span className={styles.count}>
        {int(st.mutated_samples)} / {int(st.profiled_samples)}
      </span>
      <span className={styles.pct}>{pct != null ? `${pct}%` : "–"}</span>
      <div role="tooltip" id={tipId} className={styles.tooltip}>
        <strong>{st.study_name}</strong>
        <span>{label}</span>
        <span>
          {int(st.mutated_patients)} patients · {int(st.gene_mutated_samples)} samples with any call in the gene
        </span>
        <span>Denominator: {st.denominator_basis}</span>
        {panels && <span>Profiled by: {panels}</span>}
        {st.reference_genome && <span>Reference genome: {st.reference_genome}</span>}
      </div>
    </li>
  );
}

function CbioSection({ data, status }: { data: VariantIntelArtifact; status?: VariantSourceStatus }) {
  const studies = data.cbioportal || [];
  if (!studies.length) return status ? <SourceLine status={status} /> : null;
  const scope = data.cancer_filter ? `Per-study prevalence · ${data.cancer_filter}` : `Pan-cancer (${studies[0].study_name})`;
  return (
    <Section title="cBioPortal" meta={scope} hint={timing(status)}>
      <ul className={styles.studies}>
        {studies.map((st) => (
          <StudyBar key={st.study_id} st={st} />
        ))}
      </ul>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// gnomAD
// ---------------------------------------------------------------------------

function GnomadSection({ g, status }: { g?: GnomadFrequencyTS | null; status?: VariantSourceStatus }) {
  if (!g) return status ? <SourceLine status={status} /> : null;
  const parts = [
    g.exome_an != null ? `Exome AC ${int(g.exome_ac)} / AN ${int(g.exome_an)} (AF ${af(g.exome_af)})` : null,
    g.genome_an != null ? `Genome AC ${int(g.genome_ac)} / AN ${int(g.genome_an)}` : null,
    g.faf95_popmax != null ? `FAF95 ${af(g.faf95_popmax)}${g.faf95_popmax_population ? ` (${g.faf95_popmax_population})` : ""}` : null,
  ].filter(Boolean);
  return (
    <Section
      title="gnomAD"
      hint={timing(status)}
      meta={
        <a href={g.url} target="_blank" rel="noopener noreferrer" className={ui.link}>
          {g.variant_id}
        </a>
      }
    >
      <p className={styles.gnomadRow}>{parts.length ? parts.join(" · ") : "No allele counts returned"}</p>
      <p className={ui.note}>Germline population frequency ({gnomadLabel(g.dataset)}), not tumour prevalence.</p>
    </Section>
  );
}

// ---------------------------------------------------------------------------

/** Collapsed header line; see lib/variantMeta (UI-011). */
const cardMeta = variantCardMeta;

/** ClinVar / CIViC / cBioPortal / gnomAD variant cards under a Synapta answer, one per looked-up variant
 * (the first at the top level, the others in additional_variants). Every value comes from the tool result. */
export function VariantArtifact({ data }: VariantArtifactProps) {
  if (!data || !data.gene) return null;
  const extra = ((data as VariantData).additional_variants || []).filter((v) => v && v.gene);
  if (!extra.length) return <VariantCard data={data} />;
  return (
    <>
      {[data, ...extra].map((v, i) => (
        <VariantCard key={`${v.gene}-${v.protein_change || v.hgvs_c || v.rsid || i}`} data={v} />
      ))}
    </>
  );
}

function VariantCard({ data }: VariantArtifactProps) {
  const status = Object.fromEntries((data.sources || []).map((s) => [s.name, s])) as Partial<
    Record<VariantSourceStatus["name"], VariantSourceStatus>
  >;
  const change = data.protein_change ? `p.${data.protein_change}` : data.hgvs_c || data.rsid || "";
  const vcv = data.clinvar?.[0];
  const rsid = data.rsid || vcv?.rsids?.[0];
  const studies = data.cbioportal || [];
  const bases = Array.from(new Set(studies.map((s) => s.denominator_basis)));

  // Identifier links live in the body: a click anywhere in the card header toggles the card.
  const ids: React.ReactNode[] = [];
  if (data.hgvs_p) ids.push(<span key="p">{data.hgvs_p}</span>);
  if (data.gnomad?.variant_id)
    ids.push(
      <a key="g" href={data.gnomad.url} target="_blank" rel="noopener noreferrer">
        GRCh38 {data.gnomad.variant_id}
      </a>,
    );
  if (rsid)
    ids.push(
      <a key="rs" href={`https://www.ncbi.nlm.nih.gov/snp/${rsid}`} target="_blank" rel="noopener noreferrer">
        {rsid}
      </a>,
    );
  if (vcv)
    ids.push(
      <a key="vcv" href={vcv.url} target="_blank" rel="noopener noreferrer">
        {vcv.accession}
      </a>,
    );

  return (
    <Card
      icon={<Dna style={{ width: 15, height: 15 }} />}
      title={`${data.gene} ${change}`}
      meta={cardMeta(data)}
      label={`Variant evidence for ${data.gene} ${change}`}
      collapsible
      defaultOpen={false}
    >
      {ids.length > 0 && (
        <p className={styles.ids}>
          {ids.map((m, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span aria-hidden="true"> · </span>}
              {m}
            </React.Fragment>
          ))}
        </p>
      )}

      <ClinVarSection records={data.clinvar || []} status={status.clinvar} context={data.cancer_filter} />
      <CivicSection civic={data.civic} status={status.civic} />
      <CbioSection data={data} status={status.cbioportal} />
      <GnomadSection g={data.gnomad} status={status.gnomad} />

      {(data.civic || studies.length > 0) && (
        <Disclosure summary="About these data">
          <div className={styles.about}>
            {data.civic && (
              <p className={ui.note}>
                CIViC evidence levels: A validated, B clinical, C case study, D preclinical, E inferential. Levels C–E are
                preliminary.
              </p>
            )}
            {studies.length > 0 && (
              <p className={ui.note}>
                cBioPortal bar length is the share of profiled samples (0–100%). Denominator: {bases.join("; ")}. Studies are
                shown separately because cohorts can overlap.
              </p>
            )}
          </div>
        </Disclosure>
      )}
    </Card>
  );
}
