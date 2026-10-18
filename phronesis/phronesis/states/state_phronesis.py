"""
This is the state file for the Phronesis agent.
"""

from ...vivarix.states.state_vivarix import Vivarix
from ...lumicell.states.state_lumicell import Lumicell
from ...synapta.states.state_synapta import (
    Synapta,
)
from ...lithoscribe.state.state_lithoscribe import Lithoscribe


class Phronesis(
    Vivarix,
    Synapta,
    Lumicell,
    Lithoscribe,
):
    """
    The state for the Phronesis agent.

    This class inherits from the classes:
    1. Vivarix
    2. Synapta
    3. Lumicell
    4. Lithoscribe
    """
