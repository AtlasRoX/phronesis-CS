// Client for the agentic platform routes: /api/frameworks, /api/agent/runs, /api/jobs and /api/projects
// (docs/plans/agentic-system.md §2 F13, F15, F16). Every call takes the backend URL and the page's
// authHeaders helper, like datasetService, so the bearer token is only ever sent to the committed URL.

import { ApiError, apiFetch, delay, readSseEvents, requestJson, type AuthHeaders, type SseFrame } from "./apiClient.ts";
import type {
  AgentOptions,
  AgentOptionsSettings,
  ApproveBody,
  CreateJobResponse,
  FrameworksResponse,
  JobSummary,
  MemoryFact,
  Project,
  RunControlState,
} from "@/types/agentic";

export { readSseEvents };
export type { AuthHeaders };

/** Every API failure is an ApiError; the old name stays for existing `instanceof AgentApiError` checks. */
export { ApiError as AgentApiError };

const request = <T>(url: string, init: RequestInit): Promise<T> => requestJson<T>(url, init);

const json = (authHeaders: AuthHeaders) => authHeaders({ "Content-Type": "application/json" });
const enc = encodeURIComponent;

/** Pulls a list out of either a bare array or an object wrapping it under one of `keys`. */
function listFrom<T>(body: unknown, keys: string[]): T[] {
  if (Array.isArray(body)) return body as T[];
  if (body && typeof body === "object") {
    for (const k of keys) {
      const v = (body as Record<string, unknown>)[k];
      if (Array.isArray(v)) return v as T[];
    }
  }
  return [];
}

// ---------------------------------------------------------------------------------------------------------------
// Frameworks and request options
// ---------------------------------------------------------------------------------------------------------------

// ---------------------------------------------------------------------------------------------------------------
// Frameworks and request options
// ---------------------------------------------------------------------------------------------------------------

export function fetchFrameworks(backendUrl: string, authHeaders: AuthHeaders, signal?: AbortSignal): Promise<FrameworksResponse> {
  return request<FrameworksResponse>(`${backendUrl}/api/frameworks`, { headers: authHeaders(), signal });
}

export function fetchEffortLevels(backendUrl: string, authHeaders: AuthHeaders, signal?: AbortSignal): Promise<import("@/types/agentic").EffortLevelsResponse> {
  return request<import("@/types/agentic").EffortLevelsResponse>(`${backendUrl}/api/effort-levels`, { headers: authHeaders(), signal });
}

export const EFFORT_LEVELS: {
  id: import("@/types/agentic").EffortLevel;
  name: string;
  tagline: string;
  description: string;
  icon: string;
  badge: string;
  color: string;
}[] = [
  {
    id: "low",
    name: "Fast",
    tagline: "Fast & Focused",
    description: "Minimal thinking overhead, 2 subqueries, 4 literature articles, 1 draft round. Best for quick lookups and fast facts.",
    icon: "Zap",
    badge: "⚡ Fast",
    color: "#10b981",
  },
  {
    id: "medium",
    name: "Balanced",
    tagline: "Standard Deliberation",
    description: "Balanced reasoning, 3 subqueries, 8 literature articles, 2 draft rounds. Recommended default for thorough clinical research.",
    icon: "Scale",
    badge: "⚖️ Balanced",
    color: "#06b6d4",
  },
  {
    id: "high",
    name: "Deep",
    tagline: "Deep Deliberation",
    description: "Multi-angle clinical deliberation, 5 subqueries, 14 literature articles, 3 draft rounds. Recommended for complex multi-target analysis.",
    icon: "Brain",
    badge: "🧠 Deep",
    color: "#8b5cf6",
  },
  {
    id: "ultra",
    name: "Ultra",
    tagline: "Exhaustive Frontier",
    description: "Exhaustive stress-testing, 8 subqueries, 25 literature articles, 5 draft rounds. Maximum rigor for deep trials and discovery hypotheses.",
    icon: "Rocket",
    badge: "🚀 Ultra",
    color: "#f59e0b",
  },
];

export const DEFAULT_AGENT_OPTIONS: AgentOptionsSettings = {
  require_plan_approval: false,
  allow_clarification: true,
  enable_counter_evidence: true,
  enable_hypotheses: true,
  enable_code_analysis: true,
  effort_level: "medium",
  budgetEnabled: false,
  budget: { max_tool_calls: 40, max_llm_calls: 30, max_seconds: 600, max_draft_rounds: 2 },
};

