"""
SBML model calibration: parameter estimation against an attached time-course table, with profile-likelihood
identifiability (feature 11).

Method
- Simulation: COPASI (Hoops et al., Bioinformatics 2006, doi:10.1093/bioinformatics/btl485) through basico
  (Bergmann et al., JOSS 2023, doi:10.21105/joss.05553): the model is loaded with the Vivarix engine's own loader
  (BioModels accession or an uploaded SBML file in the upload store), parameters are resolved and applied exactly
  as the engine applies parameter overrides, and `run_time_course(values=<data time points>)` integrates from the
  model's initial state for every evaluation. COPASI keeps process-global state, so the whole calibration runs in
  the calling worker thread while holding the engine's COPASI lock.
- Objective: chi2 = sum over observed points of ((obs - sim) / sigma)^2. sigma comes from a `sd_<species>` column
  when the table has one, otherwise it is 10% of the largest |observed| value of that species (a documented
  default for data without error bars, never an estimate of the real measurement error). RMSE is the unweighted
  root-mean-square residual over the same points.
- Optimiser (default backend "least_squares"): scipy.optimize.least_squares (trust-region reflective) over
  log10(parameters) inside log10 bounds (default: the model value x 1e-3 .. x 1e3), with bounded function
  evaluations, optional deterministic multistart (pyPESTO, Schaelte et al., Bioinformatics 2023,
  doi:10.1093/bioinformatics/btad711) and a wall-clock timeout that returns the best point found so far.
  With PHRONESIS_CALIBRATION_BACKEND=copasi (or backend="copasi") the estimate comes from COPASI's own
  parameter-estimation task (basico.task_parameterestimation: add_experiment, set_fit_parameters,
  run_parameter_estimation; Levenberg-Marquardt); chi2 and the profiles are then recomputed with the objective above.
- Identifiability: the profile likelihood of Raue et al. (Bioinformatics 2009, doi:10.1093/bioinformatics/btp358):
  PL(theta_i) = min over the other parameters of chi2, on a grid around the estimate (at most 3 parameters,
  7 points each). The pointwise 95% confidence interval is where PL - chi2_min < 3.84 (chi2 quantile, df = 1).
  Both sides crossing the threshold -> identifiable; a flat profile (max delta-chi2 < 0.1 over the grid) ->
  structurally_non_identifiable; otherwise (one or both sides open) -> practically_non_identifiable.
  Hass et al. (Bioinformatics 2019, doi:10.1093/bioinformatics/btz020) benchmark problems are the reference
  for regression tests of this pipeline.

`calibrate_parameters` takes a `simulate` hook (theta -> {species: values at the data time points}) so the
optimiser and the profile logic are testable without COPASI. Every value returned is JSON-safe.
"""

from __future__ import annotations

import logging
import math
import os

import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

CHI2_THRESHOLD_95 = 3.84  # chi2(0.95, df=1): pointwise 95% profile-likelihood interval (Raue et al. 2009)
FLAT_DELTA_CHI2 = 0.1  # a profile whose delta-chi2 never exceeds this over its grid is called flat
DEFAULT_SIGMA_FRACTION = 0.10
MAX_PARAMETERS = 5
MAX_PROFILE_PARAMS = 3
MAX_PROFILE_POINTS = 7
MAX_NFEV = 400
MAX_STARTS = 5
MAX_FIT_POINTS = 200
MAX_DATA_ROWS = 5000
MAX_SPECIES_COLUMNS = 20
DEFAULT_BOUND_FACTOR = 1e3
_PROFILE_INNER_NFEV = 60
_FD_STEP = 1e-4  # log10 units (0.023% change in theta)
_FAILED_RESIDUAL = 1e3

Simulator = Callable[[Dict[str, float]], Mapping[str, Sequence[float]]]


