"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Activity,
  BookOpen,
  Bot,
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  Copy,
  Cpu,
  Database,
  Dna,
  Edit2,
  FileText,
  GitBranch,
  Hourglass,
  Globe,
  Key,
  Loader2,
  Menu,
  MessageSquareText,
  MessagesSquare,
  Network,
  Workflow,
  Microscope,
  MoreHorizontal,
  PanelLeft,
  PanelLeftClose,
  Play,
  Plus,
  RefreshCw,
  Search,
  Navigation,
  Send,
  Route,
  Settings,
  Shield,
  Square,
  SquarePen,
  Sun,
  Moon,
  Trash,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import { MarkdownRenderer } from "@/components/MarkdownRenderer";
import { SmoothMarkdown } from "@/components/SmoothMarkdown";
import { ThinkingProcess } from "@/components/ThinkingProcess";
import { TaskStatusNotice, VerificationNotice, stripVerificationBanner } from "@/components/VerificationNotice";
import { MessageArtifacts } from "@/components/MessageArtifacts";
import { MessageActionsExtra } from "@/components/MessageActionsExtra";
import { FrameworkGuide, FrameworkMenu } from "@/components/FrameworkMenu";
import { EffortMenu } from "@/components/EffortMenu";
import { ChatGPTEffortControl } from "@/components/ChatGPTEffortControl";
import { ComposerAttachButton } from "@/components/ComposerAttachButton";
import { ComposerAttachmentChips } from "@/components/ComposerAttachmentChips";
import { MessageAttachmentChips } from "@/components/MessageAttachmentChips";
import { AgentOptionsSettings } from "@/components/agentic/AgentOptionsSettings";
import { JobsPanel, jobTitle } from "@/components/agentic/JobsPanel";
import { ApprovalCard, ClarificationChips, SteeringNotes, type SteerNote } from "@/components/agentic/LiveRunControls";
import { ProjectSelector } from "@/components/agentic/ProjectSelector";
import {
  DEFAULT_HISTORY_PREFS,
  localStorageService,
  type HistoryMode,
  type HistoryPrefs,
  type InvestigationItem,
  type StorageNotice,
} from "@/services/localStorageService";
import { cancelRun, createJob, EFFORT_LEVELS, fetchConversationTitle, fetchFrameworks, getJob, normalizeAgentOptions, steerRun, streamJobEvents, toAgentOptions } from "@/services/agentService";
import { ApiError, apiFetch, errorMessage, errorStatus, jsonInit, readSseEvents, requestJson, type RequestOptions } from "@/services/apiClient";
import { ArtifactRenderFallback, ErrorBoundary, MessageRenderFallback } from "@/components/ErrorBoundary";
import { useFocusTrap } from "@/lib/useFocusTrap";
import { generateDynamicTitle } from "@/lib/dynamicTitle";
import { finalizeThoughts } from "@/components/agentic/thoughts";
import { resolveAnswer } from "@/lib/assistantAnswer";
import { entitySummary, memoryBadgeCount, memoryStatTiles, memoryTypeLabel, verificationStatusLabel } from "@/lib/memoryVault";
import { Message, ThoughtStep, FrameworkOption } from "@/types/chat";
import type { MessageAttachment } from "@/types/datasets";
import type { AgentOptionsSettings as AgentOptionsState, FrameworkInfo, JobSummary, PendingApproval } from "@/types/agentic";
import type {
  BackendHealth,
  ChatResult,
  MemoryEntitiesResponse,
  MemoryEntity,
  MemorySearchResponse,
  MemorySearchResult,
  MemoryStats,
  ModelInfo,
  ModelsResponse,
  StreamEvent,
} from "@/types/api";

interface AgentMetadata {
  id: string;
  name: string;
  domain: string;
  description: string;
  tools: string[];
  color: string;
  icon: React.ReactNode;
}

const ORCHESTRATION_FRAMEWORK = "authoritative-react-compiler";
// The API's default when PHRONESIS_DEFAULT_FRAMEWORK is unset: Auto routing (docs/agentic/auto-routing.md).
const FALLBACK_DEFAULT_FRAMEWORK = "auto";

// Every framework the backend implements; descriptions are replaced by /api/frameworks when it answers.
const FRAMEWORK_OPTIONS: FrameworkOption[] = [
  {
    id: "auto",
    title: "Auto",
    thinkingBadge: "Auto routing",
    badgeColor: "var(--domain-t2kg)",
    icon: Route,
    description:
      "Reads each question and picks the best architecture for it, then brings in critics to debate the draft when the evidence conflicts or the question is safety-critical.",
    available: true,
    recommended: true,
  },
  {
    id: "agentic-plan-execute",
    title: "Plan & Execute",
    thinkingBadge: "Agentic Planner",
    badgeColor: "var(--domain-scp)",
    icon: Workflow,
    description:
      "Breaks complex questions into steps, runs search & analysis tools in parallel, and double-checks claims against counter-evidence for maximum accuracy.",
    available: true,
  },
  {
    id: "multi-agent-supervisor",
    title: "Supervisor",
    thinkingBadge: "Supervisor",
    badgeColor: "var(--domain-t2kg)",
    icon: Network,
    description:
      "A central manager coordinates and delegates tasks to domain specialists (literature, disease targets, simulations, omics).",
    available: true,
  },
  {
    id: "multi-agent-swarm",
    title: "Swarm",
    thinkingBadge: "Decentralized Swarm",
    badgeColor: "var(--nvidia-green)",
    icon: GitBranch,
    description:
      "Specialist agents collaborate peer-to-peer, handing control directly to each other until the question is answered.",
    available: true,
  },
  {
    id: "multi-agent-debate",
    title: "Debate",
    thinkingBadge: "Debate",
    badgeColor: "var(--domain-s2)",
    icon: MessagesSquare,
    description:
      "Multiple agents cross-examine and challenge each other's reasoning and evidence to eliminate bias and ensure rigorous conclusions.",
    available: true,
  },
  {
    id: "authoritative-react-compiler",
    title: "Classic Pipeline",
    thinkingBadge: "Authoritative Supervisor",
    badgeColor: "var(--accent-strong)",
    icon: Cpu,
    description:
      "A fixed, predictable 6-stage workflow that systematically gathers evidence, runs simulations, and audits findings.",
    available: true,
  },
];

const FRAMEWORK_LABEL: Record<string, string> = {
  auto: "Auto",
  "authoritative-react-compiler": "Classic pipeline",
  "agentic-plan-execute": "Plan & execute",
  "multi-agent-supervisor": "Multi-agent supervisor",
  "multi-agent-swarm": "Multi-agent swarm",
  "multi-agent-debate": "Multi-agent debate",
  "conversational-direct": "Direct reply",
};

const AUTH_TOKEN_STORAGE_KEY = "phronesis-auth-token";
const BACKEND_URL_STORAGE_KEY = "phronesis-backend-url";
const DEFAULT_MODEL = process.env.NEXT_PUBLIC_DEFAULT_MODEL || "nvidia/nemotron-3-super-120b-a12b";

