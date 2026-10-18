"use client";

import { Microscope, TriangleAlert } from "lucide-react";
import type { DatasetPublic } from "@/types/datasets";
import { CONSENT_LABELS, SOURCE_LABELS } from "@/services/datasetService";
import { Card, ChipList, Disclosure, Pill, Section, Stats, styles as ui } from "@/components/agentic/ui";
import {
  cellTypeCall,
  clusteringMethodText,
  expressionScaleText,
  geoBadge,
  mitoQcText,
  resolveDataset,
} from "@/lib/singleCell";
import styles from "./SingleCellArtifact.module.css";

export interface SingleCellArtifactProps {
  data: any;
  datasets?: DatasetPublic[];
}

interface Marker {
  gene?: string;
  log_fold_change?: number;
  p_adj?: number;
}

interface TargetExpression {
  mean_expression?: number;
  percent_expressing_cells?: number;
}

function fmtP(v: unknown): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "–";
  // Lumicell rounds p values to 6 decimals, so a reported 0 means below 5e-7.
  if (v === 0) return "< 1e-6";
  return v < 0.001 ? v.toExponential(2) : v.toPrecision(3);
}

function fmtNum(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toLocaleString() : "–";
}

function clusterOrder(a: string, b: string): number {
  const na = Number(a);
  const nb = Number(b);
  return Number.isFinite(na) && Number.isFinite(nb) ? na - nb : a.localeCompare(b);
}

/** Where the dataset came from; uploads also show their declared provenance and GEO check. */
function Provenance({ ds, prov }: { ds?: DatasetPublic; prov: Record<string, any> }) {
  const p = { ...(prov || {}), ...(ds?.public_provenance || {}) };
  const isUpload = typeof p.id === "string" && p.id.startsWith("ds_");
  if (!isUpload) {
    const name = typeof p.file_path === "string" ? p.file_path : null;
    return name ? <p className={ui.note}>Dataset file: <span className={ui.mono}>{name}</span></p> : null;
  }
  const consent = (CONSENT_LABELS as Record<string, string>)[p.consent_status] || p.consent_status || "unknown";
  const source = (SOURCE_LABELS as Record<string, string>)[p.source] || p.source;
  // SCI-014: an existing accession is not proof that the file belongs to the series.
  const geo = geoBadge(p, ds?.geo_verified === true);
  const parts = [
    "Uploaded by you",
    p.sha256 ? `sha256 ${String(p.sha256).slice(0, 12)}…` : null,
    source ? `source: ${source} (declared)` : null,
    p.organism ? `organism: ${p.organism} (declared)` : null,
    `consent: ${consent} (declared)`,
  ].filter(Boolean);
  return (
    <p className={`${ui.note} ${styles.provenance}`}>
      <span>{parts.join(" · ")}</span>
      {geo && (
        <Pill tone={geo.tone} title={geo.title}>
          {geo.tone === "warn" && <TriangleAlert style={{ width: 11, height: 11 }} aria-hidden="true" />}
          {geo.text}
        </Pill>
      )}
    </p>
  );
}