class CalibrationError(ValueError):
    """A calibration request that cannot run as stated (bad data, unknown parameter, bad bounds)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Deadline(RuntimeError):
    pass


def calibration_backend(requested: Optional[str] = None) -> str:
    value = (requested or os.getenv("PHRONESIS_CALIBRATION_BACKEND", "") or "least_squares").strip().lower()
    return "copasi" if value == "copasi" else "least_squares"


# -----------------------------------------------------------------------------
# Data
# -----------------------------------------------------------------------------

@dataclass
class TimecourseData:
    times: np.ndarray  # one entry per row (replicate rows may repeat a time)
    observed: Dict[str, np.ndarray]  # species -> values per row (NaN = not measured)
    sigma: Dict[str, np.ndarray]  # species -> sigma per row
    sigma_rule: Dict[str, str]  # species -> "sd column" | "10% of max |observed|"
    notes: List[str] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return int(sum(int(np.sum(np.isfinite(v))) for v in self.observed.values()))


def _time_column(columns: Sequence[str]) -> Optional[str]:
    for c in columns:
        if str(c).strip().lower() in ("time", "t", "time_s", "time (s)"):
            return str(c)
    return None


def load_timecourse(source: Union[str, pd.DataFrame]) -> TimecourseData:
    """A time-course table: a 'time' column, one column per observed species, optional 'sd_<species>' columns."""
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        sep = "\t" if str(source).lower().endswith(".tsv") else ","
        try:
            df = pd.read_csv(source, sep=sep, nrows=MAX_DATA_ROWS + 1)
        except Exception as exc:  # noqa: BLE001
            raise CalibrationError("DATA_UNREADABLE", f"The data table could not be parsed ({type(exc).__name__}).") from exc
    if len(df) > MAX_DATA_ROWS:
        raise CalibrationError("DATA_TOO_LARGE", f"Calibration data is limited to {MAX_DATA_ROWS} rows.")
    df.columns = [str(c).strip() for c in df.columns]
    tcol = _time_column(list(df.columns))
    if tcol is None:
        raise CalibrationError("DATA_NO_TIME", "The data table needs a 'time' column.")
    times = pd.to_numeric(df[tcol], errors="coerce").to_numpy(dtype=float)
    if not np.all(np.isfinite(times)) or np.any(times < 0):
        raise CalibrationError("DATA_BAD_TIME", "Every 'time' value must be a finite, non-negative number.")
    sd_cols = {c[3:]: c for c in df.columns if c.lower().startswith("sd_")}
    species_cols = [c for c in df.columns if c != tcol and not c.lower().startswith("sd_")]
    observed: Dict[str, np.ndarray] = {}
    sigma: Dict[str, np.ndarray] = {}
    rule: Dict[str, str] = {}
    notes: List[str] = []
    for col in species_cols:
        values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
        if not np.any(np.isfinite(values)):
            notes.append(f"column '{col}' has no numeric values and was ignored")
            continue
        observed[col] = values
        sd = sd_cols.get(col)
        if sd is not None:
            s = pd.to_numeric(df[sd], errors="coerce").to_numpy(dtype=float)
            good = np.isfinite(s) & (s > 0)
            fallback = max(DEFAULT_SIGMA_FRACTION * float(np.nanmax(np.abs(values))), 1e-12)
            sigma[col] = np.where(good, s, fallback)
            rule[col] = f"{sd} column" + ("" if bool(np.all(good)) else " (10% of max |observed| where missing)")
        else:
            sigma[col] = np.full(len(values), max(DEFAULT_SIGMA_FRACTION * float(np.nanmax(np.abs(values))), 1e-12))
            rule[col] = "10% of max |observed|"
    if not observed:
        raise CalibrationError("DATA_NO_SPECIES", "The data table has no numeric species columns besides 'time'.")
    if len(observed) > MAX_SPECIES_COLUMNS:
        raise CalibrationError("DATA_TOO_LARGE", f"Calibration data is limited to {MAX_SPECIES_COLUMNS} species columns.")
    return TimecourseData(times=times, observed=observed, sigma=sigma, sigma_rule=rule, notes=notes)


# -----------------------------------------------------------------------------
# Core: least squares over log10 parameters + profile likelihood
# -----------------------------------------------------------------------------

def _round(x: Optional[float], digits: int = 6) -> Optional[float]:
    if x is None or not math.isfinite(float(x)):
        return None
    return float(f"{float(x):.{digits}g}")


def _subsample(n: int, cap: int = MAX_FIT_POINTS) -> List[int]:
    if n <= cap:
        return list(range(n))
    return sorted({int(round(i)) for i in np.linspace(0, n - 1, cap)})


class _Objective:
    def __init__(self, data: TimecourseData, simulate: Simulator, names: List[str], deadline: float) -> None:
        self.data = data
        self.simulate = simulate
        self.names = names
        self.deadline = deadline
        self.species = list(data.observed)
        self.obs = np.concatenate([data.observed[s] for s in self.species])
        self.sig = np.concatenate([data.sigma[s] for s in self.species])
        self.mask = np.isfinite(self.obs)
        self.n = int(self.mask.sum())
        self.evaluations = 0
        self.failures = 0
        self.track = False
        self.best_x: Optional[np.ndarray] = None
        self.best_chi2 = math.inf

    def theta(self, x: np.ndarray) -> Dict[str, float]:
        return {n: float(10.0 ** v) for n, v in zip(self.names, x)}

    def simulated(self, x: np.ndarray) -> Optional[Dict[str, np.ndarray]]:
        try:
            out = self.simulate(self.theta(x))
            sim = {s: np.asarray(out[s], dtype=float) for s in self.species}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Calibration simulation failed: %s", exc)
            return None
        if any(len(v) != len(self.data.times) or not np.all(np.isfinite(v)) for v in sim.values()):
            return None
        return sim

    def residuals(self, x: np.ndarray) -> np.ndarray:
        if time.perf_counter() > self.deadline:
            raise _Deadline("calibration time budget used")
        self.evaluations += 1
        sim = self.simulated(x)
        if sim is None:
            self.failures += 1
            return np.full(self.n, _FAILED_RESIDUAL)
        vec = np.concatenate([sim[s] for s in self.species])
        r = ((self.obs - vec) / self.sig)[self.mask]
        if self.track:
            c = float(r @ r)
            if c < self.best_chi2:
                self.best_chi2, self.best_x = c, np.array(x, dtype=float)
        return r

    def chi2(self, x: np.ndarray) -> float:
        r = self.residuals(x)
        return float(r @ r)


def _fit(
    obj: _Objective,
    x_start: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    free: List[int],
    max_nfev: int,
) -> Tuple[np.ndarray, float, int, str]:
    """Minimise chi2 over the free indices (the others stay at x_start); returns (x, chi2, nfev, message)."""
    from scipy.optimize import least_squares

    x = np.array(x_start, dtype=float)
    if not free:
        return x, obj.chi2(x), 1, "no free parameters"
    width = hi[free] - lo[free]
    z0 = np.clip(x[free], lo[free] + 1e-9 * width, hi[free] - 1e-9 * width)

    last: Dict[str, Any] = {}

    def f(z: np.ndarray) -> np.ndarray:
        full = x.copy()
        full[free] = z
        r = obj.residuals(full)
        last["z"], last["r"] = np.array(z, dtype=float), r
        return r

    zlo, zhi = lo[free], hi[free]

    def jac(z: np.ndarray) -> np.ndarray:
        # Forward differences with an absolute step in log10 units: a relative step vanishes at log10(theta) = 0
        # (theta = 1) and stalls the optimiser there.
        r0 = last["r"] if "z" in last and np.array_equal(last["z"], z) else f(z)
        out = np.empty((len(r0), len(z)))
        for j in range(len(z)):
            h = _FD_STEP if z[j] + _FD_STEP <= zhi[j] else -_FD_STEP
            zj = np.array(z, dtype=float)
            zj[j] += h
            full = x.copy()
            full[free] = zj
            out[:, j] = (obj.residuals(full) - r0) / h
        return out

    res = least_squares(
        f, z0, jac=jac, bounds=(zlo, zhi), method="trf", x_scale=1.0,
        max_nfev=max(2, int(max_nfev)), xtol=1e-10, ftol=1e-10, gtol=1e-10,
    )
    x[free] = res.x
    return x, float(2.0 * res.cost), int(res.nfev), str(res.message)


def _crossing(x_in: float, d_in: float, x_out: float, d_out: float, thr: float) -> float:
    """Where delta-chi2 crosses thr between two grid points; sqrt(delta) is close to linear near an optimum."""
    a, b = math.sqrt(max(d_in, 0.0)), math.sqrt(max(d_out, 0.0))
    t = (math.sqrt(thr) - a) / (b - a) if b > a else 1.0
    return x_in + min(1.0, max(0.0, t)) * (x_out - x_in)


def _profile_one(
    obj: _Objective,
    i: int,
    x_hat: np.ndarray,
    chi2_hat: float,
    lo: np.ndarray,
    hi: np.ndarray,
    points: int,
) -> Dict[str, Any]:
    """Profile likelihood of parameter i around x_hat (log10 grid), re-optimising the others at every point."""
    n = len(x_hat)
    others = [j for j in range(n) if j != i]
    half = max(1, points // 2)
    cache: Dict[float, Tuple[float, np.ndarray]] = {}

    def evaluate(v: float, warm: np.ndarray) -> Tuple[float, np.ndarray]:
        key = round(v, 10)
        if key in cache:
            return cache[key]
        start = warm.copy()
        start[i] = v
        xs, c, _nfev, _msg = _fit(obj, start, lo, hi, others, _PROFILE_INNER_NFEV)
        cache[key] = (c, xs)
        return c, xs

    span = 1.0  # decades on each side of the estimate
    for attempt in range(3):
        step = span / half
        probes = [
            evaluate(float(np.clip(x_hat[i] + s * step, lo[i], hi[i])), x_hat)[0] - chi2_hat
            for s in (-1, 1)
            if lo[i] < x_hat[i] + s * step < hi[i]
        ]
        if attempt < 2 and probes and min(probes) > 25 * CHI2_THRESHOLD_95:
            span /= 4.0  # the profile is steep: zoom in so the interval is resolved by several points
            continue
        break
    step = span / half

    grid: List[Tuple[int, float, float]] = [(0, float(x_hat[i]), chi2_hat)]
    for side in (1, -1):
        warm = x_hat.copy()
        for k in range(1, half + 1):
            v = float(x_hat[i] + side * k * step)
            if not lo[i] <= v <= hi[i]:
                break
            c, warm = evaluate(v, warm)
            grid.append((side * k, v, c))
    grid.sort(key=lambda g: g[0])
    chi2_min = min(chi2_hat, *(g[2] for g in grid))
    deltas = [(k, v, c - chi2_min) for k, v, c in grid]
    center = next(idx for idx, g in enumerate(deltas) if g[0] == 0)

    def bound(direction: int) -> Optional[float]:
        prev = deltas[center]
        idx = center + direction
        while 0 <= idx < len(deltas):
            cur = deltas[idx]
            if cur[2] > CHI2_THRESHOLD_95:
                return _crossing(prev[1], prev[2], cur[1], cur[2], CHI2_THRESHOLD_95)
            prev = cur
            idx += direction
        return None

    low, high = bound(-1), bound(1)
    max_delta = max(d for _k, _v, d in deltas)
    if low is not None and high is not None:
        cls = "identifiable"
    elif max_delta < FLAT_DELTA_CHI2:
        cls = "structurally_non_identifiable"
    else:
        cls = "practically_non_identifiable"
    return {
        "profile": [{"value": _round(10.0 ** v), "chi2": _round(c)} for _k, v, c in grid],
        "ci_low": _round(10.0 ** low) if low is not None else None,
        "ci_high": _round(10.0 ** high) if high is not None else None,
        "identifiability": cls,
        "max_delta_chi2": _round(max_delta),
        "better_optimum_found": bool(chi2_min < chi2_hat - 1e-6 * max(1.0, chi2_hat)),
        "span_decades": _round(span),
    }


def _check_bounds(names: List[str], initial: Dict[str, float], bounds: Optional[Mapping[str, Sequence[float]]]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[str]]:
    x0, lo, hi = [], [], []
    notes: List[str] = []
    for n in names:
        start = initial.get(n)
        if start is None or not math.isfinite(start) or start <= 0:
            notes.append(f"{n}: model value {start!r} is not positive; started at 1 with bounds 1e-6..1e6")
            start, b = 1.0, (1e-6, 1e6)
        else:
            b = (start / DEFAULT_BOUND_FACTOR, start * DEFAULT_BOUND_FACTOR)
        given = (bounds or {}).get(n)
        if given is not None:
            try:
                b = (float(given[0]), float(given[1]))
            except (TypeError, ValueError, IndexError) as exc:
                raise CalibrationError("BOUNDS_INVALID", f"Bounds for '{n}' must be [lower, upper].") from exc
            if not (math.isfinite(b[0]) and math.isfinite(b[1]) and 0 < b[0] < b[1]):
                raise CalibrationError("BOUNDS_INVALID", f"Bounds for '{n}' must satisfy 0 < lower < upper (log-scale fit).")
        x0.append(math.log10(min(max(start, b[0]), b[1])))
        lo.append(math.log10(b[0]))
        hi.append(math.log10(b[1]))
    return np.array(x0), np.array(lo), np.array(hi), notes


def _calibrate_core(
    data: TimecourseData,
    simulate: Simulator,
    names: List[str],
    initial: Dict[str, float],
    *,
    bounds: Optional[Mapping[str, Sequence[float]]],
    profile: bool,
    profile_params: Optional[Sequence[str]],
    profile_points: int,
    max_nfev: int,
    n_starts: int,
    deadline: float,
    estimate: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    x0, lo, hi, notes = _check_bounds(names, initial, bounds)
    obj = _Objective(data, simulate, names, deadline)
    if obj.n < len(names) + 1:
        raise CalibrationError("DATA_TOO_SMALL", f"{obj.n} observed points cannot constrain {len(names)} parameter(s).")
    status = "ok"
    message = ""
    nfev_total = 0
    timed_out = False
    free = list(range(len(names)))
    try:
        initial_chi2 = obj.chi2(x0)
    except _Deadline:
        raise CalibrationError("TIMEOUT", "The calibration time budget was used before the first simulation finished.")

    obj.track = True
    best: Tuple[np.ndarray, float] = (x0, initial_chi2)
    try:
        if estimate is not None:
            xe = np.array([math.log10(max(estimate[n], 1e-300)) for n in names])
            best = (xe, obj.chi2(xe))
            message = "estimate from COPASI parameter estimation"
        else:
            rng = np.random.default_rng(20090801)
            starts = [x0] + [rng.uniform(lo, hi) for _ in range(max(0, min(MAX_STARTS, n_starts) - 1))]
            for s in starts:
                xs, c, nfev, msg = _fit(obj, s, lo, hi, free, max_nfev)
                nfev_total += nfev
                if c < best[1]:
                    best, message = (xs, c), msg
            if "maximum number of function evaluations" in message.lower():
                status = "partial"
                message = f"stopped at the evaluation limit ({max_nfev} per start): {message}"
    except _Deadline:
        status, timed_out = "partial", True
        message = "stopped by the calibration timeout; best point found so far is reported"
        if obj.best_x is not None and obj.best_chi2 < best[1]:
            best = (obj.best_x, obj.best_chi2)
    obj.track = False
    x_hat, chi2_hat = best

    profiles: Dict[str, Any] = {}
    if profile and not timed_out:
        wanted = [p for p in (profile_params or names[:MAX_PROFILE_PARAMS]) if p in names][:MAX_PROFILE_PARAMS]
        points = max(3, min(MAX_PROFILE_POINTS, int(profile_points)))
        for p in wanted:
            try:
                profiles[p] = _profile_one(obj, names.index(p), x_hat, chi2_hat, lo, hi, points)
            except _Deadline:
                notes.append(f"profile of {p} stopped by the calibration timeout")
                status = "partial"
                break

    sim = obj.simulated(x_hat) or {}
    fit: Dict[str, Any] = {}
    sq: List[float] = []
    for s in obj.species:
        o = data.observed[s]
        v = sim.get(s)
        keep = [i for i in _subsample(len(o)) if math.isfinite(o[i])]
        fit[s] = {
            "time": [_round(data.times[i]) for i in keep],
            "observed": [_round(o[i]) for i in keep],
            "simulated": [_round(v[i]) for i in keep] if v is not None else [],
        }
        if v is not None:
            m = np.isfinite(o)
            sq.extend(((o[m] - v[m]) ** 2).tolist())
    rmse = math.sqrt(sum(sq) / len(sq)) if sq else None

    parameters: List[Dict[str, Any]] = []
    for idx, n in enumerate(names):
        prof = profiles.get(n)
        est = 10.0 ** x_hat[idx]
        at_bound = bool(abs(x_hat[idx] - lo[idx]) < 1e-3 or abs(x_hat[idx] - hi[idx]) < 1e-3)
        parameters.append(
            {
                "name": n,
                "initial": _round(initial.get(n)),
                "estimate": _round(est),
                "ci_low": prof["ci_low"] if prof else None,
                "ci_high": prof["ci_high"] if prof else None,
                "identifiability": prof["identifiability"] if prof else "not_assessed",
                "bounds": [_round(10.0 ** lo[idx]), _round(10.0 ** hi[idx])],
                "at_bound": at_bound,
            }
        )
    if obj.failures:
        notes.append(f"{obj.failures} of {obj.evaluations} simulations failed and were penalised")
    return {
        "status": status,
        "objective": {
            "chi2": _round(chi2_hat),
            "initial_chi2": _round(initial_chi2),
            "rmse": _round(rmse),
            "n_points": obj.n,
            "n_parameters": len(names),
            "sigma_rule": dict(data.sigma_rule),
        },
        "parameters": parameters,
        "profiles": {k: v["profile"] for k, v in profiles.items()},
        "profile_details": {
            k: {kk: vv for kk, vv in v.items() if kk != "profile"} for k, v in profiles.items()
        },
        "fit": fit,
        "optimizer": {
            "method": "scipy.optimize.least_squares (trf, log10 parameters)",
            "message": message[:300],
            "nfev": nfev_total,
            "evaluations": obj.evaluations,
            "starts": max(1, min(MAX_STARTS, n_starts)) if estimate is None else 0,
        },
        "threshold": {"delta_chi2": CHI2_THRESHOLD_95, "confidence": 0.95, "df": 1, "flat_delta_chi2": FLAT_DELTA_CHI2},
        "notes": notes + list(data.notes),
    }


# -----------------------------------------------------------------------------
# COPASI / basico
# -----------------------------------------------------------------------------

def _match_columns(data: TimecourseData, species_names: List[str]) -> Tuple[TimecourseData, List[str]]:
    """Keep the data columns that name a model species (exact, '[X]' or case-insensitive unique match)."""
    by_lower: Dict[str, List[str]] = {}
    for s in species_names:
        by_lower.setdefault(s.lower(), []).append(s)
    observed, sigma, rule = {}, {}, {}
    notes = list(data.notes)
    for col, values in data.observed.items():
        name = col[1:-1] if col.startswith("[") and col.endswith("]") else col
        target = name if name in species_names else (by_lower.get(name.lower(), [None])[0] if len(by_lower.get(name.lower(), [])) == 1 else None)
        if target is None:
            notes.append(f"column '{col}' matches no model species and was ignored")
            continue
        observed[target], sigma[target], rule[target] = values, data.sigma[col], data.sigma_rule[col]
    if not observed:
        raise CalibrationError(
            "DATA_NO_MODEL_SPECIES",
            "No data column names a model species; columns must be species names (e.g. 'Erk2-PP').",
        )
    return TimecourseData(times=data.times, observed=observed, sigma=sigma, sigma_rule=rule, notes=notes), notes


def _copasi_fit_name(resolved: Dict[str, Any]) -> str:
    if resolved["kind"] == "species":
        return f"[{resolved['resolved']}]_0"
    if resolved["kind"] == "global_parameter":
        return f"Values[{resolved['resolved']}]"
    return str(resolved["resolved"])


def _copasi_estimate(
    basico: Any,
    model: Any,
    data: TimecourseData,
    names: List[str],
    resolved: Dict[str, Dict[str, Any]],
    initial: Dict[str, float],
    bounds: Optional[Mapping[str, Sequence[float]]],
    read_value: Callable[[Any, Dict[str, Any]], Optional[float]],
) -> Dict[str, float]:
    """COPASI's own parameter estimation (Levenberg-Marquardt) for the same data; returns the fitted values."""
    _x0, lo, hi, _notes = _check_bounds(names, initial, bounds)
    frame = pd.DataFrame({"Time": data.times, **{f"[{s}]": v for s, v in data.observed.items()}})
    with tempfile.TemporaryDirectory(prefix="phronesis_pe_") as tmp:
        basico.add_experiment("phronesis_data", frame, data_dir=tmp, model=model)
        try:
            basico.set_fit_parameters(
                [
                    {
                        "name": _copasi_fit_name(resolved[n]),
                        "lower": float(10.0 ** lo[i]),
                        "upper": float(10.0 ** hi[i]),
                        "start": float(min(max(initial.get(n) or 1.0, 10.0 ** lo[i]), 10.0 ** hi[i])),
                    }
                    for i, n in enumerate(names)
                ],
                model=model,
            )
            basico.run_parameter_estimation(method=basico.PE.LEVENBERG_MARQUARDT, update_model=True, model=model)
            out = {n: read_value(model, resolved[n]) for n in names}
        finally:
            try:
                basico.remove_experiments(model=model)
            except Exception:  # noqa: BLE001
                pass
    if any(v is None or not math.isfinite(v) or v <= 0 for v in out.values()):
        raise RuntimeError("COPASI parameter estimation returned no usable solution")
    return {k: float(v) for k, v in out.items()}  # type: ignore[arg-type]


