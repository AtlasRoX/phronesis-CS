"use client";

import React, { useState } from "react";
import { Network } from "lucide-react";
import type { PrimeKGAnalysis, PrimeKGArtifact as PrimeKGData, PrimeKGNode, PrimeKGSection } from "@/types/primekg";
import { Card, ChipList, Disclosure, ExtLink, Pill, Section, arr, humanize, isNum, safeUrl, str, styles as ui } from "@/components/agentic/ui";
import type { ChipItem, Tone } from "@/components/agentic/ui";
import { repurposingCheck } from "@/lib/kgCalibration";
import styles from "./PrimeKGArtifact.module.css";

export interface PrimeKGArtifactProps {
  data: PrimeKGData;
}

const CONTEXT_HINT = "related to the question's disease";

/** "glioblastoma (disease)" -> "glioblastoma": PrimeKG's type qualifier adds nothing in a heading. */
function short(name: unknown): string {
  return str(name).replace(/\s*\(disease\)$/i, "");
}

/** DrugBank lists some compounds as ALL-CAPS IUPAC names; lower-case those (chemical names are lower case anyway).
 * Gene symbols are left alone, and short all-caps names (abbreviations) too. */
function tidyName(node: PrimeKGNode): string {
  const name = str(node?.name);
  if (node?.type === "gene/protein" || name !== name.toUpperCase()) return name;
  return name.replace(/[^A-Za-z]/g, "").length > 12 ? name.toLowerCase() : name;
}

function nodeTitle(node: PrimeKGNode): string {
  return [str(node?.name), str(node?.curie || node?.id)].filter(Boolean).join(" · ");
}

/** A node name, linked when PrimeKG has a source URL; long names are cut with an ellipsis (full name in the tooltip). */
function NodeName({ node }: { node: PrimeKGNode }) {
  if (!node) return null;
  const url = safeUrl(node.url);
  return url ? (
    <a href={url} target="_blank" rel="noopener noreferrer" className={`${ui.link} ${styles.name}`} title={nodeTitle(node)}>
      {tidyName(node)}
    </a>
  ) : (
    <span className={styles.name} title={nodeTitle(node)}>
      {tidyName(node)}
    </span>
  );
}

function chipOf(node: PrimeKGNode, i: number): ChipItem {
  return {
    key: String(node.index ?? node.id ?? i),
    label: <span className={styles.name}>{tidyName(node)}</span>,
    href: node.url,
    title: nodeTitle(node),
    highlight: !!node.context_match,
  };
}

/** A PrimeKG section as chips; `extra` is what the backend counted but did not send. */
function NodeChips({ section, limit = 8, empty }: { section?: PrimeKGSection; limit?: number; empty?: string }) {
  const items = arr<PrimeKGNode>(section?.items).filter(Boolean);
  const total = isNum(section?.total) ? section.total : items.length;
  return (
    <ChipList
      items={items.map(chipOf)}
      limit={limit}
      extra={Math.max(0, total - items.length)}
      highlightHint={CONTEXT_HINT}
      empty={empty}
    />
  );
}

interface Fact {
  label: string;
  section?: PrimeKGSection;
  limit?: number;
  value?: React.ReactNode;
}