/** Ranges the backend validates (AgentBudgetOptions in app/backend/main.py). */
export const BUDGET_LIMITS = {
  max_tool_calls: { min: 1, max: 200, step: 1, label: "Tool calls" },
  max_llm_calls: { min: 1, max: 120, step: 1, label: "Model calls" },
  max_seconds: { min: 0, max: 7200, step: 10, label: "Wall-clock seconds (0 = no limit)" },
  max_draft_rounds: { min: 0, max: 5, step: 1, label: "Draft rounds" },
} as const;

const clamp = (v: unknown, lo: number, hi: number, fallback: number): number => {
  const n = typeof v === "number" && Number.isFinite(v) ? v : fallback;
  return Math.min(hi, Math.max(lo, Math.round(n)));
};

/** Settings read back from storage, with every field present and in range. */
export function normalizeAgentOptions(saved: unknown): AgentOptionsSettings {
  const s = (saved && typeof saved === "object" ? saved : {}) as Partial<AgentOptionsSettings>;
  const b = (s.budget && typeof s.budget === "object" ? s.budget : {}) as Partial<AgentOptionsSettings["budget"]>;
  const d = DEFAULT_AGENT_OPTIONS;
  const bool = (v: unknown, f: boolean) => (typeof v === "boolean" ? v : f);
  const validEfforts: import("@/types/agentic").EffortLevel[] = ["low", "medium", "high", "ultra"];
  const effort_level = validEfforts.includes(s.effort_level as any)
    ? (s.effort_level as import("@/types/agentic").EffortLevel)
    : d.effort_level;

  return {
    require_plan_approval: bool(s.require_plan_approval, d.require_plan_approval),
    allow_clarification: bool(s.allow_clarification, d.allow_clarification),
    enable_counter_evidence: bool(s.enable_counter_evidence, d.enable_counter_evidence),
    enable_hypotheses: bool(s.enable_hypotheses, d.enable_hypotheses),
    enable_code_analysis: bool(s.enable_code_analysis, d.enable_code_analysis),
    effort_level,
    budgetEnabled: bool(s.budgetEnabled, d.budgetEnabled),
    budget: {
      max_tool_calls: clamp(b.max_tool_calls, 1, 200, d.budget.max_tool_calls),
      max_llm_calls: clamp(b.max_llm_calls, 1, 120, d.budget.max_llm_calls),
      max_seconds: clamp(b.max_seconds, 0, 7200, d.budget.max_seconds),
      max_draft_rounds: clamp(b.max_draft_rounds, 0, 5, d.budget.max_draft_rounds),
    },
  };
}

