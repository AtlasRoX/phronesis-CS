// Report export, run manifest lookup and re-run requests (owned by EXPORT, feature 9).

import type { Message } from "@/types/chat";
import type { ExportFormat, RerunMode, RerunReport, RunManifest } from "@/types/runs";
import { apiFetch, detailOf, jsonInit, requestJson, type RequestOptions } from "./apiClient.ts";

const EXTENSIONS: Record<ExportFormat, string> = {
  docx: "docx",
  pdf: "pdf",
  md: "md",
  json: "json",
  bundle: "zip",
};

export interface ExportPayload {
  question: string;
  message: Message;
}

function slugify(text: string): string {
  const slug = (text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/, "");
  return slug || "answer";
}

function yyyymmdd(d: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}`;
}

export function exportFilename(fmt: ExportFormat, question: string): string {
  return `phronesis-${slugify(question)}-${yyyymmdd()}.${EXTENSIONS[fmt]}`;
}

/** Errors read "<what> failed: <status> <detail or status text>". */
function failure(what: string): RequestOptions {
  return { toError: (status, body, res) => new Error(`${what} failed: ${status} ${detailOf(body) || res.statusText || "request failed"}`) };
}

/**
 * Only the fields the report uses; activity steps and reasoning stay in the browser. `task_status` carries the outcome
 * of client-built cancelled/failed messages, which have no server manifest to fall back on (UI-016).
 */
export function exportableMessage(message: Message) {
  return {
    id: message.id,
    content: message.content,
    agent: message.agent,
    model: message.model,
    timestamp: message.timestamp,
    duration_seconds: message.duration_seconds,
    verification_report: message.verification_report,
    execution_artifacts: message.execution_artifacts,
    citations: message.citations,
    run_manifest: message.run_manifest,
    selected_agents: message.selected_agents,
    task_status: message.task_status,
    answer_revised: message.answer_revised,
    framework_used: message.framework_used,
  };
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the click has been handled; some browsers read the URL asynchronously.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** POST the message to /api/export/{fmt} and save the returned file. Throws with a readable message on failure. */
export async function exportMessage(
  fmt: ExportFormat,
  payload: ExportPayload,
  backendUrl: string,
  headers: Record<string, string>,
): Promise<void> {
  const res = await apiFetch(
    `${backendUrl}/api/export/${fmt}`,
    jsonInit("POST", { question: payload.question || "", message: exportableMessage(payload.message) }, (extra) => ({ ...headers, ...extra })),
    failure("Export"),
  );
  const blob = await res.blob();
  triggerDownload(blob, exportFilename(fmt, payload.question));
}

export function rerun(
  runId: string,
  mode: RerunMode,
  backendUrl: string,
  headers: Record<string, string>,
  options: { model?: string; apiKey?: string } = {},
): Promise<RerunReport> {
  return requestJson<RerunReport>(
    `${backendUrl}/api/runs/${encodeURIComponent(runId)}/rerun`,
    jsonInit("POST", { mode, model: options.model || undefined, api_key: options.apiKey || undefined }, (extra) => ({ ...headers, ...extra })),
    failure("Re-run"),
  );
}

export function getRun(runId: string, backendUrl: string, headers: Record<string, string>): Promise<RunManifest> {
  return requestJson<RunManifest>(`${backendUrl}/api/runs/${encodeURIComponent(runId)}`, { headers }, failure("Run lookup"));
}
