"use client";

import React from "react";
import { Terminal } from "lucide-react";
import type { SandboxAnalysisArtifact as SandboxData, SandboxTable } from "@/types/agentic";
import { Card, CopyButton, Disclosure, Section, arr, fmt, isNum, str, styles } from "./ui";

const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/;
const MAX_ROWS = 50;

function DataTable({ t }: { t: SandboxTable }) {
  const rows = arr<any>(t.rows).slice(0, MAX_ROWS);
  let cols = arr<string>(t.columns).map((c) => str(c));
  if (!cols.length && rows.length && rows[0] && typeof rows[0] === "object" && !Array.isArray(rows[0])) cols = Object.keys(rows[0]);
  if (!rows.length) return <p className={styles.muted}>Empty table.</p>;
  const value = (row: any, ci: number) =>
    Array.isArray(row) ? row[ci] : row && typeof row === "object" ? row[cols[ci]] : ci === 0 ? row : undefined;
  const width = cols.length || (Array.isArray(rows[0]) ? rows[0].length : 1);
  // A column whose values are all numbers is right-aligned, header included.
  const numeric = Array.from(
    { length: width },
    (_, ci) => rows.some((r) => isNum(value(r, ci))) && rows.every((r) => isNum(value(r, ci)) || value(r, ci) == null)
  );
  return (
    <>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          {cols.length > 0 && (
            <thead>
              <tr>
                {cols.map((c, i) => (
                  <th key={i} scope="col" className={numeric[i] ? styles.num : undefined}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {Array.from({ length: width }, (_, ci) => {
                  const v = value(r, ci);
                  return (
                    <td key={ci} className={isNum(v) ? `${styles.num} ${styles.mono}` : styles.mono}>
                      {isNum(v) ? fmt(v, 4) : str(v) || "—"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {arr(t.rows).length > MAX_ROWS && <p className={styles.note}>Showing the first {MAX_ROWS} rows of {arr(t.rows).length}.</p>}
    </>
  );
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Code-as-action analysis (CodeAct) run in the restricted Python sandbox over an attached dataset. */
export function SandboxAnalysisArtifact({ data }: { data: SandboxData }) {
  if (!data || typeof data !== "object") return null;
  const status = str(data.status) || "unknown";
  const figures = arr<{ name?: string; png_base64?: string }>(data.figures)
    .map((f) => ({ name: str(f?.name), b64: str(f?.png_base64).replace(/\s+/g, "") }))
    .filter((f) => f.b64 && BASE64_RE.test(f.b64))
    .slice(0, 3);
  const tables = arr<SandboxTable>(data.tables).filter((t) => t && typeof t === "object");
  const policy = data.policy || {};
  const dataset = typeof data.dataset === "string" ? data.dataset : data.dataset && typeof data.dataset === "object" ? str((data.dataset as any).name ?? (data.dataset as any).id) : "";
  const hasResult = data.result !== undefined && data.result !== null && !(typeof data.result === "object" && Object.keys(data.result).length === 0);
  const resultText = hasResult ? (typeof data.result === "string" ? data.result : JSON.stringify(data.result, null, 2)) : "";

  // Lead with the outcome: a failure or switch-off, else what the run produced.
  const outcome = status === "ok" ? "" : status === "error" ? "failed" : status === "disabled" ? "switched off" : status;
  const produced = [hasResult ? "result" : "", figures.length ? plural(figures.length, "figure") : "", tables.length ? plural(tables.length, "table") : ""]
    .filter(Boolean)
    .join(", ");
  const facts = [produced, dataset ? `dataset ${dataset}` : "", isNum(data.iterations) ? plural(data.iterations, "iteration") : ""]
    .filter(Boolean)
    .join(" · ");
  const meta = (
    <>
      {outcome && (
        <span className={styles.toneText} data-tone={status === "error" ? "bad" : undefined}>
          {outcome}
        </span>
      )}
      {outcome && facts ? " · " : ""}
      {facts}
    </>
  );

  return (
    <Card
      icon={<Terminal style={{ width: 15, height: 15 }} />}
      title="Code analysis"
      meta={meta}
      label="Sandboxed code analysis"
      collapsible
      defaultOpen={false}
    >
      {status === "disabled" && (
        <p className={styles.muted}>Code analysis is switched off on this server (PHRONESIS_SANDBOX) or in your agent settings.</p>
      )}
      {data.error && <p className={styles.errorText}>{str(data.error)}</p>}

      {hasResult && (
        <Section title="Result">
          <pre className={styles.code} style={{ maxHeight: 240 }}>
            {resultText}
          </pre>
        </Section>
      )}

      {figures.length > 0 && (
        <Section title="Figures" meta={figures.length}>
          <div className={styles.figures}>
            {figures.map((f, i) => (
              <figure key={i} className={styles.plainFigure}>
                <img src={`data:image/png;base64,${f.b64}`} alt={f.name || `Figure ${i + 1} from the analysis`} loading="lazy" />
                {f.name && <figcaption>{f.name}</figcaption>}
              </figure>
            ))}
          </div>
        </Section>
      )}

      {tables.map((t, i) => (
        <Section key={i} title={t.name || `Table ${i + 1}`} meta={plural(arr(t.rows).length, "row")}>
          <DataTable t={t} />
        </Section>
      ))}

      {data.code && (
        <Disclosure summary="Code">
          <div className={styles.codeWrap}>
            <pre className={styles.code}>{data.code}</pre>
            <span className={styles.copyBtn}>
              <CopyButton text={data.code} label="Copy code" />
            </span>
          </div>
        </Disclosure>
      )}
      {data.stdout && (
        <Disclosure summary="Output (stdout)">
          <pre className={styles.code}>{data.stdout}</pre>
        </Disclosure>
      )}
      <Disclosure summary="About the sandbox">
        {/* UI-020: only what the backend reported; a missing policy is never filled in with assumed guarantees. */}
        {policy.isolation || policy.network || isNum(policy.timeout_s) ? (
          <p className={styles.note} style={{ marginTop: 0 }}>
            Ran in a restricted Python subprocess:{" "}
            {[
              policy.isolation ? str(policy.isolation) : "isolation not reported",
              policy.network ? `network ${str(policy.network)}` : "network policy not reported",
              isNum(policy.timeout_s) ? `${policy.timeout_s}s timeout` : null,
            ]
              .filter(Boolean)
              .join(", ")}
            . This is defence in depth, not a container.
          </p>
        ) : (
          <p className={styles.note} style={{ marginTop: 0 }}>
            Sandbox policy not reported for this run, so its isolation and network restrictions are unknown.
          </p>
        )}
      </Disclosure>
    </Card>
  );
}
