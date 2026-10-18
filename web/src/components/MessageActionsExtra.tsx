"use client";

import React, { useState } from "react";
import { GitCompare } from "lucide-react";

import { ExportMenu } from "@/components/ExportMenu";
import { RerunDiffPanel } from "@/components/RerunDiffPanel";
import type { Message } from "@/types/chat";

export interface MessageActionsExtraProps {
  message: Message;
  /** The user question this answer replies to ("" when unknown). */
  question: string;
  backendUrl: string;
  authHeaders: (extra?: Record<string, string>) => Record<string, string>;
}

/** Extra answer actions after Copy: Export, and Re-run & compare for answers that carry a stored run. */
export function MessageActionsExtra({ message, question, backendUrl, authHeaders }: MessageActionsExtraProps) {
  const [rerunOpen, setRerunOpen] = useState(false);
  const isSystem = message.agent === "System" || !message.content?.trim();
  const runId = message.run_manifest?.run_id;

  return (
    <>
      <ExportMenu
        message={message}
        question={question}
        backendUrl={backendUrl}
        authHeaders={authHeaders}
        disabled={isSystem}
      />
      {runId && !isSystem && (
        <button
          type="button"
          className="msg-action-btn"
          aria-expanded={rerunOpen}
          onClick={() => setRerunOpen((v) => !v)}
          title="Re-run this answer and compare the results"
        >
          <GitCompare style={{ width: 13, height: 13 }} aria-hidden="true" /> Re-run &amp; compare
        </button>
      )}
      {runId && !isSystem && rerunOpen && (
        <RerunDiffPanel runId={runId} backendUrl={backendUrl} authHeaders={authHeaders} />
      )}
    </>
  );
}
