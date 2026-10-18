// Single-cell card logic (UI-015, SCI-009, SCI-014). Framework-free so `node --test` can import it.

type Rec = Record<string, any>;

/**
 * The attached dataset this run analysed: only an exact id match. Never "the first attachment" — that would attribute
 * another file's hash, consent and GEO status to this result (UI-015).
 */
export function resolveDataset<T extends { id?: string }>(prov: Rec | null | undefined, datasets: T[] | null | undefined): T | undefined {
  const id = prov && typeof prov.id === "string" ? prov.id : "";
  if (!id) return undefined;
  return (datasets || []).find((d) => d && d.id === id);
}

/** The mitochondrial QC statement, true to what the engine did (SCI-009). */
export function mitoQcText(qc: Rec | null | undefined): string | null {
  if (!qc || typeof qc !== "object") return null;
  const cutoff = typeof qc.max_pct_mt === "number" ? qc.max_pct_mt : null;
  if (qc.mt_filter_applied === true) return cutoff !== null ? `mitochondrial cutoff < ${cutoff}%` : "mitochondrial filter applied";
  if (qc.mt_filter_applied === false) {
    return qc.n_mt_genes === 0 ? "no mitochondrial filter (no MT- genes in the data)" : "no mitochondrial filter applied";
  }
  // Older payloads did not say whether the cutoff was applied.
  return cutoff !== null ? `mitochondrial cutoff < ${cutoff}% (whether it was applied was not reported)` : null;
}

/** The clustering method actually used ("louvain (Leiden unavailable)" included), never a hard-coded "Leiden". */
export function clusteringMethodText(cp: Rec | null | undefined): string {
  const detail = cp && typeof cp.method_detail === "string" && cp.method_detail ? cp.method_detail : "";
  const method = cp && typeof cp.method === "string" && cp.method ? cp.method : "";
  const text = detail || method;
  return text ? `${text.charAt(0).toUpperCase()}${text.slice(1)} clustering` : "clustering method not reported";
}

export interface CellTypeCall {
  label: string;
  /** high | medium | low, when the annotator reported it. */
  confidence?: string;
  status?: string;
  /** Visible qualifier, e.g. "putative, low confidence" or "ambiguous: T cell vs NK cell". */
  qualifier: string;
}

/** A cluster's putative cell type with its confidence / ambiguity, or null for "Cluster N" placeholders. */
export function cellTypeCall(cluster: string, cellTypes: Rec | null | undefined, annotations: Rec | null | undefined): CellTypeCall | null {
  const ann = annotations && typeof annotations[cluster] === "object" ? (annotations[cluster] as Rec) : null;
  const raw = (cellTypes && typeof cellTypes[cluster] === "string" ? cellTypes[cluster] : "") || (ann && typeof ann.label === "string" ? ann.label : "");
  if (!raw || raw === `Cluster ${cluster}`) return null;
  const status = ann && typeof ann.status === "string" ? ann.status : undefined;
  if (status === "unassigned") return null;
  const confidence = ann && typeof ann.confidence === "string" ? ann.confidence : undefined;
  const runner = ann?.runner_up && typeof ann.runner_up === "object" ? ann.runner_up.lineage || ann.runner_up.label : null;
  const parts = ["putative"];
  if (status === "ambiguous") parts.push(runner ? `ambiguous vs ${runner}` : "ambiguous");
  if (confidence) parts.push(`${confidence} confidence`);
  return { label: raw, confidence, status, qualifier: parts.join(", ") };
}

export interface GeoBadge {
  text: string;
  tone: "info" | "warn" | "neutral";
  title?: string;
}

/**
 * The GEO accession check says the declared accession exists on GEO; it does not show that the uploaded file belongs
 * to that series (SCI-014). Worded as such, and an organism mismatch is shown on the badge itself.
 */
export function geoBadge(p: Rec | null | undefined, dsVerified?: boolean): GeoBadge | null {
  if (!p || !p.geo_accession) return null;
  const acc = String(p.geo_accession);
  const found = p.geo_verified === true || dsVerified === true;
  if (!found) return { text: `GEO ${acc} (declared, not checked)`, tone: "neutral" };
  if (p.geo_organism_matches === false) {
    return {
      text: `GEO ${acc} exists — organism differs from the declared one`,
      tone: "warn",
      title: p.geo_title ? String(p.geo_title) : undefined,
    };
  }
  return {
    text: `GEO ${acc} accession found (declared link, file not matched)`,
    tone: "info",
    title: p.geo_title ? String(p.geo_title) : undefined,
  };
}

/** Scale of the expression values in the target table. */
export function expressionScaleText(data: Rec | null | undefined): string {
  if (!data) return "scale not reported";
  if (typeof data.target_expression_note === "string" && /log-normalized/i.test(data.target_expression_note)) return "log-normalized";
  if (Array.isArray(data.transformations)) return "log-normalized";
  return "scale not reported";
}
