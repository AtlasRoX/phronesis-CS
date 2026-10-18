"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ListChecks, Navigation, RotateCcw } from "lucide-react";
import type { AuthHeaders } from "@/services/datasetService";
import { approveRun, getRun } from "@/services/agentService";
import type { ApprovalSource, ClarificationArtifact, PendingApproval, PlanNode } from "@/types/agentic";
import { Pill, arr, str, styles } from "./ui";

const APPROVAL_TIMEOUT_S = 300;

function itemKey(item: ApprovalSource | PlanNode, i: number): string {
  return str((item as ApprovalSource).key ?? (item as PlanNode).id) || `item${i}`;
}

/**
 * Plan / DAG approval (Magentic-UI co-planning). Each source or node can be switched off before the run
 * continues; the backend auto-continues after 5 minutes without an answer.
 */
export function ApprovalCard({
  request,
  backendUrl,
  authHeaders,
  onResolved,
}: {
  request: PendingApproval;
  backendUrl: string;
  authHeaders: AuthHeaders;
  onResolved: (outcome: "approved" | "edited" | "rejected" | "expired") => void;
}) {
  const isDag = request.kind === "dag" || (!arr(request.payload?.sources).length && arr(request.payload?.nodes).length > 0);
  const items = useMemo<(ApprovalSource | PlanNode)[]>(
    () => (isDag ? arr<PlanNode>(request.payload?.nodes) : arr<ApprovalSource>(request.payload?.sources)).filter((x) => x && typeof x === "object"),
    [isDag, request]
  );
  const initial = useMemo(() => Object.fromEntries(items.map((it, i) => [itemKey(it, i), it.run !== false])), [items]);
  const [enabled, setEnabled] = useState<Record<string, boolean>>(initial);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => setEnabled(initial), [initial]);

  // A replayed job log can contain approvals that were answered long ago: only keep the card while the
  // server still lists this approval as pending (checked twice, since the event can precede registration).
  const onResolvedRef = useRef(onResolved);
  onResolvedRef.current = onResolved;
  useEffect(() => {
    let stop = false;
    const isPending = async (): Promise<boolean | null> => {
      try {
        const state = await getRun(backendUrl, authHeaders, request.run_id);
        return arr<{ approval_id?: string }>(state?.pending).some((p) => p?.approval_id === request.approval_id);
      } catch (err: any) {
        return err?.status === 404 ? false : null; // unknown on other errors: keep showing the card
      }
    };
    void (async () => {
      if ((await isPending()) !== false || stop) return;
      await new Promise((r) => setTimeout(r, 1500));
      if (stop) return;
      if ((await isPending()) === false && !stop) onResolvedRef.current("expired");
    })();
    return () => {
      stop = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request.run_id, request.approval_id, backendUrl]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const remaining = Math.max(0, Math.round(APPROVAL_TIMEOUT_S - (now - request.received_at) / 1000));
  const edited = items.some((it, i) => enabled[itemKey(it, i)] !== initial[itemKey(it, i)]);
  const onCount = items.filter((it, i) => enabled[itemKey(it, i)]).length;

  const submit = async (approved: boolean, withEdits: boolean) => {
    setBusy(true);
    setError(null);
    const payload = withEdits
      ? isDag
        ? { ...request.payload, nodes: items.map((n, i) => ({ ...n, run: !!enabled[itemKey(n, i)] })) }
        : { ...request.payload, sources: items.map((s, i) => ({ ...s, run: !!enabled[itemKey(s, i)] })) }
      : undefined;
    try {
      await approveRun(backendUrl, authHeaders, request.run_id, {
        approval_id: request.approval_id,
        approved,
        payload,
        message: note.trim() || undefined,
      });
      onResolved(!approved ? "rejected" : withEdits ? "edited" : "approved");
    } catch (err: any) {
      if (err?.status === 404 || err?.status === 409) {
        setError("This request is no longer pending (it was answered or timed out); the run has continued.");
      } else {
        setError(err?.message || "The decision could not be sent.");
      }
    } finally {
      setBusy(false);
    }
  };

  const mm = Math.floor(remaining / 60);
  const ss = String(remaining % 60).padStart(2, "0");

  return (
    <section className={`${styles.liveBox} ${styles.approvalBox}`} aria-label="Approval needed" role="region">
      <div className={styles.head} style={{ marginBottom: 8 }}>
        <span className={styles.icon} aria-hidden="true">
          <ListChecks style={{ width: 15, height: 15 }} />
        </span>
        <div className={styles.headText}>
          <div className={styles.title}>{isDag ? "Approve the planned tool steps" : "Approve the research plan"}</div>
          <div className={styles.meta} aria-live="off">
            {onCount} of {items.length} {isDag ? "steps" : "sources"} selected ·{" "}
            {remaining > 0 ? `auto-continues after 5 min (${mm}:${ss} left)` : "auto-continued on the server"}
          </div>
        </div>
        <Pill tone="warn">waiting for you</Pill>
      </div>

      {items.length > 0 ? (
        <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
          <legend className="sr-only">{isDag ? "Plan steps to run" : "Sources to run"}</legend>
          {items.map((it, i) => {
            const k = itemKey(it, i);
            const src = it as ApprovalSource;
            const node = it as PlanNode;
            const id = `apr-${request.approval_id}-${i}`;
            return (
              <label key={k} htmlFor={id} className={styles.toggleRow}>
                <input
                  id={id}
                  type="checkbox"
                  checked={!!enabled[k]}
                  disabled={busy}
                  onChange={(e) => setEnabled((p) => ({ ...p, [k]: e.target.checked }))}
                />
                <span className={enabled[k] ? undefined : styles.toggleOff}>
                  {isDag ? (
                    <>
                      <span className={styles.mono}>{node.id}</span> <span className={styles.mono} style={{ color: "var(--text-heading)" }}>{node.tool || "(tool)"}</span>
                      {arr(node.depends_on).length ? <span className={styles.meta}> · after {arr<string>(node.depends_on).join(", ")}</span> : null}
                      {node.purpose && <span className={styles.toggleHint}>{node.purpose}</span>}
                    </>
                  ) : (
                    <>
                      <span style={{ color: "var(--text-heading)" }}>{src.label || src.key}</span>
                      {src.reason && <span className={styles.toggleHint}>{src.reason}</span>}
                    </>
                  )}
                </span>
              </label>
            );
          })}
        </fieldset>
      ) : (
        <p className={styles.muted}>The run is waiting for your go-ahead.</p>
      )}

      <input
        className={styles.input}
        style={{ marginTop: 8 }}
        type="text"
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional note to the agent (e.g. focus on phase 3 trials)"
        aria-label="Optional note with your decision"
        disabled={busy}
      />
      <div className={styles.btnRow}>
        <button type="button" className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy} onClick={() => submit(true, false)}>
          Approve
        </button>
        <button
          type="button"
          className={styles.btn}
          disabled={busy || !edited}
          onClick={() => submit(true, true)}
          title={edited ? "Run only the selected items" : "Switch an item off first"}
        >
          Approve edited
        </button>
        <button type="button" className={`${styles.btn} ${styles.btnDanger}`} disabled={busy} onClick={() => submit(false, false)}>
          Reject
        </button>
        {edited && (
          <button type="button" className={styles.btn} disabled={busy} onClick={() => setEnabled(initial)}>
            <RotateCcw style={{ width: 12, height: 12 }} aria-hidden="true" /> Reset
          </button>
        )}
        {error && (
          <button type="button" className={styles.btn} onClick={() => onResolved("expired")}>
            Dismiss
          </button>
        )}
      </div>
      {!isDag && <p className={styles.note}>Rejecting switches every optional source off except the literature search.</p>}
      {error && (
        <p className={styles.errorText} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

export interface SteerNote {
  id: string;
  text: string;
  status: "sending" | "sent" | "failed";
}

/**
 * Guidance the user sent to the running agent from the composer (POST /api/agent/runs/{id}/steer), shown in
 * the live turn so it is clear what the agent was told and whether it arrived.
 */
export function SteeringNotes({ items, onRetry }: { items: SteerNote[]; onRetry: (note: SteerNote) => void }) {
  if (!items.length) return null;
  return (
    <ul className={styles.steerNotes} aria-label="Your guidance to the running agent" aria-live="polite">
      {items.map((n) => (
        <li key={n.id} className={styles.steerNote} data-status={n.status}>
          <Navigation style={{ width: 12, height: 12, flexShrink: 0, marginTop: 3 }} aria-hidden="true" />
          <span className={styles.steerNoteText}>{n.text}</span>
          <span className={styles.steerNoteStatus}>
            {n.status === "sending" ? (
              "sending…"
            ) : n.status === "sent" ? (
              "read before the next draft"
            ) : (
              <>
                not delivered ·{" "}
                <button type="button" className={styles.linkBtn} onClick={() => onRetry(n)}>
                  retry
                </button>
              </>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Clickable options for a clarifying question (task_status "needs_input"); a click sends the option. */
export function ClarificationChips({
  data,
  onSelect,
  disabled,
}: {
  data: ClarificationArtifact | undefined;
  onSelect: (option: string) => void;
  disabled?: boolean;
}) {
  const options = arr(data?.options)
    .map((o) => str(o).trim())
    .filter(Boolean)
    .slice(0, 8);
  if (!options.length) return null;
  return (
    <div role="group" aria-label="Suggested answers">
      <div className={styles.chips}>
        {options.map((o) => (
          <button key={o} type="button" className={styles.chip} disabled={disabled} onClick={() => onSelect(o)}>
            {o}
          </button>
        ))}
      </div>
      {data?.reason && <p className={styles.note}>Asked because: {str(data.reason)}</p>}
    </div>
  );
}
