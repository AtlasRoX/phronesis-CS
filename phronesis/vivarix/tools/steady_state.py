#!/usr/bin/env python3

"""
Tool for parameter scan.
"""

import logging
from typing import Annotated

import basico
import numpy as np
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from .load_arguments import ArgumentData, add_rec_events
from .load_biomodel import ModelData, load_biomodel

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# basico.run_steadystate return codes
STEADY_STATE_NOT_FOUND = 0
STEADY_STATE_FOUND = 1
STEADY_STATE_EQUILIBRIUM = 2
STEADY_STATE_NEGATIVE = 3
# Largest eigenvalue real part still treated as stable (numerical noise around zero)
STABILITY_TOLERANCE = 1e-9


def steady_state_eigenvalues(copasi_model) -> np.ndarray | None:
    """Eigenvalues of the reduced Jacobian at the current state, or None if unavailable."""
    try:
        jacobian = basico.model_info.get_reduced_jacobian_matrix(model=copasi_model)
    except Exception as exc:  # noqa: BLE001 - not every model exposes a reduced Jacobian
        logger.warning("Reduced Jacobian unavailable: %s", exc)
        return None
    if jacobian is None or getattr(jacobian, "size", 0) == 0:
        return None
    return np.linalg.eigvals(np.asarray(jacobian, dtype=float))


def check_steady_state(output: int, copasi_model) -> np.ndarray | None:
    """
    Reject steady states that were not found, have negative concentrations, or are unstable.

    Returns the Jacobian eigenvalues (None when they could not be computed).
    """
    if output == STEADY_STATE_NOT_FOUND:
        logger.error("Steady state analysis failed")
        raise ValueError("A steady state was not found")
    if output == STEADY_STATE_NEGATIVE:
        raise ValueError(
            "COPASI found a steady state with negative concentrations, which is not physical"
        )
    eigenvalues = steady_state_eigenvalues(copasi_model)
    if eigenvalues is not None and np.max(eigenvalues.real) > STABILITY_TOLERANCE:
        raise ValueError(
            "The steady state found is unstable (largest eigenvalue real part "
            f"{np.max(eigenvalues.real):.3g} > 0); the system may oscillate or diverge"
        )
    return eigenvalues


def run_steady_state(model_object, dic_species_to_be_analyzed_before_experiment):
    """
    Run the steady state analysis.

    Args:
        model_object: The model object.
        dic_species_to_be_analyzed_before_experiment: Dictionary of species data.

    Returns:
        DataFrame: The results of the steady state analysis.
    """
    # Update the fixed model species and parameters
    # These are the initial conditions of the model
    # set by the user
    model_object.update_parameters(dic_species_to_be_analyzed_before_experiment)
    logger.log(logging.INFO, "Running steady state analysis")
    # Run the steady state analysis
    output = basico.task_steadystate.run_steadystate(model=model_object.copasi_model)
    check_steady_state(output, model_object.copasi_model)
    logger.log(logging.INFO, "Steady state analysis successful")
    # Store the steady state results in a DataFrame
    df_steady_state = basico.model_info.get_species(model=model_object.copasi_model).reset_index()
    # Rename the column name to species_name
    df_steady_state.rename(columns={"name": "species_name"}, inplace=True)
    # Rename the column concentration to steady_state_concentration
    df_steady_state.rename(columns={"concentration": "steady_state_concentration"}, inplace=True)
    # Rename the column transition_time to steady_state_transition_time
    df_steady_state.rename(
        columns={"transition_time": "steady_state_transition_time"}, inplace=True
    )
    # Drop some columns
    df_steady_state.drop(
        columns=[
            "initial_particle_number",
            "initial_expression",
            "expression",
            "particle_number",
            "type",
            "particle_number_rate",
            "key",
            "sbml_id",
            "display_name",
        ],
        inplace=True,
        errors="ignore",
    )
    logger.log(logging.INFO, "Steady state results with shape %s", df_steady_state.shape)
    return df_steady_state