/** Label/value rows; a section's total sits beside its label, matches to the question's disease are counted in the tooltip. */
function Facts({ rows }: { rows: Fact[] }) {
  return (
    <dl className={styles.facts}>
      {rows.map((f) => {
        const total = f.section && isNum(f.section.total) ? f.section.total : null;
        const matches = (f.section as { context_matches?: number } | undefined)?.context_matches;
        return (
          <div className={styles.fact} key={f.label}>
            <dt title={isNum(matches) && matches > 0 ? `${matches} ${CONTEXT_HINT}` : undefined}>
              {f.label}
              {total ? <span className={styles.count}>{total}</span> : null}
            </dt>
            <dd>{f.value ?? <NodeChips section={f.section} limit={f.limit} />}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function tagTone(tag: string): Tone {
  if (tag === "supportive care") return "warn";
  if (/ pathway$/.test(tag)) return "accent";
  return "neutral";
}

/** Candidate labels such as "supportive care", "neuroendocrine-tumour agent" or "KRAS pathway". */
function Tags({ tags }: { tags?: string[] | null }) {
  const list = arr<string>(tags);
  if (!list.length) return null;
  return (
    <span className={styles.tags}>
      {list.map((t) => (
        <Pill key={t} tone={tagTone(t)} title={t === "supportive care" ? "symptom control, not anti-tumour therapy" : undefined}>
          {t}
        </Pill>
      ))}
    </span>
  );
}

function score(v: unknown): string {
  return isNum(v) ? v.toFixed(2) : "—";
}

function CandidateTable({ cands }: { cands: any[] }) {
  return (
    <div className={ui.tableScroll}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Drug</th>
            <th scope="col" className={ui.num}>
              Score
            </th>
            <th scope="col">Why</th>
          </tr>
        </thead>
        <tbody>
          {cands.map((c, i) => (
            <tr key={c.index ?? i}>
              <td className={styles.drugCell}>
                <NodeName node={c} />
                <Tags tags={c.tags} />
                {arr(c.approval_groups).length ? <div className={styles.sub}>{arr(c.approval_groups).join(", ")}</div> : null}
              </td>
              <td className={ui.num}>{score(c.score)}</td>
              <td className={ui.wrapCell}>{c.basis || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One path as a chain of nodes with the relation on each hop; older payloads only carry the text form. */
function PathChain({ path }: { path: any }) {
  const nodes = arr<PrimeKGNode>(path?.nodes).filter(Boolean);
  if (!nodes.length) return <>{str(path?.text)}</>;
  const rels = arr<string[]>(path?.relations);
  return (
    <>
      {nodes.map((n, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className={styles.rel}>{arr<string>(rels[i - 1]).map((r) => r.replace(/_/g, " ")).join(" / ")} →</span>}
          <NodeName node={n} />
        </React.Fragment>
      ))}
    </>
  );
}

function Paths({ res, limit = 4 }: { res?: Record<string, any>; limit?: number }) {
  const [all, setAll] = useState(false);
  const paths = arr(res?.paths);
  if (!paths.length) return <p className={ui.muted}>No path within {res?.max_hops ?? 3} hops.</p>;
  const shown = all ? paths : paths.slice(0, limit);
  return (
    <>
      <ol className={styles.paths}>
        {shown.map((p, i) => (
          <li key={i} title={str(p?.text) || undefined}>
            <PathChain path={p} />
          </li>
        ))}
      </ol>
      {paths.length > limit && (
        <button type="button" className={ui.chipMore} onClick={() => setAll(!all)} aria-expanded={all}>
          {all ? "show less" : `+${paths.length - limit} more`}
        </button>
      )}
    </>
  );
}

function statusTone(status: string): Tone {
  if (status === "contraindicated") return "bad";
  if (status === "indicated") return "ok";
  if (status.startsWith("off-label")) return "info";
  return "neutral";
}

function names(nodes: unknown, sep = ", "): string {
  return arr<PrimeKGNode>(nodes)
    .map((n) => short(n?.name))
    .filter(Boolean)
    .join(sep);
}

function Repurposing({ r, hint }: { r: Record<string, any>; hint: string }) {
  // KG-002: the known-indication check is in-sample unless relatives were held out; never a calibrated "high signal".
  const check = repurposingCheck(r.calibration);
  const cands = arr(r.candidates).slice(0, 10);
  const pathway = arr(r.pathway_candidates).slice(0, 6);
  const borrowed = arr<PrimeKGNode>(r.indications_borrowed_from);
  const excluded = arr(r.excluded_non_therapeutic);
  const genes = arr<string>(r.mutant_genes).join("/");
  return (
    <>
      <Section
        title="Repurposing candidates"
        hint={hint}
        meta={
          <>
            {r.disease?.name ? `for ${short(r.disease.name)} · ` : ""}
            <Pill tone={check.tone}>{check.label}</Pill>
          </>
        }
      >
        <CandidateTable cands={cands} />
        <p className={ui.note}>
          Hypotheses only{borrowed.length ? `; known indications borrowed from ${names(borrowed)}` : ""}. Ranking check:{" "}
          {check.summary}.{check.caveat ? ` ${check.caveat}` : ""}
        </p>
      </Section>
      {pathway.length ? (
        <Section title={`${genes || "Pathway"}-pathway drugs`} meta="little direct graph signal for this disease">
          <CandidateTable cands={pathway} />
        </Section>
      ) : null}
      <Disclosure summary="How this is ranked">
        {borrowed.length ? (
          <p className={ui.note}>
            {short(r.disease?.name) || "This disease"} has no recorded indications in PrimeKG; known indications are taken from{" "}
            {borrowed.map((b, i) => (
              <React.Fragment key={b.index ?? i}>
                {i > 0 && ", "}
                <NodeName node={b} />
              </React.Fragment>
            ))}
            {arr(r.borrowed_indications).length ? ` (${names(arr(r.borrowed_indications).slice(0, 6))})` : ""}.
          </p>
        ) : null}
        <p className={ui.note}>
          Known-indication check ({check.label}): {check.summary}. Known indications are scored like any other drug; how
          high they rank shows whether the graph signals carry information for this disease. It is not a probability that
          a candidate works.
        </p>
        {r.method ? <p className={ui.note}>{str(r.method)}</p> : null}
        {excluded.length ? (
          <p className={ui.note}>
            Not listed (not therapies): {excluded.slice(0, 6).map((e) => `${e.name} (${e.reason})`).join("; ")}
          </p>
        ) : null}
      </Disclosure>
    </>
  );
}

function PhenotypeRanking({ r }: { r: Record<string, any> }) {
  const ranking = arr(r.ranking).slice(0, 10);
  return (
    <div className={ui.tableScroll}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Disease</th>
            <th scope="col" className={ui.num}>
              Match
            </th>
            <th scope="col">Matched phenotypes</th>
          </tr>
        </thead>
        <tbody>
          {ranking.map((d, i) => (
            <tr key={d.index ?? i}>
              <td>
                <NodeName node={d} />
                {arr(d.top_genes).length ? <div className={styles.sub}>{arr(d.top_genes).slice(0, 3).join(", ")}</div> : null}
              </td>
              <td className={ui.num}>{str(d.coverage) || "—"}</td>
              <td className={ui.wrapCell}>{arr<string>(d.matched_query_phenotypes).join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Regimen({ r }: { r: Record<string, any> }) {
  return (
    <ul className={ui.list}>
      {arr(r.pairs).map((p, i) => {
        const flags = arr<string>(p.flags).filter((f) => !(p.recorded_interaction && f === "recorded drug-drug interaction"));
        return (
          <li key={i} className={ui.listItem}>
            <div>
              <div className={styles.pairHead}>
                <strong>
                  {p.drugs?.[0]?.name} + {p.drugs?.[1]?.name}
                </strong>
                {p.recorded_interaction ? <Pill tone="bad">recorded interaction</Pill> : null}
              </div>
              <div className={styles.sub}>
                {flags.length ? flags.join("; ") : p.recorded_interaction ? null : "no shared mechanism recorded"}
              </div>
            </div>
          </li>
        );
      })}
      {arr(r.disease_contraindications).map((c, i) => (
        <li key={`c${i}`} className={ui.listItem}>
          <div>
            <div className={styles.pairHead}>
              <Pill tone="bad">contraindicated</Pill>
              <span>in {short(c.disease?.name)}</span>
            </div>
            <div className={styles.sub}>{arr<string>(c.contraindicated_drugs).join(", ")}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function Enrichment({ r }: { r: Record<string, any> }) {
  const rows = Object.entries(r.collections || {}).flatMap(([name, col]: [string, any]) =>
    arr(col?.terms)
      .slice(0, 4)
      .map((t) => ({ collection: humanize(name), ...t })),
  );
  if (!rows.length) return <p className={ui.muted}>No enriched terms.</p>;
  return (
    <div className={ui.tableScroll}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Term</th>
            <th scope="col">Collection</th>
            <th scope="col" className={ui.num}>
              Genes
            </th>
            <th scope="col" className={ui.num}>
              q
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => (
            <tr key={`${t.collection}-${t.index ?? t.name ?? i}`}>
              <td className={ui.wrapCell} title={arr<string>(t.genes).join(", ") || undefined}>
                {str(t.name)}
              </td>
              <td className={ui.shortCell}>{t.collection}</td>
              <td className={ui.num}>
                {t.overlap}/{t.term_size}
              </td>
              <td className={ui.num}>{str(t.q_value) || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Similar({ r }: { r: Record<string, any> }) {
  const rows = arr(r.similar).slice(0, 10);
  if (!rows.length) return <p className={ui.muted}>No similar entities.</p>;
  return (
    <div className={ui.tableScroll}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Entity</th>
            <th scope="col" className={ui.num} title="Jaccard similarity of graph neighbours">
              Similarity
            </th>
            <th scope="col">Most specific shared</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s, i) => (
            <tr key={s.index ?? i}>
              <td>
                <NodeName node={s} />
              </td>
              <td className={ui.num}>{score(s.jaccard)}</td>
              <td className={ui.wrapCell}>{arr<string>(s.most_specific_shared).join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The gene profile's ranking context: the backend's context diseases, else the planner's "ranked for …" reason. */
function rankedFor(r: Record<string, any>, reason: string): string {
  const ctx = names(r.context?.diseases, " and ");
  if (ctx) return `ranked for ${ctx}`;
  const m = /, (ranked for .+)$/.exec(reason);
  return m ? m[1] : "";
}

function Analysis({ a }: { a: PrimeKGAnalysis }) {
  const r = a.result || {};
  const hint = reasonText(a);
  switch (a.operation) {
    case "repurposing":
      return <Repurposing r={r} hint={hint} />;
    case "phenotype_diagnosis": {
      const present = arr(r.present).length;
      const absent = arr(r.absent).length;
      return (
        <Section
          title="Phenotype-ranked diseases"
          hint={hint}
          meta={present ? `${present} phenotype${present === 1 ? "" : "s"}${absent ? `, ${absent} absent` : ""}` : undefined}
        >
          <PhenotypeRanking r={r} />
        </Section>
      );
    }
    case "drug_disease": {
      const status = str(r.status);
      const shared = arr<PrimeKGNode>(r.shared_genes);
      return (
        <Section title="Drug–disease relation" hint={hint} meta={`${short(r.drug?.name)} → ${short(r.disease?.name)}`}>
          <Facts
            rows={[
              { label: "Status", value: status ? <Pill tone={statusTone(status)}>{status}</Pill> : <span className={ui.muted}>—</span> },
              { label: "Shared genes", section: { total: shared.length, items: shared }, limit: 10 },
              { label: "Paths", value: <Paths res={r.paths} /> },
            ]}
          />
        </Section>
      );
    }
    case "paths":
      return (
        <Section
          title="Mechanism paths"
          hint={hint}
          meta={r.source?.name && r.target?.name ? `${short(r.source.name)} → ${short(r.target.name)}` : undefined}
        >
          <Paths res={r} />
        </Section>
      );
    case "polypharmacy":
      return (
        <Section title="Regimen check" hint={hint} meta={names(r.drugs, " + ") || undefined}>
          <Regimen r={r} />
        </Section>
      );
    case "enrichment":
      return (
        <Section title="Gene-set enrichment" hint={hint} meta={hint || undefined}>
          <Enrichment r={r} />
        </Section>
      );
    case "similar":
      return (
        <Section title="Most similar" hint={hint} meta={r.node?.name ? `to ${short(r.node.name)}` : undefined}>
          <Similar r={r} />
        </Section>
      );
    case "drug_profile":
      return (
        <Section title={r.drug?.name ? `${r.drug.name} profile` : "Drug profile"} hint={hint}>
          <Facts
            rows={[
              { label: "Indications", section: r.indications },
              { label: "Contraindications", section: r.contraindications, limit: 6 },
              { label: "Targets", section: r.proteins?.target },
              { label: "Side effects", section: r.side_effects, limit: 6 },
            ]}
          />
        </Section>
      );
    case "disease_profile": {
      const related = arr(r.indications_from_related);
      return (
        <Section
          title={r.disease?.name ? `${short(r.disease.name)} profile` : "Disease profile"}
          hint={hint}
          meta={related.length ? `indications via ${names(related.map((rel) => rel.disease))}` : undefined}
        >
          <Facts
            rows={[
              { label: "Approved drugs", section: r.indicated_drugs },
              ...related.map((rel): Fact => ({ label: `Approved for ${short(rel.disease?.name)}`, section: rel.indicated_drugs })),
              { label: "Contraindicated", section: r.contraindicated_drugs, limit: 6 },
              { label: "Genes", section: r.genes, limit: 10 },
              { label: "Phenotypes", section: r.phenotypes_present, limit: 6 },
            ]}
          />
        </Section>
      );
    }
    case "gene_profile":
      return (
        <Section title={r.gene?.name ? `${r.gene.name} profile` : "Gene profile"} hint={hint} meta={rankedFor(r, hint) || undefined}>
          <Facts
            rows={[
              { label: "Drugs (target)", section: r.drugs?.target },
              { label: "Diseases", section: r.diseases },
              { label: "Pathways", section: r.pathways, limit: 5 },
              { label: "Interactors", section: r.interactors, limit: 8 },
            ]}
          />
        </Section>
      );
    default:
      return null;
  }
}

const ONTOLOGY_ID = /\b(?:MONDO|DOID|EFO|HP|ORPHANET)[_:]\d+\b/gi;

/** The analysis reason with any raw ontology id replaced by the resolved name (older backends sent the id). */
function reasonText(a: PrimeKGAnalysis & { resolved?: PrimeKGNode[] | null }): string {
  const reason = a.reason || "";
  if (!ONTOLOGY_ID.test(reason)) return reason;
  ONTOLOGY_ID.lastIndex = 0;
  const diseases = (a.resolved || []).filter((r) => r && r.type === "disease" && r.name);
  const res = a.result || {};
  const fallback = res.disease?.name || res.target?.name || diseases[0]?.name;
  return reason.replace(ONTOLOGY_ID, (id) => {
    const key = id.toUpperCase().replace(":", "_");
    const hit = diseases.find(
      (d) =>
        String(d.curie || "").toUpperCase().replace(":", "_") === key ||
        (Array.isArray(d.mondo_ids) && d.mondo_ids.some((m: string) => m.toUpperCase().replace(":", "_") === key)),
    );
    return hit?.name || fallback || id;
  });
}

/** A few words per analysis for the collapsed card's meta line. */
function summary(a: PrimeKGAnalysis): string {
  const r = a.result || {};
  switch (a.operation) {
    case "disease_profile":
      return `${short(r.disease?.name) || "disease"} profile`;
    case "gene_profile":
      return `${r.gene?.name || "gene"} profile`;
    case "drug_profile":
      return `${r.drug?.name || "drug"} profile`;
    case "repurposing": {
      const n = arr(r.candidates).length;
      return `${n} repurposing candidate${n === 1 ? "" : "s"}`;
    }
    case "phenotype_diagnosis":
      return r.ranking?.[0]?.name ? `phenotype match: ${short(r.ranking[0].name)}` : "phenotype ranking";
    case "drug_disease":
      return `${short(r.drug?.name)} → ${short(r.disease?.name)}${r.status ? `: ${r.status}` : ""}`;
    case "paths": {
      const n = arr(r.paths).length;
      return `${n} path${n === 1 ? "" : "s"} ${short(r.source?.name)} → ${short(r.target?.name)}`;
    }
    case "polypharmacy":
      return "regimen check";
    case "enrichment":
      return "gene-set enrichment";
    case "similar":
      return `similar to ${short(r.node?.name)}`;
    default:
      return humanize(a.operation).toLowerCase();
  }
}

/** PrimeKG analyses run for this answer, one section per analysis. */
export function PrimeKGArtifact({ data }: PrimeKGArtifactProps) {
  const analyses = (data.analyses || []).filter((a) => !a.error && a.result);
  if (!analyses.length) return null;
  const prov = data.provenance || {};
  const doi = prov.dataverse_doi ? `https://doi.org/${prov.dataverse_doi.replace(/^doi:/, "")}` : null;
  const unresolved = arr<string>(data.unresolved);
  const caveats = Array.from(new Set(analyses.map((a) => str(a.result?.caveat)).filter(Boolean)));
  return (
    <Card
      icon={<Network style={{ width: 15, height: 15 }} />}
      title="PrimeKG knowledge graph"
      label="PrimeKG knowledge graph"
      collapsible
      defaultOpen={false}
      meta={`${analyses.length} ${analyses.length === 1 ? "analysis" : "analyses"} · ${analyses.map(summary).join(", ")}`}
    >
      {analyses.map((a, i) => (
        <div className={styles.analysis} key={`${a.operation}-${i}`}>
          <Analysis a={a} />
        </div>
      ))}
      <Disclosure summary="About these data">
        <p className={ui.note}>
          {doi ? <ExtLink href={doi}>PrimeKG {prov.dataverse_version || ""}</ExtLink> : `PrimeKG ${prov.dataverse_version || ""}`}
          {prov.source ? ` · ${prov.source}` : ""}
          {prov.store_built_at ? ` · store built ${prov.store_built_at}` : ""}
        </p>
        {prov.citation ? <p className={ui.note}>{prov.citation}</p> : null}
        {unresolved.length ? <p className={ui.note}>No PrimeKG node for: {unresolved.join(", ")}.</p> : null}
        {caveats.map((c) => (
          <p key={c} className={ui.note}>
            {c}
          </p>
        ))}
      </Disclosure>
    </Card>
  );
}
