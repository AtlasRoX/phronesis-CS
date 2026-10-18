"use client";

import React from "react";

interface ErrorBoundaryProps {
  /** Used in the console message, e.g. `Artifact "primekg"` or `Message ast_123`. */
  name: string;
  /** Shown instead of the children after a render error (nothing by default). */
  fallback?: React.ReactNode;
  /** When this changes after a failure the children are tried again (e.g. the message content). */
  resetKey?: unknown;
  children: React.ReactNode;
}

/** Contains a render error to one subtree, so one faulty message or card never blanks the conversation. */
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error(`${this.props.name} failed to render`, error);
  }

  componentDidUpdate(prev: ErrorBoundaryProps) {
    if (this.state.failed && !Object.is(prev.resetKey, this.props.resetKey)) this.setState({ failed: false });
  }

  render() {
    return this.state.failed ? (this.props.fallback ?? null) : this.props.children;
  }
}

/** Fallback for a message body that could not be rendered: the raw text, so nothing is lost. */
export function MessageRenderFallback({ content }: { content: string }) {
  return (
    <div role="note" className="message-render-fallback">
      <p style={{ margin: "0 0 8px", fontSize: "12px", color: "var(--text-muted)" }}>
        This message could not be displayed with formatting; the plain text is shown instead.
      </p>
      <div style={{ whiteSpace: "pre-wrap", color: "var(--text-body)" }}>{content}</div>
    </div>
  );
}

function rawJson(data: unknown, limit = 20000): string {
  let text: string;
  try {
    text = JSON.stringify(data, null, 2) ?? String(data);
  } catch {
    text = String(data);
  }
  return text.length > limit ? `${text.slice(0, limit)}
… (${text.length - limit} more characters not shown)` : text;
}

/**
 * Fallback for a result card that threw while rendering (UI-006, UI-001): says so visibly and offers the raw data,
 * so a failed card is never mistaken for "nothing was found".
 */
export function ArtifactRenderFallback({ label, data }: { label: string; data?: unknown }) {
  return (
    <div role="note" className="message-render-fallback">
      <p style={{ margin: 0, fontSize: "12px", color: "var(--text-muted)" }}>
        The {label} result could not be displayed. This is a display error, not a finding about the data.
      </p>
      {data !== undefined && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: "pointer", fontSize: "12px" }}>Open raw data</summary>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: "11px", maxHeight: 320, overflow: "auto" }}>{rawJson(data)}</pre>
        </details>
      )}
    </div>
  );
}