/** A backend base URL the user typed, normalized, or null when it is not an http(s) URL. */
function parseBackendUrl(value: string): string | null {
  try {
    const url = new URL(value.trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch {
    return null;
  }
}

export default function ClinicalRDAssistant() {
  const [messages, setMessages] = useState<Message[]>([]);

  const [inputMessage, setInputMessage] = useState("");
  // Datasets attached to the next question; they stay attached after sending until removed.
  const [attachedDatasets, setAttachedDatasets] = useState<MessageAttachment[]>([]);
  const [isWebSearchEnabled, setIsWebSearchEnabled] = useState(true);
  const [selectedModel, setSelectedModel] = useState(DEFAULT_MODEL);
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [selectedFramework, setSelectedFramework] = useState<string>(FALLBACK_DEFAULT_FRAMEWORK);
  // True once the user (or their saved settings) picked a framework; until then the backend default applies.
  const frameworkChosenRef = useRef(false);
  const [frameworkCatalog, setFrameworkCatalog] = useState<FrameworkInfo[] | null>(null);
  const [isFrameworkDropdownOpen, setIsFrameworkDropdownOpen] = useState(false);
  const [isFrameworkGuideOpen, setIsFrameworkGuideOpen] = useState(false);
  const [isEffortDropdownOpen, setIsEffortDropdownOpen] = useState(false);
  // Agentic settings (sent as agent_options), research project (project_id) and background submission.
  const [agentOptions, setAgentOptions] = useState<AgentOptionsState>(() => normalizeAgentOptions(null));
  const [projectId, setProjectId] = useState<string | null>(null);
  const [runInBackground, setRunInBackground] = useState(false);
  const [jobsRefreshKey, setJobsRefreshKey] = useState(0);

  useEffect(() => {
    try {
      const savedSettings = localStorageService.getSettings();
      const saved = savedSettings.selectedFramework;
      // Settings saved before the agentic frameworks may hold an id the backend never ran; drop those.
      if (saved && FRAMEWORK_OPTIONS.some((fw) => fw.id === saved && fw.available)) {
        setSelectedFramework(saved);
        frameworkChosenRef.current = true;
      } else if (saved) {
        localStorageService.saveSettings({ ...savedSettings, selectedFramework: undefined });
      }
      if (savedSettings.selectedModel) setSelectedModel(savedSettings.selectedModel);
      setAgentOptions(normalizeAgentOptions(savedSettings.agentOptions));
      if (typeof savedSettings.projectId === "string" && /^prj_[A-Za-z0-9]{6,40}$/.test(savedSettings.projectId)) {
        setProjectId(savedSettings.projectId);
      }
    } catch {
      // ignore
    }
  }, []);

  const updateAgentOptions = (next: AgentOptionsState) => {
    setAgentOptions(next);
    localStorageService.saveSettings({ agentOptions: next });
  };

  const updateProjectId = (next: string | null) => {
    setProjectId(next);
    localStorageService.saveSettings({ projectId: next || undefined });
  };

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      const target = event.target as HTMLElement;
      if (!target.closest?.(".investigation-menu-container")) {
        setOpenMenuThreadId(null);
      }
      if (!target.closest?.(".framework-selector-container")) {
        setIsFrameworkDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, []);

  const [isProcessing, setIsProcessing] = useState(false);
  const [activeAgentId, setActiveAgentId] = useState<string | null>(null);
  const [threadId, setThreadId] = useState("session_default");
  const [backendUrl, setBackendUrl] = useState(
    process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000"
  );
  // The URL field edits a draft; requests use backendUrl only after the draft is committed and valid,
  // so a half-typed host never receives the bearer token.
  const [backendUrlDraft, setBackendUrlDraft] = useState(backendUrl);
  const [backendUrlError, setBackendUrlError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [authTokenDraft, setAuthTokenDraft] = useState("");
  // The token is kept for this browser session only, unless the user chose "Remember on this device".
  const [rememberAuthToken, setRememberAuthToken] = useState(false);
  const [backendHealth, setBackendHealth] = useState<BackendHealth | null>(null);
  const [backendHealthError, setBackendHealthError] = useState<string | null>(null);
  // Set when the API answers 503 (no auth configured) or 401/403 (token missing or wrong).
  const [authProblem, setAuthProblem] = useState<string | null>(null);
  // Saved history is only read once the stored token and backend URL are known (UI-007).
  const [credentialsLoaded, setCredentialsLoaded] = useState(false);
  const [historyPrefs, setHistoryPrefs] = useState<HistoryPrefs>(DEFAULT_HISTORY_PREFS);
  const [storageNotice, setStorageNotice] = useState<StorageNotice | null>(null);

  useEffect(() => {
    try {
      const remembered = localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || "";
      const token = sessionStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || remembered;
      setRememberAuthToken(!!remembered);
      setAuthToken(token);
      setAuthTokenDraft(token);
      const savedUrl = parseBackendUrl(localStorage.getItem(BACKEND_URL_STORAGE_KEY) || "");
      if (savedUrl) {
        setBackendUrl(savedUrl);
        setBackendUrlDraft(savedUrl);
      }
    } catch {
      // storage unavailable
    }
    setCredentialsLoaded(true);
  }, []);

  const commitBackendUrl = () => {
    const parsed = parseBackendUrl(backendUrlDraft);
    if (!parsed) {
      setBackendUrlError("Enter a full http:// or https:// URL, e.g. http://127.0.0.1:8000");
      return;
    }
    setBackendUrlError(null);
    setBackendUrlDraft(parsed);
    if (parsed === backendUrl) return;
    setBackendUrl(parsed);
    try {
      localStorage.setItem(BACKEND_URL_STORAGE_KEY, parsed);
    } catch {
      // storage unavailable
    }
  };

  // Health is public (no token sent); it reports the auth mode and which providers are configured.
  useEffect(() => {
    const controller = new AbortController();
    setBackendHealthError(null);
    requestJson<BackendHealth | null>(`${backendUrl}/api/health`, { signal: controller.signal }, { wrapNetworkErrors: false })
      .then((health) => setBackendHealth(health))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setBackendHealth(null);
        setBackendHealthError(err instanceof ApiError ? `status ${err.status}` : errorMessage(err, "unreachable"));
      });
    fetchFrameworks(backendUrl, () => ({}), controller.signal)
      .then((data) => {
        const list = Array.isArray(data?.frameworks) ? data.frameworks.filter((f) => f && typeof f.id === "string") : [];
        setFrameworkCatalog(list.length ? list : null);
        const def = typeof data?.default === "string" ? data.default : "";
        if (!frameworkChosenRef.current && def && FRAMEWORK_OPTIONS.some((fw) => fw.id === def)) setSelectedFramework(def);
      })
      .catch(() => {
        // Optional: the static list and the documented default keep working.
      });
    requestJson<ModelsResponse | null>(`${backendUrl}/api/models`, { signal: controller.signal })
      .then((data) => {
        const models = Array.isArray(data?.models) ? data.models : [];
        setAvailableModels(
          models.filter((m): m is ModelInfo => !!m && typeof m === "object" && typeof (m as { id?: unknown }).id === "string")
        );
      })
      .catch(() => {
        // The model list is optional; the default model keeps working.
      });
    return () => controller.abort();
  }, [backendUrl]);

  /** Stores the token in sessionStorage, or in localStorage when it should be remembered on this device. */
  const updateAuthToken = (value: string, remember: boolean = rememberAuthToken) => {
    setAuthToken(value);
    try {
      const keep = remember ? localStorage : sessionStorage;
      const drop = remember ? sessionStorage : localStorage;
      drop.removeItem(AUTH_TOKEN_STORAGE_KEY);
      if (value) keep.setItem(AUTH_TOKEN_STORAGE_KEY, value);
      else keep.removeItem(AUTH_TOKEN_STORAGE_KEY);
    } catch {
      // storage unavailable
    }
  };

  const updateRememberAuthToken = (remember: boolean) => {
    setRememberAuthToken(remember);
    updateAuthToken(authToken, remember);
  };

  const authHeaders = (extra: Record<string, string> = {}): Record<string, string> =>
    authToken ? { ...extra, Authorization: `Bearer ${authToken}` } : extra;
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  // Clinical Memory Vault State
  const [showMemoryVault, setShowMemoryVault] = useState(false);
  const [memoryStats, setMemoryStats] = useState<MemoryStats | null>(null);
  const [memoryEntities, setMemoryEntities] = useState<MemoryEntity[]>([]);
  const [memorySearchQuery, setMemorySearchQuery] = useState("");
  const [memorySearchResults, setMemorySearchResults] = useState<MemorySearchResult[]>([]);
  const [isSearchingMemory, setIsSearchingMemory] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);

  /** Records an auth problem from any API response status so the banner can explain it. */
  const noteAuthStatus = (status: number) => {
    if (status === 503) setAuthProblem("The API reports that authentication is not configured on the server.");
    else if (status === 401 || status === 403) setAuthProblem("The API rejected the access token. Set it under Settings → Phronesis Access Token.");
    else if (status >= 200 && status < 300) setAuthProblem(null);
  };

  // Page calls note auth problems and keep the browser's own network error text ("Failed to fetch").
  const pageApi: RequestOptions = { onStatus: noteAuthStatus, wrapNetworkErrors: false };

  /** "<what> (status N)." for an HTTP failure, "<what>: <reason>." for a network failure. */
  const describeFailure = (what: string, err: unknown): string =>
    err instanceof ApiError && err.status > 0 ? `${what} (status ${err.status}).` : `${what}: ${errorMessage(err, "backend unreachable")}.`;

  const fetchMemoryVault = async () => {
    const [stats, entities] = await Promise.allSettled([
      requestJson<MemoryStats | null>(`${backendUrl}/api/memory/stats`, { headers: authHeaders() }, pageApi),
      requestJson<MemoryEntitiesResponse | null>(`${backendUrl}/api/memory/entities`, { headers: authHeaders() }, pageApi),
    ]);
    if (stats.status === "fulfilled") setMemoryStats(stats.value);
    if (entities.status === "fulfilled") setMemoryEntities(entities.value?.entities || []);
    const failed = stats.status === "rejected" ? stats.reason : entities.status === "rejected" ? entities.reason : null;
    setMemoryError(failed ? describeFailure("Memory could not be loaded", failed) : null);
  };

  useEffect(() => {
    fetchMemoryVault();
  }, [backendUrl, authToken]);

  useEffect(() => {
    if (showMemoryVault) {
      fetchMemoryVault();
    }
  }, [showMemoryVault]);

  const handleSearchMemory = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!memorySearchQuery.trim()) return;
    setIsSearchingMemory(true);
    try {
      const data = await requestJson<MemorySearchResponse | null>(
        `${backendUrl}/api/memory/search`,
        jsonInit("POST", { query: memorySearchQuery }, authHeaders),
        pageApi
      );
      setMemorySearchResults(data?.results || []);
      setMemoryError(null);
    } catch (err: unknown) {
      setMemoryError(describeFailure("Memory search failed", err));
    } finally {
      setIsSearchingMemory(false);
    }
  };

  /** Deletes one thread's server-side memory; returns whether the server confirmed it. */
  const deleteServerThreadMemory = async (id: string): Promise<boolean> => {
    try {
      await apiFetch(`${backendUrl}/api/memory/thread/${encodeURIComponent(id)}`, { method: "DELETE", headers: authHeaders() }, pageApi);
      return true;
    } catch (err: unknown) {
      setMemoryError(describeFailure("Server memory for this investigation was not cleared", err));
      return false;
    }
  };

  const handleClearCurrentThreadMemory = async () => {
    if (await deleteServerThreadMemory(threadId)) setMemoryError(null);
    fetchMemoryVault();
  };

  const handleClearAllMemory = async () => {
    if (!window.confirm("Are you sure you want to clear all of your stored clinical memories?")) return;
    try {
      await apiFetch(`${backendUrl}/api/memory/clear`, { method: "DELETE", headers: authHeaders() }, pageApi);
      setMemorySearchResults([]);
      setMemoryError(null);
    } catch (err: unknown) {
      setMemoryError(describeFailure("Memory was not cleared", err));
    }
    fetchMemoryVault();
  };

  // Sync theme with localStorage & document root
  useEffect(() => {
    try {
      const active = (document.documentElement.getAttribute("data-theme") as "dark" | "light") ||
        (localStorage.getItem("phronesis-theme") as "dark" | "light") ||
        "dark";
      setTheme(active);
    } catch {
      // Fallback
    }
  }, []);

  const toggleTheme = () => {
    const current = (document.documentElement.getAttribute("data-theme") as "dark" | "light") || theme;
    const nextTheme = current === "dark" ? "light" : "dark";
    setTheme(nextTheme);
    try {
      localStorage.setItem("phronesis-theme", nextTheme);
    } catch {
      // ignore storage error
    }
    document.documentElement.classList.remove("light", "dark");
    document.documentElement.classList.add(nextTheme);
    document.documentElement.setAttribute("data-theme", nextTheme);
  };

  // Live thinking state during active streaming
  const [liveThoughts, setLiveThoughts] = useState<ThoughtStep[]>([]);
  const [liveStartTime, setLiveStartTime] = useState<number | null>(null);
  const [liveElapsedSeconds, setLiveElapsedSeconds] = useState<number>(0);
  // Streamed tokens accumulate in refs and are flushed to state at most once per animation frame.
  const [liveReasoning, setLiveReasoning] = useState("");
  const [liveContent, setLiveContent] = useState("");
  const liveReasoningRef = useRef("");
  const liveContentRef = useRef("");
  const liveFlushRef = useRef<number | null>(null);
  // After a content_reset the old answer stays on screen until the replacement arrives (no blank flash).
  const liveResetPendingRef = useRef(false);
  // What the smooth reveal has on screen; handed to the saved message so it finishes from there.
  const liveShownRef = useRef("");
  const streamHandoffRef = useRef<{ id: string; text: string; at: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Agentic run of the streaming answer: run id (run_control event), a pending approval, and whether the
  // agent rewrote the answer after new evidence (content_reset reason "agent_round").
  const [liveRunId, setLiveRunId] = useState<string | null>(null);
  const liveRunIdRef = useRef<string | null>(null);
  const [liveApproval, setLiveApproval] = useState<PendingApproval | null>(null);
  const [liveRevised, setLiveRevised] = useState(false);
  const liveRevisedRef = useRef(false);
  // Guidance typed into the composer while an agentic run streams, and a graceful stop in flight.
  const [liveSteers, setLiveSteers] = useState<SteerNote[]>([]);
  const [stoppingRun, setStoppingRun] = useState(false);
  const isNearBottomRef = useRef(true);
  const justStreamedIdRef = useRef<string | null>(null);
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);

  // Local storage usage and investigations state
  const [localStorageStats, setLocalStorageStats] = useState<{
    usedKb: number;
    threadCount: number;
    messageCount: number;
    isQuotaHealthy: boolean;
  }>({ usedKb: 0, threadCount: 0, messageCount: 0, isQuotaHealthy: true });

  const [investigations, setInvestigations] = useState<InvestigationItem[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string>("session_default");
  // The thread on screen, readable from a stream callback that started under another thread.
  const activeThreadIdRef = useRef<string>("session_default");
  // The thread whose question is running; its live panel shows only while that thread is on screen.
  const [streamingThreadId, setStreamingThreadId] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);

  // Collapse the sidebar to its icon rail on narrow screens so the conversation keeps the space.
  useEffect(() => {
    const query = window.matchMedia("(max-width: 900px)");
    const sync = () => {
      if (query.matches) setIsSidebarOpen(false);
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  // Close modals when pressing Escape key
  useEffect(() => {
    if (!showSettings && !showMemoryVault) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setShowSettings(false);
        setShowMemoryVault(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showSettings, showMemoryVault]);
  // Modal dialogs keep Tab inside them and return focus to the opener when they close.
  const settingsDialogRef = useFocusTrap<HTMLDivElement>(showSettings);
  const memoryDialogRef = useFocusTrap<HTMLDivElement>(showMemoryVault);
  const [openMenuThreadId, setOpenMenuThreadId] = useState<string | null>(null);
  const [editingThreadId, setEditingThreadId] = useState<string | null>(null);
  const [editTitleInput, setEditTitleInput] = useState<string>("");
  const [hoveredThreadId, setHoveredThreadId] = useState<string | null>(null);
  const [isHoveredExpand, setIsHoveredExpand] = useState<boolean>(false);

  // Grouping helper based on real investigation timestamps
  const getInvestigationDateGroup = (dateIso?: string): string => {
    if (!dateIso) return "Today";
    try {
      const d = new Date(dateIso);
      const now = new Date();
      const diffMs = now.getTime() - d.getTime();
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
      if (diffDays <= 0) return "Today";
      if (diffDays === 1) return "Yesterday";
      if (diffDays <= 7) return "Previous 7 days";
      return "Older";
    } catch {
      return "Today";
    }
  };

  const refreshLocalStorageStats = () => {
    setLocalStorageStats(localStorageService.getStorageUsage());
  };

  const handleSelectInvestigation = (inv: InvestigationItem) => {
    activeThreadIdRef.current = inv.id;
    setActiveThreadId(inv.id);
    setThreadId(inv.id);
    localStorageService.setActiveThreadId(inv.id);
    const msgs = localStorageService.getMessages(inv.id);
    setMessages(msgs);
    refreshLocalStorageStats();
  };

  const handleDeleteInvestigation = (id: string) => {
    if (streamingThreadId === id) {
      window.alert("This investigation is still running. Stop it before deleting it.");
      return;
    }
    if (!window.confirm("Delete this investigation? Its saved messages and the server's memory of it are removed.")) return;
    // Server memory is scoped to the thread; without this it outlives the conversation (orphaned).
    void deleteServerThreadMemory(id).then(() => fetchMemoryVault());
    localStorageService.deleteInvestigation(id);
    const updated = localStorageService.getInvestigations();
    setInvestigations(updated);
    refreshLocalStorageStats();
    if (activeThreadId === id) {
      if (updated.length > 0) {
        handleSelectInvestigation(updated[0]);
      } else {
        handleResetSession();
      }
    }
  };

  const handleStartRename = (inv: InvestigationItem) => {
    setEditingThreadId(inv.id);
    setEditTitleInput(inv.title);
    setOpenMenuThreadId(null);
  };

  const handleSaveRename = (id: string) => {
    const trimmed = editTitleInput.trim();
    if (trimmed) {
      localStorageService.renameInvestigation(id, trimmed);
      setInvestigations(localStorageService.getInvestigations());
      refreshLocalStorageStats();
    }
    setEditingThreadId(null);
  };

  const handleCancelRename = () => {
    setEditingThreadId(null);
  };

  /** Applies a history-storage preference and reloads the (possibly moved or pruned) history. */
  const changeHistoryPrefs = (next: Partial<HistoryPrefs>) => {
    const prefs = localStorageService.setHistoryPrefs(next);
    setHistoryPrefs(prefs);
    setInvestigations(localStorageService.getInvestigations());
    // The open conversation stays on screen; save it to the new location so a reload in the same session keeps it.
    if (messages.length) localStorageService.saveMessages(threadId, messages);
    refreshLocalStorageStats();
  };

  const handleClearAllLocalHistory = () => {
    if (
      !window.confirm(
        "Are you sure you want to clear all locally saved investigations and chat history from browser storage?"
      )
    )
      return;
    localStorageService.clearAllLocalData();
    const invs = localStorageService.getInvestigations();
    setInvestigations(invs);
    const firstId = invs[0]?.id || `session_${Date.now()}`;
    activeThreadIdRef.current = firstId;
    setActiveThreadId(firstId);
    setThreadId(firstId);
    setMessages([]);
    refreshLocalStorageStats();
  };

  const handleCopyMessage = async (msgId: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedMessageId(msgId);
      setTimeout(() => setCopiedMessageId(null), 2000);
    } catch (err) {
      console.error("Failed to copy message:", err);
    }
  };

  function normalizeLegacyAgentNames(text: string): string {
    if (!text) return "";
    return text
      .replace(/\bTalk2KnowledgeGraphs?\b/gi, "Synapta")
      .replace(/\bTalk2BioModels?\b/gi, "Vivarix")
      .replace(/\bTalk2Cells?\b/gi, "Lumicell")
      .replace(/\bTalk2Scholars?\b/gi, "Lithoscribe")
      .replace(/\bTalk2AI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b/gi, "Phronesis")
      .replace(/\bAI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b/gi, "Phronesis");
  }

  const chatEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea dynamically based on text content to avoid cutting off or clipping text
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      // An empty box stays one line tall; measuring would count the wrapped placeholder.
      const scrollHeight = inputMessage ? textareaRef.current.scrollHeight : 0;
      const targetHeight = Math.min(Math.max(scrollHeight, 28), 200);
      textareaRef.current.style.height = `${targetHeight}px`;
    }
  }, [inputMessage]);

  // UI-007: saved history belongs to one backend + access token. It is (re)loaded once the stored credentials are
  // known and whenever either changes, so another user's token never shows (or deletes) this user's investigations.
  useEffect(() => {
    if (!credentialsLoaded) return;
    localStorageService.configure(backendUrl, authToken);
    setHistoryPrefs(localStorageService.getHistoryPrefs());
    const savedInvs = localStorageService.getInvestigations();
    setInvestigations(savedInvs);
    const initialActiveId = localStorageService.getActiveThreadId();
    activeThreadIdRef.current = initialActiveId;
    setActiveThreadId(initialActiveId);
    setThreadId(initialActiveId);
    setMessages(localStorageService.getMessages(initialActiveId));
    refreshLocalStorageStats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentialsLoaded, backendUrl, authToken]);

  // UI-008: slimming, pruning and expiry of saved history are announced, never silent.
  useEffect(() => localStorageService.subscribe((notice) => setStorageNotice(notice)), []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        handleResetSession();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const scrollToBottom = (_instant: boolean = false) => {
    // Scroll only the messages panel. scrollIntoView would also scroll every ancestor, including the
    // overflow-hidden <main>, pushing the header off screen and leaving a blank band under the composer.
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
    }
  };

  useEffect(() => {
    // The welcome screen reads top-down; only follow the bottom once a conversation exists.
    if (messages.length === 0 && !isProcessing) {
      if (messagesContainerRef.current) messagesContainerRef.current.scrollTop = 0;
      return;
    }
    // Follow new output only while the reader is at the bottom; scrolling up to read pauses it.
    if (!isNearBottomRef.current) return;
    scrollToBottom(true);
    // Double-check scroll after DOM paint
    const t = setTimeout(() => scrollToBottom(true), 50);
    return () => clearTimeout(t);
  }, [messages, isProcessing, liveThoughts, liveReasoning, liveContent]);

  // The reveal grows the answer between stream events: keep following it while the reader is at the bottom.
  const handleLiveReveal = useCallback((text: string) => {
    liveShownRef.current = text;
    const el = messagesContainerRef.current;
    if (el && isNearBottomRef.current) el.scrollTop = el.scrollHeight;
  }, []);

  const handleSavedReveal = useCallback(() => {
    const el = messagesContainerRef.current;
    if (el && isNearBottomRef.current) el.scrollTop = el.scrollHeight;
  }, []);

  const handleMessagesScroll = () => {
    const el = messagesContainerRef.current;
    if (el) isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  // Live timer tick during processing
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null;
    if (isProcessing && liveStartTime) {
      interval = setInterval(() => {
        setLiveElapsedSeconds(Math.round(((Date.now() - liveStartTime) / 1000) * 10) / 10);
      }, 100);
    } else {
      setLiveElapsedSeconds(0);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [isProcessing, liveStartTime]);

  const domainAgents: AgentMetadata[] = [
    {
      id: "t2kg",
      name: "Synapta",
      domain: "PrimeKG & Target Networks",
      description: "Full PrimeKG graph analytics: repurposing, mechanism paths, phenotype differentials, regimen checks.",
      tools: ["PrimeKG Full Graph", "PrimeKG Subgraphs", "BioContext OpenTargets", "BioContext KEGG", "Entity normalization", "ChEMBL", "ClinVar/CIViC/cBioPortal/gnomAD"],
      color: "#38bdf8",
      icon: <Dna className="w-4 h-4 text-sky-400" />,
    },
    {
      id: "t2b",
      name: "Vivarix",
      domain: "Kinetic Modeling & ODEs",
      description: "COPASI BASICO, SBML simulations, parameter scans, steady-state curves.",
      tools: ["SimulateModel", "SearchModels", "BioContext UniProt", "Uploaded SBML"],
      color: "#10b981",
      icon: <Activity className="w-4 h-4 text-emerald-400" />,
    },
    {
      id: "scp",
      name: "Lumicell",
      domain: "Single-Cell Transcriptomics",
      description: "Single Cell Portal, AnnData scRNA-seq matrices, cell cluster markers.",
      tools: ["SingleCellPortal", "BioContext PanglaoDB", "BioContext Ensembl", "Uploaded .h5ad/.csv"],
      color: "#a855f7",
      icon: <Microscope className="w-4 h-4 text-purple-400" />,
    },
    {
      id: "s2",
      name: "Lithoscribe",
      domain: "Literature & Clinical Trials",
      description: "PubMed, EuropePMC, Semantic Scholar, clinical paper synthesis.",
      tools: ["SemanticScholar", "BioContext EuropePMC", "ClinicalTrials.gov", "openFDA"],
      color: "#f59e0b",
      icon: <BookOpen className="w-4 h-4 text-amber-400" />,
    },
  ];

  const agentIdMap: Record<string, string> = {
    Synapta: "t2kg",
    Vivarix: "t2b",
    Lumicell: "scp",
    Lithoscribe: "s2",
  };

  const flushLiveText = () => {
    liveFlushRef.current = null;
    setLiveReasoning(liveReasoningRef.current);
    if (!liveResetPendingRef.current) setLiveContent(liveContentRef.current);
  };

  const scheduleLiveFlush = () => {
    if (liveFlushRef.current === null) {
      liveFlushRef.current = requestAnimationFrame(flushLiveText);
    }
  };

  const resetLiveState = () => {
    if (liveFlushRef.current !== null) {
      cancelAnimationFrame(liveFlushRef.current);
      liveFlushRef.current = null;
    }
    liveReasoningRef.current = "";
    liveContentRef.current = "";
    liveResetPendingRef.current = false;
    liveShownRef.current = "";
    setLiveThoughts([]);
    setLiveReasoning("");
    setLiveContent("");
    liveRunIdRef.current = null;
    liveRevisedRef.current = false;
    setLiveRunId(null);
    setLiveApproval(null);
    setLiveRevised(false);
    setLiveSteers([]);
    setStoppingRun(false);
  };

  const sanitizeThoughts = (thoughts: ThoughtStep[]): ThoughtStep[] =>
    thoughts.map((th) => ({
      ...th,
      title: normalizeLegacyAgentNames(th.title || ""),
      text: normalizeLegacyAgentNames(th.text || ""),
    }));

  // Intelligent dynamic conversation title generator (biomedical-aware heuristic)
  const conversationTitle = (text: string, max = 52) => generateDynamicTitle(text, max);

  /** Refines the conversation title via background AI endpoint without blocking user interaction */
  const refineConversationTitleAsync = (targetThreadId: string, queryText: string, modelToUse?: string) => {
    if (!queryText.trim() || !targetThreadId) return;
    void fetchConversationTitle(backendUrl, authHeaders, queryText, modelToUse, apiKey)
      .then((res) => {
        if (res?.title) {
          const updated = localStorageService.updateInvestigationTitle(targetThreadId, res.title, false);
          if (updated) {
            setInvestigations(localStorageService.getInvestigations());
            refreshLocalStorageStats();
          }
        }
      })
      .catch(() => {
        // Fallback dynamic title is already set instantly
      });
  };

  const nowTimestamp = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const buildAssistantMessage = (res: ChatResult, model: string): Message => {
    // UI-009: an empty or missing answer is a failure, never a fabricated "completed" line.
    const answer = resolveAnswer(res);
    return {
      id: `ast_${Date.now()}`,
      sender: "assistant",
      content: normalizeLegacyAgentNames(answer.content),
      agent: "Clinical Orchestrator",
      model: res.model_used || model,
      steps: res.steps || [],
      // The run is done: a step the backend left "running" must not spin forever.
      thoughts: finalizeThoughts(sanitizeThoughts(res.thoughts || [])),
      reasoning: res.reasoning || "",
      selected_agents: res.selected_agents || [],
      duration_seconds: res.duration_seconds,
      verification_report: res.verification_report,
      task_status: answer.task_status,
      framework_used: res.framework_used || undefined,
      execution_artifacts: res.execution_artifacts,
      citations: res.citations && Object.keys(res.citations.entries || {}).length ? res.citations : undefined,
      run_manifest: res.run_manifest || undefined,
      query_context: res.query_context || undefined,
      run_id: liveRunIdRef.current || (res.execution_artifacts?.agent_run?.run_id as string | undefined) || undefined,
      answer_revised: liveRevisedRef.current || undefined,
      timestamp: nowTimestamp(),
    };
  };

  // The question an answer replies to: the nearest earlier user message.
  const previousUserContent = (idx: number): string => {
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i]?.sender === "user") return messages[i].content;
    }
    return "";
  };

  /**
   * Saves an answer to the thread it was asked in. The view only changes if that thread is still on
   * screen: switching investigations mid-run must not paint the old thread into the new one.
   */
  const appendAssistantMessage = (assistantMessage: Message, baseMessages: Message[], forThreadId: string) => {
    // The answer was already on screen while streaming; don't replay the entrance animation.
    if (liveContentRef.current) {
      justStreamedIdRef.current = assistantMessage.id;
      // The saved message picks up the reveal where the live view left it and finishes it smoothly.
      streamHandoffRef.current = { id: assistantMessage.id, text: liveShownRef.current, at: performance.now() };
    }
    const allMessages = [...baseMessages, assistantMessage];
    localStorageService.saveMessages(forThreadId, allMessages);
    if (activeThreadIdRef.current === forThreadId) setMessages(allMessages);
    setInvestigations(localStorageService.getInvestigations());
    refreshLocalStorageStats();
  };

  const handleStopGenerating = () => {
    abortRef.current?.abort();
  };

  /**
   * Stop button: an agentic run is cancelled on the server so the partial answer is kept; a second click,
   * a failed cancel, or a run without a run id drops the connection instead.
   */
  const handleStopRun = () => {
    const runId = liveRunIdRef.current;
    if (!runId || stoppingRun) {
      handleStopGenerating();
      return;
    }
    setStoppingRun(true);
    cancelRun(backendUrl, authHeaders, runId).catch(() => handleStopGenerating());
  };

  const sendSteer = async (note: SteerNote) => {
    const runId = liveRunIdRef.current;
    if (!runId) return;
    setLiveSteers((prev) => [...prev.filter((n) => n.id !== note.id), { ...note, status: "sending" }]);
    let status: SteerNote["status"] = "sent";
    try {
      await steerRun(backendUrl, authHeaders, runId, note.text);
    } catch {
      status = "failed";
    }
    setLiveSteers((prev) => prev.map((n) => (n.id === note.id ? { ...n, status } : n)));
  };

  /**
   * Handler for one stream event (chat stream or a background job's event log). Thoughts accumulate in
   * `accumulatedThoughts`; the final result is passed to onDone; an error event throws.
   */
  const createStreamEventHandler = (accumulatedThoughts: ThoughtStep[], onDone: (result: ChatResult | null | undefined) => void) => (event: unknown) => {
    const parsed = event as StreamEvent | null;
    switch (parsed?.type) {
      case "thought": {
        const [th] = sanitizeThoughts([parsed.data]);
        const idx = th.id ? accumulatedThoughts.findIndex((t) => t.id === th.id) : -1;
        if (idx >= 0) accumulatedThoughts[idx] = th;
        else accumulatedThoughts.push(th);
        setLiveThoughts([...accumulatedThoughts]);
        if (th.agent) {
          setActiveAgentId(agentIdMap[normalizeLegacyAgentNames(th.agent)] || "orchestrator");
        }
        break;
      }
      case "reasoning_delta":
        liveReasoningRef.current += parsed.delta || "";
        scheduleLiveFlush();
        break;
      case "content_delta":
        liveContentRef.current += parsed.delta || "";
        liveResetPendingRef.current = false;
        scheduleLiveFlush();
        break;
      case "content_reset":
        liveContentRef.current = "";
        liveResetPendingRef.current = true;
        if (parsed.reason === "agent_round") {
          liveRevisedRef.current = true;
          setLiveRevised(true);
        }
        scheduleLiveFlush();
        break;
      case "run_control":
        if (typeof parsed.run_id === "string" && parsed.run_id) {
          liveRunIdRef.current = parsed.run_id;
          setLiveRunId(parsed.run_id);
        }
        break;
      case "approval_request":
        if (typeof parsed.approval_id === "string" && parsed.approval_id) {
          const runId = typeof parsed.run_id === "string" && parsed.run_id ? parsed.run_id : liveRunIdRef.current;
          if (runId) setLiveApproval({ ...parsed, type: "approval_request", run_id: runId, received_at: Date.now() });
        }
        break;
      case "done":
        setLiveApproval(null);
        onDone(parsed.result);
        break;
      case "error":
        throw new Error(parsed.detail || "The orchestrator reported an error.");
    }
  };

  const newClientRequestId = (): string =>
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;

  /** The request body for a chat or job; agentic fields are only added for agentic frameworks. */
  const buildRequestPayload = (text: string, sendThreadId: string, sendModel: string): Record<string, unknown> => {
    const body: Record<string, unknown> = {
      message: text,
      thread_id: sendThreadId,
      model: sendModel,
      enable_web_search: isWebSearchEnabled,
      api_key: apiKey || undefined,
      dataset_ids: attachedDatasets.length ? attachedDatasets.map((d) => d.id) : undefined,
      framework: selectedFramework,
      // UI-021: one id per question, reused by the JSON fallback retry, so a server that saw the first POST can
      // recognise the retry as the same request instead of starting a second run.
      client_request_id: newClientRequestId(),
    };
    if (selectedFramework !== ORCHESTRATION_FRAMEWORK) {
      body.agent_options = toAgentOptions(agentOptions);
      if (projectId) body.project_id = projectId;
    }
    return body;
  };

  const handleSendMessage = async (textToSend?: string) => {
    const text = textToSend || inputMessage;
    if (!text.trim() || isProcessing) return;
    if (runInBackground && !textToSend) {
      void handleSubmitJob(text);
      return;
    }
    const sendThreadId = threadId;
    const sendModel = selectedModel;

    const userMessage: Message = {
      id: `usr_${Date.now()}`,
      sender: "user",
      content: text,
      attachments: attachedDatasets.length ? [...attachedDatasets] : undefined,
      timestamp: nowTimestamp(),
    };

    const currentMessages = [...messages, userMessage];
    setMessages(currentMessages);
    const dynamicTitle = conversationTitle(text);
    localStorageService.saveMessages(sendThreadId, currentMessages, dynamicTitle);
    setInvestigations(localStorageService.getInvestigations());
    refreshLocalStorageStats();
    refineConversationTitleAsync(sendThreadId, text, sendModel);

    setInputMessage("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "28px";
    }
    const startedAt = Date.now();
    setIsProcessing(true);
    setStreamingThreadId(sendThreadId);
    resetLiveState();
    setLiveStartTime(startedAt);
    setActiveAgentId("orchestrator");
    isNearBottomRef.current = true;

    const controller = new AbortController();
    abortRef.current = controller;
    const accumulatedThoughts: ThoughtStep[] = [];
    let streamOpened = false;

    const requestBody = JSON.stringify(buildRequestPayload(text, sendThreadId, sendModel));

    const systemMessage = (content: string): Message => ({
      id: `err_${Date.now()}`,
      sender: "assistant",
      content,
      agent: "System",
      model: sendModel,
      steps: [],
      thoughts: [],
      selected_agents: [],
      timestamp: nowTimestamp(),
    });

    try {
      // An HTTP answer is final (auth, validation, rate limit, server error): the JSON endpoint would say the same.
      const response = await apiFetch(
        `${backendUrl}/api/chat/stream`,
        { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: requestBody, signal: controller.signal },
        { ...pageApi, style: "detailed" }
      );
      if (!response.body) throw new Error("The streaming response had no body");
      streamOpened = true;

      let finalResult: ChatResult | null = null;
      const handleEvent = createStreamEventHandler(accumulatedThoughts, (result) => {
        finalResult = result ?? null;
      });
      // Heartbeat comments (": ping") keep the stall timer alive; a silent connection throws "Connection stalled".
      await readSseEvents(response.body, (ev) => {
        handleEvent(ev);
        return !!finalResult;
      });

      if (!finalResult) {
        throw new Error("The connection closed before the answer finished.");
      }
      appendAssistantMessage(buildAssistantMessage(finalResult, sendModel), currentMessages, sendThreadId);
    } catch (err: unknown) {
      const aborted = controller.signal.aborted;
      if (err instanceof ApiError && err.status > 0) {
        appendAssistantMessage(systemMessage(`> ⚠️ **Request not accepted.** ${err.message}`), currentMessages, sendThreadId);
      } else if (!streamOpened && !aborted && err instanceof TypeError) {
        // Only a network failure (fetch TypeError, e.g. a proxy that blocks streaming) retries the JSON endpoint once.
        try {
          const result = await requestJson<ChatResult | null>(
            `${backendUrl}/api/chat`,
            { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: requestBody, signal: controller.signal },
            { ...pageApi, style: "detailed" }
          );
          appendAssistantMessage(buildAssistantMessage(result ?? {}, sendModel), currentMessages, sendThreadId);
        } catch (jsonErr: unknown) {
          if (controller.signal.aborted) {
            appendAssistantMessage(systemMessage("> Stopped before the answer started."), currentMessages, sendThreadId);
          } else if (jsonErr instanceof ApiError && jsonErr.status > 0) {
            appendAssistantMessage(systemMessage(`> ⚠️ **Request not accepted.** ${jsonErr.message}`), currentMessages, sendThreadId);
          } else {
            // Honest error handling without fabricating simulated outputs
            appendAssistantMessage(
              systemMessage(
                `> ⚠️ **Phronesis backend unreachable** at \`${backendUrl}\`.\n>\n` +
                  `> ${errorMessage(jsonErr) || errorMessage(err) || "The server is offline, unreachable, or timed out"}.\n>\n` +
                  "> Start it with `uvicorn app.backend.main:app --port 8000`, then send your question again."
              ),
              currentMessages,
              sendThreadId
            );
          }
        }
      } else {
        // The run started: keep what already arrived instead of discarding the work.
        const partial = liveContentRef.current.trim();
        const note = aborted
          ? "> Stopped. The answer above is incomplete and was not verified."
          : `> ⚠️ **The answer stopped early**: ${errorMessage(err, "connection lost")}`;
        appendAssistantMessage(
          {
            id: `ast_${Date.now()}`,
            sender: "assistant",
            content: partial ? `${partial}\n\n${note}` : note,
            agent: "Clinical Orchestrator",
            model: sendModel,
            steps: [],
            thoughts: sanitizeThoughts(accumulatedThoughts).map((th) =>
              th.status === "running" ? { ...th, status: "error", text: aborted ? "Stopped" : "Interrupted" } : th
            ),
            reasoning: liveReasoningRef.current,
            selected_agents: [],
            duration_seconds: Math.round((Date.now() - startedAt) / 100) / 10,
            task_status: {
              task_status: aborted ? "cancelled" : "failed",
              reason: aborted ? "Stopped by you before the answer finished." : "The connection ended before the answer finished.",
            },
            timestamp: nowTimestamp(),
          },
          currentMessages,
          sendThreadId
        );
      }
    } finally {
      abortRef.current = null;
      setIsProcessing(false);
      setStreamingThreadId(null);
      resetLiveState();
      setActiveAgentId(null);
    }
  };

  /** Queues the question as a background job (POST /api/jobs); the answer is read later from the Jobs panel. */
  const handleSubmitJob = async (text: string) => {
    const sendThreadId = threadId;
    const sendModel = selectedModel;
    const userMessage: Message = {
      id: `usr_${Date.now()}`,
      sender: "user",
      content: text,
      attachments: attachedDatasets.length ? [...attachedDatasets] : undefined,
      timestamp: nowTimestamp(),
    };
    const currentMessages = [...messages, userMessage];
    setMessages(currentMessages);
    const dynamicTitle = conversationTitle(text);
    localStorageService.saveMessages(sendThreadId, currentMessages, dynamicTitle);
    setInvestigations(localStorageService.getInvestigations());
    refreshLocalStorageStats();
    refineConversationTitleAsync(sendThreadId, text, sendModel);
    setInputMessage("");
    if (textareaRef.current) textareaRef.current.style.height = "28px";
    isNearBottomRef.current = true;

    let content: string;
    let jobId: string | undefined;
    try {
      const res = await createJob(backendUrl, authHeaders, buildRequestPayload(text, sendThreadId, sendModel));
      jobId = res.job_id;
      content =
        `> Queued as background job \`${res.job_id}\`. It keeps running on the server, even if you close this tab. ` +
        "Open it from **Jobs** to follow it live or to read the answer.";
      setJobsRefreshKey((k) => k + 1);
    } catch (err: unknown) {
      const status = errorStatus(err);
      if (status === 401 || status === 403) setAuthProblem("The API rejected the access token. Set it under Settings → Phronesis Access Token.");
      content = `> ⚠️ **Background job not started.** ${errorMessage(err, "The backend did not accept the job.")}`;
    }
    appendAssistantMessage(
      {
        id: `job_${Date.now()}`,
        sender: "assistant",
        content,
        agent: "Background jobs",
        model: sendModel,
        steps: [],
        thoughts: [],
        selected_agents: [],
        job_id: jobId,
        timestamp: nowTimestamp(),
      },
      currentMessages,
      sendThreadId
    );
  };

  /** Reattaches to a background job's event log (replay, then follow) and renders it as a live answer. */
  const handleOpenJob = async (job: JobSummary) => {
    if (isProcessing) return;
    // UI-014: a job's answer belongs to the investigation it was asked in (its server memory is that thread's), not to
    // whatever investigation is on screen. Switch to it, creating its record if this browser no longer has it.
    const jobThreadId = typeof job.request?.thread_id === "string" && job.request.thread_id ? job.request.thread_id : threadId;
    const sendThreadId = jobThreadId;
    const sendModel = (typeof job.request?.model === "string" && job.request.model) || selectedModel;
    const question = typeof job.request?.message === "string" ? job.request.message : "";
    let baseMessages = sendThreadId === threadId ? [...messages] : localStorageService.getMessages(sendThreadId);
    if (sendThreadId !== threadId) {
      if (!localStorageService.getInvestigations().some((i) => i.id === sendThreadId)) {
        const now = new Date().toISOString();
        localStorageService.saveInvestigation({
          id: sendThreadId,
          title: conversationTitle(jobTitle(job)),
          createdAt: now,
          updatedAt: now,
          messageCount: 0,
          preview: "Background job",
        });
      }
      activeThreadIdRef.current = sendThreadId;
      setActiveThreadId(sendThreadId);
      setThreadId(sendThreadId);
      localStorageService.setActiveThreadId(sendThreadId);
      setMessages(baseMessages);
      setInvestigations(localStorageService.getInvestigations());
    }
    if (question && !baseMessages.some((m) => m.sender === "user" && m.content === question)) {
      baseMessages = [...baseMessages, { id: `usr_${Date.now()}`, sender: "user", content: question, timestamp: nowTimestamp() }];
      setMessages(baseMessages);
      localStorageService.saveMessages(sendThreadId, baseMessages, conversationTitle(jobTitle(job)));
      setInvestigations(localStorageService.getInvestigations());
    }

    const startedAt = Date.now();
    setIsProcessing(true);
    setStreamingThreadId(sendThreadId);
    resetLiveState();
    setLiveStartTime(startedAt);
    setActiveAgentId("orchestrator");
    isNearBottomRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    const accumulatedThoughts: ThoughtStep[] = [];
    let finalResult: ChatResult | null = null;
    let doneSeen = false;

    try {
      const handleEvent = createStreamEventHandler(accumulatedThoughts, (result) => {
        doneSeen = true;
        finalResult = result ?? null;
      });
      // Replay from the start (the live view was just reset); a dropped connection resumes from the last seq.
      await streamJobEvents(
        backendUrl,
        authHeaders,
        job.job_id,
        0,
        (ev) => {
          handleEvent(ev);
          return doneSeen;
        },
        controller.signal
      );
      if (!finalResult && doneSeen) {
        // The done event may not repeat the result; the job record holds it.
        const record = await getJob(backendUrl, authHeaders, job.job_id, controller.signal).catch(() => null);
        finalResult = (record?.result as ChatResult | null | undefined) ?? null;
      }
      if (!finalResult) throw new Error("The job's event stream ended before the answer finished.");
      appendAssistantMessage({ ...buildAssistantMessage(finalResult, sendModel), job_id: job.job_id }, baseMessages, sendThreadId);
    } catch (err: unknown) {
      const aborted = controller.signal.aborted;
      const partial = liveContentRef.current.trim();
      const note = aborted
        ? "> Detached from the background job; it keeps running on the server. Open it again from **Jobs**."
        : `> ⚠️ **Could not follow the background job**: ${errorMessage(err, "connection lost")}`;
      appendAssistantMessage(
        {
          id: `ast_${Date.now()}`,
          sender: "assistant",
          content: partial ? `${partial}\n\n${note}` : note,
          agent: "Background jobs",
          model: sendModel,
          steps: [],
          thoughts: sanitizeThoughts(accumulatedThoughts).map((th) =>
            th.status === "running" ? { ...th, status: "error", text: aborted ? "Detached" : "Interrupted" } : th
          ),
          reasoning: liveReasoningRef.current,
          selected_agents: [],
          duration_seconds: Math.round((Date.now() - startedAt) / 100) / 10,
          job_id: job.job_id,
          // Detaching leaves the job running on the server; any other failure means no answer was shown (UI-014).
          task_status: aborted
            ? undefined
            : { task_status: "failed", reason: `Could not follow the background job: ${errorMessage(err, "connection lost")}` },
          timestamp: nowTimestamp(),
        },
        baseMessages,
        sendThreadId
      );
    } finally {
      abortRef.current = null;
      setIsProcessing(false);
      setStreamingThreadId(null);
      resetLiveState();
      setActiveAgentId(null);
      setJobsRefreshKey((k) => k + 1);
    }
  };

  const handleResetSession = () => {
    const newInv = localStorageService.createNewThread(
      "New Clinical Investigation",
      selectedFramework,
      selectedModel
    );
    setInvestigations(localStorageService.getInvestigations());
    activeThreadIdRef.current = newInv.id;
    setActiveThreadId(newInv.id);
    setThreadId(newInv.id);
    setMessages([]);
    refreshLocalStorageStats();
  };

  // Static options, with the backend's own descriptions (and availability) once /api/frameworks answered.
  const frameworkOptions: FrameworkOption[] = FRAMEWORK_OPTIONS.map((fw) => {
    if (!frameworkCatalog) return fw;
    const info = frameworkCatalog.find((f) => f.id === fw.id);
    return info
      ? { ...fw, description: info.description || fw.description }
      : { ...fw, available: false, description: "Not offered by this backend" };
  });
  const currentFramework = frameworkOptions.find((fw) => fw.id === selectedFramework) || frameworkOptions[0];
  const chooseFramework = (id: string) => {
    frameworkChosenRef.current = true;
    setSelectedFramework(id);
    setIsFrameworkDropdownOpen(false);
    try {
      localStorageService.saveSettings({ ...localStorageService.getSettings(), selectedFramework: id });
    } catch {}
  };
  // While an agentic run streams in this thread, the composer steers it instead of asking a new question.
  const liveHere = isProcessing && streamingThreadId === activeThreadId;
  const canSteer = liveHere && !!liveRunId && !stoppingRun;
  const submitComposer = () => {
    if (canSteer) {
      const text = inputMessage.trim();
      if (!text) return;
      setInputMessage("");
      void sendSteer({ id: `steer_${Date.now()}`, text: text.slice(0, 2000), status: "sending" });
      return;
    }
    handleSendMessage();
  };
  const isAgenticFramework = selectedFramework !== ORCHESTRATION_FRAMEWORK;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "row",
        height: "100vh",
        width: "100vw",
        overflow: "clip",
        backgroundColor: "var(--bg-primary)",
        color: "var(--text-main)",
        transition: "background-color 0.25s ease, color 0.25s ease",
      }}
    >
      {/* Left Sidebar: Collapsible Modern Architecture (260px expanded, 60px collapsed icon-rail) */}
      <aside
        style={{
          width: isSidebarOpen ? "260px" : "60px",
          minWidth: isSidebarOpen ? "260px" : "60px",
          borderRight: "1px solid var(--border-subtle)",
          backgroundColor: "var(--bg-sidebar)",
          display: "flex",
          flexDirection: "column",
          height: "100vh",
          overflow: "hidden",
          transition: "width 0.26s cubic-bezier(0.2, 0, 0, 1), background-color 0.25s ease",
          flexShrink: 0,
          zIndex: 40,
        }}
      >
        {/* 1. Sidebar Top Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: isSidebarOpen ? "space-between" : "center",
            padding: isSidebarOpen ? "10px 12px 6px 14px" : "10px 0 6px 0",
            height: "48px",
            minHeight: "48px",
            flexShrink: 0,
            overflow: "hidden",
            boxSizing: "border-box",
            transition: "padding 0.2s ease",
          }}
        >
          {isSidebarOpen ? (
            <>
              {/* Phronesis Brand */}
              <div style={{ display: "flex", alignItems: "center", height: "26px", overflow: "hidden" }}>
                <img
                  src="/logo-tight.png"
                  alt="Phronesis"
                  className="phronesis-tight-logo"
                  style={{
                    height: "22px",
                    width: "auto",
                    objectFit: "contain",
                    display: "inline-block",
                  }}
                />
              </div>

              {/* Close Sidebar Icon Button */}
              <button
                type="button"
                onClick={() => setIsSidebarOpen(false)}
                style={{
                  width: "32px",
                  height: "32px",
                  borderRadius: "8px",
                  backgroundColor: "transparent",
                  border: "none",
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  transition: "all 0.15s ease",
                  padding: 0,
                  flexShrink: 0,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.color = "var(--text-heading)";
                  e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.color = "var(--text-muted)";
                  e.currentTarget.style.backgroundColor = "transparent";
                }}
                title="Collapse sidebar"
              >
                <PanelLeftClose style={{ width: 18, height: 18 }} />
              </button>
            </>
          ) : (
            /* Collapsed Header: Brand Icon by default -> PanelLeft on hover */
            <button
              type="button"
              onClick={() => setIsSidebarOpen(true)}
              style={{
                width: "36px",
                height: "36px",
                borderRadius: "8px",
                backgroundColor: isHoveredExpand ? "var(--bg-surface-hover)" : "transparent",
                border: "none",
                color: isHoveredExpand ? "var(--text-heading)" : "var(--text-muted)",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transition: "all 0.15s ease",
                padding: 0,
              }}
              onMouseEnter={() => setIsHoveredExpand(true)}
              onMouseLeave={() => setIsHoveredExpand(false)}
              title="Expand sidebar"
            >
              {isHoveredExpand ? (
                <PanelLeft
                  style={{
                    width: 20,
                    height: 20,
                    color: "var(--text-heading)",
                  }}
                />
              ) : (
                <img
                  src="/icon.png"
                  alt="Phronesis"
                  className="phronesis-icon"
                  style={{
                    width: "22px",
                    height: "22px",
                    objectFit: "contain",
                    display: "block",
                  }}
                />
              )}
            </button>
          )}
        </div>

        {/* 2. New Investigation Button */}
        <div
          style={{
            padding: isSidebarOpen ? "4px 10px 8px 10px" : "4px 0 8px 0",
            display: "flex",
            justifyContent: "center",
            flexShrink: 0,
            transition: "padding 0.2s ease",
          }}
        >
          {isSidebarOpen ? (
            <button
              type="button"
              onClick={handleResetSession}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                width: "100%",
                padding: "8px 10px",
                borderRadius: "8px",
                backgroundColor: "transparent",
                border: "1px solid var(--border-subtle)",
                color: "var(--text-main)",
                fontSize: "13px",
                fontWeight: 500,
                cursor: "pointer",
                transition: "all 0.15s ease",
                whiteSpace: "nowrap",
                overflow: "hidden",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
                e.currentTarget.style.borderColor = "var(--border-highlight)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = "transparent";
                e.currentTarget.style.borderColor = "var(--border-subtle)";
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <SquarePen style={{ width: 14, height: 14, color: "var(--text-muted)", flexShrink: 0 }} />
                New investigation
              </span>
              <span
                style={{
                  fontSize: "11px",
                  color: "var(--text-faint)",
                  fontFamily: "var(--font-mono, monospace)",
                  padding: "1.5px 5px",
                  borderRadius: "4px",
                  backgroundColor: "var(--bg-surface-hover)",
                  border: "1px solid var(--border-subtle)",
                  flexShrink: 0,
                }}
              >
                ⌘N
              </span>
            </button>
          ) : (
            <button
              type="button"
              onClick={handleResetSession}
              title="New investigation (⌘N)"
              style={{
                width: "36px",
                height: "36px",
                borderRadius: "8px",
                backgroundColor: "transparent",
                border: "1px solid var(--border-subtle)",
                color: "var(--text-main)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                transition: "all 0.15s ease",
                padding: 0,
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
                e.currentTarget.style.borderColor = "var(--border-highlight)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = "transparent";
                e.currentTarget.style.borderColor = "var(--border-subtle)";
              }}
            >
              <SquarePen style={{ width: 16, height: 16, color: "var(--text-main)" }} />
            </button>
          )}
        </div>

        {/* 3. History List (Expanded Grouped List vs Collapsed Icons) */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            flex: 1,
            overflowY: "auto",
            overflowX: "hidden",
            padding: isSidebarOpen ? "0 8px 10px 8px" : "0 6px 10px 6px",
            transition: "padding 0.2s ease",
          }}
        >
          {isSidebarOpen ? (
            /* Expanded Grouped List */
            ["Today", "Yesterday", "Previous 7 days", "Older"].map((groupName) => {
              const groupItems = investigations.filter(
                (inv) => getInvestigationDateGroup(inv.updatedAt || inv.createdAt) === groupName
              );
              if (groupItems.length === 0) return null;

              return (
                <div key={groupName} style={{ marginBottom: "12px" }}>
                  <div
                    style={{
                      fontSize: "11px",
                      fontWeight: 600,
                      color: "var(--text-faint)",
                      padding: "8px 8px 4px 8px",
                      letterSpacing: "0.02em",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                    }}
                  >
                    {groupName}
                  </div>

                  <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                    {groupItems.map((inv) => {
                      const isActive = activeThreadId === inv.id;
                      const isEditing = editingThreadId === inv.id;
                      const isHovered = hoveredThreadId === inv.id;

                      if (isEditing) {
                        return (
                          <div
                            key={inv.id}
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: "6px",
                              height: "36px",
                              padding: "0 8px",
                              borderRadius: "8px",
                              backgroundColor: "var(--bg-surface-hover)",
                              border: "1px solid var(--domain-t2kg)",
                            }}
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="text"
                              value={editTitleInput}
                              onChange={(e) => setEditTitleInput(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") handleSaveRename(inv.id);
                                if (e.key === "Escape") handleCancelRename();
                              }}
                              autoFocus
                              style={{
                                flex: 1,
                                minWidth: 0,
                                background: "transparent",
                                border: "none",
                                outline: "none",
                                color: "var(--text-main)",
                                fontSize: "13px",
                                fontWeight: 500,
                              }}
                            />
                            <button
                              type="button"
                              onClick={() => handleSaveRename(inv.id)}
                              title="Save title"
                              style={{
                                background: "transparent",
                                border: "none",
                                color: "var(--domain-t2b)",
                                cursor: "pointer",
                                padding: "2px",
                                display: "flex",
                                alignItems: "center",
                              }}
                            >
                              <Check style={{ width: 14, height: 14 }} />
                            </button>
                            <button
                              type="button"
                              onClick={handleCancelRename}
                              title="Cancel"
                              style={{
                                background: "transparent",
                                border: "none",
                                color: "var(--text-faint)",
                                cursor: "pointer",
                                padding: "2px",
                                display: "flex",
                                alignItems: "center",
                              }}
                            >
                              <X style={{ width: 14, height: 14 }} />
                            </button>
                          </div>
                        );
                      }

                      return (
                        <div
                          key={inv.id}
                          onMouseEnter={() => setHoveredThreadId(inv.id)}
                          onMouseLeave={() => setHoveredThreadId(null)}
                          style={{
                            position: "relative",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            height: "36px",
                            padding: "0 8px 0 10px",
                            borderRadius: "8px",
                            backgroundColor: isActive
                              ? "var(--bg-surface-active)"
                              : isHovered
                              ? "var(--bg-surface-hover)"
                              : "transparent",
                            color: isActive ? "var(--text-heading)" : "var(--text-muted)",
                            cursor: "pointer",
                            fontSize: "13px",
                            fontWeight: isActive ? 500 : 400,
                            transition: "all 0.15s ease",
                            overflow: "hidden",
                          }}
                        >
                          <button
                            type="button"
                            className="thread-row-button"
                            onClick={() => handleSelectInvestigation(inv)}
                            onFocus={() => setHoveredThreadId(inv.id)}
                            aria-current={isActive ? "page" : undefined}
                            title={inv.title}
                          >
                            <span
                              style={{
                                flex: 1,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                                fontSize: "13px",
                                lineHeight: "20px",
                                paddingRight:
                                  isHovered || isActive || openMenuThreadId === inv.id ? "26px" : "0px",
                                transition: "padding-right 0.12s ease",
                              }}
                            >
                              {inv.title}
                              {streamingThreadId === inv.id ? " · running" : ""}
                            </span>
                          </button>

                          {(isHovered || isActive || openMenuThreadId === inv.id) && (
                            <div
                              className="investigation-menu-container"
                              style={{
                                position: "absolute",
                                right: "6px",
                                top: "50%",
                                transform: "translateY(-50%)",
                                display: "flex",
                                alignItems: "center",
                              }}
                              onClick={(e) => e.stopPropagation()}
                            >
                              <button
                                type="button"
                                onClick={() =>
                                  setOpenMenuThreadId((prev) => (prev === inv.id ? null : inv.id))
                                }
                                style={{
                                  background: "transparent",
                                  border: "none",
                                  color: "var(--text-muted)",
                                  cursor: "pointer",
                                  padding: "4px",
                                  borderRadius: "6px",
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  transition: "all 0.15s ease",
                                }}
                                onMouseEnter={(e) => {
                                  e.currentTarget.style.color = "var(--text-heading)";
                                  e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
                                }}
                                onMouseLeave={(e) => {
                                  e.currentTarget.style.color = "var(--text-muted)";
                                  e.currentTarget.style.backgroundColor = "transparent";
                                }}
                                title="Options"
                                aria-label={`Options for ${inv.title}`}
                                aria-haspopup="menu"
                                aria-expanded={openMenuThreadId === inv.id}
                              >
                                <MoreHorizontal style={{ width: 14, height: 14 }} />
                              </button>

                              {openMenuThreadId === inv.id && (
                                <div
                                  style={{
                                    position: "absolute",
                                    top: "calc(100% + 4px)",
                                    right: 0,
                                    width: "124px",
                                    backgroundColor: "var(--bg-surface)",
                                    border: "1px solid var(--border-subtle)",
                                    borderRadius: "8px",
                                    boxShadow: "var(--card-shadow)",
                                    padding: "4px",
                                    zIndex: 120,
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: "2px",
                                  }}
                                >
                                  <button
                                    type="button"
                                    onClick={() => handleStartRename(inv)}
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: "8px",
                                      padding: "6px 8px",
                                      borderRadius: "6px",
                                      background: "none",
                                      border: "none",
                                      color: "var(--text-main)",
                                      fontSize: "12px",
                                      cursor: "pointer",
                                      textAlign: "left",
                                      width: "100%",
                                      transition: "background 0.15s",
                                    }}
                                    onMouseEnter={(e) => {
                                      e.currentTarget.style.backgroundColor =
                                        "var(--bg-surface-hover)";
                                    }}
                                    onMouseLeave={(e) => {
                                      e.currentTarget.style.backgroundColor = "transparent";
                                    }}
                                  >
                                    <Edit2 style={{ width: 12, height: 12, color: "var(--domain-t2kg)" }} />
                                    Rename
                                  </button>

                                  <button
                                    type="button"
                                    onClick={() => {
                                      handleDeleteInvestigation(inv.id);
                                      setOpenMenuThreadId(null);
                                    }}
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: "8px",
                                      padding: "6px 8px",
                                      borderRadius: "6px",
                                      background: "none",
                                      border: "none",
                                      color: "#ef4444",
                                      fontSize: "12px",
                                      cursor: "pointer",
                                      textAlign: "left",
                                      width: "100%",
                                      transition: "background 0.15s",
                                    }}
                                    onMouseEnter={(e) => {
                                      e.currentTarget.style.backgroundColor =
                                        "rgba(239, 68, 68, 0.12)";
                                    }}
                                    onMouseLeave={(e) => {
                                      e.currentTarget.style.backgroundColor = "transparent";
                                    }}
                                  >
                                    <Trash2 style={{ width: 12, height: 12 }} />
                                    Delete
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })
          ) : (
            /* Collapsed List with Icons */
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px", width: "100%" }}>
              {investigations.map((inv) => {
                const isActive = activeThreadId === inv.id;
                const isHovered = hoveredThreadId === inv.id;
                return (
                  <button
                    key={inv.id}
                    type="button"
                    onClick={() => handleSelectInvestigation(inv)}
                    onMouseEnter={() => setHoveredThreadId(inv.id)}
                    onMouseLeave={() => setHoveredThreadId(null)}
                    title={inv.title}
                    style={{
                      position: "relative",
                      width: "36px",
                      height: "36px",
                      borderRadius: "8px",
                      backgroundColor: isActive
                        ? "var(--bg-surface-active)"
                        : isHovered
                        ? "var(--bg-surface-hover)"
                        : "transparent",
                      border: isActive
                        ? "1px solid var(--border-subtle)"
                        : "1px solid transparent",
                      color: isActive ? "var(--text-heading)" : "var(--text-muted)",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      transition: "all 0.15s ease",
                      padding: 0,
                    }}
                  >
                    <MessageSquareText style={{ width: 16, height: 16 }} />
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* 4. Sidebar Bottom Footer (ChatGPT-style User / Settings Bar) */}
        <div
          style={{
            padding: isSidebarOpen ? "8px 10px" : "8px 0",
            borderTop: "1px solid var(--border-subtle)",
            marginTop: "auto",
            display: "flex",
            justifyContent: "center",
            flexShrink: 0,
            transition: "padding 0.2s ease",
          }}
        >
          {isSidebarOpen ? (
            <button
              type="button"
              onClick={() => setShowSettings(!showSettings)}
              aria-expanded={showSettings}
              aria-label="Clinical R&D settings"
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "10px",
                padding: "7px 10px",
                borderRadius: "8px",
                cursor: "pointer",
                transition: "all 0.15s ease",
                width: "100%",
                boxSizing: "border-box",
                background: "transparent",
                border: "none",
                color: "inherit",
                font: "inherit",
                textAlign: "left",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = "transparent";
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    width: "28px",
                    height: "28px",
                    borderRadius: "50%",
                    background: "var(--accent-gradient)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "var(--accent-ink)",
                    fontSize: "11px",
                    fontWeight: 700,
                    flexShrink: 0,
                    boxShadow: "0 1px 3px rgba(0, 0, 0, 0.1)",
                  }}
                >
                  CR
                </div>
                <span
                  style={{
                    fontSize: "13.5px",
                    fontWeight: 500,
                    color: "var(--text-main)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  Clinical R&D
                </span>
              </div>
              <Settings
                style={{
                  width: 16,
                  height: 16,
                  color: "var(--text-muted)",
                  flexShrink: 0,
                  opacity: 0.8,
                }}
                aria-hidden="true"
              />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setShowSettings(!showSettings)}
              title="Clinical R&D (Settings)"
              style={{
                width: "32px",
                height: "32px",
                borderRadius: "50%",
                background: "var(--accent-gradient)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--accent-ink)",
                fontSize: "11px",
                fontWeight: 700,
                cursor: "pointer",
                border: "none",
                padding: 0,
                boxShadow: "0 1px 4px rgba(0, 0, 0, 0.12)",
                transition: "transform 0.15s ease",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.transform = "scale(1.08)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.transform = "scale(1)";
              }}
            >
              CR
            </button>
          )}
        </div>
      </aside>

      {/* Main Workspace Stage (Full 100vh) */}
      <main
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          height: "100vh",
          overflow: "clip", // not "hidden": a hidden box can still be scrolled by script and shift the whole layout
          position: "relative",
          minWidth: 0,
        }}
      >
        {/* Main Stage Top Header (Floating, transparent, 52px) */}
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0 18px",
            height: "52px",
            minHeight: "52px",
            backgroundColor: "transparent",
            zIndex: 30,
            flexShrink: 0,
          }}
        >
          {/* Left: Active Investigation Title (Clean & Subtle) */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span
              style={{
                fontSize: "13.5px",
                fontWeight: 500,
                color: "var(--text-muted)",
                maxWidth: "360px",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {investigations.find((i) => i.id === activeThreadId)?.title || "Clinical Investigation"}
            </span>
          </div>

          {/* Right Header Controls (Clean icons) */}
          <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
            {/* Theme Toggle Button */}
            <button
              onClick={toggleTheme}
              id="theme-toggle-btn"
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: "32px",
                height: "32px",
                borderRadius: "8px",
                backgroundColor: "transparent",
                border: "none",
                color: "var(--text-muted)",
                cursor: "pointer",
                transition: "all 0.15s ease",
                padding: 0,
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = "var(--text-heading)";
                e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.color = "var(--text-muted)";
                e.currentTarget.style.backgroundColor = "transparent";
              }}
              title="Toggle theme (Light / Dark)"
            >
              <Sun className="theme-icon-sun" style={{ width: 18, height: 18, color: "#fbbf24" }} />
              <Moon className="theme-icon-moon" style={{ width: 18, height: 18, color: "var(--domain-t2kg)" }} />
            </button>

            {/* New Investigation Button (when sidebar is closed, quick access) */}
            {!isSidebarOpen && (
              <button
                onClick={handleResetSession}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: "32px",
                  height: "32px",
                  borderRadius: "8px",
                  backgroundColor: "transparent",
                  border: "none",
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  transition: "all 0.15s ease",
                  padding: 0,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.color = "var(--text-heading)";
                  e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.color = "var(--text-muted)";
                  e.currentTarget.style.backgroundColor = "transparent";
                }}
                title="New Investigation"
              >
                <SquarePen style={{ width: 17, height: 17 }} />
              </button>
            )}

            <ProjectSelector backendUrl={backendUrl} authHeaders={authHeaders} projectId={projectId} onChange={(id) => updateProjectId(id)} />
            <JobsPanel
              backendUrl={backendUrl}
              authHeaders={authHeaders}
              refreshKey={`${jobsRefreshKey}:${authToken}`}
              onOpen={(job) => void handleOpenJob(job)}
              openDisabled={isProcessing}
            />

            {/* Memory Vault Toggle Button */}
            <button
              onClick={() => {
                setShowMemoryVault(!showMemoryVault);
                if (showSettings) setShowSettings(false);
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "5px",
                padding: "4px 9px",
                borderRadius: "8px",
                backgroundColor: showMemoryVault ? "var(--bg-surface-active)" : "transparent",
                border: `1px solid ${showMemoryVault ? "var(--domain-t2kg)" : "transparent"}`,
                color: showMemoryVault ? "var(--domain-t2kg)" : "var(--text-muted)",
                cursor: "pointer",
                transition: "all 0.15s ease",
                fontSize: "12px",
                fontWeight: 600,
              }}
              title="Clinical Memory Vault (Working Memory, Episodic History & Entity Knowledge)"
            >
              <Brain style={{ width: 15, height: 15, color: "var(--domain-t2kg)" }} />
              <span className="hidden sm:inline">Memory</span>
              {memoryBadgeCount(memoryStats) > 0 && (
                <span
                  style={{
                    backgroundColor: "color-mix(in srgb, var(--domain-t2kg) 12%, transparent)",
                    color: "var(--domain-t2kg)",
                    fontSize: "10px",
                    fontWeight: 700,
                    borderRadius: "6px",
                    padding: "1px 6px",
                  }}
                >
                  {memoryBadgeCount(memoryStats)}
                </span>
              )}
            </button>

            {/* Settings Button */}
            <button
              onClick={() => {
                setShowSettings(!showSettings);
                if (showMemoryVault) setShowMemoryVault(false);
              }}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: "32px",
                height: "32px",
                borderRadius: "8px",
                backgroundColor: "transparent",
                border: "none",
                color: showSettings ? "var(--domain-t2kg)" : "var(--text-muted)",
                cursor: "pointer",
                transition: "all 0.15s ease",
                padding: 0,
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = "var(--text-heading)";
                e.currentTarget.style.backgroundColor = "var(--bg-surface-hover)";
              }}
              onMouseLeave={(e) => {
                if (!showSettings) {
                  e.currentTarget.style.color = "var(--text-muted)";
                  e.currentTarget.style.backgroundColor = "transparent";
                }
              }}
              title="Settings"
            >
              <Settings style={{ width: 18, height: 18 }} />
            </button>
          </div>
        </header>

        {/* Settings Modal Dialog */}
        {showSettings && (
          <div
            ref={settingsDialogRef}
            className="modal-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="settings-modal-title"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setShowSettings(false);
            }}
          >
            <div
              className="modal-dialog-container"
              style={{ maxWidth: "640px" }}
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
              <div className="modal-header">
                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                  <div
                    style={{
                      width: "32px",
                      height: "32px",
                      borderRadius: "8px",
                      backgroundColor: "rgba(167, 179, 255, 0.1)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      color: "var(--domain-t2kg)",
                      flexShrink: 0,
                    }}
                  >
                    <Settings style={{ width: 17, height: 17 }} aria-hidden="true" />
                  </div>
                  <div>
                    <h2
                      id="settings-modal-title"
                      style={{ margin: 0, fontWeight: 600, fontSize: "15px", color: "var(--text-heading)" }}
                    >
                      Assistant Settings
                    </h2>
                    <p style={{ margin: "2px 0 0", fontSize: "12px", color: "var(--text-faint)" }}>
                      Backend API, models, authentication, and runtime agent parameters
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  className="modal-close-btn"
                  onClick={() => setShowSettings(false)}
                  aria-label="Close settings"
                >
                  <X style={{ width: 15, height: 15 }} aria-hidden="true" />
                </button>
              </div>

              {/* Modal Body */}
              <div className="modal-body">
                <div>
                  <label htmlFor="settings-backend-url" style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                    Backend FastAPI URL
                  </label>
                  <input
                    id="settings-backend-url"
                    type="url"
                    inputMode="url"
                    value={backendUrlDraft}
                    onChange={(e) => {
                      setBackendUrlDraft(e.target.value);
                      setBackendUrlError(null);
                    }}
                    onBlur={commitBackendUrl}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitBackendUrl();
                    }}
                    aria-invalid={backendUrlError ? true : undefined}
                    aria-describedby={backendUrlError ? "settings-backend-url-error" : undefined}
                    style={{
                      width: "100%",
                      padding: "8px 12px",
                      borderRadius: "6px",
                      backgroundColor: "var(--bg-surface)",
                      border: "1px solid var(--border-subtle)",
                      color: "var(--text-main)",
                      fontSize: "13px",
                    }}
                  />
                  {backendUrlError && (
                    <div id="settings-backend-url-error" className="settings-field-error" role="alert">
                      {backendUrlError}
                    </div>
                  )}
                </div>
                <div className="settings-status" aria-live="polite">
                  {backendHealthError ? (
                    <span className="settings-status-chip" data-ok="false">
                      Backend unreachable ({backendHealthError})
                    </span>
                  ) : backendHealth ? (
                    <>
                      <span className="settings-status-chip" data-ok="true">Backend reachable</span>
                      <span className="settings-status-chip" data-ok={backendHealth.auth?.mode !== "disabled"}>
                        Auth: {backendHealth.auth?.mode === "disabled" ? "disabled (local development)" : "bearer token required"}
                      </span>
                      <span className="settings-status-chip" data-ok={!!backendHealth.llm_providers?.nvidia_nim?.available}>
                        NVIDIA key {backendHealth.llm_providers?.nvidia_nim?.available ? "configured" : "missing"}
                      </span>
                      <span className="settings-status-chip" data-ok={!!backendHealth.llm_providers?.openai?.available}>
                        OpenAI key {backendHealth.llm_providers?.openai?.available ? "configured" : "missing"}
                      </span>
                      {authProblem && (
                        <span className="settings-status-chip" data-ok="false">
                          {authProblem}
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="settings-status-chip">Checking backend…</span>
                  )}
                </div>
                <div>
                  <label htmlFor="settings-auth-token" style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                    Phronesis Access Token (Bearer)
                  </label>
                  <input
                    id="settings-auth-token"
                    type="password"
                    autoComplete="off"
                    placeholder="token issued by your Phronesis administrator"
                    value={authTokenDraft}
                    onChange={(e) => setAuthTokenDraft(e.target.value.trim())}
                    onBlur={() => {
                      if (authTokenDraft !== authToken) updateAuthToken(authTokenDraft);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && authTokenDraft !== authToken) updateAuthToken(authTokenDraft);
                    }}
                    aria-describedby="settings-auth-token-storage"
                    style={{
                      width: "100%",
                      padding: "8px 12px",
                      borderRadius: "6px",
                      backgroundColor: "var(--bg-surface)",
                      border: "1px solid var(--border-subtle)",
                      color: "var(--text-main)",
                      fontSize: "13px",
                    }}
                  />
                  <label
                    style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "6px", fontSize: "12px", color: "var(--text-muted)", cursor: "pointer" }}
                  >
                    <input
                      type="checkbox"
                      checked={rememberAuthToken}
                      onChange={(e) => updateRememberAuthToken(e.target.checked)}
                    />
                    Remember on this device
                  </label>
                  <div id="settings-auth-token-storage" style={{ marginTop: "2px", fontSize: "11.5px", color: "var(--text-faint)" }}>
                    {rememberAuthToken
                      ? "Kept in this browser's local storage until you clear it. Use only on a device you trust."
                      : "Kept for this browser tab session only; it is forgotten when the tab is closed."}
                  </div>
                </div>
                <div>
                  <label htmlFor="settings-model" style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                    Model
                  </label>
                  <select
                    id="settings-model"
                    value={selectedModel}
                    onChange={(e) => {
                      setSelectedModel(e.target.value);
                      localStorageService.saveSettings({ selectedModel: e.target.value });
                    }}
                    style={{
                      width: "100%",
                      padding: "8px 12px",
                      borderRadius: "6px",
                      backgroundColor: "var(--bg-surface)",
                      border: "1px solid var(--border-subtle)",
                      color: "var(--text-main)",
                      fontSize: "13px",
                    }}
                  >
                    {!availableModels.some((m) => m.id === selectedModel) && <option value={selectedModel}>{selectedModel}</option>}
                    {availableModels.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name || m.id}
                        {m.provider ? ` (${m.provider})` : ""}
                      </option>
                    ))}
                  </select>
                  {availableModels.find((m) => m.id === selectedModel)?.description && (
                    <div style={{ marginTop: "4px", fontSize: "12px", color: "var(--text-faint)" }}>
                      {availableModels.find((m) => m.id === selectedModel)?.description}
                    </div>
                  )}
                </div>
                <div>
                  <label htmlFor="settings-api-key" style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                    LLM Provider API Key (Optional Override)
                  </label>
                  <input
                    id="settings-api-key"
                    type="password"
                    autoComplete="off"
                    placeholder="api-key-..."
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    style={{
                      width: "100%",
                      padding: "8px 12px",
                      borderRadius: "6px",
                      backgroundColor: "var(--bg-surface)",
                      border: "1px solid var(--border-subtle)",
                      color: "var(--text-main)",
                      fontSize: "13px",
                    }}
                  />
                </div>
                <AgentOptionsSettings value={agentOptions} onChange={updateAgentOptions} agentic={isAgenticFramework} />
              </div>

              {/* Modal Footer */}
              <div className="modal-footer">
                <button
                  type="button"
                  onClick={() => setShowSettings(false)}
                  style={{
                    padding: "7px 18px",
                    borderRadius: "6px",
                    background: "var(--accent-gradient)",
                    color: "var(--accent-ink)",
                    fontSize: "12.5px",
                    fontWeight: 600,
                    border: "none",
                    cursor: "pointer",
                  }}
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Clinical Memory Vault Modal Dialog */}
        {showMemoryVault && (
          <div
            ref={memoryDialogRef}
            className="modal-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="memory-vault-modal-title"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setShowMemoryVault(false);
            }}
          >
            <div
              className="modal-dialog-container"
              style={{ maxWidth: "800px" }}
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
              <div className="modal-header">
                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                  <div
                    style={{
                      width: "32px",
                      height: "32px",
                      borderRadius: "8px",
                      backgroundColor: "rgba(167, 179, 255, 0.1)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      color: "var(--domain-t2kg)",
                      flexShrink: 0,
                    }}
                  >
                    <Brain style={{ width: 17, height: 17 }} aria-hidden="true" />
                  </div>
                  <div>
                    <h2
                      id="memory-vault-modal-title"
                      style={{ margin: 0, fontWeight: 700, fontSize: "16px", color: "var(--text-heading)" }}
                    >
                      Clinical Memory Vault
                    </h2>
                    <p style={{ margin: "2px 0 0", fontSize: "11px", color: "var(--text-muted)" }}>
                      Durable multi-tier memory: working dialogue, episodic history, entity knowledge & semantic search
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  className="modal-close-btn"
                  onClick={() => setShowMemoryVault(false)}
                  aria-label="Close memory vault"
                >
                  <X style={{ width: 15, height: 15 }} aria-hidden="true" />
                </button>
              </div>

              {/* Modal Body */}
              <div className="modal-body">

                {/* Stats from /api/memory/stats, with the field names the API actually returns (UI-005). */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                    gap: "10px",
                  }}
                >
                  {memoryStatTiles(memoryStats).map((tile) => (
                    <div
                      key={tile.label}
                      style={{
                        padding: "10px 14px",
                        borderRadius: "10px",
                        backgroundColor: "var(--bg-surface)",
                        border: "1px solid var(--border-subtle)",
                      }}
                    >
                      <div style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "2px" }}>{tile.label}</div>
                      <div style={{ fontSize: "20px", fontWeight: 700, color: "var(--domain-t2kg)" }}>{tile.value}</div>
                      <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>{tile.sub}</div>
                    </div>
                  ))}
                </div>

                {memoryError && (
                  <div className="memory-error" role="alert">
                    {memoryError}
                  </div>
                )}

                {/* Hybrid Search Input */}
                <form onSubmit={handleSearchMemory} style={{ display: "flex", gap: "8px" }} role="search">
                  <label htmlFor="memory-search" className="sr-only">
                    Search memory
                  </label>
                  <div style={{ position: "relative", flex: 1 }}>
                    <Search
                      style={{
                        position: "absolute",
                        left: "12px",
                        top: "50%",
                        transform: "translateY(-50%)",
                        width: 14,
                        height: 14,
                        color: "var(--text-muted)",
                      }}
                    />
                    <input
                      id="memory-search"
                      type="text"
                      placeholder="Search memory across past targets, literature citations, or ODE biomodels..."
                      value={memorySearchQuery}
                      onChange={(e) => setMemorySearchQuery(e.target.value)}
                      style={{
                        width: "100%",
                        padding: "8px 12px 8px 36px",
                        borderRadius: "8px",
                        backgroundColor: "var(--bg-surface)",
                        border: "1px solid var(--border-subtle)",
                        color: "var(--text-main)",
                        fontSize: "13px",
                        outline: "none",
                      }}
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={isSearchingMemory || !memorySearchQuery.trim()}
                    style={{
                      padding: "8px 16px",
                      borderRadius: "8px",
                      background: "var(--accent-gradient)",
                      color: "var(--accent-ink)",
                      fontWeight: 600,
                      fontSize: "12px",
                      border: "none",
                      cursor: "pointer",
                    }}
                  >
                    {isSearchingMemory ? "Searching..." : "Search Vault"}
                  </button>
                </form>

                {/* Search Results Display if active */}
                {memorySearchResults.length > 0 && (
                  <div
                    style={{
                      padding: "12px",
                      borderRadius: "8px",
                      backgroundColor: "var(--bg-surface)",
                      border: "1px solid var(--domain-t2kg)",
                      display: "flex",
                      flexDirection: "column",
                      gap: "8px",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", fontWeight: 600, color: "var(--domain-t2kg)" }}>
                      <span>Matched {memorySearchResults.length} Memory Chunks for &quot;{memorySearchQuery}&quot;</span>
                      <button
                        onClick={() => setMemorySearchResults([])}
                        style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: "11px" }}
                      >
                        Clear Results
                      </button>
                    </div>
                    {memorySearchResults.map((res, i) => (
                      <div
                        key={res.node_id || i}
                        style={{
                          padding: "8px",
                          borderRadius: "6px",
                          backgroundColor: "rgba(255, 255, 255, 0.02)",
                          border: "1px solid var(--border-subtle)",
                          fontSize: "12px",
                          lineHeight: 1.4,
                        }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "4px" }}>
                          <span style={{ fontWeight: 600, color: "var(--domain-t2b)", fontSize: "11px" }}>
                            {memoryTypeLabel(res.memory_type)}
                            <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
                              {" · "}
                              {verificationStatusLabel(res.verification_status).text}
                            </span>
                          </span>
                          <span style={{ fontSize: "10px", color: "var(--text-faint)" }}>Match score: {res.score}</span>
                        </div>
                        <div style={{ whiteSpace: "pre-wrap", color: "var(--text-body)" }}>{res.content}</div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Discovered Biological Entities Vault */}
                <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                  <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-heading)" }}>
                    Entities mentioned in your memories ({memoryEntities.length})
                  </span>
                  {memoryEntities.length === 0 ? (
                    <div style={{ fontSize: "12px", color: "var(--text-muted)", padding: "12px", textAlign: "center" }}>
                      No clinical entities indexed yet. Run your first investigation to populate the memory vault!
                    </div>
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", maxHeight: "180px", overflowY: "auto" }}>
                      {memoryEntities.map((ent, i) => (
                        <div
                          key={ent.entity || i}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "2px 8px",
                            borderRadius: "6px",
                            backgroundColor: "color-mix(in srgb, var(--domain-t2kg) 12%, transparent)",
                            fontSize: "11.5px",
                          }}
                        >
                          <span style={{ fontWeight: 700, color: "var(--domain-t2kg)" }}>{ent.entity}</span>
                          <span
                            style={{
                              fontSize: "10px",
                              color: "var(--text-muted)",
                              padding: "0 2px",
                              fontWeight: 600,
                            }}
                          >
                            {entitySummary(ent)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Local Browser Storage Service Tier */}
                <div
                  style={{
                    padding: "12px",
                    borderRadius: "8px",
                    backgroundColor: "rgba(255, 255, 255, 0.02)",
                    border: "1px solid var(--border-subtle)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "8px",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                      <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-heading)" }}>
                        Local Browser Storage Service
                      </span>
                      <span
                        style={{
                          fontSize: "10px",
                          padding: "2px 6px",
                          borderRadius: "4px",
                          backgroundColor: localStorageStats.isQuotaHealthy
                            ? "var(--nvidia-glow)"
                            : "rgba(239, 68, 68, 0.15)",
                          color: localStorageStats.isQuotaHealthy ? "var(--nvidia-green)" : "#ef4444",
                          fontWeight: 600,
                        }}
                      >
                        {localStorageStats.isQuotaHealthy ? "Optimal Persistence" : "Storage High"}
                      </span>
                    </div>
                    <button
                      onClick={handleClearAllLocalHistory}
                      style={{
                        fontSize: "11px",
                        color: "var(--text-muted)",
                        background: "none",
                        border: "none",
                        cursor: "pointer",
                        textDecoration: "underline",
                      }}
                    >
                      Clear Local Browser Cache
                    </button>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "8px" }}>
                    <div
                      style={{
                        padding: "6px 8px",
                        borderRadius: "6px",
                        backgroundColor: "var(--bg-surface)",
                        border: "1px solid var(--border-subtle)",
                      }}
                    >
                      <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>Saved Investigations</div>
                      <div style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-main)" }}>
                        {localStorageStats.threadCount}
                      </div>
                    </div>
                    <div
                      style={{
                        padding: "6px 8px",
                        borderRadius: "6px",
                        backgroundColor: "var(--bg-surface)",
                        border: "1px solid var(--border-subtle)",
                      }}
                    >
                      <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>Cached Messages</div>
                      <div style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-main)" }}>
                        {localStorageStats.messageCount}
                      </div>
                    </div>
                    <div
                      style={{
                        padding: "6px 8px",
                        borderRadius: "6px",
                        backgroundColor: "var(--bg-surface)",
                        border: "1px solid var(--border-subtle)",
                      }}
                    >
                      <div style={{ fontSize: "10px", color: "var(--text-faint)" }}>Local Footprint</div>
                      <div style={{ fontSize: "13px", fontWeight: 700, color: "var(--domain-t2b)" }}>
                        {localStorageStats.usedKb} KB
                      </div>
                    </div>
                  </div>

                  {/* UI-007: where conversation history is kept, and for how long. */}
                  <fieldset style={{ border: "none", margin: 0, padding: 0, display: "grid", gap: "6px" }}>
                    <legend style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                      Conversations can contain clinical details. History is kept per backend and access token, unencrypted
                      in this browser.
                    </legend>
                    <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px" }}>
                      <span style={{ minWidth: 110 }}>Keep history</span>
                      <select
                        value={historyPrefs.mode}
                        onChange={(e) => changeHistoryPrefs({ mode: e.target.value as HistoryMode })}
                        style={{ fontSize: "12px" }}
                      >
                        <option value="device">on this device</option>
                        <option value="session">for this browser session only</option>
                        <option value="off">don&apos;t keep (this tab only)</option>
                      </select>
                    </label>
                    <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px" }}>
                      <span style={{ minWidth: 110 }}>Delete after</span>
                      <select
                        value={String(historyPrefs.retentionDays)}
                        onChange={(e) => changeHistoryPrefs({ retentionDays: Number(e.target.value) })}
                        style={{ fontSize: "12px" }}
                        disabled={historyPrefs.mode === "off"}
                      >
                        <option value="1">1 day without activity</option>
                        <option value="7">7 days without activity</option>
                        <option value="30">30 days without activity</option>
                        <option value="90">90 days without activity</option>
                        <option value="0">never (until cleared)</option>
                      </select>
                    </label>
                  </fieldset>
                </div>

                {/* Vault Management Actions */}
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    paddingTop: "10px",
                    borderTop: "1px solid var(--border-subtle)",
                  }}
                >
                  <button
                    onClick={handleClearCurrentThreadMemory}
                    style={{
                      fontSize: "11.5px",
                      color: "var(--text-muted)",
                      backgroundColor: "transparent",
                      border: "1px solid var(--border-subtle)",
                      borderRadius: "6px",
                      padding: "5px 12px",
                      cursor: "pointer",
                    }}
                  >
                    Clear Current Thread Memory
                  </button>
                  <button
                    onClick={handleClearAllMemory}
                    style={{
                      fontSize: "11.5px",
                      color: "#ef4444",
                      backgroundColor: "rgba(239, 68, 68, 0.1)",
                      border: "1px solid rgba(239, 68, 68, 0.25)",
                      borderRadius: "6px",
                      padding: "5px 12px",
                      cursor: "pointer",
                      fontWeight: 600,
                    }}
                  >
                    Clear All Vault Memory
                  </button>
                </div>
              </div>

              {/* Modal Footer */}
              <div className="modal-footer">
                <button
                  type="button"
                  onClick={() => setShowMemoryVault(false)}
                  style={{
                    padding: "7px 18px",
                    borderRadius: "6px",
                    background: "var(--accent-gradient)",
                    color: "var(--accent-ink)",
                    fontSize: "12.5px",
                    fontWeight: 600,
                    border: "none",
                    cursor: "pointer",
                  }}
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        )}

          {authProblem && (
            <div className="backend-banner" role="alert">
              {authProblem}{" "}
              <button
                type="button"
                onClick={() => setShowSettings(true)}
                style={{ background: "none", border: "none", padding: 0, color: "inherit", textDecoration: "underline", cursor: "pointer", font: "inherit" }}
              >
                Open settings
              </button>
            </div>
          )}

          {/* Messages Scroll Area */}
          {/* A log: finished messages are announced politely; the live turn is silent until it completes (aria-busy). */}
          <div
            ref={messagesContainerRef}
            onScroll={handleMessagesScroll}
            role="log"
            aria-live="polite"
            aria-relevant="additions"
            aria-label="Conversation"
            aria-busy={isProcessing && streamingThreadId === activeThreadId ? true : undefined}
            style={{
              flex: 1,
              minHeight: 0,
              overflowY: "auto",
              padding: "28px max(20px, calc((100% - 768px) / 2)) 24px",
              display: "flex",
              flexDirection: "column",
              gap: "30px",
            }}
          >
            {messages.length === 0 ? (
              <div className="hero">
                <div className="hero-emblem">
                  <img src="/icon-tight.png" alt="" className="welcome-emblem" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
                </div>
                <h1 className="hero-title">What shall we investigate today?</h1>
                <p className="hero-subtitle">
                  Phronesis coordinates knowledge graphs, kinetic simulations, single-cell analysis, and the
                  literature, then checks the answer&apos;s claims against the retrieved evidence and shows what was
                  and was not checked.
                </p>
              </div>
            ) : (
              messages.map((msg, idx) => {
              const isAssistant = msg.sender === "assistant";
              const hasThoughts = (msg.thoughts && msg.thoughts.length > 0) || !!msg.reasoning;

              return (
                <ErrorBoundary key={msg.id} name={`Message ${msg.id}`} resetKey={msg.content} fallback={<MessageRenderFallback content={msg.content} />}>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: isAssistant ? "flex-start" : "flex-end",
                    width: "100%",
                  }}
                >
                  {/* Sender meta */}
                  <div className="msg-meta">
                    {isAssistant ? (
                      <>
                        <span className="msg-avatar" aria-hidden="true">
                          <img
                            src="/icon-tight.png"
                            alt=""
                            className="phronesis-icon"
                            style={{ width: "13px", height: "13px", objectFit: "contain" }}
                          />
                        </span>
                        <span style={{ fontWeight: 600, color: "var(--text-main)" }}>{msg.agent || "Phronesis"}</span>
                        {msg.framework_used ? (
                          <span
                            style={{ color: "var(--text-faint)" }}
                            title={
                              msg.execution_artifacts?.framework_routing?.reason
                                ? `Auto chose ${FRAMEWORK_LABEL[msg.framework_used] || msg.framework_used}: ${msg.execution_artifacts.framework_routing.reason}`
                                : `Ran: ${msg.framework_used}${msg.model ? ` · ${msg.model}` : ""}`
                            }
                          >
                            {msg.execution_artifacts?.framework_routing?.requested === "auto" ? "Auto · " : ""}
                            {FRAMEWORK_LABEL[msg.framework_used] || msg.framework_used}
                          </span>
                        ) : null}
                        {msg.duration_seconds && !hasThoughts ? (
                          <span className="tabular" style={{ color: "var(--text-faint)", display: "flex", alignItems: "center", gap: "4px" }}>
                            <Clock style={{ width: 11, height: 11 }} />
                            {msg.duration_seconds < 10 ? msg.duration_seconds.toFixed(1) : Math.round(msg.duration_seconds)}s
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span style={{ fontWeight: 600, color: "var(--text-muted)" }}>You</span>
                    )}
                    <span aria-hidden="true">&middot;</span>
                    <span>{msg.timestamp}</span>
                  </div>

                  {/* Message Bubble Container */}
                  <div
                    style={{
                      maxWidth: isAssistant ? "100%" : "min(600px, 85%)",
                      width: isAssistant ? "100%" : "auto",
                      display: "flex",
                      flexDirection: "column",
                      gap: "10px",
                    }}
                  >
                    {/* Collapsible Thinking Process for Assistant Messages (Claude / ChatGPT Style) */}
                    {isAssistant && hasThoughts && (
                      <ThinkingProcess
                        thoughts={msg.thoughts || []}
                        reasoning={msg.reasoning}
                        durationSeconds={msg.duration_seconds}
                      />
                    )}

                    {/* Main Content Bubble */}
                    <div
                      className={
                        isAssistant
                          ? `msg-assistant-card${justStreamedIdRef.current === msg.id ? " msg-no-enter" : ""}`
                          : "msg-user"
                      }
                      style={{ position: "relative" }}
                    >
                      {/* Content: Markdown rendering for Assistant, pre-wrap text for User */}
                      {isAssistant ? (
                        <>
                          <TaskStatusNotice outcome={msg.task_status} />
                          <VerificationNotice report={msg.verification_report} />
                          <div
                            style={
                              (msg.verification_report && !msg.verification_report.passed) ||
                              (msg.task_status && msg.task_status.task_status !== "completed")
                                ? { marginTop: 14 }
                                : undefined
                            }
                          >
                            {msg.answer_revised && (
                              <div className="answer-revised-note" role="note">
                                <RefreshCw style={{ width: 12, height: 12 }} aria-hidden="true" />
                                Answer revised after new evidence or critique
                              </div>
                            )}
                            <ErrorBoundary name={`Answer ${msg.id}`} resetKey={msg.content} fallback={<MessageRenderFallback content={msg.content} />}>
                            {justStreamedIdRef.current === msg.id ? (
                              <SmoothMarkdown
                                content={stripVerificationBanner(msg.content)}
                                streaming={false}
                                citations={msg.citations}
                                initialText={
                                  streamHandoffRef.current?.id === msg.id &&
                                  performance.now() - streamHandoffRef.current.at < 1500
                                    ? streamHandoffRef.current.text
                                    : undefined
                                }
                                onReveal={handleSavedReveal}
                              />
                            ) : (
                              <MarkdownRenderer content={stripVerificationBanner(msg.content)} citations={msg.citations} />
                            )}
                            </ErrorBoundary>
                          </div>
                          {msg.task_status?.task_status === "needs_input" && msg.execution_artifacts?.clarification && (
                            <ClarificationChips
                              data={msg.execution_artifacts.clarification}
                              onSelect={(option) => handleSendMessage(option)}
                              disabled={isProcessing || idx !== messages.length - 1}
                            />
                          )}
                          {msg.storage_slimmed === "plots" && (
                            <p className="composer-footnote storage-notice" role="note">
                              Saved without plots and source passages: browser storage was full.
                            </p>
                          )}
                          {/* Each card has its own boundary inside MessageArtifacts (with a visible fallback); this outer one only
                              catches a failure of the list itself (UI-001, UI-006). */}
                          <ErrorBoundary
                            name={`Artifacts of ${msg.id}`}
                            resetKey={msg.execution_artifacts}
                            fallback={<ArtifactRenderFallback label="result cards" data={msg.execution_artifacts} />}
                          >
                            <MessageArtifacts
                              artifacts={msg.execution_artifacts}
                              question={idx > 0 && messages[idx - 1].sender === "user" ? messages[idx - 1].content : undefined}
                              taskStatus={msg.task_status}
                            />
                          </ErrorBoundary>
                        </>
                      ) : (
                        <>
                          <div style={{ whiteSpace: "pre-wrap" }}>{msg.content}</div>
                          {msg.attachments?.length ? <MessageAttachmentChips attachments={msg.attachments} /> : null}
                        </>
                      )}
                    </div>

                    {isAssistant && (
                      <div className="msg-actions">
                        <button
                          type="button"
                          className="msg-action-btn"
                          onClick={() => handleCopyMessage(msg.id, msg.content)}
                          aria-label="Copy answer"
                        >
                          {copiedMessageId === msg.id ? (
                            <>
                              <Check style={{ width: 13, height: 13, color: "var(--nvidia-green)" }} /> Copied
                            </>
                          ) : (
                            <>
                              <Copy style={{ width: 13, height: 13 }} /> Copy
                            </>
                          )}
                        </button>
                        <MessageActionsExtra
                          message={msg}
                          question={previousUserContent(idx)}
                          backendUrl={backendUrl}
                          authHeaders={authHeaders}
                        />
                      </div>
                    )}
                  </div>
                </div>
                </ErrorBoundary>
              );
            })
          )}

            {/* Live Streaming Thinking (shown while processing in Claude/ChatGPT style) */}
            {/* Live turn: same layout as a finished assistant message, so nothing jumps on completion */}
            {isProcessing && streamingThreadId === activeThreadId && (
              <div
                aria-busy="true"
                aria-live="off"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-start",
                  width: "100%",
                }}
              >
                <div className="msg-meta">
                  <span className="msg-avatar" aria-hidden="true">
                    <img
                      src="/icon-tight.png"
                      alt=""
                      className="phronesis-icon"
                      style={{ width: "13px", height: "13px", objectFit: "contain" }}
                    />
                  </span>
                  <span style={{ fontWeight: 600, color: "var(--text-main)" }}>Clinical Orchestrator</span>
                </div>

                <div
                  style={{
                    maxWidth: "100%",
                    width: "100%",
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px",
                  }}
                >
                  <ThinkingProcess
                    thoughts={liveThoughts}
                    reasoning={liveReasoning}
                    isStreaming={true}
                    elapsedSeconds={liveElapsedSeconds}
                    answerStarted={liveContent.trim().length > 0}
                  />

                  {liveApproval && (
                    <ApprovalCard
                      key={liveApproval.approval_id}
                      request={liveApproval}
                      backendUrl={backendUrl}
                      authHeaders={authHeaders}
                      onResolved={() => setLiveApproval(null)}
                    />
                  )}

                  {liveContent.trim() && (
                    <div className="msg-assistant-card" style={{ position: "relative" }}>
                      {/* UI-003: the live text has not been checked yet; the verifier may still remove or revise claims. */}
                      <div className="verify-draft-label" role="status">
                        <Hourglass style={{ width: 12, height: 12 }} aria-hidden="true" />
                        Draft — verification pending; claims may still be revised or removed
                      </div>
                      {liveRevised && (
                        <div className="answer-revised-note" role="status">
                          <RefreshCw style={{ width: 12, height: 12 }} aria-hidden="true" />
                          Answer revised after new evidence or critique
                        </div>
                      )}
                      {/* Remounting (e.g. after switching back to this thread) resumes from what was already shown. */}
                      <ErrorBoundary name="Live answer" fallback={<MessageRenderFallback content={liveContent} />}>
                        <SmoothMarkdown
                          content={liveContent}
                          streaming
                          initialText={liveShownRef.current || undefined}
                          onReveal={handleLiveReveal}
                        />
                      </ErrorBoundary>
                    </div>
                  )}

                  <SteeringNotes items={liveSteers} onRetry={(n) => void sendSteer(n)} />
                </div>
              </div>
            )}

            {/* Bottom spacer for clean scroll padding */}
            <div style={{ minHeight: "32px", height: "32px" }} />
            <div ref={chatEndRef} />
          </div>

          {/* Composer */}
          <div className="composer-dock">
            <form
              className="composer"
              data-steering={canSteer || undefined}
              onSubmit={(e) => {
                e.preventDefault();
                submitComposer();
              }}
            >
              <ComposerAttachmentChips
                attached={attachedDatasets}
                onRemove={(id) => setAttachedDatasets((a) => a.filter((x) => x.id !== id))}
              />
              <textarea
                ref={textareaRef}
                value={inputMessage}
                onChange={(e) => setInputMessage(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submitComposer();
                  }
                }}
                aria-label={canSteer ? "Guidance for the running agent" : "Research question"}
                placeholder={
                  canSteer
                    ? "Guide this run, e.g. “prioritise phase 3 data”…"
                    : liveHere
                      ? stoppingRun
                        ? "Stopping the run…"
                        : "Draft your next question while this answer finishes…"
                      : "Ask about a target, a BioModel, or attach a dataset…"
                }
                rows={1}
                style={{
                  width: "100%",
                  minHeight: "28px",
                  maxHeight: "200px",
                  backgroundColor: "transparent",
                  border: "none",
                  outline: "none",
                  color: "var(--text-main)",
                  lineHeight: "1.6",
                  padding: "2px 2px",
                  resize: "none",
                  fontFamily: "var(--font-sans)",
                  overflowY: "auto",
                  boxSizing: "border-box",
                  display: "block",
                }}
              />

              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "8px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "6px", minWidth: 0, position: "relative" }}>
                  <ComposerAttachButton
                    attached={attachedDatasets}
                    onAttach={(a) => setAttachedDatasets((p) => (p.some((x) => x.id === a.id) ? p : [...p, a].slice(0, 5)))}
                    onDeleted={(id) => setAttachedDatasets((p) => p.filter((x) => x.id !== id))}
                    onUpdated={(a) => setAttachedDatasets((p) => p.map((x) => (x.id === a.id ? a : x)))}
                    backendUrl={backendUrl}
                    authHeaders={authHeaders}
                    disabled={isProcessing}
                  />

                  <button
                    type="button"
                    className="composer-pill"
                    data-active={isWebSearchEnabled}
                    aria-pressed={isWebSearchEnabled}
                    onClick={() => setIsWebSearchEnabled((prev) => !prev)}
                    title={`Web Search: ${isWebSearchEnabled ? "Enabled (live biomedical literature & web)" : "Disabled"}`}
                    aria-label={`Toggle web search, currently ${isWebSearchEnabled ? "enabled" : "disabled"}`}
                    style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
                  >
                    <Globe style={{ width: 13, height: 13, flexShrink: 0 }} />
                    <span className="composer-pill-label">Search</span>
                    {isWebSearchEnabled && <span className="composer-pill-dot" />}
                  </button>

                  <button
                    type="button"
                    className="composer-pill"
                    data-active={runInBackground}
                    aria-pressed={runInBackground}
                    onClick={() => setRunInBackground((prev) => !prev)}
                    title={`Background Job: ${runInBackground ? "Enabled (runs asynchronously in server job queue)" : "Disabled (streams in active session)"}`}
                    aria-label={`Toggle background execution, currently ${runInBackground ? "enabled" : "disabled"}`}
                    style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
                  >
                    <Clock style={{ width: 13, height: 13, flexShrink: 0 }} />
                    <span className="composer-pill-label">Background</span>
                    {runInBackground && <span className="composer-pill-dot" />}
                  </button>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
                  <ChatGPTEffortControl
                    effortLevel={agentOptions.effort_level || "medium"}
                    onEffortChange={(lvl) => updateAgentOptions({ ...agentOptions, effort_level: lvl })}
                    selectedFramework={selectedFramework}
                    onFrameworkChange={chooseFramework}
                    frameworkOptions={frameworkOptions}
                    onOpenGuide={() => setIsFrameworkGuideOpen(true)}
                  />

                  {canSteer && inputMessage.trim() && (
                    <button type="submit" className="send-btn" aria-label="Send guidance to the running agent" title="Steer the run">
                      <Navigation style={{ width: 14, height: 14 }} />
                    </button>
                  )}
                  {isProcessing ? (
                    <button
                      type="button"
                      className="send-btn send-btn-stop"
                      onClick={handleStopRun}
                      aria-label={stoppingRun ? "Stopping; click again to stop immediately" : "Stop the run"}
                      title={
                        stoppingRun
                          ? "Stopping… click again to stop immediately"
                          : liveRunId
                            ? "Stop the run and keep the partial answer"
                            : "Stop"
                      }
                      data-stopping={stoppingRun || undefined}
                    >
                      {stoppingRun ? (
                        <Loader2 className="spin" style={{ width: 13, height: 13 }} />
                      ) : (
                        <Square style={{ width: 11, height: 11 }} fill="currentColor" />
                      )}
                    </button>
                  ) : (
                    <button
                      type="submit"
                      className="send-btn"
                      disabled={!inputMessage.trim()}
                      aria-label="Send question"
                      title="Send"
                    >
                      <Send style={{ width: 15, height: 15 }} />
                    </button>
                  )}
                </div>
              </div>
            </form>
            {storageNotice && (
              <p className="composer-footnote storage-notice" role="status">
                {storageNotice.message}{" "}
                <button type="button" className="storage-notice-dismiss" onClick={() => setStorageNotice(null)}>
                  Dismiss
                </button>
              </p>
            )}
            {canSteer && (
              <p className="composer-footnote" role="status">
                Messages sent now steer the running agent: it reads them before writing and after each draft. Stop keeps the partial answer.
              </p>
            )}
            {isProcessing && streamingThreadId !== activeThreadId && (
              <p className="composer-footnote" role="status">
                A question is still running in another investigation; its answer is saved there when it finishes.
              </p>
            )}
            {!canSteer && (
              <p className="composer-footnote">
                Each answer says whether its claims were checked against the retrieved evidence; check primary sources
                before acting on them.
              </p>
            )}
          </div>
        </main>
        <FrameworkGuide
          open={isFrameworkGuideOpen}
          options={frameworkOptions}
          selected={selectedFramework}
          onSelect={chooseFramework}
          onClose={() => setIsFrameworkGuideOpen(false)}
        />
    </div>
  );
}
