"use client";

import React from "react";
import { Dna } from "lucide-react";
import type { OmicsIntelArtifact, OmicsSection } from "@/types/agentic";
import { SOURCE_NAMES } from "./evidenceLabels";
import { Bar, Card, ChipList, Disclosure, ExtLink, GenericTable, Pill, Section, Stats, arr, fmt, humanize, isNum, pick, str, styles } from "./ui";
import { gwasEffect } from "@/lib/gwasEffect";

const MAX = 15;
/** Uncertainty / significance columns of unknown sources are shown first, never cut by the column cap (UI-022). */
const SIGNIFICANCE_COLUMNS = ["p_value", "pvalue", "fdr", "q_value", "padj", "confidence", "score"];

/** Rows from `items`, or the first array found under one of `keys`. */
function rowsOf(section: OmicsSection | undefined, ...keys: string[]): Record<string, any>[] {
  if (!section || typeof section !== "object") return [];
  for (const k of ["items", ...keys]) {
    const v = (section as any)[k];
    if (Array.isArray(v)) return v.filter((r) => r && typeof r === "object");
  }
  return [];
}

function num(v: unknown): number | null {
  if (isNum(v)) return v;
  if (typeof v === "string" && v.trim() && Number.isFinite(Number(v))) return Number(v);
  return null;
}

function plural(n: number, word: string, many = `${word}s`): string {
  return `${n} ${n === 1 ? word : many}`;
}

// Parsed rows, shared by each section and the card's collapsed summary.

function gwasRows(s: OmicsSection) {
  return rowsOf(s, "associations").map((r) => ({
    raw: r,
    trait: str(pick(r, "trait", "efo_trait", "reported_trait", "disease_trait", "traitName")),
    p: num(pick(r, "p_value", "pvalue", "p", "pValue")),
  }));
}

function gtexRows(s: OmicsSection) {
  return rowsOf(s, "tissues", "expression")
    .map((r) => ({
      tissue: str(pick(r, "tissue", "tissue_site_detail", "tissueSiteDetail", "tissueSiteDetailId", "name")),
      value: num(pick(r, "median_tpm", "median", "tpm", "value")),
    }))
    .filter((r): r is { tissue: string; value: number } => Boolean(r.tissue) && r.value != null)
    .sort((a, b) => b.value - a.value);
}

function stringRows(s: OmicsSection) {
  return rowsOf(s, "partners", "interactions")
    .map((r) => ({
      name: str(pick(r, "partner", "preferred_name", "preferredName_B", "name", "gene", "stringId_B")),
      score: num(pick(r, "score", "combined_score")),
    }))
    .filter((r) => r.name)
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
}

function druggablePartners(s: OmicsSection): any[] {
  return arr((s as any).druggable_partners).filter((r: any) => r && typeof r === "object" && str(r.partner));
}

