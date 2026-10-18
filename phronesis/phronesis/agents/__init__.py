"""
The legacy agents package.

Submodules are imported lazily: `main_agent` pulls in hydra, langgraph-supervisor, Milvus and torch-geometric, so it
is loaded only when it is first used (`phronesis.phronesis.agents.main_agent`), not on every `verifier` import.
"""

import importlib
from typing import Any

_LAZY_SUBMODULES = ("main_agent",)

__all__ = ["main_agent"]


def __getattr__(name: str) -> Any:
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_LAZY_SUBMODULES))
