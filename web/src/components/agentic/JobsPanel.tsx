"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Inbox, RefreshCw, RotateCcw, Square, Trash2, X } from "lucide-react";
import type { AuthHeaders } from "@/services/datasetService";
import { cancelJob, deleteJob, listJobs, resumeJob } from "@/services/agentService";
import type { JobSummary } from "@/types/agentic";
import { FRAMEWORK_NAMES } from "./AgentRunPanel";
import { Pill, statusTone, str, styles } from "./ui";

const ACTIVE = new Set(["queued", "running", "waiting_approval"]);
const POLL_MS = 5000;

function when(v: unknown): string {
  if (v == null || v === "") return "";
  const d = typeof v === "number" ? new Date(v < 1e12 ? v * 1000 : v) : new Date(String(v));
  if (Number.isNaN(d.getTime())) return str(v);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function jobTitle(j: JobSummary): string {
  const text = str(j.title || j.request?.message || j.message).replace(/\s+/g, " ").trim();
  return text ? (text.length > 90 ? `${text.slice(0, 89)}…` : text) : j.job_id;
}

/** Background jobs (F13): list, status, open (reattach to the event stream), cancel, resume, delete. */
export function JobsPanel({
  backendUrl,
  authHeaders,
  refreshKey,
  onOpen,
  openDisabled,
}: {
  backendUrl: string;
  authHeaders: AuthHeaders;
  /** Change to reload (e.g. after submitting a job, or when the access token changes). */
  refreshKey: number | string;
  onOpen: (job: JobSummary) => void;
  /** True while another answer is streaming; opening a job waits until it finishes. */
  openDisabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const authRef = useRef(authHeaders);
  authRef.current = authHeaders;

  const load = useCallback(async () => {
    try {
      const list = await listJobs(backendUrl, authRef.current);
      list.sort((a, b) => String(b.created ?? b.created_at ?? "").localeCompare(String(a.created ?? a.created_at ?? "")));
      setJobs(list);
      setError(null);
      setUnavailable(false);
    } catch (err: any) {
      if (err?.status === 404) setUnavailable(true);
      else setError(err?.message || "Jobs could not be loaded.");
    }
  }, [backendUrl]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const activeCount = jobs.filter((j) => ACTIVE.has(str(j.status))).length;

  // Poll while the panel is open, or slowly while a job is still running (keeps the badge honest).
  useEffect(() => {
    if (!open && activeCount === 0) return;
    const t = setInterval(() => void load(), open ? POLL_MS : POLL_MS * 4);
    return () => clearInterval(t);
  }, [open, activeCount, load]);

  useEffect(() => {
    if (!open) return;
    void load();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, load]);

  const act = async (j: JobSummary, fn: typeof cancelJob, label: string) => {
    setBusyId(j.job_id);
    try {
      await fn(backendUrl, authHeaders, j.job_id);
      await load();
    } catch (err: any) {
      setError(`${label} failed: ${err?.message || "unknown error"}`);
    } finally {
      setBusyId(null);
    }
  };

  if (unavailable) return null;

  return (
    <>
      <button
        type="button"
        className={styles.headerBtn}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
        title="Background jobs"
      >
        <Inbox style={{ width: 15, height: 15, flexShrink: 0 }} aria-hidden="true" />
        <span className="hidden sm:inline">Jobs</span>
        {activeCount > 0 && (
          <span className={styles.badgeCount} aria-label={`${activeCount} running`}>
            {activeCount}
          </span>
        )}
      </button>

      {open && (
        <>
          <div
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 79,
              backgroundColor: "rgba(3, 4, 12, 0.65)",
              backdropFilter: "blur(6px)",
              WebkitBackdropFilter: "blur(6px)",
              animation: "modalFadeIn 0.15s ease-out forwards",
            }}
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <div className={styles.drawer} role="dialog" aria-modal="true" aria-label="Background jobs">
          <div className={styles.drawerHead}>
            <div>
              <div className={styles.title}>Background jobs</div>
              <div className={styles.meta}>Runs keep going on the server when you close the tab.</div>
            </div>
            <div className={styles.row}>
              <button type="button" className={styles.btn} onClick={() => void load()} aria-label="Refresh jobs">
                <RefreshCw style={{ width: 12, height: 12 }} aria-hidden="true" />
              </button>
              <button type="button" className={styles.btn} onClick={() => setOpen(false)} aria-label="Close jobs">
                <X style={{ width: 13, height: 13 }} aria-hidden="true" />
              </button>
            </div>
          </div>
          <div className={styles.drawerBody}>
            {error && (
              <p className={styles.errorText} role="alert">
                {error}
              </p>
            )}
            {jobs.length === 0 ? (
              <p className={styles.muted}>No background jobs yet. Switch on &quot;Background&quot; in the composer to queue a question.</p>
            ) : (
              jobs.map((j) => {
                const status = str(j.status) || "unknown";
                const active = ACTIVE.has(status);
                const fw = str(j.request?.framework);
                return (
                  <div key={j.job_id} className={styles.itemBox}>
                    <div className={styles.row} style={{ alignItems: "flex-start" }}>
                      <span className={styles.rowGrow} style={{ color: "var(--text-heading)", overflowWrap: "anywhere" }}>
                        {jobTitle(j)}
                      </span>
                      <Pill tone={statusTone(status)}>{status.replace(/_/g, " ")}</Pill>
                    </div>
                    <div className={styles.meta} style={{ marginTop: 3 }}>
                      {[fw ? FRAMEWORK_NAMES[fw] || fw : "", when(j.created ?? j.created_at), j.job_id].filter(Boolean).join(" · ")}
                    </div>
                    {j.error && <p className={styles.errorText}>{str(j.error)}</p>}
                    <div className={styles.btnRow} style={{ marginTop: 6 }}>
                      <button
                        type="button"
                        className={styles.btn}
                        disabled={openDisabled}
                        title={openDisabled ? "Wait for the running answer to finish" : active ? "Follow the live run in the investigation it was asked in" : "Show the result in the investigation it was asked in"}
                        onClick={() => {
                          onOpen(j);
                          setOpen(false);
                        }}
                      >
                        <ExternalLink style={{ width: 12, height: 12 }} aria-hidden="true" /> Open
                      </button>
                      {active && (
                        <button type="button" className={`${styles.btn} ${styles.btnDanger}`} disabled={busyId === j.job_id} onClick={() => act(j, cancelJob, "Cancel")}>
                          <Square style={{ width: 11, height: 11 }} aria-hidden="true" /> Cancel
                        </button>
                      )}
                      {(status === "interrupted" || status === "failed" || status === "cancelled") && (
                        <button type="button" className={styles.btn} disabled={busyId === j.job_id} onClick={() => act(j, resumeJob, "Resume")}>
                          <RotateCcw style={{ width: 12, height: 12 }} aria-hidden="true" /> Resume
                        </button>
                      )}
                      {!active && (
                        <button
                          type="button"
                          className={`${styles.btn} ${styles.btnDanger}`}
                          disabled={busyId === j.job_id}
                          onClick={() => {
                            if (window.confirm("Delete this job and its saved events?")) void act(j, deleteJob, "Delete");
                          }}
                          aria-label="Delete job"
                        >
                          <Trash2 style={{ width: 12, height: 12 }} aria-hidden="true" />
                        </button>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </>
    )}
    </>
  );
}
