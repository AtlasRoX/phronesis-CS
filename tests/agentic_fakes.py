"""
Offline fakes for agentic tests: a scripted chat model routed by phase marker, and an orchestrator wired to
the recording registry with that model.

Contract: every agentic phase's system prompt starts with "[phase:<key>]" (e.g. "[phase:plan_execute]").
Calls without a marker are the answer synthesis (or the Reflexion correction, which contains
"<audit_defects>" in the user turn). A responder value is a string or fn(system, user) -> str.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from langchain_core.messages import AIMessage

from phronesis.agents import authoritative_orchestrator as orch_module
from tests.fakes import ExtraTools, make_offline_orchestrator

Responder = Union[str, Callable[[str, str], str]]

_PHASE_RE = re.compile(r"^\s*\[phase:([a-z0-9_]+)\]")

DEFAULT_SYNTHESIS = (
    "## Answer\n\nKRAS is associated with pancreatic carcinoma in the retrieved target-disease evidence. "
    "The evidence available for this question is limited to the sources listed below."
)


def phase_of(system: str, user: str = "") -> str:
    m = _PHASE_RE.match(system or "")
    if m:
        return m.group(1)
    if "<audit_defects>" in (user or ""):
        return "correction"
    return "synthesis"


class ScriptedLLM:
    """Chat model stand-in: ainvoke only, so astream_with_reasoning uses its ainvoke fallback."""

    def __init__(self, responses: Dict[str, Responder], default_synthesis: str = DEFAULT_SYNTHESIS) -> None:
        self.responses = dict(responses)
        self.default_synthesis = default_synthesis
        self.calls: List[Tuple[str, str, str]] = []  # (phase, system, user)
        self.temperature = 0.0

    def respond(self, system: str, user: str) -> str:
        phase = phase_of(system, user)
        self.calls.append((phase, system, user))
        value = self.responses.get(phase)
        if value is None:
            if phase in ("synthesis", "correction"):
                return self.default_synthesis
            return "{}"
        return value(system, user) if callable(value) else value

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> AIMessage:
        system = str(getattr(messages[0], "content", "")) if messages else ""
        user = str(getattr(messages[-1], "content", "")) if messages else ""
        return AIMessage(content=self.respond(system, user))

    def phases(self) -> List[str]:
        return [c[0] for c in self.calls]

    def prompts(self, phase: str) -> List[str]:
        return [c[2] for c in self.calls if c[0] == phase]


def make_agentic_orchestrator(
    monkeypatch: Any,
    tmp_path: Any,
    responses: Optional[Dict[str, Responder]] = None,
    extra_tools: Optional[ExtraTools] = None,
    default_synthesis: str = DEFAULT_SYNTHESIS,
) -> Tuple[Any, List[Tuple[str, Dict[str, Any]]], ScriptedLLM]:
    """Offline orchestrator whose model is a ScriptedLLM (use framework='agentic-plan-execute' etc.)."""
    orch, calls = make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=extra_tools)
    llm = ScriptedLLM(responses or {}, default_synthesis=default_synthesis)
    monkeypatch.setattr(orch_module, "get_llm", lambda *a, **k: llm)
    monkeypatch.setenv("PHRONESIS_PLANNER", "off")  # the query planner model stays out of agentic tests
    return orch, calls, llm


async def collect_events(orch: Any, **kwargs: Any) -> Tuple[Any, List[Dict[str, Any]]]:
    events: List[Dict[str, Any]] = []

    async def cb(ev: Dict[str, Any]) -> None:
        events.append(ev)

    res = await orch.execute_inquiry(event_callback=cb, **kwargs)
    return res, events
