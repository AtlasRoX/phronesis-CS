"""Extension module owned by WP4 (feature 11, SBML model calibration; helper tool for feature 6 hypotheses).

Registers two LOCAL_COMPUTE registry tools:
- vivarix_calibrate_parameters: least-squares parameter estimation of a BioModels or uploaded SBML model against
  an attached time-course table, with profile-likelihood identifiability (phronesis.vivarix.calibration; COPASI
  doi:10.1093/bioinformatics/btl485, basico doi:10.21105/joss.05553, Raue 2009 doi:10.1093/bioinformatics/btp358).
- vivarix_list_parameters: the species, parameters and reaction schemes of a model, so agent phases can check
  that a parameter exists before proposing or running a perturbation.

Data files are accepted only from the upload store: the path must be the original table of an uploaded dataset
(<upload root>/<user>/ds_<id>/original.csv|tsv next to its manifest). Phases obtain it from a resolved attachment
(attachment_table_path); a path typed into a question or invented by a model is rejected.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

CALIBRATE_TOOL = "vivarix_calibrate_parameters"
LIST_TOOL = "vivarix_list_parameters"
CALIBRATE_TIMEOUT_SECONDS = 300.0
# The calibration returns its best point before the registry's hard timeout cuts it off.
_INNER_TIMEOUT_SECONDS = 270.0

_DATASET_DIR_RE = re.compile(r"^ds_[0-9a-f]{12}$")
_TABLE_NAMES = ("original.csv", "original.tsv")
_VALIDATION_CODES = {
    "NO_PARAMETERS", "TOO_MANY_PARAMETERS", "METHOD_UNSUPPORTED", "DATA_REQUIRED", "DATA_UNREADABLE", "DATA_TOO_LARGE",
    "DATA_NO_TIME", "DATA_BAD_TIME", "DATA_NO_SPECIES", "DATA_NO_MODEL_SPECIES", "DATA_TOO_SMALL", "BOUNDS_INVALID",
    "MODEL_REQUIRED", "INVALID_MODEL_ID", "UNKNOWN_PARAMETER", "AMBIGUOUS_PARAMETER", "DUPLICATE_PARAMETER",
    "UNSUPPORTED_TIME_UNIT", "DATA_PATH_REJECTED",
}

_MODEL_ID = r"^(?:BIOMD\d{10}|MODEL\d{10}|ds_[0-9a-f]{12})$"


class VivarixCalibrateInput(BaseModel):
    biomodel_id: Optional[str] = Field(default=None, pattern=_MODEL_ID, description="BioModels accession or uploaded model id")
    sbml_path: Optional[str] = Field(default=None, description="server path of an uploaded SBML model (from the attachment)")
    data_path: str = Field(..., min_length=1, max_length=1024, description="server path of an attached time-course table")
    parameter_names: List[str] = Field(..., min_length=1, max_length=5, description="model parameters or species to fit")
    bounds: Optional[Dict[str, List[float]]] = Field(default=None, max_length=5, description="name -> [lower, upper] (> 0)")
    profile: bool = True
    profile_params: Optional[List[str]] = Field(default=None, max_length=3)
    profile_points: int = Field(default=7, ge=3, le=7)
    max_nfev: int = Field(default=200, ge=5, le=400)
    n_starts: int = Field(default=1, ge=1, le=5)
    data_time_unit: Optional[str] = Field(default=None, max_length=16, description="unit of the data's time column (default: the model's)")
    model_label: Optional[str] = Field(default=None, max_length=200)
    dataset_label: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _needs_model(self) -> "VivarixCalibrateInput":
        if not (self.biomodel_id or self.sbml_path):
            raise ValueError("give biomodel_id or sbml_path")
        return self


class VivarixListParametersInput(BaseModel):
    biomodel_id: Optional[str] = Field(default=None, pattern=_MODEL_ID)
    sbml_path: Optional[str] = None
    model_label: Optional[str] = Field(default=None, max_length=200)
    limit: int = Field(default=200, ge=1, le=500)

    @model_validator(mode="after")
    def _needs_model(self) -> "VivarixListParametersInput":
        if not (self.biomodel_id or self.sbml_path):
            raise ValueError("give biomodel_id or sbml_path")
        return self


# -----------------------------------------------------------------------------
# Attachment paths
# -----------------------------------------------------------------------------

def _upload_root() -> str:
    from ..datasets.store import default_upload_root

    return str(default_upload_root())


def is_attachment_table_path(path: Optional[str]) -> bool:
    """True only for the original table of an uploaded dataset inside the upload store."""
    from ..datasets.paths import _is_within

    if not path or "\x00" in str(path):
        return False
    try:
        real = os.path.realpath(str(path))
    except (OSError, ValueError):
        return False
    if not _is_within(real, _upload_root()) or not os.path.isfile(real):
        return False
    parent = os.path.dirname(real)
    return (
        os.path.basename(real).lower() in _TABLE_NAMES
        and bool(_DATASET_DIR_RE.match(os.path.basename(parent)))
        and os.path.isfile(os.path.join(parent, "manifest.json"))
    )


def attachment_table_path(att: Any) -> Optional[str]:
    """The uploaded original table (CSV/TSV) of a resolved 'table' attachment, or None."""
    if att is None or getattr(att, "kind", None) != "table":
        return None
    analysis = getattr(att, "analysis_path", None)
    if not analysis:
        return None
    folder = os.path.dirname(str(analysis))
    for name in _TABLE_NAMES:
        candidate = os.path.join(folder, name)
        if is_attachment_table_path(candidate):
            return candidate
    return None


def _is_attachment_model_path(path: Optional[str]) -> bool:
    from ..datasets.paths import _is_within

    if not path:
        return True
    try:
        real = os.path.realpath(str(path))
    except (OSError, ValueError):
        return False
    return _is_within(real, _upload_root()) and os.path.isfile(real)


# -----------------------------------------------------------------------------
# Executors
# -----------------------------------------------------------------------------

def _failure(tool: str, message: str, code: str, data: Optional[Dict[str, Any]] = None) -> Any:
    from ..tools.registry import ErrorType, ToolResult

    if code == "SOLVER_BUSY":
        etype = ErrorType.TIMEOUT
    elif code in _VALIDATION_CODES:
        etype = ErrorType.VALIDATION_ERROR
    else:
        etype = ErrorType.SIMULATION_FAILED
    return ToolResult(
        success=False, tool_name=tool, error=message, error_type=etype, data=data,
        metadata={"adapter": "LOCAL_COMPUTE", "validation_code": code},
    )


def run_calibration_tool(**kwargs: Any) -> Any:
    from ..tools.registry import ToolResult
    from ..vivarix.calibration import calibrate_parameters

    data_path = kwargs.get("data_path")
    if not is_attachment_table_path(data_path):
        return _failure(CALIBRATE_TOOL, "The data file must be an attached (uploaded) CSV/TSV table.", "DATA_PATH_REJECTED")
    if not _is_attachment_model_path(kwargs.get("sbml_path")):
        return _failure(CALIBRATE_TOOL, "The SBML model must be an uploaded model file.", "DATA_PATH_REJECTED")
    result = calibrate_parameters(
        parameter_names=list(kwargs.get("parameter_names") or []),
        biomodel_id=kwargs.get("biomodel_id"),
        sbml_path=kwargs.get("sbml_path"),
        data_path=str(data_path),
        bounds=kwargs.get("bounds"),
        profile=bool(kwargs.get("profile", True)),
        profile_params=kwargs.get("profile_params"),
        profile_points=int(kwargs.get("profile_points") or 7),
        max_nfev=int(kwargs.get("max_nfev") or 200),
        n_starts=int(kwargs.get("n_starts") or 1),
        timeout_seconds=_INNER_TIMEOUT_SECONDS,
        model_label=kwargs.get("model_label"),
        dataset_label=kwargs.get("dataset_label") or "attached table",
        data_time_unit=kwargs.get("data_time_unit"),
    )
    if result.get("status") == "error":
        return _failure(CALIBRATE_TOOL, str(result.get("error") or "calibration failed"), str(result.get("error_code") or ""), result)
    return ToolResult(
        success=True,
        tool_name=CALIBRATE_TOOL,
        data=result,
        canonical_ids={"biomodel_id": str(result.get("model_id"))} if result.get("model_id") else {},
        metadata={"adapter": "LOCAL_COMPUTE", "backend": result.get("backend"), "status": result.get("status")},
    )


def run_list_parameters_tool(**kwargs: Any) -> Any:
    from ..tools.registry import ToolResult
    from ..vivarix.calibration import CalibrationError, list_model_parameters

    if not _is_attachment_model_path(kwargs.get("sbml_path")):
        return _failure(LIST_TOOL, "The SBML model must be an uploaded model file.", "DATA_PATH_REJECTED")
    try:
        data = list_model_parameters(
            biomodel_id=kwargs.get("biomodel_id"), sbml_path=kwargs.get("sbml_path"),
            model_label=kwargs.get("model_label"), limit=int(kwargs.get("limit") or 200),
        )
    except CalibrationError as exc:
        return _failure(LIST_TOOL, str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001
        code = "SOLVER_BUSY" if type(exc).__name__ == "SolverBusyError" else "MODEL_LOAD_FAILED"
        return _failure(LIST_TOOL, f"{type(exc).__name__}: {str(exc)[:300]}", code)
    return ToolResult(success=True, tool_name=LIST_TOOL, data=data, metadata={"adapter": "LOCAL_COMPUTE"})


def register_tools(registry: Any) -> None:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    registry.register(
        ToolUniverseEntry(
            name=CALIBRATE_TOOL,
            description=(
                "Calibrate (fit / estimate) kinetic parameters of a BioModels or uploaded SBML model against an "
                "attached time-course table by least squares in COPASI/basico, with profile-likelihood confidence "
                "intervals and identifiability (identifiable, practically or structurally non-identifiable)."
            ),
            domain="vivarix",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=VivarixCalibrateInput,
            canonical_id_types=["biomodel_id"],
            timeout_seconds=CALIBRATE_TIMEOUT_SECONDS,
            tags=["calibration", "parameter_estimation", "fitting", "identifiability", "profile_likelihood", "copasi"],
            is_async=False,
        ),
        executor=run_calibration_tool,
    )
    registry.register(
        ToolUniverseEntry(
            name=LIST_TOOL,
            description="List a kinetic model's species, global and reaction parameters and reaction schemes (COPASI/basico).",
            domain="vivarix",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=VivarixListParametersInput,
            canonical_id_types=["biomodel_id"],
            timeout_seconds=60.0,
            tags=["model_info", "parameters", "species", "sbml"],
            is_async=False,
        ),
        executor=run_list_parameters_tool,
    )
