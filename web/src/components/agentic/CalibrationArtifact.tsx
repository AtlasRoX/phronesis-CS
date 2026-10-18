"use client";

import React from "react";
import { ChartLine } from "lucide-react";
import type { CalibrationParameter, ModelCalibrationArtifact } from "@/types/agentic";
import { Card, Disclosure, Pill, Section, Stats, arr, fmt, isNum, str, styles } from "./ui";

const CHI2_95 = 3.84; // Δχ² for a 95% interval, one degree of freedom (Raue et al. 2009)
const W = 260;
const H = 140;
const M = { l: 34, r: 8, t: 8, b: 20 };

const IDENT: Record<string, { label: string; tone: "ok" | "warn" | "bad"; title: string }> = {
  identifiable: { label: "identifiable", tone: "ok", title: "The profile likelihood crosses the 95% threshold on both sides" },
  practically_non_identifiable: {
    label: "practically non-identifiable",
    tone: "warn",
    title: "The profile stays below the threshold on one side: the data do not bound this parameter",
  },
  structurally_non_identifiable: {
    label: "structurally non-identifiable",
    tone: "bad",
    title: "The profile is flat: the model structure cannot determine this parameter",
  },
};

type Pt = { x: number; y: number };

function scale(values: number[], lo: number, hi: number, log: boolean) {
  const vs = log ? values.map(Math.log10) : values;
  let min = Math.min(...vs);
  let max = Math.max(...vs);
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    min = 0;
    max = 1;
  }
  if (max === min) {
    max += 1;
    min -= 1;
  }
  const f = (v: number) => lo + (((log ? Math.log10(v) : v) - min) / (max - min)) * (hi - lo);
  return { f, min: log ? 10 ** min : min, max: log ? 10 ** max : max };
}

