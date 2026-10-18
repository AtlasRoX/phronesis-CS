"""
Orchestration frameworks and agentic runtime settings.

The classic pipeline ("authoritative-react-compiler") runs no agentic phase, so its behaviour and tests are
unchanged. Every other framework runs the registered phases that list it (or all agentic frameworks).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from phronesis.config.settings import FLAG, get_float, get_int, get_str

CLASSIC = "authoritative-react-compiler"
AGENTIC = "agentic-plan-execute"
SUPERVISOR = "multi-agent-supervisor"
SWARM = "multi-agent-swarm"
DEBATE = "multi-agent-debate"
# Not a framework of its own: the router (phronesis/agentic/router.py) picks one of AGENTIC_FRAMEWORKS per question.
AUTO = "auto"

AGENTIC_FRAMEWORKS = (AGENTIC, SUPERVISOR, SWARM, DEBATE)
ALL_FRAMEWORKS = (AUTO, CLASSIC, *AGENTIC_FRAMEWORKS)

FRAMEWORK_DESCRIPTIONS: Dict[str, str] = {
    AUTO: (
        "Reads each question and picks the best architecture for it, then brings in critics to debate the draft "
        "when the evidence conflicts or the question is safety-critical."
    ),
    CLASSIC: "A fixed, predictable 6-stage workflow that systematically gathers evidence, runs simulations, and audits findings.",
    AGENTIC: (
        "Breaks complex questions into steps, runs search & analysis tools in parallel, "
        "and double-checks claims against counter-evidence for maximum accuracy."
    ),
    SUPERVISOR: "A central manager coordinates and delegates tasks to domain specialists (literature, disease targets, simulations, omics).",
    SWARM: "Specialist agents collaborate peer-to-peer, handing control directly to each other until the question is answered.",
    DEBATE: "Multiple agents cross-examine and challenge each other's reasoning and evidence to eliminate bias and ensure rigorous conclusions.",
}

# -----------------------------------------------------------------------------
# Effort / Thinking Levels (analogous to ChatGPT reasoning_effort and Claude extended thinking)
# -----------------------------------------------------------------------------
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_ULTRA = "ultra"
ALL_EFFORT_LEVELS = (EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH, EFFORT_ULTRA)

EFFORT_PROFILES: Dict[str, Dict[str, Any]] = {
    EFFORT_LOW: {
        "level": EFFORT_LOW,
        "label": "Low",
        "badge": "⚡ Fast",
        "short_desc": "Minimal thinking, direct response",
        "description": "Fastest turnaround with direct synthesis, top 4 literature passages, and 1 draft round.",
        "reasoning_effort": "low",
        "thinking_budget_tokens": 1024,
        "max_subqueries": 2,
        "max_passages": 4,
        "max_draft_rounds": 1,
        "max_tool_calls": 15,
        "max_llm_calls": 12,
        "max_seconds": 180.0,
        "enable_thinking": False,
        "deliberation_depth": "minimal",
    },
    EFFORT_MEDIUM: {
        "level": EFFORT_MEDIUM,
        "label": "Medium",
        "badge": "⚖️ Balanced",
        "short_desc": "Standard analytical depth",
        "description": "Balanced deliberation: 2-3 subqueries, standard literature & clinical trials, up to 2 draft rounds.",
        "reasoning_effort": "medium",
        "thinking_budget_tokens": 4096,
        "max_subqueries": 3,
        "max_passages": 8,
        "max_draft_rounds": 2,
        "max_tool_calls": 40,
        "max_llm_calls": 30,
        "max_seconds": 600.0,
        "enable_thinking": True,
        "deliberation_depth": "standard",
    },
    EFFORT_HIGH: {
        "level": EFFORT_HIGH,
        "label": "High",
        "badge": "🧠 Deep",
        "short_desc": "Deep multi-agent deliberation",
        "description": "Deep investigation: 4-5 decomposed subqueries, cross-database search, 3 draft rounds, strict verification.",
        "reasoning_effort": "high",
        "thinking_budget_tokens": 16384,
        "max_subqueries": 5,
        "max_passages": 14,
        "max_draft_rounds": 3,
        "max_tool_calls": 80,
        "max_llm_calls": 60,
        "max_seconds": 1800.0,
        "enable_thinking": True,
        "deliberation_depth": "deep",
    },
    EFFORT_ULTRA: {
        "level": EFFORT_ULTRA,
        "label": "Ultra",
        "badge": "🚀 Ultra / Max",
        "short_desc": "Exhaustive scientific investigation",
        "description": "Exhaustive exploration: full multi-agent orchestration, ODE kinetics, 25+ passages, up to 5 draft rounds.",
        "reasoning_effort": "high",
        "thinking_budget_tokens": 32768,
        "max_subqueries": 8,
        "max_passages": 25,
        "max_draft_rounds": 5,
        "max_tool_calls": 150,
        "max_llm_calls": 100,
        "max_seconds": 3600.0,
        "enable_thinking": True,
        "deliberation_depth": "exhaustive",
    },
}


def get_effort_profile(level: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve the deliberation and budget profile for an effort level (defaults to 'medium')."""
    raw = (level or "").strip().lower()
    if raw in ("max", "highest", "extreme"):
        raw = EFFORT_ULTRA
    elif raw in ("deep",):
        raw = EFFORT_HIGH
    elif raw in ("fast", "minimal", "quick"):
        raw = EFFORT_LOW
    elif raw in ("standard", "balanced", "normal"):
        raw = EFFORT_MEDIUM
    return EFFORT_PROFILES.get(raw, EFFORT_PROFILES[EFFORT_MEDIUM])


def is_agentic(framework: Optional[str]) -> bool:
    """Whether a run gets a run control and agent phases; Auto always routes to an agentic framework."""
    return framework in AGENTIC_FRAMEWORKS or framework == AUTO


def resolve_framework(requested: Optional[str], default: Optional[str] = None) -> str:
    """A known framework id; unknown or empty values fall back to the default (env PHRONESIS_DEFAULT_FRAMEWORK)."""
    value = (requested or "").strip()
    if value in ALL_FRAMEWORKS:
        return value
    fallback = (default or os.getenv("PHRONESIS_DEFAULT_FRAMEWORK", "") or CLASSIC).strip()
    return fallback if fallback in ALL_FRAMEWORKS else CLASSIC


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Integer setting clamped to [lo, hi]; unset, blank or invalid values give the default (phronesis.config)."""
    return get_int(name, default, min=lo, max=hi)


def env_float(name: str, default: float, lo: float, hi: float) -> float:
    """Float setting clamped to [lo, hi]; unset, blank or invalid values give the default (phronesis.config)."""
    return get_float(name, default, min=lo, max=hi)


def env_flag(name: str, default: str = "auto") -> str:
    """off | auto | on for one agentic capability."""
    return get_str(name, default, choices=FLAG)


def agent_model(model_name: str) -> str:
    """The model used for planning/critique calls (PHRONESIS_AGENT_MODEL), else the turn's model."""
    return os.getenv("PHRONESIS_AGENT_MODEL", "").strip() or model_name