def _with_basico(
    *,
    biomodel_id: Optional[str],
    sbml_path: Optional[str],
    model_label: Optional[str],
    data: TimecourseData,
    parameter_names: List[str],
    data_time_unit: Optional[str],
    run: Callable[[TimecourseData, Simulator, List[str], Dict[str, float], Optional[Callable[[], Dict[str, float]]]], Dict[str, Any]],
    backend: str,
    bounds: Optional[Mapping[str, Sequence[float]]],
) -> Dict[str, Any]:
    import basico  # type: ignore[import-untyped,import-not-found]  # pyright: ignore[reportMissingImports]

    from . import engine as eng

    clean_id = str(biomodel_id or "").strip()
    vivarix = eng.VivarixSimulationEngine()
    if not sbml_path:
        try:
            eng._validate_model_id(clean_id)
        except eng.SimulationValidationError as exc:
            raise CalibrationError(exc.code, str(exc)) from exc
        vivarix._prefetch_model(clean_id)  # outside the COPASI lock: a download never blocks other simulations

    model = None
    with eng._copasi_session():
        try:
            model, prov = vivarix._resolve_model(clean_id, sbml_path, model_label, None)
            units = eng._model_units(model)
            label = clean_id or "uploaded model"
            resolved: Dict[str, Dict[str, Any]] = {}
            for n in parameter_names:
                try:
                    resolved[n] = eng.VivarixSimulationEngine._resolve_parameter(model, n, label)
                except eng.SimulationValidationError as exc:
                    raise CalibrationError(exc.code, str(exc)) from exc
            targets = [r["resolved"] for r in resolved.values()]
            if len(set(targets)) != len(targets):
                raise CalibrationError("DUPLICATE_PARAMETER", "Two requested names resolve to the same model quantity.")
            species_names = [str(s) for s in basico.get_species(model=model).index]
            mapped, _ = _match_columns(data, species_names)
            times = np.array(mapped.times, dtype=float)
            if data_time_unit:
                try:
                    times = times * eng._seconds_per_time_unit(data_time_unit) / eng._seconds_per_time_unit(units["time"])
                except eng.SimulationValidationError as exc:
                    raise CalibrationError(exc.code, str(exc)) from exc
            sim_times = np.unique(times)
            row_index = np.searchsorted(sim_times, times)
            species = list(mapped.observed)
            baseline = {n: eng.VivarixSimulationEngine._read_value(model, r) for n, r in resolved.items()}

            def simulate(theta: Dict[str, float]) -> Dict[str, np.ndarray]:
                for n, v in theta.items():
                    eng.VivarixSimulationEngine._apply_value(model, resolved[n], float(v))
                tc = basico.run_time_course(values=[float(t) for t in sim_times], model=model, use_initial_values=True)
                if tc is None or tc.empty:
                    raise RuntimeError("empty trajectory")
                idx = np.asarray(tc.index, dtype=float)
                pick = np.array([int(np.argmin(np.abs(idx - t))) for t in sim_times])
                return {s: tc[s].to_numpy(dtype=float)[pick][row_index] for s in species}

            initial = {n: float(v) for n, v in baseline.items() if v is not None}

            copasi_estimate: Optional[Callable[[], Dict[str, float]]] = None
            if backend == "copasi":
                def copasi_estimate() -> Dict[str, float]:
                    return _copasi_estimate(
                        basico, model, mapped, parameter_names, resolved, initial, bounds,
                        eng.VivarixSimulationEngine._read_value,
                    )

            out = run(mapped, simulate, parameter_names, initial, copasi_estimate)
            for p in out.get("parameters", []):
                r = resolved.get(p["name"], {})
                p["resolved"], p["kind"] = r.get("resolved"), r.get("kind")
            out["model_id"] = clean_id or None
            out["model_name"] = prov.get("name") or None
            out["units"] = {"time": units["time"], "concentration": units["concentration"]}
            out["data_time_unit"] = data_time_unit or units["time"]
            out["model_source"] = "user_upload" if sbml_path else "biomodels"
            return out
        finally:
            eng._free_datamodel(model)


