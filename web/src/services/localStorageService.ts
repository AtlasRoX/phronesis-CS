/**
 * Phronesis Local Storage Service
 * Persistence for investigations (threads), chat message histories and user preferences in the browser.
 *
 * Privacy (UI-007): conversations can hold clinical details, so history is
 * - namespaced by backend origin + a fingerprint of the access token, so another user (token) or another backend on
 *   the same browser never sees or deletes this history;
 * - kept where the user chose: on this device (localStorage), for this browser session only (sessionStorage), or not
 *   at all (memory of the open tab);
 * - pruned after a retention period (default 30 days) unless the user keeps it indefinitely.
 *
 * Quota (UI-008): when a thread does not fit, messages are slimmed and, as a last resort, an old thread's messages are
 * removed. Every such step is reported (return value + listeners) and marked on the data, never silent.
 *
 * Imports are type-only and relative, so `node --test` can load this module with a fake `window`.
 */

import type { Message } from "../types/chat";
import type { AgentOptionsSettings } from "../types/agentic";

export interface InvestigationItem {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  framework?: string;
  model?: string;
  messageCount: number;
  preview?: string;
  /** True when the user explicitly renamed this investigation manually */
  isCustomTitle?: boolean;
  /** Set when this thread's messages were removed to free browser storage (UI-008). */
  messagesPruned?: boolean;
}

export interface UserSettings {
  apiKey?: string;
  selectedModel?: string;
  selectedFramework?: string;
  isWebSearchEnabled?: boolean;
  theme?: "dark" | "light";
  /** Agentic request settings (agent_options); normalised on read by agentService.normalizeAgentOptions. */
  agentOptions?: AgentOptionsSettings;
  /** Selected research project (prj_...), sent as project_id with agentic requests. */
  projectId?: string;
}

/** Where conversation history is kept. */
export type HistoryMode = "device" | "session" | "off";

export interface HistoryPrefs {
  mode: HistoryMode;
  /** Investigations not updated for this many days are removed; 0 keeps them until cleared. */
  retentionDays: number;
}

export const DEFAULT_HISTORY_PREFS: HistoryPrefs = { mode: "device", retentionDays: 30 };

/** What a save did; anything but "full" lost data that the reader should be told about (UI-008). */
export interface SaveResult {
  saved: boolean;
  /** full: everything kept; slim: diagnostics (manifest, query context, source list) dropped; no_plots: plots and
   * citation passage text dropped too. */
  level: "full" | "slim" | "no_plots" | "none";
  /** Investigations whose saved messages were removed to make room. */
  prunedThreadIds: string[];
}

export interface StorageNotice {
  kind: "slimmed" | "pruned" | "not_saved" | "expired";
  message: string;
  threadIds: string[];
}

const STORAGE_KEYS = {
  INVESTIGATIONS: "phronesis_investigations_v2",
  ACTIVE_THREAD: "phronesis_active_thread_id_v2",
  THREAD_MESSAGES_PREFIX: "phronesis_thread_messages_v2_",
  SETTINGS: "phronesis_user_settings_v2",
  HISTORY_PREFS: "phronesis_history_prefs_v1",
};

const LEGACY_DUMMY_IDS = new Set(["thread-kras", "thread-her2", "thread-insulin", "thread-nsclc"]);

type KV = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;

