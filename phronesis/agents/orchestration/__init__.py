"""Stages of an inquiry run, split out of ``AuthoritativeOrchestrator._execute_inquiry``.

The coordinator calls them in order on one shared :class:`RunContext`:
plan (read the question, route sources) -> gather (evidence and computation tools) -> draft (prompt and first
draft) -> agent_rounds (agentic phases and rewrites) -> verify (verification, revision, claim removal,
citations) -> finalize (memory, task status, result).
"""

from .context import RunContext

__all__ = ["RunContext"]