class SteadyStateInput(BaseModel):
    """
    Input schema for the steady state tool.
    """

    sys_bio_model: ModelData = Field(description="model data", default=None)
    arg_data: ArgumentData = Field(
        description="time, species, and reocurring data"
        " that must be set before the steady state analysis"
        " as well as the experiment name",
        default=None,
    )
    tool_call_id: Annotated[str, InjectedToolCallId]
    state: Annotated[dict, InjectedState]


# Note: It's important that every field has type hints. BaseTool is a
# Pydantic class and not having type hints can lead to unexpected behavior.
class SteadyStateTool(BaseTool):
    """
    Tool to bring a model to steady state.
    """

    name: str = "steady_state"
    description: str = "A tool to bring a model to steady state."
    args_schema: type[BaseModel] = SteadyStateInput

    def _run(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[dict, InjectedState],
        sys_bio_model: ModelData = None,
        arg_data: ArgumentData = None,
    ) -> Command:
        """
        Run the tool.

        Args:
            tool_call_id (str): The tool call ID. This is injected by the system.
            state (dict): The state of the tool.
            sys_bio_model (ModelData): The model data.
            arg_data (ArgumentData): The argument data.

        Returns:
            Command: The updated state of the tool.
        """
        logger.log(
            logging.INFO,
            "Calling the steady_state tool %s, %s",
            sys_bio_model,
            arg_data,
        )
        sbml_paths = state.get("sbml_file_path") or []
        sbml_file_path = sbml_paths[-1] if sbml_paths else None
        sys_bio_model = sys_bio_model or ModelData()
        experiment_name = getattr(arg_data, "experiment_name", None) or "steady_state"
        model_object = load_biomodel(sys_bio_model, sbml_file_path=sbml_file_path)
        if model_object is None:
            raise ValueError("Steady state analysis needs a BioModels id or an uploaded SBML file")
        # Prepare the dictionary of species data
        # that will be passed to the simulate method
        # of the BasicoModel class
        dic_species_to_be_analyzed_before_experiment = {}
        if arg_data:
            # Prepare the dictionary of species data
            if arg_data.species_to_be_analyzed_before_experiment is not None:
                dic_species_to_be_analyzed_before_experiment = dict(
                    zip(
                        arg_data.species_to_be_analyzed_before_experiment.species_name,
                        arg_data.species_to_be_analyzed_before_experiment.species_concentration,
                        strict=False,
                    )
                )
            # Add reocurring events (if any) to the model
            if arg_data.reocurring_data is not None:
                add_rec_events(model_object, arg_data.reocurring_data)
        # Run the parameter scan
        df_steady_state = run_steady_state(
            model_object, dic_species_to_be_analyzed_before_experiment
        )
        logger.debug("Steady state results: %s", df_steady_state)
        # Prepare the dictionary of scanned data
        # that will be passed to the state of the graph
        dic_steady_state_data = {
            "name": experiment_name,
            "source": (sys_bio_model.biomodel_id if sys_bio_model.biomodel_id else "upload"),
            "tool_call_id": tool_call_id,
            "data": df_steady_state.to_dict(orient="records"),
        }
        # Prepare the dictionary of updated state for the model
        dic_updated_state_for_model = {}
        for key, value in {
            "model_id": [sys_bio_model.biomodel_id],
            "sbml_file_path": [sbml_file_path],
            "dic_steady_state_data": [dic_steady_state_data],
        }.items():
            if value:
                dic_updated_state_for_model[key] = value
        # Return the updated state
        return Command(
            update=dic_updated_state_for_model
            | {
                # Update the message history
                "messages": [
                    ToolMessage(
                        content=f"Steady state analysis of {experiment_name} was successful.",
                        tool_call_id=tool_call_id,
                        artifact={"dic_data": df_steady_state.to_dict(orient="records")},
                    )
                ],
            }
        )
