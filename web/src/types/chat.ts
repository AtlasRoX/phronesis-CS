import React from "react";
import type { CitationMap } from "./citations";
import type { DatasetPublic, MessageAttachment } from "./datasets";
import type { DrugIntelArtifact } from "./drugs";
import type { LiteratureArtifact } from "./literature";
import type { RunManifest, SourceRecord } from "./runs";
import type { DiseaseTargetsArtifact } from "./diseaseTargets";
import type { PrimeKGArtifact } from "./primekg";
import type { ClinicalTrialsArtifact } from "./trials";
import type { VariantIntelArtifact } from "./variants";
import type { AgenticArtifacts } from "./agentic";

/** Structured outputs rendered beside an answer; features add keys here through their own type files. */
export interface ExecutionArtifacts extends AgenticArtifacts {
  vivarix?: any;
  vivarix_scan?: any; // parameter scan, rendered by the same simulation card
  lumicell?: any;
  discovery?: any;
  clinical_trials?: ClinicalTrialsArtifact;
  drug_intel?: DrugIntelArtifact;
  variant_intel?: VariantIntelArtifact;
  disease_targets?: DiseaseTargetsArtifact;
  primekg?: PrimeKGArtifact;
  literature?: LiteratureArtifact;
  datasets?: DatasetPublic[];
  sources?: SourceRecord[];
  [k: string]: any;
}

/** Whether the requested operation actually ran; kept separate from evidence grounding (audit F02). */
export type TaskStatus = "completed" | "partial" | "needs_input" | "failed" | "cancelled";

export interface TaskOutcome {
  task_status: TaskStatus;
  requested_operation?: string | null;
  executed_operation?: string | null;
  request_match?: boolean;
  reason?: string;
  grounding_status?: "supported" | "partial" | "unsupported" | "contradicted" | "not_checked";
  /** Why a run stopped short of the full request (budget used, sub-questions left open); empty when complete. */
  incomplete_reasons?: string[];
  /** Sub-questions the answer did not fully answer. */
  unresolved?: string[];
}

export type ClaimStatus = "supported" | "contradicted" | "unsupported" | "not_checked";

/** One checked claim from the verifier (audit F01). */
export interface ClaimCheck {
  claim_id: string;
  claim_type?: "clinical" | "mechanistic" | "computational" | "absence" | string;
  claim_text: string;
  evidence?: { source_key?: string; passage?: string; passage_id?: string }[];
  status: ClaimStatus;
  reason?: string;
  /** false: the claim's cited PMID/DOI did not support it, another retrieved source did. */
  cited_source_supported?: boolean | null;
}

export interface VerificationReport {
  passed?: boolean;
  verification_score?: number;
  defects?: { description?: string; claim?: string; [k: string]: any }[];
  claims?: ClaimCheck[];
  [k: string]: any;
}

/** "unreported": the run ended while the step was still running, so whether it completed is unknown (UI-013). */
export type ThoughtStatus = "running" | "done" | "error" | "skipped" | "unreported";

export interface ThoughtStep {
  /** Stable step id; the backend re-sends a step with the same id to update it in place. */
  id?: string;
  stage: string;
  title: string;
  text: string;
  /** Older saved messages have no status; treat those as done. */
  status?: ThoughtStatus;
  elapsed_seconds?: number;
  duration_seconds?: number | null;
  agent?: string;
  extra?: Record<string, any>;
  selected_agents?: string[];
  skipped_agents?: string[];
  rationale?: string;
}

export interface Message {
  id: string;
  sender: "user" | "assistant";
  content: string;
  agent?: string;
  model?: string;
  steps?: { agent: string; role: string }[];
  thoughts?: ThoughtStep[];
  /** The model's own reasoning text, streamed from the provider (empty for non-reasoning models). */
  reasoning?: string;
  selected_agents?: string[];
  duration_seconds?: number;
  verification_report?: VerificationReport;
  task_status?: TaskOutcome;
  /** Orchestration architecture the backend actually ran. */
  framework_used?: string;
  execution_artifacts?: ExecutionArtifacts;
  citations?: CitationMap;
  run_manifest?: RunManifest;
  /** Datasets attached to a user message (ids and display names only). */
  attachments?: MessageAttachment[];
  query_context?: Record<string, any>;
  /** Agentic run id (from the run_control event), for run-control calls and display. */
  run_id?: string;
  /** Background job this answer was streamed from (F13). */
  job_id?: string;
  /** True when the agent rewrote the answer after new evidence or critique (content_reset reason agent_round). */
  answer_revised?: boolean;
  /** Set when browser storage was full and this saved copy lost diagnostics, or plots and passages (UI-008). */
  storage_slimmed?: "diagnostics" | "plots";
  timestamp: string;
}

export interface FrameworkOption {
  id: string;
  title: string;
  thinkingBadge: string;
  badgeColor: string;
  icon: any;
  description: string;
  /** False for architectures the backend does not implement; shown disabled, never sent. */
  available: boolean;
  /** Shown first with a "Recommended" tag and a divider before the fixed architectures. */
  recommended?: boolean;
}

export interface AgentMetadata {
  id: string;
  name: string;
  domain: string;
  description: string;
  tools: string[];
  color: string;
  icon: React.ReactNode;
}
