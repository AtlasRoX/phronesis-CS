// Response and stream-event shapes of the core API routes the chat page calls (/api/health, /api/models,
// /api/memory/*, /api/chat[/stream]). Every field is optional: the page renders whatever the server sends.

import type { ApprovalRequestEvent } from "./agentic";
import type { Message, ThoughtStep } from "./chat";

/** GET /api/health (public: no token is sent). */
export interface BackendHealth {
  status?: string;
  auth?: { mode?: string; [k: string]: unknown };
  llm_providers?: Record<string, { available?: boolean; [k: string]: unknown } | undefined>;
  [k: string]: unknown;
}

export interface ModelInfo {
  id: string;
  name?: string;
  provider?: string;
  description?: string;
}

/** GET /api/models */
export interface ModelsResponse {
  models?: unknown[];
}

/** GET /api/memory/stats (clinical_memory.get_memory_stats). Legacy fields are optional and no longer sent. */
export interface MemoryStats {
  status?: string;
  db_size_kb?: number;
  total_nodes?: number;
  total_threads?: number;
  total_verified_empirical?: number;
  total_contradictions_resolved?: number;
  /** @deprecated never returned by the current API */
  total_entities_vault?: number;
  /** @deprecated never returned by the current API */
  total_messages?: number;
  /** @deprecated never returned by the current API */
  total_episodes?: number;
  [k: string]: unknown;
}

/** One entity referenced in the user's memories (clinical_memory.get_entity_vault). */
export interface MemoryEntity {
  entity?: string;
  mention_count?: number;
  first_seen?: string;
  last_seen?: string;
  [k: string]: unknown;
}

/** GET /api/memory/entities */
export interface MemoryEntitiesResponse {
  entities?: MemoryEntity[];
  count?: number;
}

/** One hit of clinical_memory.search_memory. */
export interface MemorySearchResult {
  node_id?: string;
  thread_id?: string;
  memory_type?: string;
  verification_status?: string;
  score?: number | string;
  content?: string;
  sources?: unknown;
  [k: string]: unknown;
}

/** POST /api/memory/search */
export interface MemorySearchResponse {
  results?: MemorySearchResult[];
}

/** The final answer: the body of POST /api/chat, the `result` of a stream's done event and of a finished job. */
export interface ChatResult {
  response?: string;
  model_used?: string;
  steps?: Message["steps"];
  thoughts?: ThoughtStep[];
  reasoning?: string;
  selected_agents?: string[];
  duration_seconds?: number;
  verification_report?: Message["verification_report"];
  task_status?: Message["task_status"] | null;
  framework_used?: string | null;
  execution_artifacts?: Message["execution_artifacts"];
  citations?: Message["citations"] | null;
  run_manifest?: Message["run_manifest"] | null;
  query_context?: Message["query_context"] | null;
  [k: string]: unknown;
}

/** Events of /api/chat/stream and of a background job's event log. Unknown types are ignored. */
export type StreamEvent =
  | { type: "thought"; data: ThoughtStep }
  | { type: "reasoning_delta"; delta?: string }
  | { type: "content_delta"; delta?: string }
  | { type: "content_reset"; reason?: string }
  | { type: "run_control"; run_id?: string }
  | (Omit<ApprovalRequestEvent, "run_id"> & { run_id?: string })
  | { type: "done"; result?: ChatResult | null }
  | { type: "error"; detail?: string };
