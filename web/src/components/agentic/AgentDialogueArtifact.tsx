"use client";

import React from "react";
import { MessagesSquare } from "lucide-react";
import type { AgentDialogueArtifact as AgentDialogueData, AgentDialogueTurn } from "@/types/agentic";
import { Card, ChipList, Disclosure, Section, arr, humanize, isNum, str, styles } from "./ui";

const TOPOLOGY: Record<string, { label: string; title: string }> = {
  supervisor: { label: "Supervisor", title: "Supervisor (Magentic-One style ledger)" },
  swarm: { label: "Swarm", title: "Swarm (handoffs between specialists)" },
  debate: { label: "Debate", title: "Debate (critic personas)" },
};

const SHOW_FIRST = 12;

/** Handoffs, critiques and the closing turn are coloured; routine actions stay plain. */
function actionTone(action: string): string | undefined {
  if (action.includes("handoff")) return "accent";
  if (action.includes("critique") || action.includes("critic")) return "warn";
  if (action.includes("finish")) return "ok";
  return undefined;
}

function Turn({ t }: { t: AgentDialogueTurn }) {
  const action = str(t.action);
  return (
    <div className={styles.turn} role="listitem">
      <span className={styles.turnAgent}>{humanize(str(t.agent) || "agent")}</span>
      <span className={styles.turnBody}>
        {t.summary ? str(t.summary) : <span className={styles.muted}>(no summary)</span>}
        {(action || t.tool || isNum(t.round)) && (
          <span className={styles.itemMeta}>
            {action && (
              <span className={styles.toneText} data-tone={actionTone(action.toLowerCase())}>
                {action}
              </span>
            )}
            {t.tool && <span className={styles.mono}>{str(t.tool)}</span>}
            {isNum(t.round) && <span>round {t.round}</span>}
          </span>
        )}
      </span>
    </div>
  );
}

/** Turns between agents in a supervisor, swarm or debate run. */
export function AgentDialogueArtifact({ data }: { data: AgentDialogueData }) {
  if (!data || typeof data !== "object") return null;
  const turns = arr<AgentDialogueTurn>(data.turns).filter((t) => t && typeof t === "object");
  if (!turns.length) return null;
  const agents = Array.from(new Set(turns.map((t) => str(t.agent)).filter(Boolean)));
  const topology = str(data.topology);
  const topo = TOPOLOGY[topology] || (topology ? { label: humanize(topology), title: humanize(topology) } : null);
  const head = turns.slice(0, SHOW_FIRST);
  const rest = turns.slice(SHOW_FIRST);
  const last = turns[turns.length - 1];
  const meta = (
    <>
      {topo && <span title={topo.title}>{topo.label} · </span>}
      {turns.length} turn{turns.length === 1 ? "" : "s"}
      {agents.length ? ` among ${agents.length} agent${agents.length === 1 ? "" : "s"}` : ""}
      {last?.agent ? ` · last word: ${humanize(str(last.agent))}` : ""}
    </>
  );

  return (
    <Card
      icon={<MessagesSquare style={{ width: 15, height: 15 }} />}
      title="Agent dialogue"
      meta={meta}
      label="Multi-agent dialogue"
      collapsible
      defaultOpen={false}
    >
      {agents.length > 1 && (
        <Section title="Agents" meta={agents.length}>
          <ChipList items={agents.map((a) => ({ key: a, label: humanize(a) }))} limit={10} />
        </Section>
      )}
      <Section title="Turns" meta={rest.length ? `first ${head.length} of ${turns.length}` : turns.length}>
        <div role="list">
          {head.map((t, i) => (
            <Turn key={i} t={t} />
          ))}
        </div>
        {rest.length > 0 && (
          <Disclosure summary={`${rest.length} more turn${rest.length === 1 ? "" : "s"}`}>
            <div role="list">
              {rest.map((t, i) => (
                <Turn key={i} t={t} />
              ))}
            </div>
          </Disclosure>
        )}
      </Section>
    </Card>
  );
}
