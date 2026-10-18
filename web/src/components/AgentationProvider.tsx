"use client";

import React, { useEffect, useState } from "react";
import dynamic from "next/dynamic";

const Agentation = dynamic(
  () => import("agentation").then((mod) => mod.Agentation),
  { ssr: false }
);

export function AgentationProvider() {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    try {
      const pos = localStorage.getItem("feedback-toolbar-position");
      if (pos) {
        const parsed = JSON.parse(pos);
        if (typeof parsed.x === "number" && parsed.x < 280) {
          localStorage.removeItem("feedback-toolbar-position");
        }
      }
    } catch {
      // ignore
    }
    setMounted(true);
  }, []);

  if (!mounted) {
    return null;
  }

  const endpoint =
    process.env.NEXT_PUBLIC_AGENTATION_ENDPOINT || "http://localhost:4747";

  return (
    <Agentation
      endpoint={endpoint}
      appName="Phronesis"
      copyToClipboard={true}
    />
  );
}
