"use client";

import React from "react";
import { Activity } from "lucide-react";
import { Card, Disclosure, Pill, Section, type Tone, styles as ui } from "@/components/agentic/ui";
import { STEADY_ATOL_REL, STEADY_RTOL, changeText, checkSteadyState, largestChange } from "@/lib/simulationMath";
import styles from "./SimulationArtifact.module.css";

interface SimulationArtifactProps {
  data: any;
}

function fmt(v: unknown): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e5)) return v.toExponential(2);
  // Four significant figures keeps columns comparable (10.00, 280.0, 0.1021).
  return v.toPrecision(4);
}

const STABILITY: Record<string, { label: string; tone: Tone; text: string }> = {
  STABLE: {
    label: "stable",
    tone: "ok",
    text: "COPASI's steady state is stable (every Jacobian eigenvalue has a negative real part).",
  },
  STABLE_FOCUS: {
    label: "stable focus",
    tone: "ok",
    text: "COPASI's steady state is a stable focus: the system spirals into it with damped oscillations.",
  },
  UNSTABLE: {
    label: "unstable",
    tone: "bad",
    text: "COPASI's steady state is unstable (a Jacobian eigenvalue has a positive real part); the system moves away from it.",
  },
  UNSTABLE_OSCILLATORY: {
    label: "unstable, oscillatory",
    tone: "bad",
    text: "COPASI's steady state is unstable with complex eigenvalues; the system likely oscillates (limit cycle) instead of settling.",
  },
  MARGINAL: {
    label: "marginal",
    tone: "warn",
    text: "COPASI's steady state is marginally stable (largest eigenvalue real part ≈ 0); stability cannot be decided.",
  },
};

const CONVERGENCE: Record<string, { label: string; text: string }> = {
  ENDPOINT_STABLE: { label: "settled by the end", text: "Species values had stopped changing by the end of the run." },
  SLOWLY_VARYING: { label: "still changing slowly", text: "Species values were still changing slowly at the end of the run." },
  TRANSIENT_DYNAMICS: { label: "still changing", text: "Species values were still changing at the end of the run (transient dynamics)." },
  OSCILLATORY: { label: "oscillating", text: "Species values were oscillating at the end of the run; end values are one point on a cycle." },
};

