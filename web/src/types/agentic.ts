// Types for the agentic system (docs/plans/agentic-system.md §3 artifact contracts, §2 F13/F15/F16 routes).
// Every field is optional: cards must render whatever subset the backend sends.

// ---------------------------------------------------------------------------------------------------------------
// Frameworks
// ---------------------------------------------------------------------------------------------------------------

export type FrameworkId =
  | "auto"
  | "authoritative-react-compiler"
  | "agentic-plan-execute"
  | "multi-agent-supervisor"
  | "multi-agent-swarm"
  | "multi-agent-debate";

export interface FrameworkInfo {
  id: string;
  description?: string;
  agentic?: boolean;
}

export interface FrameworksResponse {
  default?: string;
  frameworks?: FrameworkInfo[];
}

// ---------------------------------------------------------------------------------------------------------------
// Effort Levels & Thinking Engine
// ---------------------------------------------------------------------------------------------------------------

export type EffortLevel = "low" | "medium" | "high" | "ultra";

export interface EffortProfileInfo {
  level: EffortLevel;
  name: string;
  tagline: string;
  description: string;
  reasoning_effort: string;
  thinking_budget_tokens: number;
  max_subqueries: number;
  max_passages: number;
  max_draft_rounds: number;
  max_tool_calls: number;
  max_llm_calls: number;
  max_seconds: number;
  enable_thinking: boolean;
  deliberation_depth: string;
  icon: string;
}

export interface EffortLevelsResponse {
  default: EffortLevel;
  levels: EffortProfileInfo[];
}

// ---------------------------------------------------------------------------------------------------------------
// Request options (ChatRequest.agent_options in app/backend/main.py)
// ---------------------------------------------------------------------------------------------------------------

export interface AgentBudgetOptions {
  max_tool_calls?: number; // 1..200
  max_llm_calls?: number; // 1..120
  max_tokens?: number; // 1000..2,000,000
  max_seconds?: number; // 0 (no limit)..7200
  max_draft_rounds?: number; // 0..5
}

export interface AgentOptions {
  require_plan_approval?: boolean;
  allow_clarification?: boolean;
  enable_counter_evidence?: boolean;
  enable_hypotheses?: boolean;
  enable_code_analysis?: boolean;
  effort_level?: EffortLevel;
  budget?: AgentBudgetOptions;
}

/** What the settings panel stores; `budgetEnabled` off means "use the server's limits". */
export interface AgentOptionsSettings {
  require_plan_approval: boolean;
  allow_clarification: boolean;
  enable_counter_evidence: boolean;
  enable_hypotheses: boolean;
  enable_code_analysis: boolean;
  effort_level: EffortLevel;
  budgetEnabled: boolean;
  budget: Required<Pick<AgentBudgetOptions, "max_tool_calls" | "max_llm_calls" | "max_seconds" | "max_draft_rounds">>;
}

// ---------------------------------------------------------------------------------------------------------------
// SSE events added by the agentic runtime
// ---------------------------------------------------------------------------------------------------------------

export interface RunControlEvent {
  type: "run_control";
  run_id: string;
  framework?: string;
  interactive?: boolean;
}

export interface ApprovalSource {
  key: string;
  label?: string;
  run?: boolean;
  reason?: string;
}

export interface ApprovalRequestEvent {
  type: "approval_request";
  run_id: string;
  approval_id: string;
  kind: "plan" | "dag" | string;
  payload?: { sources?: ApprovalSource[]; nodes?: PlanNode[]; [k: string]: any };
}

/** An approval request as held by the UI, with the time it arrived (for the 5-minute auto-continue note). */
export interface PendingApproval extends ApprovalRequestEvent {
  received_at: number;
}

// ---------------------------------------------------------------------------------------------------------------
// Run control routes (/api/agent/runs/{run_id})
// ---------------------------------------------------------------------------------------------------------------

export interface RunControlState {
  run_id: string;
  interactive?: boolean;
  cancelled?: boolean;
  pending?: { approval_id: string; kind: string; payload?: any; created?: number }[];
  decisions?: Record<string, any>[];
  queued_steering?: number;
}

export interface ApproveBody {
  approval_id: string;
  approved: boolean;
  payload?: Record<string, any>;
  message?: string;
}

// ---------------------------------------------------------------------------------------------------------------
// Jobs (F13)
// ---------------------------------------------------------------------------------------------------------------

export type JobStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | string;

export interface JobSummary {
  job_id: string;
  run_id?: string;
  status?: JobStatus;
  created?: string | number;
  updated?: string | number;
  created_at?: string | number;
  updated_at?: string | number;
  request?: { message?: string; framework?: string; thread_id?: string; [k: string]: any };
  message?: string;
  title?: string;
  error?: string;
  result?: any;
  [k: string]: any;
}

