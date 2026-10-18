"""Extension module owned by CITATIONS (feature 4, clickable citations).

Nothing to register: the orchestrator calls phronesis.agents.citations.build_citation_map directly after
verification, and other features feed it through EvidenceSource.citations() or data['citation_records'].
"""

from __future__ import annotations

from typing import Any


def register_tools(registry: Any) -> None:
    return None
