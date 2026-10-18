"use client";

import React from "react";
import { ExternalLink, Pill as PillIcon, ShieldAlert } from "lucide-react";
import type {
  DrugBioactivity,
  DrugChemblSection,
  DrugIntelArtifact,
  DrugRecord,
  DrugSafety,
  DrugSafetyLabelDetail,
  DrugSafetySkipped,
} from "@/types/drugs";
import { Card, ChipList, Disclosure, Pill, Section, type Tone, styles as ui } from "@/components/agentic/ui";
import styles from "./DrugSafetyArtifact.module.css";

export interface DrugSafetyArtifactProps {
  data: DrugIntelArtifact;
}

type Label = NonNullable<DrugSafety["label"]> & DrugSafetyLabelDetail;

/** Fields newer backends add (drugintel/schemas.py); optional so older artifacts still render. */
type Drug = DrugRecord & { mention?: string | null };

interface BioactiveCompound {
  chembl_id: string;
  pref_name?: string | null;
  max_phase?: number | null;
  molecule_type?: string | null;
  best_pchembl?: number | null;
  standard_type?: string | null;
  standard_relation?: string | null;
  standard_value?: number | null;
  standard_units?: string | null;
  assay_chembl_id?: string | null;
  activity_id?: number | null;
  records?: number;
}

type Chembl = DrugChemblSection & {
  drugs: Drug[];
  target_bioactive_compounds?: BioactiveCompound[];
  target_bioactive_distinct?: number | null;
  target_activity_records_total?: number | null;
  target_activity_records_fetched?: number | null;
  degrader_question?: boolean;
};

type Approval = {
  status: "approved" | "combination_only" | "not_found" | "error";
  application_number?: string | null;
  first_approval_date?: string | null;
  first_approval_application?: string | null;
  efficacy_supplements_total?: number;
  error?: string | null;
  url?: string | null;
};

type Safety = DrugSafety & {
  excluded_terms?: number;
  excluded_reactions?: { term: string; count: number }[];
  deaths_reported?: number | null;
  /** The name FAERS reports were found under (the US name "mesalamine" for "mesalazine"). */
  faers_matched_name?: string | null;
  faers_match_field?: string | null;
  us_name?: string | null;
  /** Drugs@FDA approval status (drugintel/schemas.py FdaApproval). */
  approval?: Approval | null;
};

/** One drug across both sources: its ChEMBL record and its openFDA safety record, when each exists. */
interface DrugRow {
  key: string;
  name: string;
  drug?: Drug;
  safety?: Safety;
}

type Target = NonNullable<DrugChemblSection["target"]>;

const COMPOUND_URL = "https://www.ebi.ac.uk/chembl/explore/compound/";
const ASSAY_URL = "https://www.ebi.ac.uk/chembl/explore/assay/";
const TARGET_URL = "https://www.ebi.ac.uk/chembl/explore/target/";

const SMALL_WORDS = new Set(["of", "in", "and", "the", "to", "with", "by", "for", "on", "at", "or"]);

/** ChEMBL and FAERS store many names in capitals. Sentence-case long ones only when no word could be an
 * acronym or symbol ("ALT INCREASED", "KRAS", "CHEMBL4535757" stay as they are). */