/** The `agent_options` request field for the stored settings. */
export function toAgentOptions(s: AgentOptionsSettings): AgentOptions {
  const out: AgentOptions = {
    require_plan_approval: s.require_plan_approval,
    allow_clarification: s.allow_clarification,
    enable_counter_evidence: s.enable_counter_evidence,
    enable_hypotheses: s.enable_hypotheses,
    enable_code_analysis: s.enable_code_analysis,
    effort_level: s.effort_level || "medium",
  };
  if (s.budgetEnabled) out.budget = { ...s.budget };
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Run control (F15)
// ---------------------------------------------------------------------------------------------------------------

export function getRun(backendUrl: string, authHeaders: AuthHeaders, runId: string): Promise<RunControlState> {
  return request(`${backendUrl}/api/agent/runs/${enc(runId)}`, { headers: authHeaders() });
}

export function approveRun(backendUrl: string, authHeaders: AuthHeaders, runId: string, body: ApproveBody): Promise<unknown> {
  return request(`${backendUrl}/api/agent/runs/${enc(runId)}/approve`, {
    method: "POST",
    headers: json(authHeaders),
    body: JSON.stringify(body),
  });
}

export function steerRun(backendUrl: string, authHeaders: AuthHeaders, runId: string, message: string): Promise<unknown> {
  return request(`${backendUrl}/api/agent/runs/${enc(runId)}/steer`, {
    method: "POST",
    headers: json(authHeaders),
    body: JSON.stringify({ message }),
  });
}

export function cancelRun(backendUrl: string, authHeaders: AuthHeaders, runId: string): Promise<unknown> {
  return request(`${backendUrl}/api/agent/runs/${enc(runId)}/cancel`, { method: "POST", headers: authHeaders() });
}

// ---------------------------------------------------------------------------------------------------------------
// Background jobs (F13)
// ---------------------------------------------------------------------------------------------------------------

export function createJob(backendUrl: string, authHeaders: AuthHeaders, body: Record<string, unknown>): Promise<CreateJobResponse> {
  return request(`${backendUrl}/api/jobs`, { method: "POST", headers: json(authHeaders), body: JSON.stringify(body) });
}

export async function listJobs(backendUrl: string, authHeaders: AuthHeaders): Promise<JobSummary[]> {
  const body = await request<unknown>(`${backendUrl}/api/jobs`, { headers: authHeaders() });
  return listFrom<JobSummary>(body, ["jobs", "items"]).filter((j) => j && typeof j.job_id === "string");
}

export function getJob(backendUrl: string, authHeaders: AuthHeaders, jobId: string, signal?: AbortSignal): Promise<JobSummary> {
  return request(`${backendUrl}/api/jobs/${enc(jobId)}`, { headers: authHeaders(), signal });
}

export function cancelJob(backendUrl: string, authHeaders: AuthHeaders, jobId: string): Promise<unknown> {
  return request(`${backendUrl}/api/jobs/${enc(jobId)}/cancel`, { method: "POST", headers: authHeaders() });
}

export function resumeJob(backendUrl: string, authHeaders: AuthHeaders, jobId: string): Promise<unknown> {
  return request(`${backendUrl}/api/jobs/${enc(jobId)}/resume`, { method: "POST", headers: authHeaders() });
}

export function deleteJob(backendUrl: string, authHeaders: AuthHeaders, jobId: string): Promise<unknown> {
  return request(`${backendUrl}/api/jobs/${enc(jobId)}`, { method: "DELETE", headers: authHeaders() });
}

export interface JobStreamOptions {
  /** Reconnect attempts after a dropped or stalled connection (reset whenever new events arrive). */
  maxReconnects?: number;
  /** First reconnect delay; doubles per consecutive failure. */
  reconnectDelayMs?: number;
  /** Stall timeout per connection (see readSseEvents). */
  stallMs?: number;
}

/** The log sequence number of a job event: the SSE `id:` line, else the event's own `seq`. */
function seqOf(ev: unknown, frame: SseFrame): number | null {
  if (frame.id && /^\d+$/.test(frame.id)) return Number(frame.id);
  const seq = ev && typeof ev === "object" ? (ev as { seq?: unknown }).seq : undefined;
  return typeof seq === "number" && Number.isFinite(seq) ? seq : null;
}

/** Some logs wrap each event as {seq, event}; unwrap so callers see the chat event shape. */
function unwrapJobEvent(ev: unknown): unknown {
  if (ev && typeof ev === "object") {
    const e = ev as { type?: unknown; event?: unknown };
    if (!e.type && e.event && typeof e.event === "object") return e.event;
  }
  return ev;
}

/**
 * Follows a job's event log: replays events after `after`, then follows live ones until done/error.
 * Events carry the chat stream's types. onEvent returns true to stop. Resolves when the stream ends.
 *
 * A dropped or stalled connection is reopened with `after=<last seen seq>`, so no event is replayed twice
 * and none is missed. An HTTP error (401, 404, ...) is final and thrown at once.
 */
export async function streamJobEvents(
  backendUrl: string,
  authHeaders: AuthHeaders,
  jobId: string,
  after: number,
  onEvent: (event: unknown) => boolean | void,
  signal?: AbortSignal,
  options: JobStreamOptions = {}
): Promise<void> {
  const maxReconnects = options.maxReconnects ?? 3;
  const baseDelay = options.reconnectDelayMs ?? 1000;
  let last = Math.max(0, Math.floor(after));
  let failures = 0;
  for (;;) {
    let progressed = false;
    let stopped = false;
    try {
      const res = await apiFetch(`${backendUrl}/api/jobs/${enc(jobId)}/events?after=${last}`, {
        headers: authHeaders({ Accept: "text/event-stream" }),
        signal,
      });
      if (!res.body) throw new ApiError(0, "The job event stream had no body.");
      await readSseEvents(
        res.body,
        (ev, frame) => {
          const seq = seqOf(ev, frame);
          if (seq !== null && seq > last) {
            last = seq;
            progressed = true;
          }
          if (onEvent(unwrapJobEvent(ev))) {
            stopped = true;
            return true;
          }
          return false;
        },
        { stallMs: options.stallMs }
      );
      // A clean end with nothing new means the job is no longer active and its log is drained.
      if (stopped || !progressed) return;
      failures = 0;
    } catch (err) {
      if (signal?.aborted || stopped) throw err;
      if (err instanceof ApiError && err.status > 0) throw err;
      if (progressed) failures = 0;
      failures += 1;
      if (failures > maxReconnects) throw err;
    }
    await delay(baseDelay * 2 ** Math.max(0, failures - 1), signal);
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Projects and project memory (F16)
// ---------------------------------------------------------------------------------------------------------------

/** Accepts `id` or `project_id` from the API. */
function normalizeProject(raw: unknown): Project | null {
  if (!raw || typeof raw !== "object") return null;
  const p = raw as Record<string, unknown>;
  const id = typeof p.id === "string" ? p.id : typeof p.project_id === "string" ? p.project_id : "";
  if (!id) return null;
  return { ...p, id, name: typeof p.name === "string" && p.name ? p.name : id } as Project;
}

function normalizeFact(raw: unknown): MemoryFact | null {
  if (!raw || typeof raw !== "object") return null;
  const f = raw as Record<string, unknown>;
  const id = typeof f.id === "string" || typeof f.id === "number" ? String(f.id) : typeof f.fact_id === "string" ? f.fact_id : "";
  return id ? ({ ...f, id } as MemoryFact) : null;
}

export async function listProjects(backendUrl: string, authHeaders: AuthHeaders): Promise<Project[]> {
  const body = await request<unknown>(`${backendUrl}/api/projects`, { headers: authHeaders() });
  return listFrom<unknown>(body, ["projects", "items"]).map(normalizeProject).filter((p): p is Project => p !== null);
}

export async function createProject(
  backendUrl: string,
  authHeaders: AuthHeaders,
  name: string,
  description = ""
): Promise<Project> {
  const body = await request<{ project?: unknown } | null>(`${backendUrl}/api/projects`, {
    method: "POST",
    headers: json(authHeaders),
    body: JSON.stringify({ name, description }),
  });
  const p = normalizeProject(body?.project ?? body);
  if (!p) throw new ApiError(0, "The server did not return the new project.");
  return p;
}

export async function getProject(backendUrl: string, authHeaders: AuthHeaders, projectId: string): Promise<Project | null> {
  const body = await request<{ project?: unknown } | null>(`${backendUrl}/api/projects/${enc(projectId)}`, { headers: authHeaders() });
  return normalizeProject(body?.project ?? body);
}

export function patchProject(
  backendUrl: string,
  authHeaders: AuthHeaders,
  projectId: string,
  patch: { name?: string; description?: string }
): Promise<unknown> {
  return request(`${backendUrl}/api/projects/${enc(projectId)}`, {
    method: "PATCH",
    headers: json(authHeaders),
    body: JSON.stringify(patch),
  });
}

export function deleteProject(backendUrl: string, authHeaders: AuthHeaders, projectId: string): Promise<unknown> {
  return request(`${backendUrl}/api/projects/${enc(projectId)}`, { method: "DELETE", headers: authHeaders() });
}

export async function getProjectMemory(
  backendUrl: string,
  authHeaders: AuthHeaders,
  projectId: string,
  query = ""
): Promise<MemoryFact[]> {
  const q = query.trim() ? `?query=${enc(query.trim())}` : "";
  const body = await request<unknown>(`${backendUrl}/api/projects/${enc(projectId)}/memory${q}`, { headers: authHeaders() });
  return listFrom<unknown>(body, ["facts", "items", "memory", "recalled"]).map(normalizeFact).filter((f): f is MemoryFact => f !== null);
}

export function patchFact(
  backendUrl: string,
  authHeaders: AuthHeaders,
  projectId: string,
  factId: string,
  patch: { invalid?: boolean; object?: string; confidence?: number }
): Promise<unknown> {
  return request(`${backendUrl}/api/projects/${enc(projectId)}/memory/${enc(factId)}`, {
    method: "PATCH",
    headers: json(authHeaders),
    body: JSON.stringify(patch),
  });
}

export function deleteFact(backendUrl: string, authHeaders: AuthHeaders, projectId: string, factId: string): Promise<unknown> {
  return request(`${backendUrl}/api/projects/${enc(projectId)}/memory/${enc(factId)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
}

export interface TitleResponse {
  title: string;
  method: "ai" | "heuristic";
}

/**
 * Request an intelligent, biomedical-aware dynamic conversation title from the backend.
 * Falls back to local heuristic extraction if network or model fails.
 */
export function fetchConversationTitle(
  backendUrl: string,
  authHeaders: AuthHeaders,
  message: string,
  model?: string,
  apiKey?: string,
  signal?: AbortSignal
): Promise<TitleResponse> {
  return request<TitleResponse>(`${backendUrl}/api/chat/title`, {
    method: "POST",
    headers: json(authHeaders),
    body: JSON.stringify({
      message,
      model: model || undefined,
      api_key: apiKey || undefined,
    }),
    signal,
  });
}

