#!/usr/bin/env python3

"""
This is the state file for the Lumicell agent.
"""

from langgraph.prebuilt.chat_agent_executor import AgentState


class Lumicell(AgentState):
    """
    The state for the Lumicell agent.
    """

    search_table: str