function tidy(name: string): string {
  if (name.length <= 12 || name !== name.toUpperCase() || /^[A-Z]+\d+$/.test(name)) return name;
  const words = name.match(/[A-Za-z]+/g) ?? [];
  if (!words.length || words.some((w) => w.length < 4 && !SMALL_WORDS.has(w.toLowerCase()))) return name;
  const lower = name.toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

/** A name that truncates with an ellipsis; the full original text is the tooltip. */
function Name({ text, className }: { text: string; className?: string }) {
  return (
    <span className={`${styles.truncate} ${className ?? ""}`} title={text}>
      {tidy(text)}
    </span>
  );
}

/** The other name FAERS reports were found under, when it differs from the queried ingredient. */
function faersAlias(s: Safety): string | null {
  const name = s.faers_matched_name;
  return name && name.trim().toLowerCase() !== (s.ingredient || "").trim().toLowerCase() ? name : null;
}

/** "KRAS · GTPase KRas": the symbol the user asked about and ChEMBL's target name, else whichever is known. */
function targetName(t: Target): string {
  const sym = t.symbol_query;
  const pref = t.pref_name;
  if (sym && pref && sym.toLowerCase() !== pref.toLowerCase()) return `${sym} · ${pref}`;
  return pref || sym || t.target_chembl_id;
}

/** The drug's name as the reader knows it: ChEMBL's preferred name, else the user's words, never a bare id.
 * ChEMBL stores names in capitals, so the user's spelling of the same name is preferred when it matches. */
function drugName(d: Drug): string {
  const asked = d.mention || d.query_name;
  if (d.pref_name && asked && asked.toLowerCase() === d.pref_name.toLowerCase()) return asked;
  return d.pref_name || asked;
}

function num(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "–";
  // Shortest exact form: 1, 2.2, 0.013 (values are shown as ChEMBL stores them, never rounded).
  return String(v);
}

function count(v: number | null | undefined): string {
  // Fixed en-US grouping so the card matches the counts quoted in the answer (46,623).
  return typeof v === "number" ? v.toLocaleString("en-US") : "–";
}

function plural(n: number, word: string): string {
  return `${word}${n === 1 ? "" : "s"}`;
}

function phaseText(d: DrugRecord): string {
  if (d.max_phase == null) return d.max_phase_meaning || "no phase recorded";
  return `max phase ${num(d.max_phase)} (${d.max_phase_meaning})`;
}

function labelDate(raw?: string | null): string | null {
  if (!raw) return null;
  return /^\d{8}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}` : raw;
}

/** "< 13 nM": the measured value as ChEMBL stores it, with its relation when not "=". */
function potencyValue(b: { standard_relation?: string | null; standard_value?: number | null; standard_units?: string | null }): string {
  return [b.standard_relation && b.standard_relation !== "=" ? b.standard_relation : null, num(b.standard_value), b.standard_units]
    .filter((x) => x != null && x !== "")
    .join(" ");
}

function bestActivity(d?: Drug): DrugBioactivity | null {
  let best: DrugBioactivity | null = null;
  for (const b of d?.bioactivities ?? []) {
    if (typeof b.pchembl_value === "number" && (!best || b.pchembl_value > (best.pchembl_value as number))) best = b;
  }
  return best;
}

function seriousShare(s: Safety): string | null {
  return s.serious_reports != null && s.total_reports > 0 ? `${Math.round((s.serious_reports / s.total_reports) * 100)}%` : null;
}

function sameDrug(d: Drug, s: Safety): boolean {
  if (s.chembl_id && s.chembl_id === d.chembl_id) return true;
  const ingredient = (s.ingredient || "").toLowerCase();
  return [d.pref_name, d.mention, d.query_name].some((n) => n && n.toLowerCase() === ingredient);
}

/** Pairs each ChEMBL drug with its openFDA record; FDA-only ingredients follow as their own rows. */
function buildRows(drugs: Drug[], safety: Safety[]): DrugRow[] {
  const left = [...safety];
  const rows: DrugRow[] = drugs.map((d) => {
    const i = left.findIndex((s) => sameDrug(d, s));
    return { key: d.chembl_id, name: drugName(d), drug: d, safety: i >= 0 ? left.splice(i, 1)[0] : undefined };
  });
  return rows.concat(left.map((s) => ({ key: `fda:${s.ingredient}`, name: s.ingredient, safety: s })));
}

function approvalYear(a: Approval): string | null {
  return /^(\d{4})/.exec(a.first_approval_date ?? "")?.[1] ?? null;
}

/** Drugs@FDA status as a short pill word plus the full sentence (application, first approval, supplements). */
function approvalStatus(a: Approval, ingredient: string): { tone: Tone; short: string; detail: string } {
  if (a.status === "approved") {
    const first = a.first_approval_date
      ? `first approved ${a.first_approval_date}${a.first_approval_application && a.first_approval_application !== a.application_number ? ` (${a.first_approval_application})` : ""}`
      : null;
    const supps = a.efficacy_supplements_total
      ? `${a.efficacy_supplements_total} efficacy ${plural(a.efficacy_supplements_total, "supplement")}`
      : null;
    return { tone: "ok", short: "FDA-approved", detail: [a.application_number ?? "application unknown", first, supps].filter(Boolean).join(" · ") };
  }
  if (a.status === "combination_only") {
    return { tone: "warn", short: "Combination only", detail: `In Drugs@FDA only within combination products (${a.application_number ?? "application unknown"})` };
  }
  if (a.status === "not_found") {
    return { tone: "neutral", short: "Not in Drugs@FDA", detail: `No approved NDA/BLA for ${ingredient}` };
  }
  return { tone: "faint", short: "Status unknown", detail: `Drugs@FDA lookup failed; FDA approval status unknown${a.error ? ` (${a.error})` : ""}` };
}

function approvedLookupFailed(chembl: DrugChemblSection): boolean {
  return chembl.target_approved_status === "failed";
}

/** The collapsed card's one line: approval, potency, FAERS volume and boxed warnings across the drugs. */
function cardMeta(rows: DrugRow[], chembl: Chembl | undefined, missing: string[]): string {
  const parts: string[] = [];
  const n = rows.length;
  const names = (rs: DrugRow[]) => rs.map((r) => tidy(r.name)).join(", ");

  const approvals = rows.map((r) => r.safety?.approval).filter((a): a is Approval => !!a);
  if (n === 1 && approvals.length === 1) {
    const a = approvals[0];
    const year = approvalYear(a);
    parts.push(a.status === "approved" ? `FDA-approved${year ? ` ${year}` : ""}` : approvalStatus(a, rows[0].name).short);
  } else if (approvals.length) {
    const k = approvals.filter((a) => a.status === "approved").length;
    parts.push(k === approvals.length && k === n ? (n === 2 ? "both FDA-approved" : `all ${n} FDA-approved`) : `${k} of ${approvals.length} FDA-approved`);
  }

  const ranked = rows
    .map((r) => ({ r, b: bestActivity(r.drug) }))
    .filter((x): x is { r: DrugRow; b: DrugBioactivity } => x.b != null)
    .sort((x, y) => (y.b.pchembl_value as number) - (x.b.pchembl_value as number));
  if (ranked.length === 1) {
    const { r, b } = ranked[0];
    parts.push(`${n > 1 ? `${tidy(r.name)} ` : ""}best ${b.standard_type} ${potencyValue(b)} (pChEMBL ${num(b.pchembl_value)})`);
  } else if (ranked.length > 1 && ranked.length <= 3) {
    parts.push(`pChEMBL ${ranked.map(({ r, b }) => `${tidy(r.name)} ${num(b.pchembl_value)}`).join(", ")}`);
  } else if (ranked.length > 3) {
    parts.push(`most potent ${tidy(ranked[0].r.name)} (pChEMBL ${num(ranked[0].b.pchembl_value)})`);
  }

  const faers = rows.filter((r) => r.safety);
  if (faers.length === 1 && n === 1) {
    const s = faers[0].safety as Safety;
    const share = seriousShare(s);
    // UI-010: report counts, not incidence; "serious" is a share of reports, not a risk rate.
    parts.push(`${count(s.total_reports)} FAERS reports${share ? ` (${share} of reports serious)` : ""} — reports, not incidence`);
  } else if (faers.length && faers.length <= 3) {
    parts.push(`FAERS reports (not incidence): ${faers.map((r) => `${tidy(r.name)} ${count(r.safety?.total_reports)}`).join(", ")}`);
  } else if (faers.length) {
    // One report can name several of these drugs, so per-drug counts are not summed.
    parts.push(`FAERS reports for ${faers.length} drugs (not incidence)`);
  }

  const boxed = rows.filter((r) => r.safety?.label?.boxed_warning);
  const labelled = rows.filter((r) => r.safety?.label).length;
  if (boxed.length) parts.push(n === 1 ? "boxed warning" : `boxed warning: ${names(boxed)}`);
  else if (labelled) parts.push(labelled === 1 ? "no boxed warning" : "no boxed warnings");

  const withdrawn = rows.filter((r) => r.drug?.withdrawn_flag);
  if (withdrawn.length) parts.push(n === 1 ? "withdrawn (ChEMBL)" : `withdrawn: ${names(withdrawn)}`);

  if (!n && chembl?.target) {
    if (approvedLookupFailed(chembl)) parts.push("approved drugs unknown (lookup failed)");
    else {
      const approved = chembl.target_approved_total ?? (chembl.target_approved_drugs ?? []).length;
      parts.push(`${count(approved)} approved ${plural(approved, "drug")}`);
    }
    const compounds = chembl.target_bioactive_compounds ?? [];
    if (compounds.length) {
      const distinct = chembl.target_bioactive_distinct ?? compounds.length;
      const total = chembl.target_activity_records_total;
      const exact = total != null && total <= (chembl.target_activity_records_fetched ?? 0);
      parts.push(`${exact ? "" : "at least "}${count(distinct)} bioactive ${plural(distinct, "compound")}`);
    }
  }
  if (missing.length) parts.push(`no ChEMBL match for ${missing.join(", ")}`);
  return parts.join(" · ");
}

function ChemblLink({ id, base = COMPOUND_URL }: { id: string; base?: string }) {
  return (
    <a className={styles.idLink} href={`${base}${encodeURIComponent(id)}`} target="_blank" rel="noopener noreferrer">
      {id}
    </a>
  );
}

function SourceLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a className={styles.extLink} href={href} target="_blank" rel="noopener noreferrer">
      {children} <ExternalLink aria-hidden="true" style={{ width: 12, height: 12 }} />
    </a>
  );
}

/** One row per drug so approval, potency and FAERS volume can be compared down each column. */
function GlanceTable({ rows }: { rows: DrugRow[] }) {
  const best = rows.map((r) => bestActivity(r.drug));
  const top = Math.max(...best.map((b) => b?.pchembl_value ?? -Infinity));
  const hasPotency = Number.isFinite(top);
  const hasFda = rows.some((r) => r.safety);
  return (
    <div className={styles.scroll}>
      <table className={ui.table}>
        <caption className={styles.srOnly}>Side-by-side summary of each drug</caption>
        <thead>
          <tr>
            <th scope="col">Drug</th>
            <th scope="col">FDA status</th>
            {hasPotency && (
              <>
                <th scope="col">Best potency</th>
                <th scope="col" className={ui.num}>pChEMBL</th>
              </>
            )}
            {hasFda && (
              <>
                <th scope="col" className={ui.num}>FAERS reports</th>
                <th scope="col" className={ui.num}>Serious</th>
                <th scope="col">Boxed warning</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const b = best[i];
            const s = r.safety;
            const status = s?.approval ? approvalStatus(s.approval, s.ingredient) : null;
            const year = s?.approval?.status === "approved" ? approvalYear(s.approval) : null;
            const share = s ? seriousShare(s) : null;
            return (
              <tr key={r.key}>
                <td>
                  <span className={styles.head}>
                    <Name text={r.name} className={`${styles.name} ${styles.cellName}`} />
                    {r.drug?.withdrawn_flag && <Pill tone="bad">Withdrawn</Pill>}
                  </span>
                </td>
                <td className={styles.nowrap}>
                  {status ? (
                    <>
                      <Pill tone={status.tone} title={status.detail}>
                        {status.short}
                      </Pill>
                      {year && <span className={styles.faint}> {year}</span>}
                    </>
                  ) : r.drug?.max_phase != null ? (
                    <span className={styles.faint}>ChEMBL max phase {num(r.drug.max_phase)}</span>
                  ) : (
                    "–"
                  )}
                </td>
                {hasPotency && (
                  <>
                    <td className={styles.nowrap}>{b ? `${b.standard_type} ${potencyValue(b)}` : "–"}</td>
                    <td className={`${ui.num} ${b && b.pchembl_value === top ? styles.best : ""}`}>{num(b?.pchembl_value)}</td>
                  </>
                )}
                {hasFda && (
                  <>
                    <td className={ui.num}>{s ? count(s.total_reports) : "–"}</td>
                    <td className={ui.num}>
                      {s?.serious_reports != null ? count(s.serious_reports) : "–"}
                      {share && <span className={styles.faint}> {share}</span>}
                    </td>
                    <td>
                      {s?.label?.boxed_warning ? (
                        <Pill tone="warn">Yes</Pill>
                      ) : s?.label ? (
                        <span className={styles.faint}>None</span>
                      ) : s ? (
                        <span className={styles.faint}>{s.label_error ? "Unknown" : "No label"}</span>
                      ) : (
                        "–"
                      )}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** ChEMBL identity, development stage and mechanisms of one drug. */
function ProfileItem({ drug, showName }: { drug: Drug; showName: boolean }) {
  const name = drugName(drug);
  const mechanisms = drug.mechanisms ?? [];
  const asked = drug.mention || drug.query_name;
  const matchedOther = asked && drug.pref_name && asked.toLowerCase() !== drug.pref_name.toLowerCase() && asked !== drug.chembl_id;
  return (
    <li className={styles.item}>
      <div className={styles.head}>
        {showName && <Name text={name} className={styles.name} />}
        <ChemblLink id={drug.chembl_id} />
        {drug.withdrawn_flag && <Pill tone="bad">Withdrawn (ChEMBL)</Pill>}
        {drug.chembl_black_box_flag === 1 && <Pill tone="warn">ChEMBL black-box flag</Pill>}
      </div>
      <div className={ui.meta}>
        {[drug.molecule_type, phaseText(drug), drug.first_approval ? `first approval ${drug.first_approval}` : null]
          .filter(Boolean)
          .join(" · ")}
        {matchedOther && ` · matched “${asked}”${drug.matched_via === "synonym" ? " as a synonym" : ""}`}
      </div>
      {mechanisms.length > 0 && (
        <ul className={styles.mechList} aria-label={`Mechanisms of action for ${name}`}>
          {mechanisms.map((m, i) => (
            <li key={`${m.target_chembl_id}-${i}`} className={styles.mech}>
              {/* ChEMBL action types are a fixed vocabulary in capitals ("INHIBITOR"); sentence case is safe. */}
              <Pill>{m.action_type ? m.action_type.charAt(0) + m.action_type.slice(1).toLowerCase() : "Mechanism"}</Pill>
              <span className={styles.mechText}>{m.mechanism_of_action}</span>
              {m.target_chembl_id && (
                <span className={styles.faint}>
                  {m.target_pref_name ? `${m.target_pref_name} · ` : ""}
                  <ChemblLink id={m.target_chembl_id} base={TARGET_URL} />
                </span>
              )}
              {m.form_chembl_id && m.form_chembl_id !== drug.chembl_id && (
                <span className={styles.faint}>recorded on salt form {m.form_chembl_id}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** IC50/Ki/Kd records against the question's target, grouped by drug in one table so drugs line up. */
function PotencySection({ chembl, drugs }: { chembl: Chembl; drugs: Drug[] }) {
  const target = chembl.target;
  if (!target || !drugs.length) return null;
  const measured = drugs.filter((d) => (d.bioactivities ?? []).length);
  const unmeasured = drugs.filter((d) => !(d.bioactivities ?? []).length);
  const multi = drugs.length > 1;
  const targetText = `${target.pref_name || target.target_chembl_id}${target.symbol_query ? ` (${target.symbol_query})` : ""}`;
  const shownOf = (d: Drug) => {
    const shown = (d.bioactivities ?? []).length;
    const total = d.bioactivity_total ?? shown;
    return `${shown} of ${count(total)} ${plural(total, "record")}`;
  };
  return (
    <Section
      title={`Potency vs ${targetText}`}
      meta={!multi && measured.length ? shownOf(measured[0]) : undefined}
      hint={`ChEMBL IC50, Ki and Kd records with a pChEMBL value, highest pChEMBL first${chembl.chembl_release ? ` · ${chembl.chembl_release}` : ""}`}
    >
      {measured.length > 0 && (
        <div className={styles.scroll}>
          <table className={ui.table}>
            <caption className={styles.srOnly}>ChEMBL IC50, Ki and Kd records</caption>
            <thead>
              <tr>
                {multi && <th scope="col">Drug</th>}
                <th scope="col">Type</th>
                <th scope="col" className={ui.num}>Value</th>
                <th scope="col" className={ui.num}>pChEMBL</th>
                <th scope="col">Assay</th>
              </tr>
            </thead>
            <tbody>
              {measured.map((d) =>
                d.bioactivities.map((b, i) => (
                  <tr key={`${d.chembl_id}-${b.activity_id}`}>
                    {multi && i === 0 && (
                      <td rowSpan={d.bioactivities.length} className={styles.groupCell}>
                        <Name text={drugName(d)} className={`${styles.name} ${styles.cellName}`} />
                        <div className={styles.faint}>{shownOf(d)}</div>
                      </td>
                    )}
                    <td>{b.standard_type}</td>
                    <td className={ui.num}>{potencyValue(b)}</td>
                    <td className={ui.num}>{num(b.pchembl_value)}</td>
                    <td className={styles.assayCell}>
                      <ChemblLink id={b.assay_chembl_id} base={ASSAY_URL} />
                      {b.data_validity_comment && (
                        <>
                          {" "}
                          <Pill tone="warn" title="ChEMBL data validity comment">
                            {b.data_validity_comment}
                          </Pill>
                        </>
                      )}
                      {b.assay_description && (
                        <div className={styles.clamp2} title={b.assay_description}>
                          {b.assay_description}
                        </div>
                      )}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
      {unmeasured.length > 0 && (
        <p className={ui.note}>
          No IC50/Ki/Kd records with a pChEMBL value against {target.pref_name || target.target_chembl_id}
          {multi ? ` for ${unmeasured.map((d) => tidy(drugName(d))).join(", ")}` : ""}.
        </p>
      )}
    </Section>
  );
}

/** Approved drugs acting on the target, as chips; drugs from the question are highlighted. */
function ApprovedForTarget({ chembl, asked }: { chembl: Chembl; asked: Set<string> }) {
  const rows = chembl.target_approved_drugs ?? [];
  const target = chembl.target;
  if (!target || (!rows.length && !approvedLookupFailed(chembl))) return null;
  const total = chembl.target_approved_total;
  return (
    <Section
      title={`Approved drugs acting on ${targetName(target)}`}
      meta={
        <>
          {rows.length ? (total != null ? `${rows.length} of ${count(total)} · ` : `${rows.length} (total not determined) · `) : ""}
          <ChemblLink id={target.target_chembl_id} base={TARGET_URL} />
        </>
      }
      hint={`Max phase 4 = approved by at least one regulator${chembl.chembl_release ? ` · ${chembl.chembl_release}` : ""}`}
    >
      {approvedLookupFailed(chembl) ? (
        <p className={ui.muted}>
          The ChEMBL lookup of approved drugs acting on {targetName(target)}{" "}
          {chembl.target_approved_error_kind === "timeout" ? "timed out" : "failed"}, so they are unknown here (not evidence that none exist).
        </p>
      ) : (
        <ChipList
          limit={12}
          extra={total != null ? Math.max(0, total - rows.length) : 0}
          highlightHint="One of the drugs in this question"
          items={rows.map((a) => ({
            key: a.chembl_id,
            label: a.pref_name ? tidy(a.pref_name) : a.chembl_id,
            href: `${COMPOUND_URL}${encodeURIComponent(a.chembl_id)}`,
            title: [
              a.pref_name ?? "name not retrieved from ChEMBL",
              a.chembl_id,
              a.molecule_type,
              a.first_approval ? `first approval ${a.first_approval}` : null,
              a.mechanism_of_action,
            ]
              .filter(Boolean)
              .join(" · "),
            highlight: asked.has(a.chembl_id),
          }))}
        />
      )}
    </Section>
  );
}

/** Research compounds with measured activity, shown only when the question named a target but no drug. */
function BioactiveForTarget({ chembl }: { chembl: Chembl }) {
  const rows = chembl.target_bioactive_compounds ?? [];
  const target = chembl.target;
  if (!target || chembl.target_activity_records_fetched == null || (chembl.drugs ?? []).length) return null;
  const label = target.symbol_query || target.pref_name || target.target_chembl_id;
  const total = chembl.target_activity_records_total;
  const fetched = chembl.target_activity_records_fetched;
  const distinct = chembl.target_bioactive_distinct ?? rows.length;
  const exact = total != null && total <= fetched;
  if (!rows.length) {
    return <p className={ui.note}>ChEMBL lists no compound with a measured IC50/Ki/Kd/EC50 (pChEMBL) value against {label}.</p>;
  }
  return (
    <Section
      title={`Bioactive compounds vs ${label}`}
      meta={
        <>
          {exact ? `${count(distinct)} ${plural(distinct, "compound")}` : `at least ${count(distinct)} compounds`} ·{" "}
          <ChemblLink id={target.target_chembl_id} base={TARGET_URL} />
        </>
      }
      hint={exact ? undefined : `Top ${count(fetched)} of ${count(total)} activity records`}
    >
      {(chembl.target_approved_drugs ?? []).length === 0 && !approvedLookupFailed(chembl) && (
        <p className={styles.lead}>No approved drug acts on {label} in ChEMBL (max phase 4 mechanisms); these compounds have measured activity only.</p>
      )}
      <div className={styles.scroll}>
        <table className={ui.table}>
          <caption className={styles.srOnly}>Compounds with measured activity against {label} in ChEMBL</caption>
          <thead>
            <tr>
              <th scope="col">Compound</th>
              <th scope="col" className={ui.num}>Max phase</th>
              <th scope="col">Best record</th>
              <th scope="col" className={ui.num}>pChEMBL</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.chembl_id}>
                <td>
                  {c.pref_name ? (
                    <div>
                      <Name text={c.pref_name} className={styles.cellName} />
                    </div>
                  ) : null}
                  <ChemblLink id={c.chembl_id} />
                </td>
                <td className={ui.num}>{c.max_phase == null ? "–" : num(c.max_phase)}</td>
                <td className={styles.nowrap}>
                  {[c.standard_type, potencyValue(c)].filter(Boolean).join(" ")}
                  {c.assay_chembl_id ? (
                    <div>
                      <ChemblLink id={c.assay_chembl_id} base={ASSAY_URL} />
                    </div>
                  ) : null}
                </td>
                <td className={ui.num}>{num(c.best_pchembl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {chembl.degrader_question && (
        <p className={ui.note}>ChEMBL does not label compounds as PROTACs or degraders; these are listed by measured activity only.</p>
      )}
    </Section>
  );
}

function FaersBars({ safety }: { safety: Safety }) {
  const rows = safety.top_reactions ?? [];
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <table className={styles.bars}>
      <caption className={styles.srOnly}>Most reported reactions in FAERS reports mentioning {safety.ingredient}</caption>
      <thead className={styles.srOnly}>
        <tr>
          <th scope="col">Reaction (MedDRA preferred term)</th>
          <th scope="col">Reports</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.term} title={`${r.term}: ${count(r.count)} reports`}>
            <th scope="row" className={styles.barTerm}>
              <span className={styles.truncate}>{tidy(r.term)}</span>
            </th>
            <td>
              <div className={styles.barCell}>
                <span className={styles.barTrack}>
                  <span className={styles.bar} style={{ width: `${Math.max(1.5, (r.count / max) * 100)}%` }} />
                </span>
                <span className={styles.barValue}>{count(r.count)}</span>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FaersBlock({ row, showName }: { row: DrugRow; showName: boolean }) {
  const s = row.safety as Safety;
  const alias = faersAlias(s);
  const share = seriousShare(s);
  return (
    <div className={styles.faersBlock}>
      {showName && <Name text={row.name} className={styles.name} />}
      <div className={styles.stats}>
        <span>
          <strong>{count(s.total_reports)}</strong> reports
          {alias && <span title={`FAERS reports were found under the US name ${alias}`}> as {alias}</span>}
        </span>
        {s.serious_reports != null && (
          <span>
            <strong>{count(s.serious_reports)}</strong> serious{share && <span className={styles.faint}> ({share})</span>}
          </span>
        )}
        {s.deaths_reported != null && (
          <span title="Death is recorded as a report outcome, not as a reaction">
            <strong>{count(s.deaths_reported)}</strong> death outcomes
          </span>
        )}
      </div>
      {(s.top_reactions ?? []).length > 0 ? (
        <FaersBars safety={s} />
      ) : (
        <p className={ui.muted}>FAERS returned no reports for this search{s.us_name ? ` (also searched as ${s.us_name})` : ""}.</p>
      )}
    </div>
  );
}

/** FAERS volume and top reactions per drug; several drugs sit side by side for comparison. */
function FaersSection({ rows }: { rows: DrugRow[] }) {
  const items = rows.filter((r) => r.safety);
  if (!items.length) return null;
  const multi = items.length > 1;
  const updated = [...new Set(items.map((r) => r.safety?.last_updated).filter(Boolean))];
  return (
    <Section
      title="Adverse event reports (FAERS)"
      meta={updated.length === 1 ? `data to ${updated[0]}` : undefined}
      caveat="Spontaneous reports that mention each drug: they show what is reported, not how often it happens. Counts and the serious share cannot be compared between drugs as risk rates, and one report can mention several drugs."
    >
      <div className={multi ? styles.faersGrid : undefined}>
        {items.map((r) => (
          <FaersBlock key={r.key} row={r} showName={multi} />
        ))}
      </div>
    </Section>
  );
}

function ApprovalLine({ safety }: { safety: Safety }) {
  const a = safety.approval;
  if (!a) return null;
  const status = approvalStatus(a, safety.ingredient);
  return (
    <div className={styles.line}>
      <Pill tone={status.tone}>{status.short}</Pill>
      <span>{status.detail}</span>
      {a.url ? <SourceLink href={a.url}>Drugs@FDA</SourceLink> : null}
    </div>
  );
}

function LabelBlock({ label, ingredient }: { label: Label; ingredient: string }) {
  const product = [label.brand_name, label.application_number, label.manufacturer_name, labelDate(label.effective_time) ? `effective ${labelDate(label.effective_time)}` : null]
    .filter(Boolean)
    .join(" · ");
  const anyTruncated = label.warnings_truncated || label.contraindications_truncated;
  return (
    <>
      <div className={styles.line}>
        <span>{label.harmonized && product ? `Label: ${product}` : `Label for ${ingredient} · brand/application not harmonized by openFDA`}</span>
        <SourceLink href={label.dailymed_url}>DailyMed</SourceLink>
      </div>
      {label.combination_product && (
        <p className={ui.warnNote} role="note">
          Combination product{label.generic_name ? `: ${label.generic_name}` : ""}. No single-ingredient label for {ingredient} was found; these warnings may come from the other ingredients.
        </p>
      )}
      {label.boxed_warning ? (
        <div className={styles.boxed} role="note" aria-label="Boxed warning">
          <div className={styles.boxedHead}>
            <ShieldAlert aria-hidden="true" style={{ width: 14, height: 14 }} /> Boxed warning
            {label.boxed_warning_truncated && <span className={styles.faint}> · excerpt, truncated</span>}
          </div>
          <p className={styles.boxedText}>{label.boxed_warning}</p>
        </div>
      ) : (
        <p className={ui.note}>No boxed warning in this label.</p>
      )}
      {(label.warnings || label.contraindications) && (
        <Disclosure
          summary={
            <>
              {label.warnings_section === "warnings" ? "Warnings" : "Warnings and precautions"}
              {label.contraindications ? " · contraindications" : ""}
              {anyTruncated ? " (excerpt, truncated)" : " (excerpt)"}
            </>
          }
        >
          {label.warnings && (
            <p className={styles.excerpt}>
              {label.warnings}
              {label.warnings_truncated ? " …" : ""}
            </p>
          )}
          {label.contraindications && (
            <p className={styles.excerpt}>
              {label.contraindications}
              {label.contraindications_truncated ? " …" : ""}
            </p>
          )}
        </Disclosure>
      )}
    </>
  );
}

/** Drugs@FDA status and the FDA label (boxed warning first) per drug. */
function FdaSection({ rows, skipped }: { rows: DrugRow[]; skipped: DrugSafetySkipped[] }) {
  const items = rows.filter((r) => r.safety);
  if (!items.length && !skipped.length) return null;
  return (
    <Section title="FDA approval and label">
      {items.length > 0 && (
        <ul className={styles.items}>
          {items.map((r) => {
            const s = r.safety as Safety;
            const label = (s.label ?? null) as Label | null;
            return (
              <li key={r.key} className={styles.item}>
                {items.length > 1 && <Name text={r.name} className={styles.name} />}
                <ApprovalLine safety={s} />
                {label ? (
                  <LabelBlock label={label} ingredient={s.ingredient} />
                ) : s.label_error ? (
                  <p className={ui.note}>FDA label lookup failed for {s.ingredient}, so whether a label exists could not be checked ({s.label_error}).</p>
                ) : (
                  <p className={ui.note}>No FDA label found for {s.ingredient}.</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {skipped.map((s) => (
        <p key={`${s.drug}-${s.reason}`} className={ui.note}>
          FDA data not queried for {s.drug}: {s.reason}.
        </p>
      ))}
    </Section>
  );
}

/** Sources, methods, FAERS caveats and search provenance, out of the way of the results. */
function AboutData({ chembl, rows, hasPotency }: { chembl?: Chembl; rows: DrugRow[]; hasPotency: boolean }) {
  const safety = rows.filter((r) => r.safety);
  // openFDA repeats the same caveats and disclaimer for every drug; say them once.
  const caveats = [...new Set(safety.flatMap((r) => r.safety?.caveats ?? []))];
  const disclaimers = [...new Set(safety.map((r) => r.safety?.disclaimer).filter(Boolean))];
  const compounds = chembl?.target_bioactive_compounds ?? [];
  return (
    <Disclosure summary="About these data">
      <div className={styles.about}>
        <p>
          Sources:{" "}
          {[
            chembl ? `ChEMBL${chembl.chembl_release ? ` ${chembl.chembl_release.replace(/^ChEMBL_/, "")}` : ""} (mechanisms, potency, approved drugs)` : null,
            safety.length ? "openFDA (Drugs@FDA, FAERS, drug labels)" : null,
          ]
            .filter(Boolean)
            .join(" · ")}
          .
        </p>
        {hasPotency && <p>Potency rows are ChEMBL IC50, Ki and Kd records with a pChEMBL value (−log₁₀ molar; higher is more potent), highest first.</p>}
        {(chembl?.target_approved_drugs ?? []).length > 0 && <p>Approved drugs on the target are ChEMBL max phase 4: approved by at least one regulator.</p>}
        {compounds.length > 0 && !(chembl?.drugs ?? []).length && (
          <p>Bioactive compounds are research compounds ranked by measured activity (highest pChEMBL first), not approved drugs.</p>
        )}
        {caveats.map((c) => (
          <p key={c}>{c}</p>
        ))}
        {disclaimers.map((d) => (
          <p key={d}>openFDA: {d}</p>
        ))}
        {safety.map((r) => {
          const s = r.safety as Safety;
          const excluded = s.excluded_terms ?? (s.excluded_reactions ?? []).length;
          const examples = (s.excluded_reactions ?? [])
            .filter((x) => x.term.toUpperCase() !== "DEATH")
            .slice(0, 2)
            .map((x) => x.term.toLowerCase());
          return (
            <p key={r.key}>
              <strong className={styles.aboutName}>{tidy(r.name)}:</strong>{" "}
              {faersAlias(s) ? `FAERS reports found under the US name ${faersAlias(s)}. ` : ""}
              {excluded > 0
                ? `Excluded ${excluded} non-reaction ${plural(excluded, "term")} (e.g. ${examples.length ? examples.join(", ") : "off-label use, disease progression"}). `
                : ""}
              {s.deaths_reported != null ? "Death counts are report outcomes, not reactions. " : ""}
              {s.last_updated ? `Data last updated ${s.last_updated} · ` : ""}search <code>{s.faers_search}</code>
            </p>
          );
        })}
      </div>
    </Disclosure>
  );
}

/** ChEMBL drug profile and openFDA safety card. */
export function DrugSafetyArtifact({ data }: DrugSafetyArtifactProps) {
  const chembl = data.chembl as Chembl | undefined;
  const safety: Safety[] = Array.isArray(data.safety) ? data.safety : data.safety ? [data.safety] : [];
  const skipped: DrugSafetySkipped[] = data.safety_skipped ?? [];
  const drugs = (chembl?.drugs ?? []).filter((d) => d && d.chembl_id);
  const missing = [...(chembl?.unresolved_names ?? []), ...(chembl?.invalid_chembl_ids ?? [])];
  const compounds = chembl?.target_bioactive_compounds ?? [];
  if (
    !drugs.length &&
    !safety.length &&
    !(chembl?.target_approved_drugs ?? []).length &&
    !(chembl && approvedLookupFailed(chembl)) &&
    !compounds.length &&
    !missing.length &&
    !skipped.length
  ) {
    return null;
  }
  const rows = buildRows(drugs, safety);
  const target = chembl?.target;
  const targetLabel = target ? target.symbol_query || target.pref_name || target.target_chembl_id : null;
  const title = rows.length
    ? rows.map((r) => tidy(r.name)).join(" · ")
    : targetLabel
      ? `Drugs and compounds acting on ${targetLabel}`
      : "Drug evidence";
  const sources = [chembl ? `ChEMBL${chembl.chembl_release ? ` ${chembl.chembl_release.replace(/^ChEMBL_/, "")}` : ""}` : null, safety.length ? "openFDA" : null]
    .filter(Boolean)
    .join(" · ");
  const meta = cardMeta(rows, chembl, missing) || sources;
  const hasPotency = !!target && drugs.some((d) => (d.bioactivities ?? []).length);
  const missingNote = missing.length > 0 && <p className={ui.note}>No exact ChEMBL match for {missing.join(", ")}.</p>;

  return (
    <Card
      icon={<PillIcon style={{ width: 15, height: 15 }} />}
      title={title}
      meta={meta}
      label="Drug and safety evidence"
      collapsible
      defaultOpen={false}
    >
      <div className={styles.body}>
        {rows.length > 1 && (
          <Section title="At a glance">
            <GlanceTable rows={rows} />
          </Section>
        )}
        {drugs.length > 0 ? (
          <Section title="Profile and mechanism">
            <ul className={styles.items}>
              {drugs.map((d) => (
                <ProfileItem key={d.chembl_id} drug={d} showName={drugs.length > 1} />
              ))}
            </ul>
            {missingNote}
          </Section>
        ) : (
          missingNote
        )}
        {chembl && <PotencySection chembl={chembl} drugs={drugs} />}
        <FaersSection rows={rows} />
        <FdaSection rows={rows} skipped={skipped} />
        {chembl && <ApprovedForTarget chembl={chembl} asked={new Set(drugs.map((d) => d.chembl_id))} />}
        {chembl && <BioactiveForTarget chembl={chembl} />}
        {(chembl || safety.length > 0) && <AboutData chembl={chembl} rows={rows} hasPotency={hasPotency} />}
      </div>
    </Card>
  );
}
