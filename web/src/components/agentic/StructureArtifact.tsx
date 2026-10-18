"use client";

import React from "react";
import { Atom } from "lucide-react";
import type { PdbEntry, StructureIntelArtifact } from "@/types/agentic";
import { Card, ChipList, Disclosure, ExtLink, GenericTable, Pill, Section, arr, fmt, isNum, str, styles } from "./ui";

/** AlphaFold pLDDT bands (Jumper et al. 2021): >90 very high, 70-90 confident, 50-70 low, <50 very low. */
function plddtBand(score: unknown, given?: unknown): { label: string; tone: "ok" | "info" | "warn" | "bad" } {
  const g = str(given).toLowerCase();
  const s = isNum(score) ? score : NaN;
  const label = g || (s > 90 ? "very high" : s >= 70 ? "confident" : s >= 50 ? "low" : Number.isFinite(s) ? "very low" : "");
  const tone = label.includes("very high") ? "ok" : label.includes("confident") || label === "high" ? "info" : label.includes("very low") ? "bad" : "warn";
  return { label: label || "unknown", tone };
}

/** A ligand as a chip: its id as the label, its name (when the backend sends one) as the tooltip. */
function ligandChip(l: unknown, i: number): { key: string; label: string; title?: string } | null {
  if (typeof l === "string") return l ? { key: `${l}-${i}`, label: l } : null;
  if (l && typeof l === "object") {
    const id = str((l as any).id);
    const name = str((l as any).name);
    const label = id || name;
    return label ? { key: `${label}-${i}`, label, title: id && name ? name : undefined } : null;
  }
  return null;
}

/** AlphaFold DB model and experimental PDB structures for the target, plus any docking or generation output. */
export function StructureArtifact({ data }: { data: StructureIntelArtifact }) {
  if (!data || typeof data !== "object") return null;
  const af = data.alphafold && typeof data.alphafold === "object" ? data.alphafold : null;
  const entries = arr<PdbEntry>(data.pdb_entries).filter((e) => e && typeof e === "object").slice(0, 50);
  const band = af ? plddtBand(af.mean_plddt, af.plddt_band) : null;
  const uniprotUrl = data.uniprot ? `https://www.uniprot.org/uniprotkb/${encodeURIComponent(data.uniprot)}/entry` : null;
  const generated = arr<any>(data.generated);
  const docking = data.docking && typeof data.docking === "object" ? data.docking : null;
  const best = entries
    .filter((e) => isNum(e.resolution))
    .reduce<PdbEntry | null>((b, e) => (!b || (e.resolution as number) < (b.resolution as number) ? e : b), null);

  // Collapsed, the card still answers "is there a structure, and how good is it?"
  const meta = [
    af && band ? `AlphaFold pLDDT ${isNum(af.mean_plddt) ? fmt(af.mean_plddt, 1) : "—"} (${band.label})` : "no AlphaFold model",
    entries.length ? `${entries.length} PDB entr${entries.length === 1 ? "y" : "ies"}` : "no experimental structure",
    best ? `best ${fmt(best.resolution, 2)} Å${best.pdb_id ? ` (${best.pdb_id})` : ""}` : "",
    data.uniprot ? `UniProt ${data.uniprot}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<Atom style={{ width: 15, height: 15 }} />}
      title={`Structure${data.gene ? ` · ${data.gene}` : ""}`}
      meta={meta}
      label="Protein structure"
      collapsible
      defaultOpen={false}
    >
      {af ? (
        <Section title="AlphaFold model" meta={af.version != null ? `v${str(af.version)}` : undefined}>
          <div className={styles.row} style={{ flexWrap: "wrap" }}>
            <span className={styles.mono} style={{ color: "var(--text-heading)" }}>
              {af.model_id || "—"}
            </span>
            {band && (
              <Pill tone={band.tone} title="Mean predicted local distance difference test (pLDDT), 0-100">
                pLDDT {isNum(af.mean_plddt) ? fmt(af.mean_plddt, 1) : "—"} · {band.label}
              </Pill>
            )}
          </div>
          <div className={styles.linkRow}>
            {af.url && <ExtLink href={af.url}>AlphaFold DB entry</ExtLink>}
            {af.pdb_url && <ExtLink href={af.pdb_url}>Model file (PDB)</ExtLink>}
            {uniprotUrl && <ExtLink href={uniprotUrl}>UniProt {data.uniprot}</ExtLink>}
          </div>
          <p className={styles.note}>A predicted model, not an experimental structure. Low-pLDDT regions are often disordered.</p>
        </Section>
      ) : (
        <>
          <p className={styles.muted}>No AlphaFold model was found for this protein.</p>
          {uniprotUrl && (
            <div className={styles.linkRow}>
              <ExtLink href={uniprotUrl}>UniProt {data.uniprot}</ExtLink>
            </div>
          )}
        </>
      )}

      {entries.length > 0 && (
        <Section title="Experimental structures (RCSB PDB)" meta={entries.length}>
          <div className={styles.tableScroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">PDB</th>
                  <th scope="col">Title</th>
                  <th scope="col">Method</th>
                  <th scope="col" className={styles.num}>
                    Resolution (Å)
                  </th>
                  <th scope="col">Ligands</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => {
                  const ligs = arr(e.ligands)
                    .map(ligandChip)
                    .filter((l): l is NonNullable<typeof l> => l != null);
                  const url = e.url || (e.pdb_id ? `https://www.rcsb.org/structure/${encodeURIComponent(e.pdb_id)}` : null);
                  return (
                    <tr key={e.pdb_id || i}>
                      <td className={styles.mono}>
                        <ExtLink href={url}>{e.pdb_id || "—"}</ExtLink>
                      </td>
                      <td className={styles.wrapCell}>{e.title || "—"}</td>
                      <td className={styles.shortCell}>{e.method || "—"}</td>
                      <td className={styles.num}>{isNum(e.resolution) ? fmt(e.resolution, 2) : "—"}</td>
                      <td className={styles.textCell}>{ligs.length ? <ChipList items={ligs} limit={4} /> : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {docking && (
        <Disclosure summary="Docking (DiffDock)">
          {arr(docking.poses).length ? (
            <GenericTable rows={arr(docking.poses)} />
          ) : (
            <pre className={styles.code} style={{ maxHeight: 200 }}>
              {JSON.stringify(docking, null, 2).slice(0, 4000)}
            </pre>
          )}
          <p className={styles.note}>Docking poses and confidences are model predictions; validate before use.</p>
        </Disclosure>
      )}
      {generated.length > 0 && (
        <Disclosure summary={`Generated molecules (MolMIM, ${generated.length})`}>
          {generated.every((g) => g && typeof g === "object") ? (
            <GenericTable rows={generated} />
          ) : (
            <ul className={styles.list}>
              {generated.slice(0, 50).map((g, i) => (
                <li key={i} className={`${styles.listItem} ${styles.mono}`}>
                  {str(g)}
                </li>
              ))}
            </ul>
          )}
          <p className={styles.note}>Generated analogs are untested proposals.</p>
        </Disclosure>
      )}
    </Card>
  );
}