/** A parameter scan: one row per requested point, failures listed with their reason. */
function ScanTable({ scan, unit }: { scan: any; unit?: string }) {
  const results: any[] = Array.isArray(scan?.scan_results) ? scan.scan_results : [];
  const failed: any[] = Array.isArray(scan?.failed_points) ? scan.failed_points : [];
  const target: string | undefined = scan?.target_species || undefined;
  // Without a target species, show up to three endpoint species so the table stays readable.
  const species: string[] = target
    ? [target]
    : Object.keys(results.find((r) => r?.endpoint_species)?.endpoint_species || {}).slice(0, 3);
  const rows = [
    ...results.map((r) => ({ ...r, ok: true })),
    ...failed.map((f) => ({ ...f, ok: false })),
  ].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  if (!rows.length) return null;

  return (
    <div className={ui.tableScroll}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col" className={ui.num}>
              #
            </th>
            <th scope="col" className={ui.num}>
              {scan?.resolved_parameter || scan?.parameter_name || "Parameter"}
            </th>
            {species.map((sp) => (
              <th key={sp} scope="col" className={ui.num}>
                {sp} at end{unit ? ` (${unit})` : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.index}>
              <td className={`${ui.num} ${styles.muted}`}>{(r.index ?? 0) + 1}</td>
              <td className={ui.num}>{fmt(r.applied_value ?? r.parameter_value)}</td>
              {r.ok ? (
                species.map((sp) => (
                  <td key={sp} className={ui.num}>
                    {fmt(sp === target ? r.target_response ?? r.endpoint_species?.[sp] : r.endpoint_species?.[sp])}
                  </td>
                ))
              ) : (
                <td className={styles.failed} colSpan={Math.max(species.length, 1)}>
                  failed: {String(r.error || "unknown error")}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The Vivarix (COPASI) run behind an answer: trajectory plot plus start/end concentrations, or a scan table. */
export function SimulationArtifact({ data }: SimulationArtifactProps) {
  const scan = data?.simulation_type === "PARAMETER_SCAN" ? data : data?.parameter_scan;
  const finals: Record<string, number> = data?.final_concentrations || {};
  const initials: Record<string, number> = data?.initial_concentrations || {};
  const steadyBlock = data?.steady_state;
  const steady: Record<string, number> | undefined = steadyBlock?.steady_state_concentrations;
  const stability = STABILITY[steadyBlock?.stability as string];
  const timeUnit = data?.units?.time ?? "s";
  const unit: string | undefined = data?.concentration_unit || data?.units?.quantity;
  const convergence = CONVERGENCE[(data?.diagnostics?.convergence_status || data?.convergence_status) as string];
  const overrides: any[] = Array.isArray(data?.applied_overrides) ? data.applied_overrides : [];
  const species = Object.keys(finals);
  const modelName: string = data?.model_name || "Kinetic model";
  // Did the time course actually arrive at COPASI's steady state? Absolute + relative tolerance; species missing
  // from either map make the verdict "unknown" rather than a silent pass (SCI-013).
  const steadyCheck = steady ? checkSteadyState(species, finals, steady, initials) : null;
  const duration =
    data?.duration != null
      ? `${data.duration} ${timeUnit}${
          typeof data?.duration_seconds === "number" && timeUnit !== "s" ? ` (${data.duration_seconds} s requested)` : ""
        }`
      : null;
  const scanTotal = scan ? scan.total_points ?? (scan.scan_results?.length || 0) + (scan.failed_points?.length || 0) : 0;
  const scanDone = scan ? scan.successful_points ?? scan.scan_results?.length ?? 0 : 0;

  // Headline outcome for the collapsed card: where the run ended up, and the species that moved most.
  let outcome: string;
  let note: string;
  if (scan && !species.length) {
    outcome = `${scanDone} of ${scanTotal} scan points`;
    note = `${scanDone} of ${scanTotal} scan points completed; each point started from the model's initial values.`;
  } else if (!steady) {
    outcome = convergence ? convergence.label : "ran to end";
    note = "End values are where the time course stopped, not a steady state.";
  } else if (steadyCheck?.status === "reached") {
    outcome = `reached steady state${stability ? ` (${stability.label})` : ""}`;
    note = "The time course reached COPASI's steady state by the end of the run.";
  } else if (steadyCheck?.status === "unknown") {
    outcome = `steady state comparison incomplete${stability ? ` (${stability.label})` : ""}`;
    note = steadyCheck.unmatched.length
      ? `${steadyCheck.unmatched.length} species (${steadyCheck.unmatched.slice(0, 4).join(", ")}${steadyCheck.unmatched.length > 4 ? ", …" : ""}) have no steady-state value to compare with, so whether the run reached steady state is not known.`
      : "No species could be compared with COPASI's steady state.";
  } else {
    outcome = `steady state not reached${stability ? ` (${stability.label})` : ""}`;
    note = `The time course had not reached COPASI's steady state by t = ${data?.duration} ${timeUnit} (${steadyCheck?.notReached.length ?? 0} species still away from it); that state may be unstable or need a longer run.`;
  }
  // Ranked relative to each species' start, floored at a thousandth of the run's largest value, so a zero-start
  // readout can be named and a near-zero start that barely moves cannot dominate (SCI-015).
  const biggest = largestChange(species, initials, finals);
  const meta = [
    modelName,
    outcome,
    biggest && biggest.score >= 0.001
      ? `largest change ${biggest.species} ${changeText(initials[biggest.species], finals[biggest.species], biggest.floor)}`
      : null,
    scan && species.length ? `${scanDone} of ${scanTotal} scan points` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const source =
    data?.provenance?.source === "user_upload"
      ? ["uploaded SBML", data.provenance.sbml_model_id].filter(Boolean).join(" ")
      : data?.model_id;
  const runDetails = [source, data?.solver_method ? `solver ${data.solver_method}` : null, duration ? `duration ${duration}` : null, unit ? `concentrations in ${unit}` : null]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      icon={<Activity className={styles.icon} style={{ width: 15, height: 15 }} />}
      title={scan ? "Parameter scan" : "Kinetic simulation"}
      meta={meta}
      label="Simulation output"
      collapsible
      defaultOpen={false}
    >
      {data?.plot_base64_png && (
        <Section title="Trajectory" meta={duration ?? undefined}>
          {/* Rendered server-side by matplotlib on a white canvas, so its colours cannot follow the theme. */}
          <img
            className={styles.plot}
            src={`data:image/png;base64,${data.plot_base64_png}`}
            alt={`Species concentrations over time for ${data?.model_name || "the simulated model"}`}
          />
        </Section>
      )}

      {!data?.plot_base64_png && data?.plot_dropped_for_storage && (
        <p className={ui.note}>The trajectory plot was not kept in this browser&apos;s storage (it was full).</p>
      )}

      {overrides.length > 0 && (
        <Section title="Applied overrides" meta={overrides.length}>
          <ul className={styles.overrides}>
            {overrides.map((o, i) => {
              const name = String(o.resolved || o.name || o.parameter || "parameter");
              return (
                <li key={`${name}-${i}`} className={styles.override}>
                  <span className={`${ui.mono} ${styles.name}`} title={name}>
                    {name}
                  </span>
                  <span className={styles.values}>
                    {fmt(o.previous_value ?? o.old_value)} → <strong>{fmt(o.applied_value ?? o.new_value ?? o.value)}</strong>
                  </span>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      {scan && (
        <Section title="Scan results" meta={`${scanDone} of ${scanTotal} points`}>
          <ScanTable scan={scan} unit={unit} />
        </Section>
      )}

      {species.length > 0 && (
        <Section
          title="Species"
          meta={
            <span className={styles.sectionMeta}>
              {unit ? `in ${unit}` : null}
              {stability && (
                <Pill tone={stability.tone} title={stability.text}>
                  steady state {stability.label}
                </Pill>
              )}
            </span>
          }
        >
          <div className={ui.tableScroll}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Species</th>
                  <th scope="col" className={ui.num}>
                    Start
                  </th>
                  <th scope="col" className={ui.num}>
                    End (t = {data?.duration}
                    {timeUnit})
                  </th>
                  {steady && (
                    <th scope="col" className={ui.num}>
                      Steady state
                    </th>
                  )}
                  <th scope="col" className={ui.num}>
                    Change
                  </th>
                </tr>
              </thead>
              <tbody>
                {species.map((sp) => (
                  <tr key={sp}>
                    <td>
                      <span className={styles.species} title={sp}>
                        {sp}
                      </span>
                    </td>
                    <td className={`${ui.num} ${styles.muted}`}>{fmt(initials[sp])}</td>
                    <td className={ui.num}>{fmt(finals[sp])}</td>
                    {steady && <td className={ui.num}>{fmt(steady[sp])}</td>}
                    <td className={`${ui.num} ${styles.muted}`}>{changeText(initials[sp], finals[sp])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      <p className={ui.note}>{note}</p>

      <Disclosure summary="About this run">
        {runDetails && (
          <p className={ui.note} style={{ marginTop: 0 }}>
            {runDetails}.
          </p>
        )}
        {convergence && <p className={ui.note}>{convergence.text}</p>}
        {stability && <p className={ui.note}>{stability.text}</p>}
        {steady && (
          <p className={ui.note}>
            &ldquo;Reached steady state&rdquo; is a display check, not a COPASI result: every species must end within{" "}
            {STEADY_RTOL * 100}% of its steady-state value plus {STEADY_ATOL_REL.toExponential(0)} of the largest value in the
            run (so species whose steady state is ~0 are judged on an absolute scale). Species without a steady-state value
            make the check inconclusive.
          </p>
        )}
      </Disclosure>
    </Card>
  );
}