# -----------------------------------------------------------------------------
# Public entry points
# -----------------------------------------------------------------------------

def calibrate_parameters(
    *,
    parameter_names: Sequence[str],
    biomodel_id: Optional[str] = None,
    sbml_path: Optional[str] = None,
    data_path: Optional[str] = None,
    data: Optional[pd.DataFrame] = None,
    bounds: Optional[Mapping[str, Sequence[float]]] = None,
    method: str = "least_squares",
    profile: bool = True,
    profile_params: Optional[Sequence[str]] = None,
    profile_points: int = MAX_PROFILE_POINTS,
    max_nfev: int = 200,
    n_starts: int = 1,
    timeout_seconds: float = 240.0,
    backend: Optional[str] = None,
    simulate: Optional[Simulator] = None,
    initial_values: Optional[Mapping[str, float]] = None,
    model_label: Optional[str] = None,
    dataset_label: Optional[str] = None,
    data_time_unit: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fit up to 5 parameters of a kinetic model to a time-course table and profile up to 3 of them.

    Give the model as biomodel_id (BIOMD/MODEL accession) or sbml_path (an uploaded SBML file in the upload store),
    or pass `simulate` (theta -> {species: values at each data row}) with `initial_values` to run without COPASI.
    The data is a CSV/TSV at data_path or a DataFrame: a 'time' column in the model's time unit (or
    `data_time_unit`), species columns named like the model species, optional 'sd_<species>' columns.
    Returns the model_calibration artifact; status is "ok", "partial" (time or evaluation limit) or "error".
    """
    t0 = time.perf_counter()
    names = [str(n).strip() for n in parameter_names or [] if str(n).strip()]
    names = list(dict.fromkeys(names))
    chosen_backend = calibration_backend(backend) if simulate is None else "least_squares"
    base: Dict[str, Any] = {
        "model_id": (biomodel_id or None),
        "dataset": dataset_label or (os.path.basename(data_path) if data_path else None),
        "backend": chosen_backend,
        "method": method,
    }
    try:
        if not names:
            raise CalibrationError("NO_PARAMETERS", "Name at least one parameter to calibrate.")
        if len(names) > MAX_PARAMETERS:
            raise CalibrationError("TOO_MANY_PARAMETERS", f"At most {MAX_PARAMETERS} parameters can be calibrated at once.")
        if method != "least_squares":
            raise CalibrationError("METHOD_UNSUPPORTED", "Only method='least_squares' is supported.")
        if data is None and not data_path:
            raise CalibrationError("DATA_REQUIRED", "Calibration needs a time-course table.")
        table = load_timecourse(data if data is not None else str(data_path))
        deadline = t0 + max(5.0, float(timeout_seconds))
        settings = dict(
            bounds=bounds, profile=bool(profile), profile_params=list(profile_params) if profile_params else None,
            profile_points=int(profile_points), max_nfev=max(5, min(MAX_NFEV, int(max_nfev))),
            n_starts=max(1, min(MAX_STARTS, int(n_starts))), deadline=deadline,
        )

        def run(d: TimecourseData, sim: Simulator, ns: List[str], init: Dict[str, float], copasi: Optional[Callable[[], Dict[str, float]]]) -> Dict[str, Any]:
            estimate = None
            if copasi is not None:
                try:
                    estimate = copasi()
                except Exception as exc:  # noqa: BLE001
                    logger.info("COPASI parameter estimation failed, using least squares: %s", exc)
                    base["backend"] = "least_squares"
                    base["backend_note"] = f"COPASI parameter estimation failed ({str(exc)[:160]}); scipy least squares used"
            return _calibrate_core(d, sim, ns, init, estimate=estimate, **settings)  # type: ignore[arg-type]

        if simulate is not None:
            init = {n: float(v) for n, v in (initial_values or {}).items()}
            out = run(table, simulate, names, init, None)
        else:
            if not (biomodel_id or sbml_path):
                raise CalibrationError("MODEL_REQUIRED", "Calibration needs a model: a BioModels accession or an uploaded SBML file.")
            out = _with_basico(
                biomodel_id=biomodel_id, sbml_path=sbml_path, model_label=model_label, data=table,
                parameter_names=names, data_time_unit=data_time_unit, run=run, backend=chosen_backend, bounds=bounds,
            )
        result = {**base, **{k: v for k, v in out.items() if v is not None or k not in base}}
        result["seconds"] = round(time.perf_counter() - t0, 3)
        return result
    except CalibrationError as exc:
        return {**base, "status": "error", "error": str(exc), "error_code": exc.code, "seconds": round(time.perf_counter() - t0, 3)}
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "SolverBusyError":
            return {**base, "status": "error", "error": str(exc), "error_code": "SOLVER_BUSY", "seconds": round(time.perf_counter() - t0, 3)}
        logger.warning("Calibration failed: %s", exc, exc_info=True)
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "error_code": "CALIBRATION_FAILED",
                "seconds": round(time.perf_counter() - t0, 3)}


def list_model_parameters(
    biomodel_id: Optional[str] = None,
    sbml_path: Optional[str] = None,
    model_label: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Species (initial concentrations), global and reaction parameters and reaction schemes of a model."""
    import basico  # type: ignore[import-untyped,import-not-found]  # pyright: ignore[reportMissingImports]

    from . import engine as eng

    clean_id = str(biomodel_id or "").strip()
    vivarix = eng.VivarixSimulationEngine()
    if not sbml_path:
        try:
            eng._validate_model_id(clean_id)
        except eng.SimulationValidationError as exc:
            raise CalibrationError(exc.code, str(exc)) from exc
        vivarix._prefetch_model(clean_id)
    model = None
    with eng._copasi_session():
        try:
            model, prov = vivarix._resolve_model(clean_id, sbml_path, model_label, None)
            units = eng._model_units(model)
            sp = basico.get_species(model=model)
            gp = basico.get_parameters(model=model)
            rp = basico.get_reaction_parameters(model=model)
            rx = basico.get_reactions(model=model)
        finally:
            eng._free_datamodel(model)

    def num(v: Any) -> Optional[float]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return _round(f, 8) if math.isfinite(f) else None

    species = [{"name": str(n), "initial_concentration": num(r.get("initial_concentration"))} for n, r in (sp.iterrows() if sp is not None else [])]
    global_params = [{"name": str(n), "value": num(r.get("initial_value"))} for n, r in (gp.iterrows() if gp is not None else [])]
    reaction_params = []
    for n, r in (rp.iterrows() if rp is not None else []):
        short = str(n).rsplit(").", 1)[-1] if ")." in str(n) else str(n)
        reaction_params.append(
            {"name": str(n), "short_name": short, "reaction": str(r.get("reaction") or ""), "value": num(r.get("value")),
             "mapped_to": str(r.get("mapped_to") or "") or None}
        )
    reactions = [{"name": str(n), "scheme": str(r.get("scheme") or "")} for n, r in (rx.iterrows() if rx is not None else [])]
    cap = max(1, min(500, int(limit)))
    return {
        "model_id": clean_id or None,
        "model_name": prov.get("name") or None,
        "units": {"time": units["time"], "concentration": units["concentration"]},
        "species": species[:cap],
        "global_parameters": global_params[:cap],
        "reaction_parameters": reaction_params[:cap],
        "reactions": reactions[:cap],
        "truncated": any(len(x) > cap for x in (species, global_params, reaction_params, reactions)),
    }


__all__ = [
    "CHI2_THRESHOLD_95",
    "CalibrationError",
    "TimecourseData",
    "calibrate_parameters",
    "calibration_backend",
    "list_model_parameters",
    "load_timecourse",
]