/** The Lumicell (Scanpy) run behind an answer: UMAP, clusters with top markers, and the dataset's provenance. */
export function SingleCellArtifact({ data, datasets }: SingleCellArtifactProps) {
  if (!data || typeof data !== "object") return null;
  const prov: Record<string, any> = data.dataset_provenance || {};
  // UI-015: only the dataset this run names; never fall back to another attachment.
  const ds = resolveDataset(prov, datasets);
  const counts: Record<string, number> = data.cluster_cell_counts || {};
  const markers: Record<string, Marker[]> = data.top_markers_by_cluster || {};
  const cellTypes: Record<string, string> = data.cluster_cell_types || {};
  const annotations: Record<string, any> = data.cluster_annotations || {};
  const warnings: string[] = Array.isArray(data.warnings) ? data.warnings.filter((w: unknown) => typeof w === "string" && w) : [];
  const transformations: string[] = Array.isArray(data.transformations) ? data.transformations.map(String) : [];
  const subsampling = data.subsampling && typeof data.subsampling === "object" ? data.subsampling : null;
  const mito = mitoQcText(data.qc_parameters);
  const clustering = clusteringMethodText(data.clustering_parameters);
  const exprScale = expressionScaleText(data);
  const clusters = Array.from(new Set([...Object.keys(counts), ...Object.keys(markers)])).sort(clusterOrder);
  const resolution = data.clustering_parameters?.resolution;
  const name = data.dataset_name || ds?.display_name || "Single-cell dataset";
  const expression = Object.entries((data.target_expression_summary || {}) as Record<string, TargetExpression>).sort(([a], [b]) =>
    clusterOrder(a, b),
  );

  // Headline: the largest cluster and, when a target was profiled, the cluster expressing it most widely.
  const totalCells = Object.values(counts).reduce((s, n) => s + (typeof n === "number" ? n : 0), 0);
  const largest = Object.entries(counts).reduce<[string, number] | null>((best, e) => (!best || e[1] > best[1] ? e : best), null);
  const topExpr = expression.reduce<[string, TargetExpression] | null>(
    (best, e) => ((e[1].percent_expressing_cells ?? -1) > (best?.[1].percent_expressing_cells ?? -1) ? e : best),
    null,
  );
  // Unannotated clusters come back labelled "Cluster N", which says nothing beside the cluster id. Labels are putative
  // marker-signature calls; their confidence / ambiguity travels with them (SCI-009).
  const call = (cl: string) => cellTypeCall(cl, cellTypes, annotations);
  const cellType = (cl: string) => call(cl)?.label ?? null;
  const meta = [
    `${fmtNum(data.n_cells_post_qc)} cells`,
    `${fmtNum(data.n_clusters)} clusters`,
    largest && totalCells > 0
      ? `largest: ${cellType(largest[0]) ? `putative ${cellType(largest[0])} (cluster ${largest[0]})` : `cluster ${largest[0]}`}, ${Math.round((largest[1] / totalCells) * 100)}%`
      : null,
    topExpr && typeof topExpr[1].percent_expressing_cells === "number"
      ? `target most expressed in cluster ${topExpr[0]} (${topExpr[1].percent_expressing_cells}%)`
      : null,
    subsampling?.applied ? `subsampled ${fmtNum(subsampling.n_cells_used)} of ${fmtNum(subsampling.n_cells_total)} cells` : null,
    warnings.length ? `${warnings.length} warning${warnings.length === 1 ? "" : "s"}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const method = ["Lumicell (Scanpy)", clustering, resolution != null ? `resolution ${resolution}` : null, "Wilcoxon markers", mito]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<Microscope className={styles.icon} style={{ width: 15, height: 15 }} />}
      title={
        <span className={styles.title} title={name}>
          {name}
        </span>
      }
      meta={meta}
      label="Single-cell analysis output"
      collapsible
      defaultOpen={false}
    >
      <Stats
        items={[
          { label: "Cells after QC", value: fmtNum(data.n_cells_post_qc), sub: `of ${fmtNum(data.n_cells_initial)}` },
          { label: "Genes", value: fmtNum(data.n_genes) },
          { label: "Clusters", value: fmtNum(data.n_clusters), sub: resolution != null ? `res ${resolution}` : undefined },
          ...(data.qc_parameters?.mt_filter_applied === true && data.qc_parameters?.max_pct_mt != null
            ? [{ label: "Mito cutoff", value: `< ${data.qc_parameters.max_pct_mt}%` }]
            : data.qc_parameters?.mt_filter_applied === false
              ? [{ label: "Mito filter", value: "not applied" }]
              : []),
        ]}
      />

      {/* SCI-009: engine-side caveats are shown, not dropped at render time. */}
      {warnings.length > 0 && (
        <div className={ui.warnNote} role="note">
          <strong>Analysis warnings:</strong>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {(data.input_scale || transformations.length > 0 || subsampling?.applied) && (
        <p className={ui.note}>
          {data.input_scale ? (
            <>
              Input {data.input_matrix ? <span className={ui.mono}>{String(data.input_matrix)}</span> : "matrix"} read as{" "}
              {String(data.input_scale).replace(/_/g, " ")}
              {data.scale_basis ? ` (${data.scale_basis})` : ""}.{" "}
            </>
          ) : null}
          {transformations.length ? `Transformations: ${transformations.join(", ")}. ` : ""}
          {subsampling?.applied
            ? `Analysed a random subsample of ${fmtNum(subsampling.n_cells_used)} of ${fmtNum(subsampling.n_cells_total)} cells${
                subsampling.seed != null ? ` (seed ${subsampling.seed})` : ""
              }.`
            : ""}
        </p>
      )}

      {data.plot_base64_png && (
        <Section title="UMAP">
          {/* Rendered server-side by matplotlib on a white canvas, so its colours cannot follow the theme. */}
          <img className={styles.plot} src={`data:image/png;base64,${data.plot_base64_png}`} alt={`UMAP of ${name} colored by cluster`} />
        </Section>
      )}
      {!data.plot_base64_png && data.plot_dropped_for_storage && (
        <p className={ui.note}>The UMAP plot was not kept in this browser&apos;s storage (it was full).</p>
      )}

      {clusters.length > 0 && (
        <Section title="Clusters and top markers" meta={`${clusters.length}`} hint="Marker genes ranked by Wilcoxon test, cluster vs. rest; chip tooltip shows log2 fold change and adjusted p">
          <div className={ui.tableScroll}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Cluster</th>
                  <th scope="col" className={ui.num}>
                    Cells
                  </th>
                  <th scope="col">Top markers (log2FC)</th>
                  <th scope="col" className={ui.num}>
                    Best p_adj
                  </th>
                </tr>
              </thead>
              <tbody>
                {clusters.map((cl) => {
                  const ms = (markers[cl] || []).filter((m) => m && m.gene).slice(0, 5);
                  const best = ms.reduce<number | undefined>(
                    (acc, m) => (typeof m.p_adj === "number" && (acc === undefined || m.p_adj < acc) ? m.p_adj : acc),
                    undefined,
                  );
                  const typeCall = call(cl);
                  const type = typeCall?.label;
                  return (
                    <tr key={cl}>
                      <td>
                        <span className={styles.cluster}>{cl}</span>
                        {type && (
                          <span className={styles.cellType} title={`${type} (${typeCall?.qualifier})`}>
                            {type}
                            <span className={styles.muted}> ({typeCall?.qualifier})</span>
                          </span>
                        )}
                      </td>
                      <td className={ui.num}>{fmtNum(counts[cl])}</td>
                      <td className={styles.markers}>
                        <ChipList
                          limit={5}
                          empty="–"
                          items={ms.map((m) => ({
                            key: m.gene as string,
                            label: (
                              <>
                                <span className={styles.gene}>{m.gene}</span>
                                {typeof m.log_fold_change === "number" && <span className={styles.lfc}> {m.log_fold_change.toFixed(2)}</span>}
                              </>
                            ),
                            title: [
                              m.gene,
                              typeof m.log_fold_change === "number" ? `log2FC ${m.log_fold_change.toFixed(2)}` : null,
                              typeof m.p_adj === "number" ? `p_adj ${fmtP(m.p_adj)}` : null,
                            ]
                              .filter(Boolean)
                              .join(" · "),
                          }))}
                        />
                      </td>
                      <td className={`${ui.num} ${styles.muted}`}>{fmtP(best)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className={ui.note}>
            Marker p-values are optimistic: the same cells defined the clusters. Cell types are putative marker-signature
            calls{data.annotation_method?.caveat ? `: ${data.annotation_method.caveat}` : ", not validated annotations."}
          </p>
        </Section>
      )}

      {expression.length > 0 && (
        <Section title="Target expression by cluster" caveat={data.target_expression_note || undefined}>
          <div className={ui.tableScroll}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Cluster</th>
                  <th scope="col" className={ui.num}>
                    Mean expression ({exprScale})
                  </th>
                  <th scope="col">Cells expressing</th>
                </tr>
              </thead>
              <tbody>
                {expression.map(([cl, v]) => {
                  const p = typeof v.percent_expressing_cells === "number" ? Math.max(0, Math.min(100, v.percent_expressing_cells)) : null;
                  return (
                    <tr key={cl}>
                      <td>
                        <span className={styles.cluster}>{cl}</span>
                      </td>
                      <td className={ui.num}>{v.mean_expression ?? "–"}</td>
                      <td>
                        <span className={styles.pctCell}>
                          <span className={styles.pctTrack} aria-hidden="true">
                            {p != null && <span className={styles.pctFill} style={{ width: `${p}%` }} />}
                          </span>
                          <span className={styles.pctValue}>{p != null ? `${v.percent_expressing_cells}%` : "–"}</span>
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {expression.length === 0 && data.target_gene_status && data.target_gene_status !== "measured" && (
        <p className={ui.note}>Target gene not profiled: {String(data.target_gene_status).replace(/_/g, " ")}.</p>
      )}

      <Disclosure summary="About these data">
        <p className={ui.note} style={{ marginTop: 0 }}>
          {method}.
        </p>
        {data.disclaimer && <p className={ui.note}>{data.disclaimer}</p>}
        <Provenance ds={ds} prov={prov} />
      </Disclosure>
    </Card>
  );
}
