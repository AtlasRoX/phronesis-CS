"""
Pure numpy/pandas checks on simulated trajectories (no COPASI import), shared by the Vivarix engine, the
calibration module and tests:

- `endpoint_dynamics`: classify the end of a trajectory (ENDPOINT_STABLE / SLOWLY_VARYING / TRANSIENT_DYNAMICS /
  OSCILLATORY) from its final samples, unit-free.
- `check_trajectory_complete`: COPASI (through basico) does not raise when the integrator stops early (finite-time
  blow-up, max internal steps exhausted, event failure); it logs an error and returns the rows recorded so far. A
  trajectory is accepted only when every requested output time is present and finite (SCI-002).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

# A trajectory end point must lie within this relative distance of the requested end time.
TIME_MATCH_RTOL = 1e-9


class IncompleteIntegrationError(RuntimeError):
    """The integrator stopped before the requested end time (or returned fewer output points than requested)."""

    def __init__(
        self,
        message: str,
        *,
        reached_time: Optional[float],
        requested_time: float,
        rows: int,
        expected_rows: Optional[int],
        solver_messages: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.reached_time = reached_time
        self.requested_time = requested_time
        self.rows = rows
        self.expected_rows = expected_rows
        self.solver_messages = list(solver_messages)

    def details(self) -> Dict[str, Any]:
        return {
            "reason_code": "INTEGRATION_INCOMPLETE",
            "reached_time": self.reached_time,
            "requested_time": self.requested_time,
            "rows": self.rows,
            "expected_rows": self.expected_rows,
            "solver_messages": [m[:300] for m in self.solver_messages[:3]],
        }


def _times_match(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= TIME_MATCH_RTOL * max(1.0, abs(float(a)), abs(float(b)))


def check_trajectory_complete(
    tc: Optional[pd.DataFrame],
    *,
    end_time: Optional[float] = None,
    intervals: Optional[int] = None,
    output_times: Optional[Sequence[float]] = None,
    solver_messages: Sequence[str] = (),
    time_unit: str = "",
) -> None:
    """
    Raise IncompleteIntegrationError unless `tc` is a complete, finite trajectory:
    - grid mode (end_time, intervals): exactly intervals + 1 rows and a last time equal to end_time;
    - values mode (output_times): every requested output time is present.
    Non-finite values raise RuntimeError (divergence).
    """
    unit = f" {time_unit}" if time_unit else ""
    msgs = [str(m) for m in solver_messages if m]
    if tc is None or tc.empty:
        requested = float(end_time if end_time is not None else (max(output_times) if output_times else 0.0))
        raise IncompleteIntegrationError(
            "the integrator returned no trajectory" + (f" ({msgs[0][:200]})" if msgs else ""),
            reached_time=None, requested_time=requested, rows=0,
            expected_rows=(int(intervals) + 1) if intervals is not None else (len(output_times) if output_times else None),
            solver_messages=msgs,
        )
    times = np.asarray(tc.index, dtype=float)
    reached = float(times[-1]) if len(times) else None
    if output_times is not None:
        wanted = sorted({float(t) for t in output_times})
        missing = [t for t in wanted if not any(_times_match(t, x) for x in times)]
        if missing:
            raise IncompleteIntegrationError(
                f"the integrator stopped at t = {reached:g}{unit}, before the requested output time "
                f"{missing[0]:g}{unit} ({len(missing)} of {len(wanted)} output times missing)"
                + (f"; solver: {msgs[0][:200]}" if msgs else ""),
                reached_time=reached, requested_time=wanted[-1], rows=len(times), expected_rows=len(wanted),
                solver_messages=msgs,
            )
    if end_time is not None:
        expected_rows = int(intervals) + 1 if intervals is not None else None
        if reached is None or not _times_match(reached, float(end_time)) or (expected_rows is not None and len(times) != expected_rows):
            raise IncompleteIntegrationError(
                f"the integrator stopped at t = {reached:g}{unit} of the requested {float(end_time):g}{unit} "
                f"({len(times)} of {expected_rows if expected_rows is not None else '?'} output points)"
                + (f"; solver: {msgs[0][:200]}" if msgs else ""),
                reached_time=reached, requested_time=float(end_time), rows=len(times), expected_rows=expected_rows,
                solver_messages=msgs,
            )
    if not np.all(np.isfinite(tc.to_numpy(dtype=float))):
        raise RuntimeError("ODE integration diverged: non-finite (NaN/Inf) values in the trajectory")


def solver_error_text(messages: Sequence[str]) -> List[str]:
    """The informative part of basico/COPASI error log lines (the EXCEPTION/ERROR blocks, without the report warning)."""
    out: List[str] = []
    for raw in messages:
        text = str(raw)
        parts = [p for p in text.replace("\r", "").split(">") if p.strip()]
        picked = [p.split("<", 1)[-1].strip() if "<" in p else p.strip() for p in parts if ("EXCEPTION" in p or "ERROR" in p)]
        body = " ".join(" ".join(p.split()) for p in picked) if picked else " ".join(text.split())
        if body:
            out.append(body[:400])
    return out


def endpoint_dynamics(tc: pd.DataFrame, species: List[str], seconds_per_unit: float) -> Dict[str, Any]:
    """
    Classify the end of a trajectory from the final 10% of its samples, unit-free:
    relative change = (max - min) / max(|x_end|, floor) per species over that window, and the largest
    relative rate of change per second. Oscillation is several turning points in the second half.
    """
    out: Dict[str, Any] = {"convergence_status": "UNKNOWN", "endpoint_relative_change": None, "relative_rate_per_s": None}
    if len(tc) < 3 or not species:
        return out
    values = tc[species].to_numpy(dtype=float)
    times = np.asarray(tc.index, dtype=float) * seconds_per_unit
    scale = np.nanmax(np.abs(values), axis=0)
    floor = np.maximum(scale * 1e-9, 1e-12)
    n_win = max(2, int(math.ceil(len(tc) * 0.1)))
    window = values[-n_win:]
    end_mag = np.maximum(np.abs(values[-1]), floor)
    rel_change = float(np.max((window.max(axis=0) - window.min(axis=0)) / end_mag))
    dt = np.diff(times[-n_win:])
    dt[dt == 0] = np.nan
    rates = np.abs(np.diff(window, axis=0)) / dt[:, None] / end_mag
    rel_rate = float(np.nanmax(rates)) if np.isfinite(rates).any() else 0.0

    half = values[len(values) // 2:]
    turns = 0
    if len(half) >= 5:
        most_variable = int(np.argmax(half.std(axis=0)))
        d = np.diff(half[:, most_variable])
        d = d[np.abs(d) > 1e-9 * max(scale[most_variable], 1e-12)]
        turns = int(np.sum(np.sign(d[1:]) != np.sign(d[:-1]))) if len(d) > 1 else 0

    if turns >= 4 and rel_change >= 1e-3:
        status = "OSCILLATORY"
    elif rel_change < 1e-3:
        status = "ENDPOINT_STABLE"
    elif rel_change < 1e-2:
        status = "SLOWLY_VARYING"
    else:
        status = "TRANSIENT_DYNAMICS"
    out.update(
        convergence_status=status,
        endpoint_relative_change=round(rel_change, 8),
        relative_rate_per_s=float(f"{rel_rate:.6g}"),
        turning_points_second_half=turns,
    )
    return out


def trajectory_scale(tc: pd.DataFrame, species: List[str]) -> Dict[str, float]:
    """max |value| of every species over the whole trajectory (the species' own concentration scale)."""
    if tc is None or tc.empty or not species:
        return {}
    values = np.abs(tc[species].to_numpy(dtype=float))
    peaks = np.nanmax(values, axis=0)
    return {str(s): float(f"{float(p):.6g}") for s, p in zip(species, peaks) if math.isfinite(float(p))}


__all__ = [
    "IncompleteIntegrationError",
    "check_trajectory_complete",
    "endpoint_dynamics",
    "solver_error_text",
    "trajectory_scale",
]
