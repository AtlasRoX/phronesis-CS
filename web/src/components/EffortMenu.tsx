"use client";

import React, { useEffect, useRef } from "react";
import { Brain, Check, Rocket, Scale, Zap } from "lucide-react";
import { EFFORT_LEVELS } from "@/services/agentService";
import type { EffortLevel } from "@/types/agentic";

const ICONS: Record<string, React.ComponentType<{ style?: React.CSSProperties; className?: string }>> = {
  Zap,
  Scale,
  Brain,
  Rocket,
};

export function EffortMenu({
  selected,
  onSelect,
  onClose,
}: {
  selected?: EffortLevel;
  onSelect: (effort: EffortLevel) => void;
  onClose: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const activeLevel = selected || "medium";

  useEffect(() => {
    const handleDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handleDown);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleDown);
      document.removeEventListener("keydown", handleKey);
    };
  }, [onClose]);

  return (
    <div
      ref={menuRef}
      role="listbox"
      aria-label="Thinking effort level"
      style={{
        position: "absolute",
        bottom: "calc(100% + 8px)",
        left: 0,
        zIndex: 100,
        width: "290px",
        padding: "6px",
        display: "flex",
        flexDirection: "column",
        gap: "4px",
        background: "var(--bg-surface)",
        border: "1px solid var(--border-strong)",
        borderRadius: "12px",
        boxShadow: "0 14px 40px rgba(0, 0, 0, 0.28)",
        backdropFilter: "blur(20px)",
        WebkitBackdropFilter: "blur(20px)",
        animation: "menuIn 0.18s cubic-bezier(0.16, 1, 0.3, 1) forwards",
      }}
    >
      <div
        style={{
          padding: "6px 8px 4px",
          borderBottom: "1px solid var(--border-subtle)",
          marginBottom: "2px",
        }}
      >
        <span
          style={{
            fontSize: "11px",
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            color: "var(--text-muted)",
          }}
        >
          Reasoning & Thinking Effort
        </span>
      </div>

      {EFFORT_LEVELS.map((lvl) => {
        const isSelected = activeLevel === lvl.id;
        const IconComponent = ICONS[lvl.icon] || Brain;
        return (
          <button
            key={lvl.id}
            type="button"
            role="option"
            aria-selected={isSelected}
            onClick={() => onSelect(lvl.id)}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: "10px",
              width: "100%",
              padding: "8px 10px",
              border: isSelected ? "1px solid rgba(var(--primary-rgb, 14, 165, 233), 0.3)" : "1px solid transparent",
              borderRadius: "8px",
              background: isSelected ? "var(--bg-surface-active, rgba(255, 255, 255, 0.06))" : "transparent",
              font: "inherit",
              textAlign: "left",
              color: "var(--text-main)",
              cursor: "pointer",
              transition: "background 0.12s ease, border-color 0.12s ease",
            }}
            onMouseEnter={(e) => {
              if (!isSelected) e.currentTarget.style.background = "var(--bg-surface-hover, rgba(255, 255, 255, 0.04))";
            }}
            onMouseLeave={(e) => {
              if (!isSelected) e.currentTarget.style.background = "transparent";
            }}
          >
            <div
              style={{
                width: "24px",
                height: "24px",
                borderRadius: "6px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: `color-mix(in srgb, ${lvl.color} 15%, transparent)`,
                color: lvl.color,
                flexShrink: 0,
                marginTop: "1px",
              }}
            >
              <IconComponent style={{ width: 14, height: 14 }} />
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "2px", flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" }}>
                <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main)" }}>
                  {lvl.name}
                </span>
                <span
                  style={{
                    fontSize: "10px",
                    fontWeight: 600,
                    padding: "1px 6px",
                    borderRadius: "10px",
                    background: `color-mix(in srgb, ${lvl.color} 15%, transparent)`,
                    color: lvl.color,
                  }}
                >
                  {lvl.badge}
                </span>
              </div>
              <span style={{ fontSize: "11px", color: "var(--text-muted)", lineHeight: 1.35 }}>
                {lvl.tagline}
              </span>
            </div>

            {isSelected && (
              <Check style={{ width: 14, height: 14, color: "var(--primary, #0ea5e9)", flexShrink: 0, marginTop: "4px" }} />
            )}
          </button>
        );
      })}
    </div>
  );
}
