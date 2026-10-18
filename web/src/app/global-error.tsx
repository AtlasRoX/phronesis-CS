"use client";

import { useEffect } from "react";

/** Last-resort error screen for a failure in the root layout; it replaces the whole document. */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("The application failed to render", error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "12px",
          padding: "24px",
          fontFamily: "system-ui, sans-serif",
          textAlign: "center",
          backgroundColor: "#05060f",
          color: "#e8e9f3",
        }}
      >
        <main role="alert">
          <h1 style={{ margin: 0, fontSize: "18px", fontWeight: 600 }}>Phronesis could not start</h1>
          <p style={{ maxWidth: "480px", fontSize: "14px", color: "#a0a3b8" }}>
            An unexpected error stopped the application from loading.
            {error.digest ? ` (Reference: ${error.digest})` : ""}
          </p>
          <button type="button" onClick={() => reset()} style={{ padding: "8px 16px", borderRadius: "6px", border: "none", cursor: "pointer", fontWeight: 600 }}>
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
