"""
Vivarix: kinetic-model simulation (engine.py, calibration.py) plus the vendored legacy Talk2BioModels LangGraph
stack (agents, api, configs, models, states, tools).

The legacy subpackages are loaded lazily, on first attribute access (PEP 562), so importing the production engine
(`phronesis.vivarix.engine`) never imports the legacy tools or their dependencies (SCI-022).
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_SUBPACKAGES = ("agents", "api", "configs", "models", "states", "tools")

__all__ = ["agents", "api", "configs", "models", "states", "tools"]


def __getattr__(name: str) -> Any:
    if name in _LAZY_SUBPACKAGES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
