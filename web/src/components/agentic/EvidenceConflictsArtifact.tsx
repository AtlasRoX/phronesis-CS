"use client";

import React from "react";
import { Scale } from "lucide-react";
import type { EvidenceConflict, EvidenceConflictsArtifact as ConflictsData } from "@/types/agentic";
import { Card, Disclosure, ExtLink, Section, arr, fmt, isNum, str, styles } from "./ui";

const CLASSIFIER: Record<string, { label: string; title: string; tone: "ok" | "info" | "warn" }> = {
  nli: { label: "NLI model", title: "Scored by a local natural-language-inference cross-encoder", tone: "ok" },
  llm: { label: "LLM judge", title: "Rated by the language model on an 11-point Likert rubric", tone: "info" },
  heuristic: {
    label: "heuristic (cue words)",
    title: "No NLI model or LLM judge was available: labels come from contradiction cue words and are unreliable",
    tone: "warn",
  },
};

function labelTone(label: unknown): "bad" | "ok" | "neutral" {
  const l = str(label).toLowerCase();
  if (l === "contradicts" || l === "refutes") return "bad";
  if (l === "supports") return "ok";
  return "neutral";
}

/** One claim and the passage checked against it: claim first, passage quoted, label and source as a meta line. */
function Conflict({ c }: { c: EvidenceConflict }) {
  const pmidUrl = c.pmid ? `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(String(c.pmid))}/` : null;
  const doiUrl = c.doi ? `https://doi.org/${String(c.doi).replace(/^https?:\/\/(dx\.)?doi\.org\//, "")}` : null;
  return (
    <li className={styles.divItem}>
      <span className={styles.itemHead}>{c.claim || "(claim text missing)"}</span>
      {c.passage && <blockquote className={styles.quote}>{c.passage}</blockquote>}
      <div className={styles.itemMeta}>
        <span className={styles.toneText} data-tone={labelTone(c.label)}>
          {str(c.label) || "unlabelled"}
        </span>
        {isNum(c.likert) && (
          <span
            className={styles.toneText}
            data-tone={c.likert >= 8 ? "bad" : c.likert <= 2 ? "ok" : undefined}
            title="0 = strongly supports, 5 = no evidence, 10 = strongly contradicts"
          >
            Likert {c.likert}/10
          </span>
        )}
        {isNum(c.score) && <span>score {fmt(c.score, 2)}</span>}
        {c.source_key && <span className={styles.mono}>{c.source_key}</span>}
        {pmidUrl && <ExtLink href={pmidUrl}>PMID {String(c.pmid)}</ExtLink>}
        {doiUrl && <ExtLink href={doiUrl}>DOI</ExtLink>}
      </div>
    </li>
  );
}

/** Counter-evidence search (PaperQA2/ContraCrow): claims paired with passages that may contradict them. */
export function EvidenceConflictsArtifact({ data }: { data: ConflictsData }) {
  if (!data || typeof data !== "object") return null;
  const conflicts = arr<EvidenceConflict>(data.conflicts).filter((c) => c && typeof c === "object");
  const contradicting = conflicts.filter((c) => labelTone(c.label) === "bad" || (isNum(c.likert) && c.likert >= 8));
  const others = conflicts.filter((c) => !contradicting.includes(c));
  const classifier = str(data.classifier);
  const cls = CLASSIFIER[classifier] || null;
  const queries = arr(data.queries);
  const meta = (
    <>
      {contradicting.length ? `${contradicting.length} contradicting passage${contradicting.length === 1 ? "" : "s"}` : "no contradictions found"}
      {isNum(data.claims_checked) ? ` · ${data.claims_checked} claim${data.claims_checked === 1 ? "" : "s"} checked` : ""}
      {cls ? (
        <>
          {" · "}
          <span className={styles.toneText} data-tone={cls.tone === "warn" ? "warn" : undefined} title={cls.title}>
            classifier: {cls.label}
          </span>
        </>
      ) : classifier ? (
        ` · classifier: ${classifier}`
      ) : (
        ""
      )}
    </>
  );

  return (
    <Card
      icon={<Scale style={{ width: 15, height: 15 }} />}
      title="Counter-evidence"
      meta={meta}
      label="Counter-evidence search"
      collapsible
      defaultOpen={false}
    >
      {classifier === "heuristic" && (
        <p className={styles.warnNote} style={{ marginTop: 0, marginBottom: 8 }}>
          These labels are a heuristic (contradiction cue words), not a model judgement. Read the passages before relying on them.
        </p>
      )}
      {conflicts.length === 0 ? (
        <p className={styles.muted}>No contradicting evidence was found by the counter-queries.</p>
      ) : (
        <>
          {contradicting.length > 0 ? (
            <Section title="Contradicting passages" meta={contradicting.length}>
              <ul className={styles.divList}>
                {contradicting.slice(0, 50).map((c, i) => (
                  <Conflict key={i} c={c} />
                ))}
              </ul>
            </Section>
          ) : (
            <p className={styles.muted}>No passage was rated as contradicting; {others.length} were checked.</p>
          )}
          {others.length > 0 && (
            <Disclosure summary={`Other checked passages (${others.length})`}>
              <ul className={styles.divList}>
                {others.slice(0, 50).map((c, i) => (
                  <Conflict key={i} c={c} />
                ))}
              </ul>
            </Disclosure>
          )}
        </>
      )}
      {queries.length > 0 && (
        <Disclosure summary={`Counter-queries (${queries.length})`}>
          <ul className={styles.list}>
            {queries.map((q, i) => (
              <li key={i} className={`${styles.listItem} ${styles.mono}`}>
                {str(q)}
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </Card>
  );
}
