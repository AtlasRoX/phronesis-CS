// Query plan (plan step extra) types (owned by ENTITIES, feature 3). Additive changes only.

export type QueryEntityType = "gene" | "drug" | "disease" | "variant";

export interface QueryEntity {
  type: QueryEntityType;
  mention: string;
  label: string;
  ids: Record<string, string>;
  source: string;
  matched_on?: string | null;
  gene?: string | null;
  variant_kind?: string | null;
  validated: boolean;
  ambiguous_with?: string[];
  span?: [number, number];
  aliases?: string[];
  protein_change?: string | null;
}

export interface SourceDecision {
  name: string;
  run: boolean;
  reason: string;
}

export interface QueryPlanExtra {
  entities: QueryEntity[];
  intents: string[];
  sources: SourceDecision[];
  planner: "deterministic" | "llm" | "llm_unavailable";
  normalization: "resolved" | "partial" | "offline_lexicon" | "skipped";
  latency_ms: number;
}
