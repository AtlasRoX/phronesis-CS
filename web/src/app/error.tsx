"use client";

import { useEffect } from "react";

/** Route-level error screen: shown instead of a blank page when the chat view throws while rendering. */
export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("The page failed to render", error);
  }, [error]);

  return (
    <main
      role="alert"
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "12px",
        padding: "24px",
        textAlign: "center",
        backgroundColor: "var(--bg-primary, #05060f)",
        color: "var(--text-main, #e8e9f3)",
      }}
    >
      <h1 style={{ margin: 0, fontSize: "18px", fontWeight: 600 }}>Something went wrong</h1>
      <p style={{ margin: 0, maxWidth: "480px", fontSize: "14px", color: "var(--text-muted, #a0a3b8)" }}>
        Phronesis hit an unexpected error while showing this page. Your investigations are saved in this browser.
        {error.digest ? ` (Reference: ${error.digest})` : ""}
      </p>
      <div style={{ display: "flex", gap: "8px" }}>
        <button
          type="button"
          onClick={() => reset()}
          style={{ padding: "8px 16px", borderRadius: "6px", border: "none", cursor: "pointer", fontWeight: 600 }}
        >
          Try again
        </button>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{ padding: "8px 16px", borderRadius: "6px", border: "1px solid currentColor", background: "transparent", color: "inherit", cursor: "pointer" }}
        >
          Reload
        </button>
      </div>
    </main>
  );
}
