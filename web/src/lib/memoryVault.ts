// Memory Vault display helpers (UI-005), aligned with the real API contract:
//   GET  /api/memory/stats    -> {status, db_size_kb, total_nodes, total_threads, total_verified_empirical,
//                                 total_contradictions_resolved}   (phronesis/utils/memory/clinical_memory.py)
//   GET  /api/memory/entities -> {entities: [{entity, mention_count, first_seen, last_seen}], count}
//   POST /api/memory/search   -> {results: [{node_id, thread_id, memory_type, content, verification_status, score, sources}]}
// Framework-free so `node --test` can import it.

function n(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export interface StatTile {
  label: string;
  value: string;
  sub: string;
}

/** Tiles for the vault header; a field the server did not send is not shown as 0. */
export function memoryStatTiles(stats: Record<string, unknown> | null | undefined): StatTile[] {
  if (!stats) return [];
  const tiles: StatTile[] = [];
  const nodes = n(stats.total_nodes);
  if (nodes !== null) tiles.push({ label: "Stored memories", value: String(nodes), sub: "questions, answers, findings" });
  const threads = n(stats.total_threads);
  if (threads !== null) tiles.push({ label: "Investigations", value: String(threads), sub: "threads with server memory" });
  const verified = n(stats.total_verified_empirical);
  if (verified !== null) {
    tiles.push({ label: "Tool-grounded", value: String(verified), sub: "marked empirical by the system" });
  }
  const contradictions = n(stats.total_contradictions_resolved);
  if (contradictions !== null) tiles.push({ label: "Contradictions", value: String(contradictions), sub: "flagged between memories" });
  const kb = n(stats.db_size_kb);
  if (kb !== null) tiles.push({ label: "Database size", value: `${kb} KB`, sub: "server memory store (all users)" });
  return tiles;
}

/** Count for the header badge: stored memories (older servers: messages). */
export function memoryBadgeCount(stats: Record<string, unknown> | null | undefined): number {
  if (!stats) return 0;
  return n(stats.total_nodes) ?? n(stats.total_messages) ?? 0;
}

const TYPE_LABELS: Record<string, string> = {
  USER_INSTRUCTION: "Instruction",
  QUERY_HISTORY: "Question",
  WORKING_HYPOTHESIS: "Working hypothesis",
  RETRIEVED_EVIDENCE: "Retrieved evidence",
  COMPUTED_RESULT: "Computed result",
  FAILURE_LESSON: "Failure lesson",
  DIALOGUE_TURN: "Dialogue turn",
};

export function memoryTypeLabel(type: unknown): string {
  const t = typeof type === "string" ? type : "";
  return TYPE_LABELS[t] || (t ? t.replace(/_/g, " ").toLowerCase() : "memory");
}

const STATUS_LABELS: Record<string, { text: string; tone: "ok" | "warn" | "bad" | "neutral" }> = {
  VERIFIED_EMPIRICAL: { text: "from tool output (not independently verified)", tone: "ok" },
  PROVISIONAL: { text: "provisional", tone: "warn" },
  REFUTED: { text: "refuted", tone: "bad" },
  UNVERIFIED_GENERATED: { text: "model-generated, unverified", tone: "warn" },
};

/** Verification status of a memory hit, worded for what it is (UI-005, PRV-006). */
export function verificationStatusLabel(status: unknown): { text: string; tone: "ok" | "warn" | "bad" | "neutral" } {
  const s = typeof status === "string" ? status : "";
  return STATUS_LABELS[s] || { text: s ? s.replace(/_/g, " ").toLowerCase() : "status not reported", tone: "neutral" };
}

/** "3 mentions · last 2026-10-17" for an entity of the vault. */
export function entitySummary(ent: Record<string, unknown> | null | undefined): string {
  if (!ent) return "";
  const count = n(ent.mention_count);
  const last = typeof ent.last_seen === "string" ? ent.last_seen.slice(0, 10) : "";
  return [count !== null ? `${count} mention${count === 1 ? "" : "s"}` : "", last ? `last ${last}` : ""].filter(Boolean).join(" · ");
}
