"""
Agentic layer over the Phronesis pipeline: frameworks, budget, run control (human in the loop), phase
runtime and the shared state phases read and write. Feature phases live in sibling modules and register
themselves through phronesis.extensions.agentic.
"""

from .budget import BudgetExhausted, RunBudget
from .config import AGENTIC, AGENTIC_FRAMEWORKS, ALL_FRAMEWORKS, AUTO, CLASSIC, DEBATE, SUPERVISOR, SWARM, is_agentic, resolve_framework
from .control import ApprovalDecision, RunCancelled, RunControl, create_control, get_control, release_control
from .runtime import AgenticRuntime, AgentPhase, PhaseOutcome, get_phases, register_phase, unregister_phase
from .state import AgentState, SubQuestion

__all__ = [
    "AGENTIC",
    "AGENTIC_FRAMEWORKS",
    "ALL_FRAMEWORKS",
    "AUTO",
    "CLASSIC",
    "DEBATE",
    "SUPERVISOR",
    "SWARM",
    "AgenticRuntime",
    "AgentPhase",
    "AgentState",
    "ApprovalDecision",
    "BudgetExhausted",
    "PhaseOutcome",
    "RunBudget",
    "RunCancelled",
    "RunControl",
    "SubQuestion",
    "create_control",
    "get_control",
    "get_phases",
    "is_agentic",
    "register_phase",
    "release_control",
    "resolve_framework",
    "unregister_phase",
]