function Gwas({ s }: { s: OmicsSection }) {
  const rows = rowsOf(s, "associations").slice(0, MAX);
  if (!rows.length) return <p className={styles.muted}>No associations returned.</p>;
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Trait</th>
            <th scope="col">Variant</th>
            <th scope="col" className={styles.num}>
              p-value
            </th>
            <th scope="col" className={styles.num} title="Odds ratio (OR, no effect = 1) or beta (β, no effect = 0, in trait units); each value names its measure">
              Effect (OR or β)
            </th>
            <th scope="col">Study</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const pmid = pick(r, "pmid", "pubmed_id", "pubmedId");
            const acc = pick(r, "accession_id", "study_accession", "accessionId", "study");
            const p = num(pick(r, "p_value", "pvalue", "p", "pValue"));
            const eff = gwasEffect(r);
            return (
              <tr key={i}>
                <td className={styles.wrapCell}>{str(pick(r, "trait", "efo_trait", "reported_trait", "disease_trait", "traitName")) || "—"}</td>
                <td className={styles.mono}>
                  {str(pick(r, "rsid", "variant", "snp", "risk_allele", "strongest_risk_allele", "variant_id")) || "—"}
                  {r.risk_allele && r.rsid && r.risk_allele !== r.rsid ? ` (${str(r.risk_allele)})` : ""}
                </td>
                <td className={styles.num}>{p != null ? p.toExponential(1) : "—"}</td>
                <td className={styles.num}>{eff ? eff.text : "—"}</td>
                <td className={styles.mono}>
                  {pmid ? <ExtLink href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(str(pmid))}/`}>PMID {str(pmid)}</ExtLink> : acc ? <ExtLink href={`https://www.ebi.ac.uk/gwas/studies/${encodeURIComponent(str(acc))}`}>{str(acc)}</ExtLink> : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Gtex({ s }: { s: OmicsSection }) {
  const rows = gtexRows(s).slice(0, 10);
  if (!rows.length) return <p className={styles.muted}>No expression values returned.</p>;
  const max = Math.max(...rows.map((r) => r.value), 1e-9);
  return (
    <div className={styles.thinBars}>
      {rows.map((r) => (
        <Bar key={r.tissue} label={humanize(r.tissue)} value={r.value} max={max} display={`${fmt(r.value, 1)} TPM`} title={`${r.tissue}: median ${fmt(r.value, 2)} TPM`} />
      ))}
    </div>
  );
}

function StringDb({ s }: { s: OmicsSection }) {
  const rows = stringRows(s).slice(0, MAX);
  if (!rows.length) return <p className={styles.muted}>No interaction partners returned.</p>;
  return (
    <ChipList
      items={rows.map((r) => ({ key: r.name, label: r.name, title: r.score != null ? `STRING combined score ${fmt(r.score, 3)}` : undefined }))}
      limit={MAX}
    />
  );
}

function Reactome({ s }: { s: OmicsSection }) {
  const rows = rowsOf(s, "pathways").slice(0, MAX);
  if (!rows.length) return <p className={styles.muted}>No pathways returned.</p>;
  return (
    <ChipList
      items={rows.map((r, i) => {
        const id = str(pick(r, "stId", "st_id", "id", "dbId"));
        const species = str(pick(r, "species"));
        return {
          key: id || String(i),
          label: str(pick(r, "name", "displayName", "title")) || id || "pathway",
          href: pick(r, "url") || (id ? `https://reactome.org/content/detail/${encodeURIComponent(id)}` : null),
          title: [id, species].filter(Boolean).join(" · ") || undefined,
        };
      })}
      limit={10}
    />
  );
}

const PUBCHEM_KEYS = ["CID", "cid", "name", "IUPACName", "MolecularFormula", "MolecularWeight", "XLogP", "TPSA", "HBondDonorCount", "HBondAcceptorCount", "ConnectivitySMILES", "CanonicalSMILES", "IsomericSMILES", "SMILES", "InChIKey"];

function pubchemProps(s: OmicsSection): Record<string, any>[] {
  const rows = rowsOf(s, "compounds");
  return rows.length ? rows : s.properties && typeof s.properties === "object" ? [s.properties] : [];
}

function PubChem({ s }: { s: OmicsSection }) {
  const props = pubchemProps(s);
  if (!props.length) return <p className={styles.muted}>No compound properties returned.</p>;
  return (
    <div>
      {props.slice(0, 5).map((p, i) => {
        const cid = pick(p, "CID", "cid");
        return (
          <dl key={i} className={styles.kvList}>
            {PUBCHEM_KEYS.filter((k) => p[k] != null && p[k] !== "").map((k) => (
              <React.Fragment key={k}>
                <dt>{k === "cid" ? "CID" : k}</dt>
                <dd className={/SMILES|InChI|Formula/.test(k) ? styles.mono : undefined}>
                  {(k === "CID" || k === "cid") && cid ? (
                    <ExtLink href={`https://pubchem.ncbi.nlm.nih.gov/compound/${encodeURIComponent(str(cid))}`}>{str(cid)}</ExtLink>
                  ) : isNum(p[k]) ? (
                    fmt(p[k], 3)
                  ) : (
                    str(p[k])
                  )}
                </dd>
              </React.Fragment>
            ))}
          </dl>
        );
      })}
    </div>
  );
}

function Preprints({ s }: { s: OmicsSection }) {
  const rows = rowsOf(s, "preprints", "papers", "collection").slice(0, MAX);
  if (!rows.length) return <p className={styles.muted}>No matching preprints.</p>;
  return (
    <ul className={styles.divList}>
      {rows.map((r, i) => {
        const doi = str(pick(r, "doi"));
        return (
          <li key={doi || i} className={styles.divItem}>
            <ExtLink href={pick(r, "url") || (doi ? `https://doi.org/${doi}` : null)}>{str(pick(r, "title")) || doi || "untitled"}</ExtLink>
            <div className={styles.itemMeta}>
              {[str(pick(r, "server")) || "preprint", str(pick(r, "date", "published")), str(pick(r, "authors")).slice(0, 120), doi].filter(Boolean).join(" · ")}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function DepMap({ s }: { s: OmicsSection }) {
  const scalars = Object.entries(s).filter(
    ([k, v]) => !["status", "items", "note", "scope_note", "reason", "error"].includes(k) && (isNum(v) || (typeof v === "string" && v.length < 80))
  );
  const rows = rowsOf(s, "genes", "cell_lines");
  if (!scalars.length && !rows.length) return <p className={styles.muted}>No DepMap statistics.</p>;
  return (
    <div>
      {scalars.length > 0 && <Stats items={scalars.slice(0, 8).map(([k, v]) => ({ label: humanize(k), value: isNum(v) ? fmt(v, 3) : str(v) }))} />}
      {rows.length > 0 && (
        <div style={{ marginTop: scalars.length ? 8 : 0 }}>
          <GenericTable rows={rows} maxRows={MAX} />
        </div>
      )}
    </div>
  );
}

function fdaText(d: any): string {
  return d.fda_status === "approved" && d.fda_first_approval
    ? `FDA first approval ${str(d.fda_first_approval)}`
    : d.fda_status === "not_found"
      ? "not in Drugs@FDA"
      : d.fda_status === "error"
        ? "FDA lookup failed"
        : "FDA not checked";
}

/** SynLethDB partners (generic rows) plus a compact table of approved drugs acting on the experimental partners. */
function SynLethDb({ s }: { s: OmicsSection }) {
  const rows = rowsOf(s);
  const druggable = druggablePartners(s);
  const lookup = ((s as any).druggable_lookup || {}) as Record<string, any>;
  const lookupStatus = str(lookup.status);
  return (
    <div>
      {rows.length ? <GenericTable rows={rows} maxRows={MAX} /> : <p className={styles.muted}>No partners returned.</p>}
      {druggable.length > 0 && (
        <Section
          title="Druggable partners"
          meta={druggable.length}
          hint="Approved drugs DGIdb lists for the top experimental partners; the year is the FDA first approval"
        >
          <div className={styles.tableScroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Partner</th>
                  <th scope="col">SL evidence</th>
                  <th scope="col">Approved drugs (DGIdb) · FDA year</th>
                </tr>
              </thead>
              <tbody>
                {druggable.slice(0, MAX).map((r: any) => {
                  const drugs = arr(r.drugs).filter((d: any) => d && str(d.name)).slice(0, 4);
                  return (
                    <tr key={str(r.partner)}>
                      <td className={styles.mono}>{str(r.partner)}</td>
                      <td className={styles.wrapCell}>{arr(r.evidence).map(str).join("; ") || "—"}</td>
                      <td className={styles.wrapCell}>
                        {drugs.length ? (
                          <ChipList
                            items={drugs.map((d: any, i: number) => {
                              const year = d.fda_status === "approved" ? (/\d{4}/.exec(str(d.fda_first_approval)) || [""])[0] : "";
                              return {
                                key: `${str(d.name)}-${i}`,
                                label: (
                                  <>
                                    {str(d.name).toLowerCase()}
                                    {year && <span className={styles.muted}> {year}</span>}
                                  </>
                                ),
                                title: `${arr(d.interaction_types).map(str).join(", ") || "type not stated"} · ${fdaText(d)}`,
                              };
                            })}
                            limit={4}
                          />
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className={styles.note}>A synthetic-lethal pair plus an approved drug is a hypothesis, not evidence of clinical benefit.</p>
        </Section>
      )}
      {lookupStatus === "failed" && <p className={styles.warnNote}>Approved-drug lookup for the partners failed: {str(lookup.detail)}</p>}
    </div>
  );
}

interface SourceDef {
  key: string;
  name: string;
  title: string;
  render: (s: OmicsSection) => React.ReactNode;
  /** Section heading meta: counts or the headline value. */
  count?: (s: OmicsSection) => React.ReactNode;
  /** One clause for the collapsed card ("top GWAS hit: …"); empty when the source has nothing to say. */
  summary?: (s: OmicsSection) => string;
  /** Fixed method note, shown under "About these data". */
  about?: string;
  badge?: React.ReactNode;
}

const ORDER: SourceDef[] = [
  {
    key: "gwas",
    name: "GWAS Catalog",
    title: "GWAS Catalog associations",
    render: (s) => <Gwas s={s} />,
    count: (s) => gwasRows(s).length || null,
    summary: (s) => {
      const rows = gwasRows(s);
      const top = rows.filter((r) => r.p != null && r.trait).sort((a, b) => (a.p as number) - (b.p as number))[0];
      return top ? `top GWAS: ${top.trait} (p ${(top.p as number).toExponential(0)})` : rows.length ? plural(rows.length, "GWAS association") : "";
    },
  },
  {
    key: "gtex",
    name: "GTEx",
    title: "GTEx tissue expression",
    render: (s) => <Gtex s={s} />,
    count: (s) => (gtexRows(s).length ? "median TPM" : null),
    summary: (s) => {
      const top = gtexRows(s)[0];
      return top ? `expressed most in ${humanize(top.tissue).toLowerCase()} (${fmt(top.value, 1)} TPM)` : "";
    },
    about: "GTEx: median gene expression per tissue (TPM), top tissues by median.",
  },
  {
    key: "string",
    name: "STRING",
    title: "STRING interaction partners",
    render: (s) => <StringDb s={s} />,
    count: (s) => {
      const rows = stringRows(s);
      const scores = rows.map((r) => r.score).filter((v): v is number => v != null);
      if (!rows.length) return null;
      return scores.length ? `${rows.length} · scores ${fmt(Math.min(...scores), 3)}–${fmt(Math.max(...scores), 3)}` : rows.length;
    },
    summary: (s) => {
      const n = stringRows(s).length;
      return n ? plural(n, "STRING interactor") : "";
    },
    about: "STRING: partners sorted by combined score; hover a partner for its score.",
  },
  {
    key: "reactome",
    name: "Reactome",
    title: "Reactome pathways",
    render: (s) => <Reactome s={s} />,
    count: (s) => rowsOf(s, "pathways").length || null,
    summary: (s) => {
      const n = rowsOf(s, "pathways").length;
      return n ? plural(n, "Reactome pathway") : "";
    },
  },
  {
    key: "pubchem",
    name: "PubChem",
    title: "PubChem compound properties",
    render: (s) => <PubChem s={s} />,
    summary: (s) => {
      const p = pubchemProps(s)[0];
      const name = p ? str(pick(p, "name", "IUPACName")) : "";
      return name ? `PubChem: ${name.length > 40 ? `${name.slice(0, 39)}…` : name}` : "";
    },
  },
  {
    key: "preprints",
    name: "bioRxiv / medRxiv",
    title: "Preprints (bioRxiv / medRxiv)",
    render: (s) => <Preprints s={s} />,
    count: (s) => rowsOf(s, "preprints", "papers", "collection").length || null,
    summary: (s) => {
      const n = rowsOf(s, "preprints", "papers", "collection").length;
      return n ? plural(n, "preprint") : "";
    },
    badge: <Pill tone="warn" title="Preprints have not been through peer review">not peer reviewed</Pill>,
  },
  {
    key: "depmap",
    name: "DepMap",
    title: "DepMap dependency",
    render: (s) => <DepMap s={s} />,
    about: "DepMap: CRISPR gene effect (Chronos) from a local DepMap file; more negative means more essential.",
  },
  {
    key: "synlethdb",
    name: "SynLethDB",
    title: "SynLethDB synthetic-lethal partners",
    render: (s) => <SynLethDb s={s} />,
    count: (s) => rowsOf(s).length || null,
    summary: (s) => {
      const n = rowsOf(s).length;
      const d = druggablePartners(s).length;
      return n ? `${plural(n, "SL partner")}${d ? ` (${d} druggable)` : ""}` : "";
    },
    about:
      "SynLethDB: approved drugs are those DGIdb lists for the top experimental partners; FDA dates come from Drugs@FDA for the leading drugs only.",
  },
];

/** A source the card has no dedicated section for, by its public name ("clinvar" -> "ClinVar"). */
function extraName(key: string): string {
  return SOURCE_NAMES[key.toLowerCase()] || humanize(key);
}

const BAD_STATUS = ["error", "failed", "unavailable"];
const SKIPPED_STATUS = ["skipped", "not_configured", "disabled", "empty", "no_data"];

/** Status word for a section heading, coloured; nothing when the source answered normally. */
function statusMeta(section: OmicsSection | undefined): React.ReactNode {
  const status = str(section?.status);
  if (!status || status === "ok") return null;
  const tone = BAD_STATUS.includes(status) ? "bad" : SKIPPED_STATUS.includes(status) ? undefined : "warn";
  return (
    <span className={styles.toneText} data-tone={tone}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function SourceSection({ title, section, count, badge, children }: { title: string; section: OmicsSection | undefined; count?: React.ReactNode; badge?: React.ReactNode; children: React.ReactNode }) {
  // A failure or skip reason explains an empty section, so it stays visible; scope notes go under "About these data".
  const reason = str(section?.reason || section?.error);
  const status = statusMeta(section);
  const parts = [status, count, badge].filter((p) => p != null && p !== "");
  return (
    <Section
      title={title}
      meta={
        parts.length ? (
          <span className={styles.row} style={{ gap: 8, display: "inline-flex" }}>
            {parts.map((p, i) => (
              <React.Fragment key={i}>{p}</React.Fragment>
            ))}
          </span>
        ) : undefined
      }
    >
      {children}
      {reason && <p className={styles.note}>{reason}</p>}
    </Section>
  );
}

/** Omics and chemistry sources (F20): one section per source present in the artifact. */
export function OmicsArtifact({ data }: { data: OmicsIntelArtifact }) {
  if (!data || typeof data !== "object") return null;
  const present = ORDER.filter((o) => data[o.key] && typeof data[o.key] === "object");
  const extra = Object.keys(data).filter((k) => !ORDER.some((o) => o.key === k) && data[k] && typeof data[k] === "object");
  if (!present.length && !extra.length) return null;

  const failed = [...present.map((o) => ({ name: o.name, s: data[o.key] })), ...extra.map((k) => ({ name: extraName(k), s: data[k] }))].filter((x) =>
    BAD_STATUS.includes(str(x.s?.status))
  );
  const headlines = present
    .filter((o) => !BAD_STATUS.includes(str(data[o.key]?.status)))
    .map((o) => o.summary?.(data[o.key] as OmicsSection) || "")
    .filter(Boolean);
  const total = present.length + extra.length;
  const meta = [
    ...headlines.slice(0, 4),
    headlines.length ? "" : `${plural(total, "source")}: ${[...present.map((o) => o.name), ...extra.map(extraName)].join(", ")}`,
    failed.length ? `${failed.map((f) => f.name).join(", ")} unavailable` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const notes = [
    ...present.flatMap((o) => {
      const s = data[o.key];
      const backend = str(s?.note || s?.scope_note);
      return [o.about || "", backend ? `${o.name}: ${backend}` : ""];
    }),
    ...extra.map((k) => {
      const backend = str(data[k]?.note || data[k]?.scope_note);
      return backend ? `${extraName(k)}: ${backend}` : "";
    }),
  ].filter(Boolean);

  return (
    <Card
      icon={<Dna style={{ width: 15, height: 15 }} />}
      title="Omics & chemistry sources"
      meta={meta}
      label="Omics and chemistry sources"
      collapsible
      defaultOpen={false}
    >
      {present.map((o) => (
        <SourceSection key={o.key} title={o.title} section={data[o.key]} count={o.count?.(data[o.key] as OmicsSection)} badge={o.badge}>
          {o.render(data[o.key] as OmicsSection)}
        </SourceSection>
      ))}
      {extra.map((k) => (
        <SourceSection key={k} title={extraName(k)} section={data[k]} count={rowsOf(data[k]).length || null}>
          {rowsOf(data[k]).length ? <GenericTable rows={rowsOf(data[k])} maxRows={MAX} priority={SIGNIFICANCE_COLUMNS} /> : <p className={styles.muted}>No rows.</p>}
        </SourceSection>
      ))}
      {notes.length > 0 && (
        <Disclosure summary="About these data">
          <ul className={styles.list}>
            {notes.map((n, i) => (
              <li key={i} className={styles.listItem}>
                <span className={styles.note} style={{ margin: 0 }}>
                  {n}
                </span>
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </Card>
  );
}
