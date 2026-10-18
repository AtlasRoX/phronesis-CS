"""
Vivarix Computational Systems Biology & Kinetic ODE Engine.
Executes reproducible biomodel searches, SBML loading, COPASI/BASICO simulations,
steady-state computations, and parameter sensitivity scans with rich execution artifacts.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import functools
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterator, List, Optional, Tuple
import basico  # type: ignore[import-untyped,import-not-found]  # pyright: ignore[reportMissingImports]
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..tools.registry import ErrorType, ToolResult
from .dynamics import (
    IncompleteIntegrationError,
    check_trajectory_complete,
    endpoint_dynamics,
    solver_error_text,
    trajectory_scale,
)

logger = logging.getLogger(__name__)

# Curated reference models. Every entry was checked against the model's own SBML/COPASI annotation (model name,
# bqmodel:isDescribedBy PubMed id, bqbiol:hasTaxon) of the BioModels file; entries that could not be verified
# offline were removed (SCI-003). The catalog is only a hint: _resolve_model reads the identity from the loaded
# model and never lets a catalog entry override what the SBML says.
CURATED_MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "BIOMD0000000010": {
        "accession": "BIOMD0000000010",
        "name": "Kholodenko2000 - Ultrasensitivity and negative feedback bring oscillations in MAPK cascade",
        "organism": "Xenopus laevis (NCBI taxon 8355)",
        "publication": (
            "Kholodenko BN. Negative feedback and ultrasensitivity can bring about oscillations in the mitogen-activated "
            "protein kinase cascades. Eur J Biochem. 2000; 267(6):1583-1588. DOI: 10.1046/j.1432-1327.2000.01197.x"
        ),
        "pmid": "10712587",
        "taxon": "8355",
        "description": "Negative feedback and ultrasensitivity in MAPK phosphorylation cascade (Mos, Mek1, Erk2).",
    },
    "BIOMD0000000019": {
        "accession": "BIOMD0000000019",
        "name": "Schoeberl2002 - EGF MAPK",
        "organism": "Homo sapiens (NCBI taxon 9606)",
        "publication": (
            "Schoeberl B, Eichler-Jonsson C, Gilles ED, Muller G. Computational modeling of the dynamics of the MAP "
            "kinase cascade activated by surface and internalized EGF receptors. Nat Biotechnol. 2002; 20(4):370-375. "
            "DOI: 10.1038/nbt0402-370"
        ),
        "pmid": "11923843",
        "taxon": "9606",
        "description": "EGF-induced MAPK cascade with surface and internalized EGF receptors (EGFR, Shc, Grb2/SOS, Ras, Raf, MEK, ERK).",
    },
    "BIOMD0000000048": {
        "accession": "BIOMD0000000048",
        "name": "Kholodenko1999 - EGFR signaling",
        "organism": "Rattus norvegicus (NCBI taxon 10116)",
        "publication": (
            "Kholodenko BN, Demin OV, Moehren G, Hoek JB. Quantification of short term signaling by the epidermal "
            "growth factor receptor. J Biol Chem. 1999; 274(42):30169-30181. DOI: 10.1074/jbc.274.42.30169"
        ),
        "pmid": "10514507",
        "taxon": "10116",
        "description": "Short-term EGF receptor signalling in hepatocytes: EGFR binding, dimerization, phosphorylation and Shc/Grb2/SOS/PLCgamma recruitment.",
    },
}

# Integrator settings applied to every time course (never left to whatever the loaded COPASI file holds, SCI-020).
SOLVER_METHOD = "deterministic"  # COPASI "Deterministic (LSODA)"
SOLVER_RELATIVE_TOLERANCE = 1e-6
SOLVER_ABSOLUTE_TOLERANCE = 1e-12
SOLVER_MAX_INTERNAL_STEPS = 100000

# Integration horizon, in the model's OWN time unit (SCI-019): a default run integrates DEFAULT_HORIZON_MODEL_UNITS
# (60 s for a model in seconds, 60 min for one in minutes, 60 h for one in hours) and no run integrates more than
# MAX_HORIZON_MODEL_UNITS model time units, whatever unit the request was written in.
DEFAULT_HORIZON_MODEL_UNITS = 60.0
MAX_HORIZON_MODEL_UNITS = 5000.0

# How long a pinned BioModels download is trusted before its revision is re-checked (SCI-012).
_REVISION_TTL_S = float(os.getenv("PHRONESIS_BIOMODELS_REVISION_TTL_S", str(24 * 3600)))

# BioModels accessions (and uploaded-model dataset ids) accepted by every Vivarix entry point. Anything else is
# rejected before it reaches a cache file name or a URL.
MODEL_ID_PATTERN = r"^(?:BIOMD\d{10}|MODEL\d{10}|ds_[0-9a-f]{12})$"
_MODEL_ID_RE = re.compile(MODEL_ID_PATTERN)
_ACCESSION_RE = re.compile(r"^(?:BIOMD\d{10}|MODEL\d{10})$")

# Words that say nothing about which model is meant; they never select a curated model on their own.
_SEARCH_STOPWORDS = {
    "THE", "AND", "FOR", "WITH", "MODEL", "MODELS", "SIMULATE", "SIMULATION", "KINETIC", "KINETICS", "ODE",
    "PATHWAY", "SIGNALING", "SIGNALLING", "CASCADE", "DYNAMICS", "OF", "IN", "ON", "A", "AN", "HUMAN",
    "CELL", "CELLS", "PROTEIN", "ACTIVATION", "REGULATION", "RECEPTOR",
}

# Seconds per model time unit, as COPASI reports it.
_TIME_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 1e-3, "µs": 1e-6, "μs": 1e-6, "us": 1e-6,
    "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}

# COPASI keeps a process-global root container, so concurrent BASICO calls from worker threads
# (the tool registry runs native executors in a bounded worker pool) must be serialized.
_COPASI_LOCK = threading.RLock()
# How long a call waits for another simulation to release COPASI before giving up with a TIMEOUT.
_LOCK_WAIT_SECONDS = float(os.getenv("PHRONESIS_COPASI_LOCK_WAIT", "20"))


class SimulationValidationError(ValueError):
    """A request the model cannot honour as stated (unknown/ambiguous parameter, bad range, unsupported unit)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SolverBusyError(RuntimeError):
    """COPASI stayed locked by another simulation for longer than the lock-wait budget."""


@contextlib.contextmanager
def _copasi_session(timeout: Optional[float] = None) -> Generator[None, None, None]:
    """Hold the COPASI lock, giving up after `timeout` seconds instead of queueing forever behind a stuck run."""
    wait = _LOCK_WAIT_SECONDS if timeout is None else timeout
    if not _COPASI_LOCK.acquire(timeout=wait):
        raise SolverBusyError(f"COPASI is busy with another simulation (waited {wait:g}s)")
    try:
        yield
    finally:
        _COPASI_LOCK.release()


