// Reader-facing names for evidence keys ("agent.gaps.4.lithoscribe" -> "Europe PMC (gap search)"). The backend sends
// `evidence_labels` next to `evidence_keys` when it can; this is the fallback for keys without one.

/** Public names of tool and source keys (mirrors phronesis/sources/names.py `public`). */
export const SOURCE_NAMES: Record<string, string> = {
  synapta: "OpenTargets/KEGG",
  lithoscribe: "Europe PMC",
  websurfer: "web search",
  vivarix: "COPASI simulation",
  copasi: "COPASI simulation",
  lumicell: "single-cell analysis",
  clinical_trials: "ClinicalTrials.gov",
  clinicaltrials: "ClinicalTrials.gov",
  chembl: "ChEMBL",
  openfda: "openFDA",
  disease_targets: "OpenTargets",
  opentargets: "OpenTargets",
  primekg: "PrimeKG",
  variant_intel: "ClinVar/CIViC/cBioPortal/gnomAD",
  variants: "ClinVar/CIViC/cBioPortal/gnomAD",
  structure_intel: "AlphaFold DB / RCSB PDB",
  structintel: "AlphaFold DB / RCSB PDB",
  dgidb: "DGIdb",
  pharos: "Pharos",
  bindingdb: "BindingDB",
  pubtator: "PubTator3/LitVar2",
  synlethdb: "SynLethDB",
  gwas: "GWAS Catalog",
  gtex: "GTEx",
  string_ppi: "STRING",
  string: "STRING",
  reactome: "Reactome",
  pubchem: "PubChem",
  depmap: "DepMap",
  preprints: "bioRxiv/medRxiv",
  clinvar: "ClinVar",
  civic: "CIViC",
  cbioportal: "cBioPortal",
  gnomad: "gnomAD",
  kegg: "KEGG",
  europepmc: "Europe PMC",
};

const PHASE_NAMES: Record<string, string> = {
  gaps: "gap search",
  plan_execute: "plan step",
  plan_join: "plan step",
  supervisor: "supervisor",
  swarm: "swarm",
  debate: "debate",
  counter_evidence: "counter-evidence",
  counter_evidence_recheck: "counter-evidence",
  hypothesis: "hypothesis test",
  calibrate: "calibration",
  analyst: "analysis",
  decompose: "decomposition",
};

const AGENT_KEY_RE = /^agent\.([a-z_]+)\.(\d+)\.([A-Za-z0-9_-]+)$/;

/** Public name of a tool or source key; the key, readable, when unknown. */
export function sourceName(key: string): string {
  const k = key.trim();
  const lower = k.toLowerCase();
  return SOURCE_NAMES[lower] || SOURCE_NAMES[lower.split("_")[0]] || k.replace(/_/g, " ");
}

/** "agent.gaps.4.lithoscribe" -> "Europe PMC (gap search)"; "tool:chembl" -> "ChEMBL"; "synlethdb" -> "SynLethDB".
 * Citation-like keys ("PMID:123", "DOI:...") are returned unchanged. */
export function prettyEvidenceKey(key: unknown): string {
  const k = typeof key === "string" ? key.trim() : String(key ?? "").trim();
  if (!k) return "";
  const m = AGENT_KEY_RE.exec(k);
  if (m) {
    const where = PHASE_NAMES[m[1]] || m[1].replace(/_/g, " ");
    return `${sourceName(m[3])} (${where})`;
  }
  const bare = k.startsWith("tool:") ? k.slice(5) : k;
  return bare && !bare.includes(":") ? sourceName(bare) : k;
}

/** Display names for `keys`: the backend's parallel `labels` where present, else the prettified key. Duplicate
 * names collapse to one ("Europe PMC (gap round 1)" twice reads once). */
export function evidenceDisplayNames(keys: unknown, labels?: unknown): string[] {
  const ks = Array.isArray(keys) ? keys : [];
  const ls = Array.isArray(labels) ? labels : [];
  const out: string[] = [];
  ks.forEach((k, i) => {
    const given = typeof ls[i] === "string" ? (ls[i] as string).trim() : "";
    const name = given || prettyEvidenceKey(k);
    if (name && !out.includes(name)) out.push(name);
  });
  return out;
}