export interface CreateJobResponse {
  job_id: string;
  run_id?: string;
}

// ---------------------------------------------------------------------------------------------------------------
// Projects (F16)
// ---------------------------------------------------------------------------------------------------------------

export interface Project {
  id: string;
  name: string;
  description?: string;
  created?: string | number;
  updated?: string | number;
  fact_count?: number;
  [k: string]: any;
}

export interface MemoryFact {
  id: string;
  subject?: string;
  relation?: string;
  object?: string;
  valid_from?: string | number | null;
  invalid_at?: string | number | null;
  recorded_at?: string | number | null;
  source?: string;
  confidence?: number;
  invalid?: boolean;
  tags?: string[];
  [k: string]: any;
}

// ---------------------------------------------------------------------------------------------------------------
// §3 artifacts
// ---------------------------------------------------------------------------------------------------------------

export interface SubQuestion {
  id?: string;
  text?: string;
  focus?: string;
  depends_on?: string[];
  status?: "pending" | "answered" | "partial" | "missing" | string;
  evidence_keys?: string[];
  note?: string;
}

export interface AgentBudgetState {
  /** Backend budget.to_dict() uses unprefixed keys (tool_calls, llm_calls, tokens, seconds, draft_rounds, plan_nodes). */
  limits?: Record<string, number | null | undefined>;
  used?: { tool_calls?: number; llm_calls?: number; tokens_estimated?: number; seconds?: number; [k: string]: number | undefined };
  exhausted?: boolean;
  exhausted_reason?: string | null;
  by_phase?: Record<string, any>;
}

export interface AgentTraceRow {
  phase?: string;
  tool?: string;
  purpose?: string;
  success?: boolean;
  error_type?: string | null;
  seconds?: number;
}

export interface AgentPhaseRow {
  phase?: string;
  stage?: string;
  status?: string;
  seconds?: number;
}

export interface AgentRunArtifact {
  framework?: string;
  run_id?: string;
  sub_questions?: SubQuestion[];
  budget?: AgentBudgetState;
  trace?: AgentTraceRow[];
  decisions?: Record<string, any>[];
  steering?: (string | Record<string, any>)[];
  phases?: AgentPhaseRow[];
  draft_rounds?: number;
}

export interface PlanNode {
  id: string;
  tool?: string;
  args?: Record<string, any>;
  depends_on?: string[];
  purpose?: string;
  status?: "done" | "error" | "skipped" | "pending" | "running" | string;
  result_key?: string;
  error?: string;
  seconds?: number;
  run?: boolean;
}

export interface AgentPlanArtifact {
  planner?: "llm" | "none" | string;
  rationale?: string;
  replans?: number;
  nodes?: PlanNode[];
  rejected?: { node?: string | PlanNode; reason?: string }[];
}

export interface ClarificationArtifact {
  question?: string;
  options?: string[];
  reason?: string;
  signals?: string[];
}

export interface PlanApprovalArtifact {
  approval_id?: string;
  status?: "approved" | "edited" | "rejected" | "auto" | "timeout" | string;
  sources?: ApprovalSource[];
}

export interface Hypothesis {
  id?: string;
  statement?: string;
  mechanism?: string;
  intervention?: { kind?: string; parameter?: string; factor?: number };
  readout?: string;
  prediction?: "increase" | "decrease" | string;
  result?: { baseline?: number | null; perturbed?: number | null; log2_fold_change?: number | null; supports?: boolean | null } | null;
  elo?: number;
  rank?: number;
  evidence_keys?: string[];
}

export interface HypothesesArtifact {
  target?: string;
  model_id?: string | null;
  method?: string;
  experiments_run?: number;
  hypotheses?: Hypothesis[];
  note?: string;
}

export interface EvidenceConflict {
  claim?: string;
  passage?: string;
  source_key?: string;
  pmid?: string | number | null;
  doi?: string | null;
  label?: "contradicts" | "supports" | "neutral" | string;
  likert?: number | null;
  score?: number | null;
}

export interface EvidenceConflictsArtifact {
  queries?: string[];
  claims_checked?: number;
  classifier?: "nli" | "llm" | "heuristic" | string;
  conflicts?: EvidenceConflict[];
}

export interface ClaimConfidenceRow {
  claim_id?: string;
  text?: string;
  confidence?: number;
  band?: "high" | "medium" | "low" | string;
  features?: Record<string, any>;
  nli_label?: string | null;
}

export interface ClaimConfidenceArtifact {
  answer_confidence?: number;
  method?: string;
  calibration?: { temperature?: number | null; conformal_threshold?: number | null; [k: string]: any };
  claims?: ClaimConfidenceRow[];
}

export interface SandboxTable {
  name?: string;
  columns?: string[];
  rows?: any[][] | Record<string, any>[];
}