def _copasi_serialized(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _copasi_session():
            return fn(*args, **kwargs)
    return wrapper


def _free_datamodel(model: Any) -> None:
    """Release a COPASI datamodel; the root container otherwise keeps every loaded model alive."""
    if model is None:
        return
    try:
        basico.remove_datamodel(model)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not free COPASI datamodel: %s", e)


def _release_datamodel(model: Any) -> None:
    """Free a datamodel under the COPASI lock; if the lock stays busy the model is left for COPASI to reclaim."""
    if model is None:
        return
    if _COPASI_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        try:
            _free_datamodel(model)
        finally:
            _COPASI_LOCK.release()


def _model_units(copasi_model: Any) -> Dict[str, str]:
    """Model units; get_model_units() keys are 'time_unit', 'quantity_unit' and 'volume_unit'.

    'quantity' is the AMOUNT unit COPASI reports (e.g. 'nmol'); species values are concentrations, i.e. amount per
    volume, so 'concentration' combines both (e.g. 'nmol/ml', which is uM). Consumers must use 'concentration' for
    species values, never 'quantity' (TST-005).
    """
    units = basico.model_info.get_model_units(model=copasi_model) or {}
    time_unit = units.get("time_unit") or "s"
    quantity = units.get("quantity_unit") or "nM"
    volume = units.get("volume_unit") or ""
    if quantity.endswith("M") or not volume:
        concentration = quantity  # already a molar (concentration) unit
    else:
        concentration = f"{quantity}/{volume}"
    return {"time": time_unit, "quantity": quantity, "volume": volume, "concentration": concentration}


def _seconds_per_time_unit(time_unit: str) -> float:
    factor = _TIME_UNIT_SECONDS.get(str(time_unit).strip())
    if factor is None:
        factor = _TIME_UNIT_SECONDS.get(str(time_unit).strip().lower())
    if factor is None:
        raise SimulationValidationError(
            "UNSUPPORTED_TIME_UNIT",
            f"The model's time unit '{time_unit}' is not supported; a duration in seconds cannot be converted to it.",
        )
    return factor


def _validate_model_id(clean_id: str) -> None:
    if not _MODEL_ID_RE.match(clean_id):
        raise SimulationValidationError(
            "INVALID_MODEL_ID", f"'{clean_id[:40]}' is not a BioModels accession (BIOMD##########/MODEL##########)."
        )


def _endpoint_dynamics(tc: pd.DataFrame, species: List[str], seconds_per_unit: float) -> Dict[str, Any]:
    """Classification of the trajectory end (see dynamics.endpoint_dynamics)."""
    return endpoint_dynamics(tc, species, seconds_per_unit)


def _model_horizon(duration_seconds: Optional[float], seconds_per_unit: float, time_unit: str) -> Tuple[float, float, str]:
    """
    (duration in model time units, duration in seconds, source) for a requested duration in seconds, or the
    model-scaled default horizon when none was requested. The cap is in model time units (SCI-019).
    """
    if duration_seconds is None:
        model_duration = DEFAULT_HORIZON_MODEL_UNITS
        return model_duration, model_duration * seconds_per_unit, "model_default"
    model_duration = float(duration_seconds) / seconds_per_unit
    if model_duration > MAX_HORIZON_MODEL_UNITS * (1 + 1e-12):
        raise SimulationValidationError(
            "DURATION_OUT_OF_RANGE",
            f"A duration of {float(duration_seconds):g} s is {model_duration:g} {time_unit} in this model's time unit; "
            f"the longest supported horizon is {MAX_HORIZON_MODEL_UNITS:g} {time_unit}. It was not shortened.",
        )
    return model_duration, float(duration_seconds), "requested"


class _BasicoErrorCapture(logging.Handler):
    """Collects basico's error log lines (basico logs, rather than raises, when COPASI's integrator fails)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


@functools.lru_cache(maxsize=1)
def _software_versions() -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"basico": getattr(basico, "__version__", None), "copasi": None}
    try:
        import COPASI  # type: ignore[import-untyped,import-not-found]  # pyright: ignore[reportMissingImports]

        out["copasi"] = str(COPASI.CVersion.VERSION.getVersion())
    except Exception:  # noqa: BLE001
        pass
    return out


def _initial_time(copasi_model: Any) -> float:
    try:
        return float(copasi_model.getModel().getInitialTime())
    except Exception:  # noqa: BLE001
        return 0.0


def _integrate(
    copasi_model: Any,
    *,
    duration: Optional[float] = None,
    intervals: Optional[int] = None,
    values: Optional[List[float]] = None,
    time_unit: str = "",
) -> pd.DataFrame:
    """
    One deterministic time course with explicit integrator settings (LSODA, r_tol 1e-6, a_tol 1e-12, at most 1e5
    internal steps, fixed output grid, no event output) from the model's initial values. The trajectory must be
    complete: basico returns the rows recorded before a solver failure without raising, so a short trajectory
    raises IncompleteIntegrationError instead of being reported as the requested end point (SCI-002, SCI-020).
    Call with the COPASI lock held.
    """
    kwargs: Dict[str, Any] = dict(
        model=copasi_model,
        use_initial_values=True,
        method=SOLVER_METHOD,
        r_tol=SOLVER_RELATIVE_TOLERANCE,
        a_tol=SOLVER_ABSOLUTE_TOLERANCE,
        max_steps=SOLVER_MAX_INTERNAL_STEPS,
        output_event=False,
        settings={"problem": {"Start in Steady State": False}},
    )
    t_start = _initial_time(copasi_model)
    if values is not None:
        kwargs["values"] = [float(v) for v in values]
    else:
        kwargs.update(duration=float(duration or 0.0), intervals=int(intervals or 1), automatic=False, start_time=t_start)
    capture = _BasicoErrorCapture()
    basico_logger = logging.getLogger("basico")
    basico_logger.addHandler(capture)
    try:
        tc = basico.run_time_course(**kwargs)
    finally:
        basico_logger.removeHandler(capture)
    msgs = solver_error_text(capture.messages)
    if values is not None:
        check_trajectory_complete(tc, output_times=kwargs["values"], solver_messages=msgs, time_unit=time_unit)
    else:
        check_trajectory_complete(
            tc, end_time=t_start + float(duration or 0.0), intervals=int(intervals or 1), solver_messages=msgs,
            time_unit=time_unit,
        )
    return tc


def _solver_settings(copasi_model: Any) -> Dict[str, Any]:
    """The Time-Course task settings COPASI actually used (read back after the run), plus software versions."""
    out: Dict[str, Any] = {
        "method": None,
        "relative_tolerance": None,
        "absolute_tolerance": None,
        "max_internal_steps": None,
        "automatic_step_size": None,
        "output_event": None,
        "start_in_steady_state": None,
    }
    try:
        settings = basico.get_task_settings("Time-Course", model=copasi_model) or {}
        method = settings.get("method") or {}
        problem = settings.get("problem") or {}
        out.update(
            method=method.get("name"),
            relative_tolerance=method.get("Relative Tolerance"),
            absolute_tolerance=method.get("Absolute Tolerance"),
            max_internal_steps=method.get("Max Internal Steps"),
            integrate_reduced_model=method.get("Integrate Reduced Model"),
            max_internal_step_size=method.get("Max Internal Step Size"),
            automatic_step_size=problem.get("AutomaticStepSize"),
            output_event=problem.get("Output Event"),
            start_in_steady_state=problem.get("Start in Steady State"),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Time-Course settings unavailable: %s", e)
    out.update({f"{k}_version": v for k, v in _software_versions().items()})
    return out


def _solver_label(settings: Dict[str, Any]) -> str:
    name = settings.get("method") or "Deterministic (LSODA)"
    return f"{name}; rtol {settings.get('relative_tolerance')}, atol {settings.get('absolute_tolerance')}, max {settings.get('max_internal_steps')} steps"


_PUBMED_RE = re.compile(r"pubmed[/:](\d+)", re.I)
_DOI_RE = re.compile(r"doi[/:](10\.[^\s\"']+)", re.I)
_TAXON_RE = re.compile(r"taxonomy[/:](\d+)", re.I)
_BIOMODELS_ID_RE = re.compile(r"biomodels\.db[/:]((?:BIOMD|MODEL)\d{10})", re.I)


def _model_identity(copasi_model: Any) -> Dict[str, Any]:
    """Model name, publications (bqmodel:isDescribedBy), taxa and BioModels ids from the loaded model's annotation."""
    out: Dict[str, Any] = {"name": None, "pmids": [], "dois": [], "taxa": [], "accessions": []}
    try:
        name = basico.get_model_name(model=copasi_model)
        out["name"] = str(name).strip() or None if name else None
    except Exception as e:  # noqa: BLE001
        logger.debug("Model name unavailable: %s", e)
    try:
        annotation = basico.get_miriam_annotation(model=copasi_model) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("Model annotation unavailable: %s", e)
        annotation = {}
    for d in annotation.get("descriptions") or []:
        if not isinstance(d, dict):
            continue
        uri = str(d.get("uri") or d.get("id") or "")
        qualifier = str(d.get("qualifier") or "").lower()
        if qualifier == "is described by":
            out["pmids"] += [m for m in _PUBMED_RE.findall(uri) if m not in out["pmids"]]
            out["dois"] += [m for m in _DOI_RE.findall(uri) if m not in out["dois"]]
        elif qualifier == "has taxon":
            out["taxa"] += [m for m in _TAXON_RE.findall(uri) if m not in out["taxa"]]
        elif qualifier == "is":
            out["accessions"] += [m.upper() for m in _BIOMODELS_ID_RE.findall(uri) if m.upper() not in out["accessions"]]
    return out


def identity_provenance(clean_id: str, identity: Dict[str, Any]) -> Dict[str, Any]:
    """
    Provenance for a BioModels accession, taken from the loaded model's own annotation. A curated catalog entry adds
    its citation text only when its model name and PubMed id agree with the SBML; on disagreement the SBML wins and
    the mismatch is recorded (SCI-003).
    """
    catalog = CURATED_MODEL_CATALOG.get(clean_id)
    pmids = list(identity.get("pmids") or [])
    prov: Dict[str, Any] = {
        "accession": clean_id,
        "name": identity.get("name") or (catalog or {}).get("name") or f"BioModel {clean_id}",
        "pmid": pmids[0] if pmids else None,
        "pmids": pmids,
        "dois": list(identity.get("dois") or []),
        "taxon_ids": list(identity.get("taxa") or []),
        "identity_source": "sbml_annotation" if identity.get("name") else "unverified",
    }
    mismatches: List[str] = []
    accessions = [a for a in identity.get("accessions") or [] if a.startswith(("BIOMD", "MODEL"))]
    if accessions and clean_id not in accessions:
        mismatches.append("accession")
    if catalog:
        if identity.get("name") and catalog.get("name") != identity.get("name"):
            mismatches.append("name")
        if pmids and catalog.get("pmid") not in pmids:
            mismatches.append("pmid")
        if not mismatches:
            prov.update(
                publication=catalog.get("publication"),
                organism=catalog.get("organism"),
                description=catalog.get("description"),
                pmid=catalog.get("pmid") if not pmids or catalog.get("pmid") in pmids else prov["pmid"],
                identity_source="sbml_annotation+curated_catalog" if identity.get("name") else "curated_catalog_unverified",
            )
    if mismatches:
        prov["identity_mismatch"] = mismatches
        logger.warning("BioModel %s: catalog/file identity mismatch on %s; using the model file's annotation", clean_id, mismatches)
    prov.setdefault(
        "publication",
        (f"PubMed {', '.join(pmids[:3])} (from the model's SBML annotation)" if pmids else "EMBL-EBI BioModels Database"),
    )
    prov.setdefault("description", f"Kinetic SBML pathway model {clean_id}.")
    return prov


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _revision_checks_enabled() -> bool:
    """BioModels revision checks (network): PHRONESIS_BIOMODELS_REVISION_CHECK on/off; default on, off under pytest."""
    flag = os.getenv("PHRONESIS_BIOMODELS_REVISION_CHECK", "").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


class VivarixExecutionArtifact(BaseModel):
    """Execution artifact returned with every computational simulation."""
    model_id: str
    model_name: str
    provenance: Dict[str, Any]
    simulation_type: str  # "TIME_COURSE" | "STEADY_STATE" | "PARAMETER_SCAN"
    duration: float  # in the model's time unit (units["time"]), as executed
    intervals: int
    duration_seconds: Optional[float] = None  # the integrated horizon in seconds
    duration_source: Optional[str] = None  # "requested" | "model_default" (DEFAULT_HORIZON_MODEL_UNITS model units)
    solver_method: str = "LSODA"
    solver_settings: Dict[str, Any] = Field(default_factory=dict)  # method, tolerances, step limit, versions (read back)
    # time, quantity (AMOUNT unit, e.g. nmol), volume, concentration (species values, e.g. nmol/ml)
    units: Dict[str, str] = Field(default_factory=dict)
    concentration_unit: Optional[str] = None  # species values are concentrations in this unit
    species_list: List[str] = Field(default_factory=list)
    initial_concentrations: Dict[str, float] = Field(default_factory=dict)  # after parameter overrides
    final_concentrations: Dict[str, float] = Field(default_factory=dict)
    applied_overrides: List[Dict[str, Any]] = Field(default_factory=list)
    time_series_sample: Dict[str, List[float]] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    plot_base64_png: Optional[str] = None

    def to_markdown_summary(self) -> str:
        time_unit = self.units.get("time", "s")
        unit_s = self.concentration_unit or self.units.get("concentration") or self.units.get("quantity", "nM")
        lines = [
            f"**BioModels Accession**: `{self.model_id}` ({self.model_name})",
            f"**Solver / Integrator**: {self.solver_method} (Duration: {self.duration:g} {time_unit}, Intervals: {self.intervals})",
            f"**Provenance**: {self.provenance.get('publication', 'EMBL-EBI Curated BioModels')}",
            "",
            f"| Species | Initial Conc. | Final Time-Course Conc. (t={self.duration:g} {time_unit}) | % Delta |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for sp in self.species_list[:6]:
            init_c = self.initial_concentrations.get(sp, 0.0)
            final_c = self.final_concentrations.get(sp, 0.0)
            # A species that starts at zero has no meaningful percentage change.
            delta = "N/A" if abs(init_c) < 1e-12 else f"{((final_c - init_c) / abs(init_c)) * 100:+.1f}%"
            lines.append(f"| **{sp}** | {init_c:.4g} {unit_s} | {final_c:.4g} {unit_s} | {delta} |")
        return "\n".join(lines)


class BioModelsSearchError(RuntimeError):
    """The BioModels search service failed (as opposed to returning no matching model)."""


class VivarixSimulationEngine:
    """
    COPASI/BASICO Execution Engine for Vivarix.
    Provides deterministic simulation, parameter scans, steady-state solving, and plot generation.
    """

    @staticmethod
    def search_models(query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Search the EMBL-EBI BioModels database via its REST API.

        Returns [] when the search succeeded but nothing matched (also checking the curated catalog by meaningful
        words). Raises BioModelsSearchError when the live search failed and the curated catalog has no match, so
        a service outage is never reported as "no model exists". Never substitutes an unrelated default model.
        """
        clean_q = re.sub(r'["\']', '', query).strip()
        results: List[Dict[str, Any]] = []
        live_error: Optional[str] = None

        # 1. Live query to EMBL-EBI BioModels database
        try:
            import httpx
            headers = {"Accept": "application/json", "User-Agent": "Phronesis/2.0 (Biomedical R&D Engine)"}
            resp = httpx.get(
                "https://www.biomodels.org/search",
                params={"query": clean_q, "format": "json", "numResults": limit},
                headers=headers,
                follow_redirects=True,
                timeout=8.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                for hit in data.get("models", [])[:limit]:
                    m_id = hit.get("id")
                    if m_id and _ACCESSION_RE.match(str(m_id)):
                        results.append({
                            "accession": m_id,
                            "name": hit.get("name") or f"BioModel {m_id}",
                            "url": hit.get("url") or f"https://www.ebi.ac.uk/biomodels/{m_id}",
                            "format": hit.get("format", "SBML"),
                            "submissionDate": hit.get("submissionDate", ""),
                            "description": f"Live EMBL-EBI BioModels match for query '{clean_q}'.",
                            "publication": f"EMBL-EBI BioModels Registry ({m_id})",
                        })
                if results:
                    return results
            else:
                live_error = f"HTTP {resp.status_code}"
        except Exception as e:
            live_error = f"{type(e).__name__}: {e}"
            logger.info("Live BioModels search failed for '%s': %s. Checking curated catalog.", clean_q, e)

        # 2. Curated catalog: an accession, or a meaningful word (>= 3 characters, not generic) matched whole.
        q_upper = clean_q.upper()
        tokens = [t for t in re.findall(r"[A-Z0-9][A-Z0-9-]{2,}", q_upper) if t not in _SEARCH_STOPWORDS]
        for acc, m in CURATED_MODEL_CATALOG.items():
            haystack = f"{m['name']} {m['description']}".upper()
            if acc in q_upper or any(re.search(rf"(?<![A-Z0-9]){re.escape(t)}(?![A-Z0-9])", haystack) for t in tokens):
                results.append(m)
                if len(results) >= limit:
                    break

        if not results and live_error is not None:
            raise BioModelsSearchError(f"BioModels search failed ({live_error})")
        return results

    @staticmethod
    def _create_plot_base64(df: pd.DataFrame, title: str, time_unit: str = "s", conc_unit: str = "") -> Optional[str]:
        """Generate a clean matplotlib trajectory chart encoded as base64 PNG."""
        try:
            fig, ax = plt.subplots(figsize=(7, 3.8), dpi=120)
            # Pick up to 5 top dynamic species
            cols = [c for c in df.columns if c != "Time"]
            if len(cols) > 5:
                # Rank by variance
                var_series = df[cols].var()
                if isinstance(var_series, pd.Series):
                    sorted_idx = var_series.sort_values(ascending=False)
                    if isinstance(sorted_idx, pd.Series):
                        cols = [str(c) for c in list(sorted_idx.index)[:5]]

            for col in cols:
                ax.plot(df.index, df[col], label=str(col), linewidth=1.8)

            ax.set_title(title, fontsize=11, fontweight="bold", pad=10)
            ax.set_xlabel(f"Time ({time_unit})", fontsize=10)
            ax.set_ylabel(f"Concentration ({conc_unit})" if conc_unit else "Concentration", fontsize=10)
            ax.grid(True, linestyle="--", alpha=0.4)
            ax.legend(fontsize=8, loc="best", framealpha=0.85)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format="png")
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")
        except Exception as e:
            logger.warning("Failed to render plot: %s", e)
            return None

    @staticmethod
    def _cache_dir() -> Path:
        import tempfile

        from phronesis.utils.paths import data_dir

        cache_dir = data_dir("biomodels_cache") or Path(tempfile.gettempdir()) / "phronesis_biomodels_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # ------------------------------------------------------------------
    # BioModels cache: the downloaded SBML is pinned to its revision and sha256 (SCI-012)
    #   <acc>.xml        the main SBML file exactly as downloaded (its sha256 is comparable with BioModels' own)
    #   <acc>.meta.json  {revision, main_file, sbml_sha256, remote_sha256, downloaded_at, checked_at, cps_from_sbml_sha256}
    #   <acc>.cps        COPASI's conversion of that SBML (used only while cps_from_sbml_sha256 matches the SBML)
    # ------------------------------------------------------------------
    @classmethod
    def _meta_path(cls, clean_id: str) -> Path:
        return cls._cache_dir() / f"{clean_id}.meta.json"

    @classmethod
    def _read_meta(cls, clean_id: str) -> Dict[str, Any]:
        try:
            data = json.loads(cls._meta_path(clean_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def _write_meta(cls, clean_id: str, meta: Dict[str, Any]) -> None:
        path = cls._meta_path(clean_id)
        tmp = path.with_suffix(".json.part")
        try:
            tmp.write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.debug("Could not write BioModels cache metadata for %s: %s", clean_id, e)

    @staticmethod
    def _biomodels_get(url: str, *, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        import httpx

        return httpx.get(url, params=params, headers={"Accept": "application/json", "User-Agent": "Phronesis/2.0"},
                         follow_redirects=True, timeout=timeout)

    def _latest_revision(self, clean_id: str) -> Optional[int]:
        """BioModels' latest revision number of an accession, or None when it cannot be determined."""
        try:
            resp = self._biomodels_get(f"https://www.biomodels.org/{clean_id}", params={"format": "json"}, timeout=8.0)
            if resp.status_code != 200:
                return None
            revisions = ((resp.json() or {}).get("history") or {}).get("revisions") or []
            last = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
            return int(last["version"]) if isinstance(last.get("version"), int) else None
        except Exception as e:  # noqa: BLE001
            logger.debug("BioModels revision lookup for %s failed: %s", clean_id, e)
            return None

    def _download_pinned(self, clean_id: str, revision: Optional[int]) -> bool:
        """Download the main SBML file (of `revision` when known) and record its revision and sha256."""
        cache_dir = self._cache_dir()
        for attempt in range(3):
            try:
                rf = self._biomodels_get(f"https://www.biomodels.org/model/files/{clean_id}", params={"format": "json"})
                main: Dict[str, Any] = {}
                if rf.status_code == 200:
                    entries = (rf.json() or {}).get("main") or []
                    main = entries[0] if entries and isinstance(entries[0], dict) else {}
                filename = main.get("name") or f"{clean_id}_url.xml"
                urls = ([f"https://www.biomodels.org/model/download/{clean_id}.{revision}"] if revision is not None else []) + [
                    f"https://www.biomodels.org/model/download/{clean_id}"
                ]
                for i, url in enumerate(urls):
                    rd = self._biomodels_get(url, params={"filename": filename}, timeout=15.0)
                    body = rd.content if rd.status_code == 200 else b""
                    if not body or b"<sbml" not in body[:4000]:
                        continue
                    sha = _sha256_bytes(body)
                    remote_sha = main.get("sha256sum")
                    pinned_revision = revision if (i == 0 and revision is not None) else None
                    if remote_sha and remote_sha != sha:
                        # The listed sha256 is for the latest revision; keep the file but do not claim that revision.
                        logger.info("BioModels %s: downloaded SBML sha256 differs from the listed main file", clean_id)
                    elif pinned_revision is None and revision is not None and remote_sha == sha:
                        pinned_revision = revision
                    part = cache_dir / f"{clean_id}.xml.part"
                    part.write_bytes(body)
                    os.replace(part, cache_dir / f"{clean_id}.xml")
                    stale_cps = cache_dir / f"{clean_id}.cps"
                    if stale_cps.exists():
                        with contextlib.suppress(OSError):
                            stale_cps.unlink()
                    now = datetime.now(timezone.utc).isoformat()
                    self._write_meta(clean_id, {
                        "accession": clean_id,
                        "revision": pinned_revision,
                        "latest_revision_at_download": revision,
                        "main_file": filename,
                        "sbml_sha256": sha,
                        "remote_sha256": remote_sha,
                        "downloaded_at": now,
                        "checked_at": now,
                        "source_url": url,
                    })
                    return True
            except Exception as e:  # noqa: BLE001
                logger.debug("BioModels download attempt %d for %s failed: %s", attempt + 1, clean_id, e)
            time.sleep(0.8 * (attempt + 1))
        return False

    def _prefetch_model(self, clean_id: str) -> None:
        """
        Make a BioModels accession available locally WITHOUT holding the COPASI lock, so a slow or retrying download
        never blocks other simulations. A pinned download is re-checked against BioModels' latest revision at most
        once per PHRONESIS_BIOMODELS_REVISION_TTL_S (24 h) and replaced when a newer revision exists; a legacy
        cache entry without revision metadata is upgraded to a pinned download when BioModels is reachable.
        Failures are left for the loader, which then runs the cached file and reports it as unpinned.
        """
        if not _ACCESSION_RE.match(clean_id):
            return
        cache_dir = self._cache_dir()
        sbml_file, cps_file = cache_dir / f"{clean_id}.xml", cache_dir / f"{clean_id}.cps"
        meta = self._read_meta(clean_id)
        cached = sbml_file.exists() or cps_file.exists()
        pinned = sbml_file.exists() and bool(meta.get("sbml_sha256"))
        if cached and not _revision_checks_enabled():
            return
        if pinned:
            try:
                checked = datetime.fromisoformat(str(meta.get("checked_at"))).timestamp()
            except (TypeError, ValueError):
                checked = 0.0
            if time.time() - checked < _REVISION_TTL_S:
                return
            latest = self._latest_revision(clean_id)
            if latest is None:
                return  # BioModels unreachable: keep the pinned file; its revision stays recorded
            if latest == meta.get("revision") or latest == meta.get("latest_revision_at_download"):
                meta["checked_at"] = datetime.now(timezone.utc).isoformat()
                self._write_meta(clean_id, meta)
                return
            logger.info("BioModels %s: revision %s replaces cached revision %s", clean_id, latest, meta.get("revision"))
            self._download_pinned(clean_id, latest)
            return
        self._download_pinned(clean_id, self._latest_revision(clean_id))

    def _load_copasi_model(self, clean_id: str) -> Tuple[Any, Dict[str, Any]]:
        """
        (COPASI data model, model_version) for a BioModel ID (call with the COPASI lock held). The pinned SBML is the
        executed artefact; its .cps conversion is reused only when it was made from that exact SBML (sha256).
        """
        _validate_model_id(clean_id)
        cache_dir = self._cache_dir()
        cache_file = cache_dir / f"{clean_id}.cps"
        sbml_file = cache_dir / f"{clean_id}.xml"

        last_err: Optional[Exception] = None
        if sbml_file.exists():
            try:
                body = sbml_file.read_bytes()
                sha = _sha256_bytes(body)
                meta = self._read_meta(clean_id)
                pinned = meta.get("sbml_sha256") == sha
                version = {
                    "accession": clean_id,
                    "revision": meta.get("revision") if pinned else None,
                    "main_file": meta.get("main_file") if pinned else None,
                    "sbml_sha256": sha,
                    "remote_sha256": meta.get("remote_sha256") if pinned else None,
                    "downloaded_at": meta.get("downloaded_at") if pinned else None,
                    "revision_checked_at": meta.get("checked_at") if pinned else None,
                    "cache": "pinned_sbml" if pinned and meta.get("revision") is not None else "sbml_revision_unknown",
                }
                if cache_file.exists() and meta.get("cps_from_sbml_sha256") == sha:
                    try:
                        model = basico.load_model(str(cache_file))
                        if model is not None:
                            return model, version
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to load cached model %s: %s", cache_file, e)
                copasi_model = basico.load_model_from_string(body.decode("utf-8"))
                if copasi_model is not None:
                    try:
                        basico.save_model(str(cache_file), model=copasi_model)
                        self._write_meta(clean_id, {**meta, "cps_from_sbml_sha256": sha})
                    except Exception as se:  # noqa: BLE001
                        logger.debug("Could not cache model %s: %s", clean_id, se)
                    return copasi_model, version
            except Exception as e:  # noqa: BLE001
                last_err = e

        if cache_file.exists():
            # Legacy cache entry (a COPASI file with no SBML or revision record): runs, but is reported as unpinned.
            try:
                model = basico.load_model(str(cache_file))
                if model is not None:
                    return model, {
                        "accession": clean_id, "revision": None, "sbml_sha256": None,
                        "cps_sha256": _sha256_bytes(cache_file.read_bytes()), "cache": "legacy_cps_unpinned",
                    }
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to load cached model %s: %s", cache_file, e)
                last_err = last_err or e

        # Last resort: basico's own loader (BioModels' latest file, not cached here, so not pinned).
        if clean_id.startswith("BIOMD"):
            try:
                copasi_model = basico.load_biomodel(int(clean_id[5:]))
                if copasi_model is not None:
                    return copasi_model, {"accession": clean_id, "revision": None, "sbml_sha256": None, "cache": "basico_load_biomodel_unpinned"}
            except Exception as e:  # noqa: BLE001
                last_err = e

        raise last_err or RuntimeError(f"Unable to load BioModel {clean_id}")

    @staticmethod
    def _load_uploaded_sbml(path: str) -> Any:
        """
        Load a user-uploaded SBML file. It must be the uploaded model of a dataset attached to the current run and
        owned by the run's user (ORC-013); it never touches the BIOMD cache.
        """
        from .upload_access import UploadAccessError, owned_upload_path

        try:
            real = owned_upload_path(path, kind="model")
        except UploadAccessError as exc:
            raise SimulationValidationError("DATA_PATH_REJECTED", f"uploaded model file rejected: {exc}") from exc
        model = basico.load_model(real)
        if model is None:
            raise RuntimeError("COPASI could not load the uploaded SBML model")
        return model

    def _resolve_model(
        self,
        clean_id: str,
        sbml_path: Optional[str],
        model_label: Optional[str],
        model_provenance: Optional[Dict[str, Any]],
    ) -> Tuple[Any, Dict[str, Any]]:
        """(COPASI model, provenance dict) for a BioModels accession or an uploaded SBML file."""
        if sbml_path:
            prov = dict(model_provenance or {})
            prov["source"] = "user_upload"
            prov["name"] = model_label or prov.get("display_name") or clean_id
            prov.setdefault("publication", "Uploaded SBML model (provenance declared by uploader)")
            model = self._load_uploaded_sbml(sbml_path)
            try:
                with open(sbml_path, "rb") as fh:
                    prov["model_version"] = {"sbml_sha256": _sha256_bytes(fh.read()), "cache": "user_upload"}
            except OSError:
                pass
            identity = _model_identity(model)
            if identity.get("name"):
                prov.setdefault("sbml_model_name", identity["name"])
            return model, prov
        model, version = self._load_copasi_model(clean_id)
        prov = identity_provenance(clean_id, _model_identity(model))
        prov["model_version"] = version
        return model, prov

    # ------------------------------------------------------------------
    # Parameter resolution (validated once, before anything is computed)
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_parameter(copasi_model: Any, name: str, clean_id: str) -> Dict[str, Any]:
        """
        Resolve a user parameter name to exactly one model quantity:
        species (initial concentration) > global parameter > qualified reaction parameter '(rxn).k' >
        a unique reaction-parameter suffix match ('V1' -> '(MAPKKK activation).V1').
        Unknown or ambiguous names raise SimulationValidationError; nothing is ever applied to a guess.
        """
        species_df = basico.get_species(model=copasi_model)
        if species_df is not None and name in species_df.index:
            return {"kind": "species", "resolved": name, "previous_value": float(species_df.loc[name, "initial_concentration"])}
        global_params = basico.get_parameters(model=copasi_model)
        if global_params is not None and name in global_params.index:
            return {"kind": "global_parameter", "resolved": name, "previous_value": float(global_params.loc[name, "initial_value"])}
        rxn = basico.get_reaction_parameters(model=copasi_model)
        rxn = rxn if rxn is not None else pd.DataFrame()
        if name in rxn.index:
            matches = [name]
        else:
            matches = [idx for idx in rxn.index if str(idx).endswith("." + name)]
        if not matches:
            raise SimulationValidationError(
                "UNKNOWN_PARAMETER",
                f"Invalid parameter override: parameter or species '{name}' not found in model {clean_id}",
            )
        if len(matches) > 1:
            raise SimulationValidationError(
                "AMBIGUOUS_PARAMETER",
                f"Parameter '{name}' is ambiguous in model {clean_id}; use one of: {', '.join(sorted(map(str, matches))[:8])}",
            )
        row = rxn.loc[matches[0]]
        mapped = str(row.get("mapped_to") or "")
        if str(row.get("type") or "") == "global" and mapped:
            # A reaction parameter mapped to a global quantity is set through that quantity.
            return {"kind": "global_parameter", "resolved": mapped, "via": matches[0],
                    "previous_value": float(global_params.loc[mapped, "initial_value"]) if global_params is not None and mapped in global_params.index else None}
        return {"kind": "reaction_parameter", "resolved": str(matches[0]), "previous_value": float(row.get("value", float("nan")))}

    @staticmethod
    def _apply_value(copasi_model: Any, resolved: Dict[str, Any], value: float) -> None:
        if resolved["kind"] == "species":
            basico.set_species(name=resolved["resolved"], initial_concentration=value, model=copasi_model)
        elif resolved["kind"] == "global_parameter":
            basico.set_parameters(name=resolved["resolved"], initial_value=value, model=copasi_model)
        else:
            basico.set_reaction_parameters(name=resolved["resolved"], value=value, model=copasi_model)

    @staticmethod
    def _read_value(copasi_model: Any, resolved: Dict[str, Any]) -> Optional[float]:
        try:
            if resolved["kind"] == "species":
                return float(basico.get_species(name=resolved["resolved"], exact=True, model=copasi_model)["initial_concentration"].iloc[0])
            if resolved["kind"] == "global_parameter":
                return float(basico.get_parameters(name=resolved["resolved"], exact=True, model=copasi_model)["initial_value"].iloc[0])
            return float(basico.get_reaction_parameters(model=copasi_model).loc[resolved["resolved"], "value"])
        except Exception:  # noqa: BLE001
            return None

    def _apply_overrides(self, copasi_model: Any, overrides: Optional[Dict[str, Any]], clean_id: str) -> List[Dict[str, Any]]:
        """Validate every override first, then apply them; returns what was applied (requested and read back)."""
        if not overrides:
            return []
        planned: List[Tuple[str, float, Dict[str, Any]]] = []
        for p_name, p_val in overrides.items():
            if isinstance(p_val, bool):
                raise SimulationValidationError("INVALID_PARAMETER_VALUE", f"Invalid non-numeric parameter value for '{p_name}': {p_val}")
            try:
                val_float = float(p_val)
            except (ValueError, TypeError):
                raise SimulationValidationError("INVALID_PARAMETER_VALUE", f"Invalid non-numeric parameter value for '{p_name}': {p_val}")
            if not math.isfinite(val_float):
                raise SimulationValidationError("INVALID_PARAMETER_VALUE", f"Parameter value for '{p_name}' must be finite")
            planned.append((str(p_name), val_float, self._resolve_parameter(copasi_model, str(p_name), clean_id)))
        applied: List[Dict[str, Any]] = []
        for p_name, val_float, resolved in planned:
            self._apply_value(copasi_model, resolved, val_float)
            applied.append({
                "requested": p_name,
                "resolved": resolved["resolved"],
                "kind": resolved["kind"],
                "previous_value": resolved.get("previous_value"),
                "requested_value": val_float,
                "applied_value": self._read_value(copasi_model, resolved),
            })
        return applied

    @staticmethod
    def _validation_failure(tool_name: str, clean_id: str, err: SimulationValidationError, t0: float) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            error=str(err),
            error_type=ErrorType.VALIDATION_ERROR,
            canonical_ids={"biomodel_id": clean_id},
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"adapter": "LOCAL_COMPUTE", "validation_code": err.code},
        )

    @staticmethod
    def _busy_failure(tool_name: str, clean_id: str, err: Exception, t0: float) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            error=str(err),
            error_type=ErrorType.TIMEOUT,
            canonical_ids={"biomodel_id": clean_id},
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"adapter": "LOCAL_COMPUTE", "reason_code": "SOLVER_BUSY"},
        )

    # ------------------------------------------------------------------
    # Time course
    # ------------------------------------------------------------------
    @staticmethod
    def _incomplete_failure(
        tool_name: str, clean_id: str, err: IncompleteIntegrationError, t0: float, extra: Dict[str, Any]
    ) -> ToolResult:
        """An integration that stopped before the requested end time: a failure, never a result (SCI-002)."""
        details = {**err.details(), **extra}
        return ToolResult(
            success=False,
            tool_name=tool_name,
            error=f"Simulation failed for {clean_id}: {err}. No end-point values are reported for an incomplete integration.",
            error_type=ErrorType.SIMULATION_FAILED,
            data={"model_id": clean_id, "partial": True, "integration_complete": False, **details},
            canonical_ids={"biomodel_id": clean_id},
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"adapter": "LOCAL_COMPUTE", "reason_code": "INTEGRATION_INCOMPLETE", **details},
        )

    def simulate_time_course(
        self,
        biomodel_id: str = "BIOMD0000000010",
        duration: Optional[float] = 60.0,
        intervals: int = 40,
        parameter_overrides: Optional[Dict[str, float]] = None,
        sbml_path: Optional[str] = None,
        model_label: Optional[str] = None,
        model_provenance: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        """
        Execute real time-course ODE integration using COPASI/BASICO.
        `duration` is in seconds and is converted to the model's time unit before integrating; None integrates the
        model-scaled default horizon (DEFAULT_HORIZON_MODEL_UNITS model time units). The integration must reach the
        requested end time on every output point, otherwise the run fails (SCI-002).
        Returns numerical output, diagnostics, and base64 chart artifact.
        """
        t0 = time.perf_counter()
        clean_id = str(biomodel_id or "").strip()
        tool = "vivarix_simulate_time_course"
        try:
            if not sbml_path:
                _validate_model_id(clean_id)
            if duration is not None and not (
                isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(float(duration)) and float(duration) > 0
            ):
                raise SimulationValidationError("DURATION_INVALID", f"Duration must be a positive finite number of seconds, got {duration!r}")
            if not sbml_path:
                self._prefetch_model(clean_id)
        except SimulationValidationError as ve:
            return self._validation_failure(tool, clean_id, ve, t0)

        copasi_model = None
        try:
            with _copasi_session():
                try:
                    copasi_model, prov = self._resolve_model(clean_id, sbml_path, model_label, model_provenance)
                    units = _model_units(copasi_model)
                    seconds_per_unit = _seconds_per_time_unit(units["time"])
                    model_duration, duration_seconds, duration_source = _model_horizon(duration, seconds_per_unit, units["time"])
                    applied = self._apply_overrides(copasi_model, parameter_overrides, clean_id)

                    # Initial concentrations as executed: read after the overrides are applied.
                    species_df = basico.get_species(model=copasi_model)
                    initial_concs = {str(sp): float(row.get("initial_concentration", 0.0)) for sp, row in species_df.iterrows()}

                    try:
                        tc_result: pd.DataFrame = _integrate(
                            copasi_model, duration=model_duration, intervals=intervals, time_unit=units["time"]
                        )
                    except IncompleteIntegrationError as inc:
                        return self._incomplete_failure(tool, clean_id, inc, t0, {
                            "requested_duration": model_duration,
                            "duration_seconds": duration_seconds,
                            "intervals": intervals,
                            "units": {"time": units["time"]},
                            "solver_settings": _solver_settings(copasi_model),
                        })
                    solver = _solver_settings(copasi_model)

                    species_list = [c for c in tc_result.columns if c != "Time"]
                    final_concs = {sp: float(tc_result[sp].iloc[-1]) for sp in species_list}
                    time_series_sample: Dict[str, List[float]] = {"Time": [float(x) for x in tc_result.index[:10]]}
                    for sp in species_list:
                        time_series_sample[sp] = [float(x) for x in tc_result[sp].iloc[:10]]

                    final_drift = 0.0
                    if len(tc_result) >= 2:
                        final_drift = float((tc_result[species_list].iloc[-1] - tc_result[species_list].iloc[-2]).abs().max())
                    dynamics = _endpoint_dynamics(tc_result, species_list, seconds_per_unit)

                    plot_b64 = self._create_plot_base64(
                        tc_result,
                        title=f"{prov['name']} - Species Concentration Time Course",
                        time_unit=units["time"],
                        conc_unit=units["concentration"],
                    )
                except SimulationValidationError as ve:
                    return self._validation_failure(tool, clean_id, ve, t0)

            artifact = VivarixExecutionArtifact(
                model_id=clean_id,
                model_name=prov["name"],
                provenance=prov,
                simulation_type="TIME_COURSE",
                duration=model_duration,
                duration_seconds=duration_seconds,
                duration_source=duration_source,
                intervals=intervals,
                solver_method=_solver_label(solver),
                solver_settings=solver,
                units=dict(units),
                concentration_unit=units["concentration"],
                species_list=species_list,
                initial_concentrations=initial_concs,
                final_concentrations=final_concs,
                applied_overrides=applied,
                time_series_sample=time_series_sample,
                diagnostics={
                    "total_equations": len(species_list),
                    "steps_computed": len(tc_result),
                    "steps_expected": intervals + 1,
                    "integration_complete": True,
                    "final_time": float(tc_result.index[-1]),
                    "final_max_drift": round(final_drift, 6),
                    "finite_values_verified": True,
                    "volume_unit": units["volume"],
                    "species_max_abs": trajectory_scale(tc_result, species_list),
                    "model_version": prov.get("model_version"),
                    **dynamics,
                },
                plot_base64_png=plot_b64,
            )

            return ToolResult(
                success=True,
                tool_name=tool,
                data=artifact.model_dump(),
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
                metadata={"adapter": "LOCAL_COMPUTE", "solver": "COPASI/BASICO"},
            )

        except SolverBusyError as busy:
            return self._busy_failure(tool, clean_id, busy, t0)
        except Exception as e:
            logger.error("Vivarix simulation failed: %s", e, exc_info=not sbml_path)
            return ToolResult(
                success=False,
                tool_name=tool,
                error=f"Simulation failed for {clean_id}: {e}",
                error_type=ErrorType.SIMULATION_FAILED,
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
                metadata={"adapter": "LOCAL_COMPUTE"},
            )
        finally:
            _release_datamodel(copasi_model)

    # ------------------------------------------------------------------
    # Steady state
    # ------------------------------------------------------------------
    @staticmethod
    def _stability(copasi_model: Any) -> Dict[str, Any]:
        """Eigenvalues of the reduced Jacobian at the steady state: stable only if every real part is negative."""
        try:
            jac = basico.get_reduced_jacobian_matrix(model=copasi_model)
            if jac is None or getattr(jac, "empty", True):
                return {"stable": None, "stability": "UNKNOWN", "eigenvalues": []}
            eig = np.linalg.eigvals(jac.to_numpy(dtype=float))
        except Exception as e:  # noqa: BLE001
            logger.debug("Jacobian eigenvalues unavailable: %s", e)
            return {"stable": None, "stability": "UNKNOWN", "eigenvalues": []}
        scale = max(float(np.max(np.abs(eig))) if len(eig) else 0.0, 1e-12)
        tol = 1e-9 * scale
        max_re = float(np.max(eig.real)) if len(eig) else 0.0
        oscillatory = bool(np.any(np.abs(eig.imag) > tol))
        if max_re < -tol:
            stability = "STABLE_FOCUS" if oscillatory else "STABLE"
        elif max_re > tol:
            # An unstable focus usually means sustained oscillations (a limit cycle) around this point.
            stability = "UNSTABLE_OSCILLATORY" if oscillatory else "UNSTABLE"
        else:
            stability = "MARGINAL"
        return {
            "stable": stability in ("STABLE", "STABLE_FOCUS"),
            "stability": stability,
            "max_real_eigenvalue": float(f"{max_re:.6g}"),
            "eigenvalues": [[float(f"{z.real:.6g}"), float(f"{z.imag:.6g}")] for z in eig],
        }

    def compute_steady_state(
        self,
        biomodel_id: str = "BIOMD0000000010",
        sbml_path: Optional[str] = None,
        model_label: Optional[str] = None,
        model_provenance: Optional[Dict[str, Any]] = None,
        parameter_overrides: Optional[Dict[str, float]] = None,
    ) -> ToolResult:
        """Calculate steady-state concentrations, reaction fluxes, and the Jacobian eigenvalues (stability)."""
        t0 = time.perf_counter()
        clean_id = str(biomodel_id or "").strip()
        tool = "vivarix_compute_steady_state"
        try:
            if not sbml_path:
                _validate_model_id(clean_id)
                self._prefetch_model(clean_id)
        except SimulationValidationError as ve:
            return self._validation_failure(tool, clean_id, ve, t0)

        copasi_model = None
        try:
            with _copasi_session():
                try:
                    copasi_model, prov = self._resolve_model(clean_id, sbml_path, model_label, model_provenance)
                    applied = self._apply_overrides(copasi_model, parameter_overrides, clean_id)
                except SimulationValidationError as ve:
                    return self._validation_failure(tool, clean_id, ve, t0)

                ss_res = basico.run_steadystate(model=copasi_model)
                # COPASI CSteadyStateMethod return codes: 0 notFound, 1 found, 2 foundEquilibrium, 3 foundNegative
                ss_status_names = {0: "NOT_FOUND", 1: "FOUND", 2: "FOUND_EQUILIBRIUM", 3: "FOUND_NEGATIVE"}
                ss_status = ss_status_names.get(int(ss_res) if ss_res is not None else 0, f"UNKNOWN_{ss_res}")
                if ss_status not in ("FOUND", "FOUND_EQUILIBRIUM"):
                    return ToolResult(
                        success=False,
                        tool_name=tool,
                        error=f"COPASI steady-state search did not yield a valid steady state for {clean_id} (status: {ss_status}).",
                        error_type=ErrorType.SIMULATION_FAILED,
                        canonical_ids={"biomodel_id": clean_id},
                        execution_time_seconds=time.perf_counter() - t0,
                        metadata={"steady_state_status": ss_status},
                    )
                units = _model_units(copasi_model)
                species_df = basico.get_species(model=copasi_model)
                ss_concs = {str(sp): float(row.get("concentration", 0.0)) for sp, row in species_df.iterrows()}
                reactions_df = basico.get_reactions(model=copasi_model)
                fluxes = {str(r): float(row.get("flux", 0.0)) for r, row in reactions_df.iterrows()}
                stability = self._stability(copasi_model)
                try:
                    ss_method = (basico.get_task_settings("Steady-State", model=copasi_model) or {}).get("method") or {}
                except Exception:  # noqa: BLE001
                    ss_method = {}

            ss_data: Dict[str, Any] = {
                "model_id": clean_id,
                "simulation_type": "STEADY_STATE",
                "steady_state_status": ss_status,
                "steady_state_concentrations": ss_concs,
                "reaction_fluxes": fluxes,
                "units": dict(units),
                "concentration_unit": units["concentration"],
                "applied_overrides": applied,
                "solver_settings": {
                    "method": ss_method.get("name"),
                    "resolution": ss_method.get("Resolution"),
                    "iteration_limit": ss_method.get("Iteration Limit"),
                    **{f"{k}_version": v for k, v in _software_versions().items()},
                },
                "model_version": prov.get("model_version"),
                **stability,
            }
            if not sbml_path:
                ss_data.update(model_name=prov.get("name"), provenance=prov)
            if stability.get("stable") is False:
                ss_data["stability_note"] = (
                    "This fixed point is unstable (a Jacobian eigenvalue has a positive real part); the system moves "
                    "away from it" + (" and typically oscillates" if "OSCILLATORY" in stability["stability"] else "")
                    + ". Do not describe these values as the long-run state."
                )
            if sbml_path:
                ss_data.update(model_name=prov.get("name"), provenance=prov)
            return ToolResult(
                success=True,
                tool_name=tool,
                data=ss_data,
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
                metadata={"adapter": "LOCAL_COMPUTE"},
            )
        except SolverBusyError as busy:
            return self._busy_failure(tool, clean_id, busy, t0)
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name=tool,
                error=str(e),
                error_type=ErrorType.SIMULATION_FAILED,
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
            )
        finally:
            _release_datamodel(copasi_model)

    # ------------------------------------------------------------------
    # Parameter scan
    # ------------------------------------------------------------------
    @staticmethod
    def scan_values(start_val: float, end_val: float, num_steps: int, spacing: str = "linear") -> List[float]:
        """The scan points, both endpoints included (num_steps >= 2)."""
        if num_steps < 2:
            raise SimulationValidationError("SCAN_POINTS_INVALID", "A parameter scan needs at least 2 points")
        if not (math.isfinite(start_val) and math.isfinite(end_val)):
            raise SimulationValidationError("SCAN_RANGE_INVALID", "Scan bounds must be finite numbers")
        if abs(float(end_val) - float(start_val)) < 1e-12:
            raise SimulationValidationError("SCAN_RANGE_INVALID", "Scan bounds start_val and end_val must be a distinct non-zero range")
        if spacing == "log":
            if start_val <= 0 or end_val <= 0:
                raise SimulationValidationError("SCAN_RANGE_INVALID", "A logarithmic scan needs positive bounds")
            values = np.geomspace(start_val, end_val, num_steps)
        else:
            values = np.linspace(start_val, end_val, num_steps)
        out = [float(v) for v in values]
        out[0], out[-1] = float(start_val), float(end_val)  # exact endpoints, no floating-point drift
        return out

    def run_parameter_scan(
        self,
        biomodel_id: str = "BIOMD0000000010",
        parameter_name: str = "V1",
        start_val: float = 1.0,
        end_val: float = 10.0,
        num_steps: int = 5,
        target_species: Optional[str] = "Erk2-PP",
        sbml_path: Optional[str] = None,
        model_label: Optional[str] = None,
        model_provenance: Optional[Dict[str, Any]] = None,
        duration: Optional[float] = 60.0,
        intervals: int = 100,
        spacing: str = "linear",
        report_all_species: bool = False,
    ) -> ToolResult:
        """
        Scan one parameter (or species initial concentration) over `num_steps` values from start_val to end_val,
        both included. The model is loaded once; the parameter and target species are validated before any point
        runs; every point integrates `duration` seconds (None: the model-scaled default horizon) from the model's
        initial conditions with only the scanned value changed. A point whose integration stops before the end time
        is a failed point, never a response (SCI-002). Point failures are reported individually; nothing is rounded.
        """
        t0 = time.perf_counter()
        clean_id = str(biomodel_id or "").strip()
        tool = "vivarix_parameter_scan"
        try:
            if not sbml_path:
                _validate_model_id(clean_id)
            if duration is not None and not (math.isfinite(float(duration)) and float(duration) > 0):
                raise SimulationValidationError("DURATION_INVALID", f"Duration must be a positive finite number of seconds, got {duration!r}")
            points = self.scan_values(float(start_val), float(end_val), int(num_steps), spacing)
            if not sbml_path:
                self._prefetch_model(clean_id)
        except SimulationValidationError as ve:
            return self._validation_failure(tool, clean_id, ve, t0)

        copasi_model = None
        try:
            with _copasi_session():
                try:
                    copasi_model, prov = self._resolve_model(clean_id, sbml_path, model_label, model_provenance)
                    units = _model_units(copasi_model)
                    seconds_per_unit = _seconds_per_time_unit(units["time"])
                    resolved = self._resolve_parameter(copasi_model, parameter_name, clean_id)
                    species_names = [str(s) for s in basico.get_species(model=copasi_model).index]
                    if target_species and target_species not in species_names:
                        raise SimulationValidationError(
                            "UNKNOWN_SPECIES", f"Target species '{target_species}' not found in model {clean_id}"
                        )
                    model_duration, duration_seconds, duration_source = _model_horizon(duration, seconds_per_unit, units["time"])
                except SimulationValidationError as ve:
                    return self._validation_failure(tool, clean_id, ve, t0)

                results: List[Dict[str, Any]] = []
                failed_points: List[Dict[str, Any]] = []
                for idx, val in enumerate(points):
                    try:
                        self._apply_value(copasi_model, resolved, val)
                        applied_value = self._read_value(copasi_model, resolved)
                        # Every point starts from the model's initial state and must reach the end time.
                        tc = _integrate(copasi_model, duration=model_duration, intervals=intervals, time_unit=units["time"])
                        species_cols = [str(c) for c in tc.columns if c != "Time"]
                        finals = {c: float(tc[c].iloc[-1]) for c in species_cols}
                        dyn = _endpoint_dynamics(tc, species_cols, seconds_per_unit)
                        point: Dict[str, Any] = {
                            "index": idx, "parameter_value": val, "applied_value": applied_value,
                            "convergence_status": dyn.get("convergence_status"),
                        }
                        if target_species:
                            point["target_response"] = finals[target_species]
                        if report_all_species or not target_species:
                            point["endpoint_species"] = finals
                        results.append(point)
                    except IncompleteIntegrationError as inc:
                        failed_points.append({
                            "index": idx, "parameter_value": val, "error": str(inc)[:300],
                            "reason_code": "INTEGRATION_INCOMPLETE", "reached_time": inc.reached_time,
                        })
                    except Exception as pe:  # noqa: BLE001
                        failed_points.append({"index": idx, "parameter_value": val, "error": str(pe)[:300]})
                solver = _solver_settings(copasi_model)
                # Restore the original value before the model is released (it is freed anyway; keeps logs honest).
                if resolved.get("previous_value") is not None:
                    with contextlib.suppress(Exception):
                        self._apply_value(copasi_model, resolved, float(resolved["previous_value"]))

            base = {
                "model_id": clean_id,
                "model_name": prov.get("name"),
                "simulation_type": "PARAMETER_SCAN",
                "parameter_name": parameter_name,
                "resolved_parameter": resolved["resolved"],
                "parameter_kind": resolved["kind"],
                "baseline_value": resolved.get("previous_value"),
                "target_species": target_species,
                "requested": {"start": float(start_val), "end": float(end_val), "num_steps": int(num_steps), "spacing": spacing},
                "duration": model_duration,
                "duration_seconds": duration_seconds,
                "duration_source": duration_source,
                "intervals": intervals,
                "units": dict(units),
                "concentration_unit": units["concentration"],
                "solver_method": _solver_label(solver),
                "solver_settings": solver,
                "initial_conditions": "model initial values at every point",
                "scan_results": results,
                "failed_points": failed_points,
                "total_points": len(points),
                "successful_points": len(results),
                "partial_failure": bool(failed_points),
                "model_version": prov.get("model_version"),
            }
            base["provenance"] = prov
            if not results:
                return ToolResult(
                    success=False,
                    tool_name=tool,
                    error=(
                        f"All {len(points)} simulation points failed in parameter scan for {clean_id} "
                        f"(parameter '{parameter_name}'). Errors: {[f['error'] for f in failed_points][:3]}"
                    ),
                    error_type=ErrorType.SIMULATION_FAILED,
                    data=base,
                    canonical_ids={"biomodel_id": clean_id},
                    execution_time_seconds=time.perf_counter() - t0,
                    metadata={"total_points": len(points), "failed_points": failed_points},
                )
            return ToolResult(
                success=True,
                tool_name=tool,
                data=base,
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
                metadata={"adapter": "LOCAL_COMPUTE"},
            )
        except SolverBusyError as busy:
            return self._busy_failure(tool, clean_id, busy, t0)
        except Exception as e:
            return ToolResult(
                success=False,
                tool_name=tool,
                error=str(e),
                error_type=ErrorType.SIMULATION_FAILED,
                canonical_ids={"biomodel_id": clean_id},
                execution_time_seconds=time.perf_counter() - t0,
            )
        finally:
            _release_datamodel(copasi_model)