function MiniChart({
  title,
  line,
  dots,
  hline,
  xLabel,
  yLabel,
  logX = false,
}: {
  title: string;
  line: Pt[];
  dots?: Pt[];
  hline?: number;
  xLabel: string;
  yLabel: string;
  logX?: boolean;
}) {
  const all = [...line, ...(dots || [])].filter((p) => isNum(p.x) && isNum(p.y) && (!logX || p.x > 0));
  if (all.length < 2) return null;
  const ys = all.map((p) => p.y).concat(hline != null ? [hline] : []);
  const sx = scale(all.map((p) => p.x), M.l, W - M.r, logX);
  const sy = scale(ys, H - M.b, M.t, false);
  const path = line
    .filter((p) => isNum(p.x) && isNum(p.y) && (!logX || p.x > 0))
    .sort((a, b) => a.x - b.x)
    .map((p, i) => `${i ? "L" : "M"}${sx.f(p.x).toFixed(1)},${sy.f(p.y).toFixed(1)}`)
    .join(" ");
  return (
    <div className={styles.plainChart}>
      <div className={styles.chartTitle} title={title}>
        {title}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${yLabel} against ${xLabel}`}>
        <line className={styles.axis} x1={M.l} y1={H - M.b} x2={W - M.r} y2={H - M.b} />
        <line className={styles.axis} x1={M.l} y1={M.t} x2={M.l} y2={H - M.b} />
        <text className={styles.axisText} x={M.l} y={H - 6}>
          {fmt(sx.min, 2)}
        </text>
        <text className={styles.axisText} x={W - M.r} y={H - 6} textAnchor="end">
          {fmt(sx.max, 2)} {xLabel}
        </text>
        <text className={styles.axisText} x={M.l - 4} y={H - M.b} textAnchor="end">
          {fmt(sy.min, 2)}
        </text>
        <text className={styles.axisText} x={M.l - 4} y={M.t + 8} textAnchor="end">
          {fmt(sy.max, 2)}
        </text>
        {hline != null && (
          <line className={styles.thresholdLine} x1={M.l} x2={W - M.r} y1={sy.f(hline)} y2={sy.f(hline)}>
            <title>{`Δχ² = ${CHI2_95} (95% threshold)`}</title>
          </line>
        )}
        {path && <path className={styles.seriesSim} d={path} />}
        {(dots || [])
          .filter((p) => isNum(p.x) && isNum(p.y))
          .map((p, i) => (
            <circle key={i} className={styles.seriesObs} cx={sx.f(p.x)} cy={sy.f(p.y)} r={2.4} />
          ))}
      </svg>
    </div>
  );
}

function ParamTable({ params }: { params: CalibrationParameter[] }) {
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Parameter</th>
            <th scope="col" className={styles.num}>
              Initial
            </th>
            <th scope="col" className={styles.num}>
              Estimate
            </th>
            <th scope="col" className={styles.num}>
              95% CI (profile)
            </th>
            <th scope="col">Identifiability</th>
          </tr>
        </thead>
        <tbody>
          {params.map((p, i) => {
            const id = IDENT[str(p.identifiability)];
            return (
              <tr key={p.name || i}>
                <td className={styles.mono}>{p.name || "—"}</td>
                <td className={styles.num}>{fmt(p.initial, 4)}</td>
                <td className={styles.num} style={{ color: "var(--text-heading)" }}>
                  {fmt(p.estimate, 4)}
                </td>
                <td className={styles.num}>
                  {isNum(p.ci_low) || isNum(p.ci_high) ? `[${isNum(p.ci_low) ? fmt(p.ci_low, 4) : "−∞"}, ${isNum(p.ci_high) ? fmt(p.ci_high, 4) : "∞"}]` : "—"}
                </td>
                <td>
                  {id ? (
                    <Pill tone={id.tone} title={id.title}>
                      {id.label}
                    </Pill>
                  ) : p.identifiability ? (
                    <Pill tone="neutral">{str(p.identifiability)}</Pill>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const OK_STATUS = ["ok", "converged", "succeeded"];

/** "3 of 4 identifiable · 1 practically non-identifiable": the fit's verdict in one line. */
function identifiabilityVerdict(params: CalibrationParameter[]): string {
  const counts = params.reduce<Record<string, number>>((acc, p) => {
    const k = str(p.identifiability);
    if (k) acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});
  const assessed = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!assessed) return "";
  const ok = counts.identifiable || 0;
  const rest = Object.entries(counts)
    .filter(([k]) => k !== "identifiable")
    .map(([k, v]) => `${v} ${IDENT[k]?.label || k.replace(/_/g, " ")}`);
  if (!rest.length) return ok === 1 ? "identifiable" : `all ${ok} identifiable`;
  return [`${ok} of ${assessed} identifiable`, ...rest].join(" · ");
}

/** SBML parameter estimation: fitted values, profile-likelihood CIs and identifiability, fit and profile plots. */
export function CalibrationArtifact({ data }: { data: ModelCalibrationArtifact }) {
  if (!data || typeof data !== "object") return null;
  const params = arr<CalibrationParameter>(data.parameters).filter((p) => p && typeof p === "object");
  const obj = data.objective || {};
  const fit = data.fit && typeof data.fit === "object" ? Object.entries(data.fit).slice(0, 6) : [];
  const profiles = data.profiles && typeof data.profiles === "object" ? Object.entries(data.profiles).slice(0, 3) : [];
  const dataset = typeof data.dataset === "string" ? data.dataset : data.dataset && typeof data.dataset === "object" ? str((data.dataset as any).name ?? (data.dataset as any).id) : "";
  const status = str(data.status);
  const failed = status === "error" || status === "failed";
  const verdict = identifiabilityVerdict(params);
  // Verdict first; model, data and backend are context and sit in the body.
  const meta = (
    <>
      {status && !OK_STATUS.includes(status) && (
        <>
          <span className={styles.toneText} data-tone={failed ? "bad" : undefined}>
            {status}
          </span>
          {" · "}
        </>
      )}
      {[
        params.length ? `${params.length} parameter${params.length === 1 ? "" : "s"}` : "no parameters estimated",
        verdict,
        isNum(obj.rmse) ? `RMSE ${fmt(obj.rmse, 3)}` : isNum(obj.chi2) ? `χ² ${fmt(obj.chi2, 3)}` : "",
        data.model_id ? `model ${data.model_id}` : "",
      ]
        .filter(Boolean)
        .join(" · ")}
    </>
  );
  const fitStats = [
    isNum(obj.chi2) ? { label: "χ²", value: fmt(obj.chi2, 3) } : null,
    isNum(obj.rmse) ? { label: "RMSE", value: fmt(obj.rmse, 3) } : null,
    isNum(obj.n_points) ? { label: "Data points", value: String(obj.n_points) } : null,
  ].filter((x): x is { label: string; value: string } => x != null);
  const context = [data.model_id ? `Model ${data.model_id}` : "", dataset ? `data ${dataset}` : "", data.backend ? `backend ${data.backend}` : ""]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<ChartLine style={{ width: 15, height: 15 }} />}
      title="Model calibration"
      meta={meta}
      label="Model parameter calibration"
      collapsible
      defaultOpen={false}
    >
      {(fitStats.length > 0 || context) && (
        <Section title="Fit" meta={context || undefined}>
          {fitStats.length > 0 && <Stats items={fitStats} />}
        </Section>
      )}

      <Section title="Parameters" meta={verdict || undefined}>
        {params.length > 0 ? <ParamTable params={params} /> : <p className={styles.muted}>No parameters were estimated.</p>}
      </Section>

      {fit.length > 0 && (
        <Section title="Observed vs simulated">
          <div className={styles.charts}>
            {fit.map(([species, s]) => {
              const t = arr<number>(s?.time);
              const sim = arr<number | null>(s?.simulated);
              const ob = arr<number | null>(s?.observed);
              return (
                <MiniChart
                  key={species}
                  title={species}
                  xLabel="time"
                  yLabel={species}
                  line={t.map((x, i) => ({ x, y: sim[i] as number }))}
                  dots={t.map((x, i) => ({ x, y: ob[i] as number }))}
                />
              );
            })}
          </div>
          <div className={styles.legend}>
            <span>
              <span className={styles.legendSwatch} style={{ background: "var(--domain-t2kg)" }} />
              simulated (fitted)
            </span>
            <span>
              <span className={styles.legendSwatch} style={{ background: "var(--domain-s2)", height: 6, width: 6, borderRadius: 3 }} />
              observed
            </span>
          </div>
        </Section>
      )}

      {profiles.length > 0 && (
        <Section title="Profile likelihood" hint={`Dashed line: Δχ² = ${CHI2_95}, the 95% threshold`}>
          <div className={styles.charts}>
            {profiles.map(([name, pts]) => {
              const list = arr<{ value?: number; chi2?: number }>(pts).filter((p) => isNum(p?.value) && isNum(p?.chi2));
              const min = list.length ? Math.min(...list.map((p) => p.chi2 as number)) : 0;
              const xs = list.map((p) => p.value as number);
              const logX = xs.length > 1 && xs.every((x) => x > 0) && Math.max(...xs) / Math.min(...xs) > 50;
              return (
                <MiniChart
                  key={name}
                  title={`${name}${logX ? " (log scale)" : ""}`}
                  xLabel="value"
                  yLabel="Δχ²"
                  logX={logX}
                  line={list.map((p) => ({ x: p.value as number, y: (p.chi2 as number) - min }))}
                  dots={list.map((p) => ({ x: p.value as number, y: (p.chi2 as number) - min }))}
                  hline={CHI2_95}
                />
              );
            })}
          </div>
          <Disclosure summary="How to read the profiles">
            <p className={styles.note} style={{ marginTop: 0 }}>
              Dashed line: Δχ² = {CHI2_95}, the 95% confidence threshold (one degree of freedom). A flat profile means the
              parameter is structurally non-identifiable; one open side means practically non-identifiable.
            </p>
          </Disclosure>
        </Section>
      )}
    </Card>
  );
}
