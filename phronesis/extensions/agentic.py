"""
Extension module for the agentic layer: imports every agent-phase module so each registers its phases.

A phase module that fails to import is logged and skipped; the others still load. Phases only run for
agentic frameworks (phronesis.agentic.config), so the classic pipeline is unaffected by anything here.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import List

logger = logging.getLogger(__name__)

AGENT_PHASE_MODULES = (
    "phronesis.agentic.clarify",  # 15 human in the loop: clarifying questions (before_evidence)
    "phronesis.agentic.project_memory",  # 16 project memory recall (before_evidence) and consolidation (finalize)
    "phronesis.agentic.decompose",  # 3 question decomposition (before_evidence)
    "phronesis.agentic.approval",  # 15 plan approval (approve)
    "phronesis.agentic.plan_execute",  # 1 LLM-planned tool DAG (augment; agentic + debate)
    "phronesis.agentic.topologies",  # 4 supervisor / swarm (augment) and debate (after_draft)
    "phronesis.agentic.router",  # Auto escalation to the critic debate (after_draft, before the debate)
    "phronesis.agentic.hypothesis",  # 6 hypothesis -> in-silico experiment loop (augment)
    "phronesis.agentic.analyst",  # 10 sandboxed code analysis of attached data (augment)
    "phronesis.agentic.calibrate",  # 11 SBML parameter estimation against attached data (augment)
    "phronesis.agentic.counter_evidence",  # 7 contradiction search (augment)
    "phronesis.agentic.nli",  # 8 NLI entailment judge for the verifier (augment)
    "phronesis.agentic.gaps",  # 2 gap-driven re-retrieval (after_draft)
    "phronesis.agentic.confidence",  # 9 calibrated per-claim confidence (finalize)
)

LOADED: List[ModuleType] = []

for _name in AGENT_PHASE_MODULES:
    try:
        LOADED.append(importlib.import_module(_name))
    except ModuleNotFoundError as _exc:
        if _exc.name == _name:
            logger.debug("Agent phase module %s not present", _name)
        else:
            logger.warning("Agent phase module %s failed to load: %s", _name, _exc)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("Agent phase module %s failed to load: %s", _name, _exc)