export interface SandboxAnalysisArtifact {
  status?: "ok" | "error" | "disabled" | string;
  iterations?: number;
  code?: string;
  stdout?: string;
  result?: any;
  error?: string | null;
  dataset?: string | Record<string, any> | null;
  figures?: { name?: string; png_base64?: string }[];
  tables?: SandboxTable[];
  policy?: { timeout_s?: number; network?: string; isolation?: string; [k: string]: any };
}

export type Identifiability = "identifiable" | "practically_non_identifiable" | "structurally_non_identifiable" | string;

export interface CalibrationParameter {
  name?: string;
  initial?: number | null;
  estimate?: number | null;
  ci_low?: number | null;
  ci_high?: number | null;
  identifiability?: Identifiability;
}

export interface ModelCalibrationArtifact {
  model_id?: string;
  dataset?: string | Record<string, any> | null;
  backend?: string;
  status?: string;
  objective?: { chi2?: number | null; rmse?: number | null; n_points?: number | null };
  parameters?: CalibrationParameter[];
  profiles?: Record<string, { value?: number; chi2?: number }[]>;
  fit?: Record<string, { time?: number[]; observed?: (number | null)[]; simulated?: (number | null)[] }>;
}

export interface PdbEntry {
  pdb_id?: string;
  title?: string;
  method?: string;
  resolution?: number | null;
  ligands?: (string | { id?: string; name?: string })[];
  url?: string;
}

export interface StructureIntelArtifact {
  uniprot?: string;
  gene?: string;
  alphafold?: {
    model_id?: string;
    mean_plddt?: number | null;
    plddt_band?: string;
    url?: string;
    pdb_url?: string;
    version?: string | number;
  } | null;
  pdb_entries?: PdbEntry[];
  docking?: Record<string, any> | null;
  generated?: any[] | null;
}

export interface OmicsSection {
  status?: string;
  items?: Record<string, any>[];
  note?: string;
  scope_note?: string;
  [k: string]: any;
}

export interface OmicsIntelArtifact {
  gwas?: OmicsSection;
  gtex?: OmicsSection;
  string?: OmicsSection;
  reactome?: OmicsSection;
  pubchem?: OmicsSection;
  preprints?: OmicsSection;
  depmap?: OmicsSection;
  [k: string]: OmicsSection | undefined;
}

export interface ProjectMemoryArtifact {
  project_id?: string;
  recalled?: MemoryFact[];
  written?: number;
  invalidated?: number;
}

export interface AgentDialogueTurn {
  agent?: string;
  action?: string;
  tool?: string | null;
  summary?: string;
  round?: number;
}

export interface AgentDialogueArtifact {
  topology?: string;
  turns?: AgentDialogueTurn[];
}

/** The agentic keys added to ExecutionArtifacts. */
/** Auto routing: which architecture was chosen for the question and why (phronesis/agentic/router.py). */
export interface FrameworkRoutingArtifact {
  requested: "auto";
  chosen: FrameworkId;
  label?: string;
  rule?: string;
  reason?: string;
  signals?: Record<string, unknown>;
  candidates?: FrameworkId[];
  tie_break?: "none" | "llm" | "llm_failed";
  /** Probability of the chosen architecture (softmax over the scores). */
  confidence?: number;
  probabilities?: Partial<Record<FrameworkId, number>>;
  /** The factors behind the chosen score, strongest first (weight = contribution to the score). */
  factors?: { factor: string; label: string; weight: number }[];
  features?: Record<string, number>;
  semantic?: { used: boolean; samples?: number; valid_samples?: number; question_kind?: string | null };
  /** Score nudges from past outcomes on similar questions. */
  learned?: Partial<Record<FrameworkId, { delta: number; runs: number; quality: number }>>;
  guardrail?: string | null;
  bucket?: string;
  escalation?: { to: FrameworkId; triggers: string[]; status: "escalated" | "skipped"; reason?: string } | null;
  /** The scores are heuristic suitability, not calibrated probabilities. */
  scores_note?: string;
  /** Extra cost of the chosen architecture (Debate: critic rounds and rewrites). */
  cost_note?: string;
}

export interface AgenticArtifacts {
  agent_run?: AgentRunArtifact;
  framework_routing?: FrameworkRoutingArtifact;
  agent_plan?: AgentPlanArtifact;
  clarification?: ClarificationArtifact;
  plan_approval?: PlanApprovalArtifact;
  hypotheses?: HypothesesArtifact;
  evidence_conflicts?: EvidenceConflictsArtifact;
  claim_confidence?: ClaimConfidenceArtifact;
  sandbox_analysis?: SandboxAnalysisArtifact;
  model_calibration?: ModelCalibrationArtifact;
  structure_intel?: StructureIntelArtifact;
  omics_intel?: OmicsIntelArtifact;
  project_memory?: ProjectMemoryArtifact;
  agent_dialogue?: AgentDialogueArtifact;
}