/** Storage that lives only as long as this page ("don't keep history"). */
class MemoryStorage implements KV {
  private map = new Map<string, string>();
  get length() {
    return this.map.size;
  }
  key(i: number) {
    return Array.from(this.map.keys())[i] ?? null;
  }
  getItem(k: string) {
    return this.map.has(k) ? (this.map.get(k) as string) : null;
  }
  setItem(k: string, v: string) {
    this.map.set(k, String(v));
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
}

/** FNV-1a 32-bit, hex: a non-reversible fingerprint, so the token itself never appears in a storage key. */
function fnv1a(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/**
 * Namespace for one backend + identity. The backend's origin and a fingerprint of the bearer token: changing either
 * shows a different (initially empty) history instead of the previous user's.
 */
export function storageScope(backendUrl: string, authToken: string): string {
  let origin = String(backendUrl || "").trim();
  try {
    origin = new URL(origin).origin;
  } catch {
    // keep the raw string
  }
  return fnv1a(`${origin}|${authToken || ""}`);
}

/** Messages with citation passage text removed (ids, titles, URLs and highlights are kept). */
function stripCitationPassageText(messages: Message[]): Message[] {
  return messages.map((m) => {
    if (!m.citations || !m.citations.entries) return m;
    const entries = Object.fromEntries(
      Object.entries(m.citations.entries).map(([k, entry]) => [
        k,
        { ...entry, passages: (entry.passages || []).map((p) => ({ ...p, text: "" })) },
      ])
    );
    return { ...m, citations: { ...m.citations, entries, truncated: true } };
  });
}

/** The artifact without its plot, marked so the card can say the plot was not kept (UI-008). */
function withoutPlot(value: any): any {
  if (!value || typeof value !== "object" || !("plot_base64_png" in value)) return value;
  const { plot_base64_png: _plot, ...rest } = value;
  return { ...rest, plot_dropped_for_storage: true };
}

/**
 * Messages reduced to what the thread view, re-run and export still need after a reload. The full run
 * manifest is stored server-side (re-run and export look it up by run_id); query_context and the source
 * list are diagnostic. With dropPlots, simulation/single-cell images and citation passage text go too.
 * Slimmed assistant messages carry `storage_slimmed` so the reloaded view can say what was dropped.
 */
export function slimMessages(messages: Message[], dropPlots = false): Message[] {
  const slim = messages.map((m) => {
    if (m.sender !== "assistant") return m;
    const { query_context: _qc, ...rest } = m;
    const out: Message = { ...rest, storage_slimmed: dropPlots ? "plots" : "diagnostics" };
    if (m.run_manifest) out.run_manifest = { run_id: m.run_manifest.run_id } as Message["run_manifest"];
    if (m.execution_artifacts) {
      const { sources: _sources, ...artifacts } = m.execution_artifacts;
      if (dropPlots) {
        artifacts.vivarix = withoutPlot(artifacts.vivarix);
        artifacts.vivarix_scan = withoutPlot(artifacts.vivarix_scan);
        artifacts.lumicell = withoutPlot(artifacts.lumicell);
      }
      out.execution_artifacts = artifacts;
    }
    return out;
  });
  return dropPlots ? stripCitationPassageText(slim) : slim;
}

function normalizePrefs(raw: unknown): HistoryPrefs {
  const p = raw && typeof raw === "object" ? (raw as Partial<HistoryPrefs>) : {};
  const mode: HistoryMode = p.mode === "session" || p.mode === "off" || p.mode === "device" ? p.mode : DEFAULT_HISTORY_PREFS.mode;
  const days = typeof p.retentionDays === "number" && Number.isFinite(p.retentionDays) && p.retentionDays >= 0
    ? Math.floor(p.retentionDays)
    : DEFAULT_HISTORY_PREFS.retentionDays;
  return { mode, retentionDays: days };
}

export class LocalStorageService {
  private scope = "";
  private memory = new MemoryStorage();
  private listeners = new Set<(n: StorageNotice) => void>();

  // ==========================================
  // STORAGE SELECTION, SCOPE AND RETENTION
  // ==========================================

  private win(): any {
    return typeof window === "undefined" ? null : (window as any);
  }

  /** localStorage, for settings and the history preference (never conversation content). */
  private device(): KV | null {
    const w = this.win();
    try {
      return w && w.localStorage ? (w.localStorage as KV) : null;
    } catch {
      return null;
    }
  }

  /** Where conversation history goes under the current preference. */
  private store(): KV | null {
    const mode = this.getHistoryPrefs().mode;
    if (mode === "off") return this.memory;
    const w = this.win();
    try {
      if (!w) return null;
      return (mode === "session" ? w.sessionStorage : w.localStorage) as KV;
    } catch {
      return null;
    }
  }

  private isAvailable(): boolean {
    const s = this.store();
    if (!s) return false;
    try {
      const testKey = "__phronesis_storage_test__";
      s.setItem(testKey, "1");
      s.removeItem(testKey);
      return true;
    } catch {
      return false;
    }
  }

  private k(base: string): string {
    return this.scope ? `${base}@${this.scope}` : base;
  }

  private msgKey(threadId: string): string {
    return `${STORAGE_KEYS.THREAD_MESSAGES_PREFIX}${this.scope ? `${this.scope}__` : ""}${threadId}`;
  }

  /** Listen for slimming / pruning / expiry notices; returns an unsubscribe function. */
  public subscribe(listener: (n: StorageNotice) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify(n: StorageNotice): void {
    for (const l of this.listeners) {
      try {
        l(n);
      } catch {
        // a listener's failure never breaks persistence
      }
    }
  }

  public getHistoryPrefs(): HistoryPrefs {
    const d = this.device();
    try {
      const raw = d?.getItem(STORAGE_KEYS.HISTORY_PREFS);
      return normalizePrefs(raw ? JSON.parse(raw) : null);
    } catch {
      return { ...DEFAULT_HISTORY_PREFS };
    }
  }

  /**
   * Changes where history is kept / how long. Moving away from "device" removes this scope's history from the
   * device after copying it to the new place (session storage or this tab's memory), so nothing stays behind.
   */
  public setHistoryPrefs(next: Partial<HistoryPrefs>): HistoryPrefs {
    const before = this.getHistoryPrefs();
    const prefs = normalizePrefs({ ...before, ...next });
    const from = this.store();
    const ids = this.getInvestigations().map((i) => i.id);
    const snapshot = {
      list: from?.getItem(this.k(STORAGE_KEYS.INVESTIGATIONS)) ?? null,
      active: from?.getItem(this.k(STORAGE_KEYS.ACTIVE_THREAD)) ?? null,
      msgs: ids.map((id) => [this.msgKey(id), from?.getItem(this.msgKey(id)) ?? null] as const),
    };
    try {
      this.device()?.setItem(STORAGE_KEYS.HISTORY_PREFS, JSON.stringify(prefs));
    } catch {
      return before;
    }
    if (prefs.mode !== before.mode) {
      const to = this.store();
      if (from && to && from !== to) {
        try {
          if (snapshot.list !== null) to.setItem(this.k(STORAGE_KEYS.INVESTIGATIONS), snapshot.list);
          if (snapshot.active !== null) to.setItem(this.k(STORAGE_KEYS.ACTIVE_THREAD), snapshot.active);
          for (const [key, val] of snapshot.msgs) if (val !== null) to.setItem(key, val);
        } catch {
          // the open conversation stays in memory either way
        }
        from.removeItem(this.k(STORAGE_KEYS.INVESTIGATIONS));
        from.removeItem(this.k(STORAGE_KEYS.ACTIVE_THREAD));
        for (const [key] of snapshot.msgs) from.removeItem(key);
      }
    }
    if (prefs.retentionDays !== before.retentionDays) this.pruneExpired();
    return prefs;
  }

  /**
   * Selects the history namespace for this backend + token (call before reading history, and again when either
   * changes). History written by older versions without a namespace is adopted once by the first scope that loads
   * it, so upgrading keeps the user's investigations. Expired investigations are pruned.
   */
  public configure(backendUrl: string, authToken: string): void {
    this.scope = storageScope(backendUrl, authToken);
    this.adoptLegacyHistory();
    this.pruneExpired();
  }

  public getScope(): string {
    return this.scope;
  }

  private adoptLegacyHistory(): void {
    const s = this.store();
    if (!s || !this.scope) return;
    try {
      const legacy = s.getItem(STORAGE_KEYS.INVESTIGATIONS);
      if (legacy === null || s.getItem(this.k(STORAGE_KEYS.INVESTIGATIONS)) !== null) return;
      const list = JSON.parse(legacy);
      s.setItem(this.k(STORAGE_KEYS.INVESTIGATIONS), legacy);
      const active = s.getItem(STORAGE_KEYS.ACTIVE_THREAD);
      if (active !== null) s.setItem(this.k(STORAGE_KEYS.ACTIVE_THREAD), active);
      for (const inv of Array.isArray(list) ? list : []) {
        if (!inv || typeof inv.id !== "string") continue;
        const oldKey = `${STORAGE_KEYS.THREAD_MESSAGES_PREFIX}${inv.id}`;
        const msgs = s.getItem(oldKey);
        if (msgs !== null) s.setItem(this.msgKey(inv.id), msgs);
        s.removeItem(oldKey);
      }
      s.removeItem(STORAGE_KEYS.INVESTIGATIONS);
      s.removeItem(STORAGE_KEYS.ACTIVE_THREAD);
    } catch (e) {
      console.warn("PhronesisLocalStorage: could not adopt history saved by an older version", e);
    }
  }

  /** Removes investigations not updated within the retention period; returns the removed ids. */
  public pruneExpired(now: number = Date.now()): string[] {
    const { retentionDays } = this.getHistoryPrefs();
    if (!retentionDays) return [];
    const cutoff = now - retentionDays * 86_400_000;
    const list = this.getInvestigations();
    const expired = list.filter((i) => {
      const t = new Date(i.updatedAt || i.createdAt).getTime();
      return Number.isFinite(t) && t < cutoff;
    });
    if (!expired.length) return [];
    for (const inv of expired) this.deleteInvestigation(inv.id);
    const ids = expired.map((i) => i.id);
    this.notify({
      kind: "expired",
      message: `${ids.length} investigation${ids.length === 1 ? "" : "s"} older than ${retentionDays} days ${ids.length === 1 ? "was" : "were"} removed from this browser.`,
      threadIds: ids,
    });
    return ids;
  }

  // ==========================================
  // INVESTIGATIONS (THREADS)
  // ==========================================

  public getInvestigations(): InvestigationItem[] {
    if (!this.isAvailable()) return [];
    try {
      const raw = this.store()!.getItem(this.k(STORAGE_KEYS.INVESTIGATIONS));
      if (!raw) {
        return [];
      }
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) {
        // Filter out legacy dummy threads that have 0 messages
        const valid = parsed.filter(
          (inv) => !LEGACY_DUMMY_IDS.has(inv.id) || (inv.messageCount && inv.messageCount > 0)
        );
        return valid.sort(
          (a, b) => new Date(b.updatedAt || b.createdAt).getTime() - new Date(a.updatedAt || a.createdAt).getTime()
        );
      }
      return [];
    } catch (e) {
      console.warn("PhronesisLocalStorage: failed to get investigations", e);
      return [];
    }
  }

  private saveInvestigationsList(list: InvestigationItem[]): void {
    if (!this.isAvailable()) return;
    const key = this.k(STORAGE_KEYS.INVESTIGATIONS);
    if (this.trySetItem(key, list)) return;
    // Make room without touching the list itself, then retry once.
    this.handleQuotaExceeded(new Error("investigation list does not fit"), list);
    if (!this.trySetItem(key, list)) {
      this.notify({ kind: "not_saved", message: "The investigation list could not be saved in this browser.", threadIds: [] });
    }
  }

  public saveInvestigation(item: InvestigationItem): void {
    const list = this.getInvestigations();
    const existingIndex = list.findIndex((i) => i.id === item.id);
    if (existingIndex >= 0) {
      list[existingIndex] = { ...list[existingIndex], ...item, updatedAt: new Date().toISOString() };
    } else {
      list.unshift({ ...item, createdAt: item.createdAt || new Date().toISOString(), updatedAt: new Date().toISOString() });
    }
    this.saveInvestigationsList(list);
  }

  public renameInvestigation(id: string, newTitle: string): void {
    const list = this.getInvestigations();
    const target = list.find((i) => i.id === id);
    if (target) {
      target.title = newTitle.trim();
      target.isCustomTitle = true;
      target.updatedAt = new Date().toISOString();
      this.saveInvestigationsList(list);
    }
  }

  public updateInvestigationTitle(id: string, newTitle: string, isCustom = false): boolean {
    const list = this.getInvestigations();
    const target = list.find((i) => i.id === id);
    if (!target) return false;
    if (target.isCustomTitle && !isCustom) return false;
    const cleanTitle = newTitle.trim();
    if (!cleanTitle) return false;
    target.title = cleanTitle;
    if (isCustom) target.isCustomTitle = true;
    target.updatedAt = new Date().toISOString();
    this.saveInvestigationsList(list);
    return true;
  }

  public deleteInvestigation(id: string): void {
    if (!this.isAvailable()) return;
    const list = this.getInvestigations().filter((i) => i.id !== id);
    this.saveInvestigationsList(list);
    try {
      this.store()!.removeItem(this.msgKey(id));
    } catch {
      // ignore
    }
  }

  public createNewThread(
    title: string = "New Clinical Investigation",
    framework?: string,
    model?: string
  ): InvestigationItem {
    const now = new Date().toISOString();
    const newId = `session_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`;
    const newItem: InvestigationItem = {
      id: newId,
      title,
      createdAt: now,
      updatedAt: now,
      framework,
      model,
      messageCount: 0,
      preview: "New investigation session",
    };

    const list = this.getInvestigations();
    list.unshift(newItem);
    this.saveInvestigationsList(list);
    this.setActiveThreadId(newId);
    this.saveMessages(newId, []);

    return newItem;
  }

  // ==========================================
  // ACTIVE THREAD ID
  // ==========================================

  public getActiveThreadId(): string {
    const fallbackId = `session_${Date.now()}`;
    if (!this.isAvailable()) return fallbackId;
    try {
      const active = this.store()!.getItem(this.k(STORAGE_KEYS.ACTIVE_THREAD));
      if (active && !LEGACY_DUMMY_IDS.has(active)) return active;
      const list = this.getInvestigations();
      if (list.length > 0 && list[0]?.id && !LEGACY_DUMMY_IDS.has(list[0].id)) {
        return list[0].id;
      }
      return fallbackId;
    } catch {
      return fallbackId;
    }
  }

  public setActiveThreadId(id: string): void {
    if (!this.isAvailable()) return;
    try {
      this.store()!.setItem(this.k(STORAGE_KEYS.ACTIVE_THREAD), id);
    } catch {
      // ignore
    }
  }

  // ==========================================
  // MESSAGES PERSISTENCE
  // ==========================================

  public getMessages(threadId: string): Message[] {
    if (!this.isAvailable() || !threadId) return [];
    try {
      const raw = this.store()!.getItem(this.msgKey(threadId));
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      console.warn(`PhronesisLocalStorage: failed to get messages for ${threadId}`, e);
      return [];
    }
  }

  public saveMessages(threadId: string, messages: Message[], autoGenerateTitle?: string): SaveResult {
    const none: SaveResult = { saved: false, level: "none", prunedThreadIds: [] };
    if (!this.isAvailable() || !threadId) return none;
    const key = this.msgKey(threadId);
    // Full payload first, then slimmer copies; pruning another thread's history is the last resort.
    const attempts: Array<[SaveResult["level"], () => Message[]]> = [
      ["full", () => messages],
      ["slim", () => slimMessages(messages)],
      ["no_plots", () => slimMessages(messages, true)],
    ];
    let level: SaveResult["level"] = "none";
    for (const [lvl, build] of attempts) {
      if (this.trySetItem(key, build())) {
        level = lvl;
        break;
      }
    }
    let pruned: string[] = [];
    if (level === "none") {
      pruned = this.handleQuotaExceeded(new Error(`thread ${threadId} does not fit in browser storage`), undefined, threadId);
      if (this.trySetItem(key, slimMessages(messages, true))) level = "no_plots";
    }
    const result: SaveResult = { saved: level !== "none", level, prunedThreadIds: pruned };
    if (level === "none") {
      this.notify({
        kind: "not_saved",
        message: "This investigation could not be saved in this browser (storage is full). It stays on screen until you reload.",
        threadIds: [threadId],
      });
      return result;
    }
    if (level === "no_plots") {
      this.notify({
        kind: "slimmed",
        message: "Browser storage is nearly full: plots and citation passages of this investigation were not saved, so they will be missing after a reload.",
        threadIds: [threadId],
      });
    }
    try {
      // Update investigation record
      const list = this.getInvestigations();
      const existing = list.find((i) => i.id === threadId);

      const lastMsg = messages[messages.length - 1];
      const preview = lastMsg
        ? typeof lastMsg.content === "string"
          ? lastMsg.content.slice(0, 80).replace(/\n/g, " ") + (lastMsg.content.length > 80 ? "..." : "")
          : "Message logged"
        : "No messages yet";

      const isGenericTitle = (t: string) => {
        const lower = t.toLowerCase().trim();
        return (
          lower === "new clinical investigation" ||
          lower.startsWith("new clinical investigation") ||
          lower === "clinical investigation" ||
          lower.startsWith("new conversation") ||
          lower.startsWith("new inquiry") ||
          lower.startsWith("untitled") ||
          lower.startsWith("session_") ||
          lower.startsWith("thread-")
        );
      };

      if (existing) {
        existing.messageCount = messages.length;
        existing.preview = preview;
        existing.updatedAt = new Date().toISOString();
        existing.messagesPruned = false;
        if (autoGenerateTitle && !existing.isCustomTitle && (isGenericTitle(existing.title) || messages.length <= 2)) {
          existing.title = autoGenerateTitle;
        }
        this.saveInvestigationsList(list);
      } else {
        // Auto-create thread record
        const title =
          autoGenerateTitle ||
          (messages[0]?.content
            ? messages[0].content.slice(0, 42).replace(/\n/g, " ")
            : "Clinical Investigation");

        const newItem: InvestigationItem = {
          id: threadId,
          title,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
          messageCount: messages.length,
          preview,
          isCustomTitle: false,
        };
        list.unshift(newItem);
        this.saveInvestigationsList(list);
      }
    } catch (e) {
      console.warn(`PhronesisLocalStorage: failed to update the investigation record for ${threadId}`, e);
    }
    return result;
  }

  private trySetItem(key: string, value: unknown): boolean {
    try {
      this.store()!.setItem(key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }

  // ==========================================
  // USER PREFERENCES / SETTINGS (device storage; no conversation content)
  // ==========================================

  public getSettings(): UserSettings {
    const d = this.device();
    if (!d) return {};
    try {
      const raw = d.getItem(STORAGE_KEYS.SETTINGS);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }

  public saveSettings(settings: Partial<UserSettings>): void {
    const d = this.device();
    if (!d) return;
    try {
      const current = this.getSettings();
      const updated = { ...current, ...settings };
      d.setItem(STORAGE_KEYS.SETTINGS, JSON.stringify(updated));
    } catch {
      // ignore
    }
  }

  // ==========================================
  // STATS & QUOTA MANAGEMENT
  // ==========================================

  public getStorageUsage(): {
    usedKb: number;
    threadCount: number;
    messageCount: number;
    isQuotaHealthy: boolean;
  } {
    if (!this.isAvailable()) {
      return { usedKb: 0, threadCount: 0, messageCount: 0, isQuotaHealthy: true };
    }
    try {
      const s = this.store()!;
      let totalBytes = 0;
      let msgCount = 0;
      const invs = this.getInvestigations();
      const prefix = `${STORAGE_KEYS.THREAD_MESSAGES_PREFIX}${this.scope ? `${this.scope}__` : ""}`;

      for (let i = 0; i < s.length; i++) {
        const key = s.key(i);
        if (key && key.startsWith("phronesis_")) {
          const val = s.getItem(key) || "";
          totalBytes += (key.length + val.length) * 2; // UTF-16 approximation
          if (key.startsWith(prefix)) {
            try {
              const msgs = JSON.parse(val);
              if (Array.isArray(msgs)) msgCount += msgs.length;
            } catch {
              // ignore
            }
          }
        }
      }

      const usedKb = Math.round((totalBytes / 1024) * 10) / 10;
      return {
        usedKb,
        threadCount: invs.length,
        messageCount: msgCount,
        isQuotaHealthy: usedKb < 4500, // standard localStorage limit ~5MB
      };
    } catch {
      return { usedKb: 0, threadCount: 0, messageCount: 0, isQuotaHealthy: true };
    }
  }

  /** Removes every saved conversation of every user/backend from this browser (device, session and tab memory). */
  public clearAllLocalData(): void {
    const w = this.win();
    const stores: KV[] = [this.memory];
    try {
      if (w?.localStorage) stores.push(w.localStorage);
      if (w?.sessionStorage) stores.push(w.sessionStorage);
    } catch {
      // storage unavailable
    }
    for (const s of stores) {
      try {
        const keysToRemove: string[] = [];
        for (let i = 0; i < s.length; i++) {
          const key = s.key(i);
          // Preferences (where/how long to keep history, model choice) are not conversation data.
          if (key && key.startsWith("phronesis_") && key !== STORAGE_KEYS.HISTORY_PREFS && key !== STORAGE_KEYS.SETTINGS) {
            keysToRemove.push(key);
          }
        }
        keysToRemove.forEach((k) => s.removeItem(k));
      } catch (e) {
        console.error("Failed to clear browser storage", e);
      }
    }
  }

  /**
   * Frees space by removing the messages of the oldest investigation other than the active one (and `keep`).
   * The investigation record stays, marked `messagesPruned` with an explanatory preview, and listeners are told.
   */
  private handleQuotaExceeded(err: unknown, pendingList?: InvestigationItem[], keep?: string): string[] {
    console.warn("PhronesisLocalStorage: storage full, pruning the oldest thread's messages", err);
    try {
      const list = pendingList ?? this.getInvestigations();
      if (list.length <= 3) return [];
      const activeId = this.getActiveThreadId();
      const candidate = [...list]
        .sort((a, b) => new Date(b.updatedAt || b.createdAt).getTime() - new Date(a.updatedAt || a.createdAt).getTime())
        .reverse()
        .find((i) => i.id !== activeId && i.id !== keep && !i.messagesPruned);
      if (!candidate) return [];
      this.store()!.removeItem(this.msgKey(candidate.id));
      candidate.messageCount = 0;
      candidate.messagesPruned = true;
      candidate.preview = "Messages removed to free browser storage";
      if (!pendingList) this.trySetItem(this.k(STORAGE_KEYS.INVESTIGATIONS), list);
      this.notify({
        kind: "pruned",
        message: `Browser storage was full: the saved messages of "${candidate.title}" were removed to make room.`,
        threadIds: [candidate.id],
      });
      return [candidate.id];
    } catch {
      return [];
    }
  }
}

export const localStorageService = new LocalStorageService();
