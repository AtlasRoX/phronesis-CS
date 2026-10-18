"use client";

import React, { useId, useMemo, useState } from "react";
import { Network } from "lucide-react";
import { layerNodes } from "@/lib/planGraph";
import type { AgentPlanArtifact, PlanNode } from "@/types/agentic";
import { Card, Disclosure, Section, arr, dotClass, fmt, statusTone, str, styles } from "./ui";

const NODE_W = 176;
const NODE_H = 48;
const GAP_X = 46;
const GAP_Y = 14;
const PAD = 6;

const STATUS_COLOR: Record<string, string> = {
  ok: "var(--nvidia-green)",
  bad: "var(--danger)",
  warn: "var(--warning)",
  info: "var(--domain-t2kg)",
  neutral: "var(--border-strong)",
};

/** Why the plan is empty: the planner did not run (status/error from the backend) or it chose no follow-up calls. */
export function emptyPlanMessage(data: { planner?: string; status?: string; error?: string } | null | undefined): string {
  const status = str(data?.status);
  const error = str(data?.error);
  const failed =
    ["planner_failed", "planner_unusable", "budget"].includes(status) || (data?.planner === "none" && Boolean(error));
  if (failed) {
    const why =
      status === "budget"
        ? "the agent budget ran out"
        : status === "planner_unusable" || /unusable/i.test(error)
          ? "the planner's reply was unusable"
          : "model call failed";
    return `The planner did not run (${why}), so the pipeline's own evidence was used.`;
  }
  if (error) return `The plan stopped early (${error}); the pipeline's own evidence was used.`;
  return "No follow-up tool calls were needed; the pipeline's own evidence was used.";
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Status as a coloured dot and a word: lighter than a chip in a table full of them. */
function StatusText({ status }: { status: string }) {
  return (
    <span className={styles.statusText}>
      <span className={dotClass(status)} aria-hidden="true" />
      {status}
    </span>
  );
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The tool DAG the planner built (LLMCompiler): layered left to right by dependency, coloured by status. */
export function PlanGraphArtifact({ data }: { data: AgentPlanArtifact }) {
  const nodes = useMemo(
    () => arr<PlanNode>(data?.nodes).filter((n) => n && typeof n.id === "string" && n.id).slice(0, 50),
    [data]
  );
  const rejected = arr(data?.rejected);
  const [selected, setSelected] = useState<string | null>(null);
  const arrowId = `plan-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const layout = useMemo(() => {
    const depth = layerNodes(nodes);
    const layers: PlanNode[][] = [];
    for (const n of nodes) {
      const d = depth.get(n.id) ?? 0;
      (layers[d] ||= []).push(n);
    }
    const pos = new Map<string, { x: number; y: number }>();
    layers.forEach((layer, li) => {
      (layer || []).forEach((n, ri) => pos.set(n.id, { x: PAD + li * (NODE_W + GAP_X), y: PAD + ri * (NODE_H + GAP_Y) }));
    });
    const cols = Math.max(1, layers.length);
    const rows = Math.max(1, ...layers.map((l) => (l ? l.length : 0)));
    return {
      pos,
      width: PAD * 2 + cols * NODE_W + (cols - 1) * GAP_X,
      height: PAD * 2 + rows * NODE_H + (rows - 1) * GAP_Y,
      layers: layers.length,
    };
  }, [nodes]);

  if (!data || typeof data !== "object") return null;
  const emptyMessage = emptyPlanMessage(data as AgentPlanArtifact & { status?: string; error?: string });
  if (nodes.length === 0 && rejected.length === 0) {
    // Nothing was planned: one line, not a card with an empty-graph header.
    return (
      <p className={styles.compactLine} role="note" aria-label="Agent execution plan">
        <Network style={{ width: 13, height: 13, flexShrink: 0 }} aria-hidden="true" />
        <span>
          <span className={styles.compactLabel}>Execution plan:</span> {emptyMessage}
        </span>
      </p>
    );
  }
  const counts = nodes.reduce<Record<string, number>>((acc, n) => {
    const s = n.status || "pending";
    acc[s] = (acc[s] || 0) + 1;
    return acc;
  }, {});
  // Outcome first (what ran, what failed), then how the plan was made.
  const meta = [
    nodes.length ? `${plural(nodes.length, "step")} in ${plural(layout.layers || 0, "layer")}` : "",
    Object.entries(counts)
      .map(([k, v]) => `${v} ${k}`)
      .join(", "),
    rejected.length ? `${rejected.length} rejected` : "",
    data.replans ? plural(data.replans, "replan") : "",
    data.planner ? `planner: ${data.planner === "none" ? "did not run" : data.planner}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const sel = nodes.find((n) => n.id === selected) || null;

  return (
    <Card
      icon={<Network style={{ width: 15, height: 15 }} />}
      title="Execution plan"
      meta={meta}
      label="Agent execution plan"
      collapsible
      defaultOpen={false}
    >
      {data.rationale && <p className={styles.muted} style={{ marginBottom: 10 }}>{data.rationale}</p>}
      {nodes.length === 0 ? (
        <p className={styles.muted}>{emptyMessage}</p>
      ) : (
        <div className={styles.planDag}>
          <svg
            width={layout.width}
            height={layout.height}
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            role="group"
            aria-label={`Plan graph with ${nodes.length} steps`}
          >
            <defs>
              <marker id={arrowId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M0,0 L8,4 L0,8 z" fill="var(--border-strong)" />
              </marker>
            </defs>
            {nodes.flatMap((n) =>
              arr<string>(n.depends_on)
                .filter((d) => layout.pos.has(d) && d !== n.id)
                .map((d) => {
                  const a = layout.pos.get(d)!;
                  const b = layout.pos.get(n.id)!;
                  const x1 = a.x + NODE_W;
                  const y1 = a.y + NODE_H / 2;
                  const x2 = b.x - 2;
                  const y2 = b.y + NODE_H / 2;
                  const mx = (x1 + x2) / 2;
                  return (
                    <path
                      key={`${d}->${n.id}`}
                      className={styles.dagEdge}
                      d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                      markerEnd={`url(#${arrowId})`}
                    />
                  );
                })
            )}
            {nodes.map((n) => {
              const p = layout.pos.get(n.id)!;
              const tone = statusTone(n.status || "pending");
              const color = STATUS_COLOR[tone] || STATUS_COLOR.neutral;
              const isSel = selected === n.id;
              return (
                <g
                  key={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  role="button"
                  tabIndex={0}
                  aria-pressed={isSel}
                  aria-label={`${n.id} ${n.tool || ""}: ${n.status || "pending"}${n.purpose ? `. ${n.purpose}` : ""}`}
                  style={{ cursor: "pointer", outline: "none" }}
                  onClick={() => setSelected(isSel ? null : n.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelected(isSel ? null : n.id);
                    }
                  }}
                >
                  <title>{[n.tool, n.purpose, n.error].filter(Boolean).join("\n")}</title>
                  <rect
                    width={NODE_W}
                    height={NODE_H}
                    rx={8}
                    style={{
                      fill: `color-mix(in srgb, ${color} ${isSel ? 22 : 10}%, var(--bg-surface-solid))`,
                      stroke: color,
                      strokeWidth: isSel ? 2 : 1.2,
                      strokeDasharray: n.status === "skipped" ? "4 3" : undefined,
                    }}
                  />
                  <text x={10} y={17} className={styles.dagNodeId}>
                    {truncate(`${n.id} · ${n.status || "pending"}`, 30)}
                  </text>
                  <text x={10} y={35} className={styles.dagNodeText}>
                    {truncate(n.tool || "(no tool)", 26)}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      )}

      {sel && (
        <div className={styles.dividerBlock} aria-live="polite">
          <div className={styles.row} style={{ flexWrap: "wrap" }}>
            <span className={styles.mono}>{sel.id}</span>
            <span className={styles.mono} style={{ color: "var(--text-heading)" }}>
              {sel.tool || "(no tool)"}
            </span>
            <span className={styles.meta}>
              <StatusText status={sel.status || "pending"} />
            </span>
            {arr(sel.depends_on).length > 0 && <span className={styles.meta}>after {arr<string>(sel.depends_on).join(", ")}</span>}
            {typeof sel.seconds === "number" && <span className={styles.meta}>{fmt(sel.seconds, 1)} s</span>}
          </div>
          {sel.purpose && <p className={styles.muted} style={{ marginTop: 6 }}>{sel.purpose}</p>}
          {sel.error && <p className={styles.errorText}>{sel.error}</p>}
          {sel.result_key && (
            <p className={styles.note}>
              result: <span className={styles.mono}>{sel.result_key}</span>
            </p>
          )}
          {sel.args && Object.keys(sel.args).length > 0 && (
            <pre className={styles.code} style={{ marginTop: 6, maxHeight: 180 }}>
              {JSON.stringify(sel.args, null, 2)}
            </pre>
          )}
        </div>
      )}

      {nodes.length > 0 && (
        <Disclosure summary="All steps">
          <div className={styles.tableScroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Step</th>
                  <th scope="col">Tool</th>
                  <th scope="col">Depends on</th>
                  <th scope="col">Purpose</th>
                  <th scope="col">Status</th>
                  <th scope="col" className={styles.num}>
                    Seconds
                  </th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => (
                  <tr key={n.id}>
                    <td className={styles.mono}>{n.id}</td>
                    <td className={styles.mono}>{n.tool || "—"}</td>
                    <td>{arr(n.depends_on).join(", ") || "—"}</td>
                    <td className={styles.wrapCell}>
                      {n.purpose || "—"}
                      {n.error && <span className={styles.errorText} style={{ display: "block" }}>{n.error}</span>}
                    </td>
                    <td>
                      <StatusText status={n.status || "pending"} />
                    </td>
                    <td className={styles.num}>{fmt(n.seconds, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Disclosure>
      )}

      {rejected.length > 0 && (
        <Section title="Rejected before running" meta={rejected.length}>
          <ul className={styles.list}>
            {rejected.slice(0, 50).map((r: any, i) => {
              const node = r?.node;
              const label = typeof node === "string" ? node : node && typeof node === "object" ? `${str(node.id)} ${str(node.tool)}`.trim() : "step";
              return (
                <li key={i} className={styles.listItem}>
                  <span className={styles.mono} style={{ color: "var(--danger)", flexShrink: 0 }}>
                    {label || "step"}
                  </span>
                  <span className={styles.muted}>{str(r?.reason) || "rejected by validation"}</span>
                </li>
              );
            })}
          </ul>
        </Section>
      )}
    </Card>
  );
}
