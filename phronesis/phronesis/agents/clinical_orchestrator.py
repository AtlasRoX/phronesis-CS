"""
Intelligent Clinical R&D Orchestrator Engine for Phronesis.
Authoritative bridge delegating to the unified AuthoritativeOrchestrator:
- Biomni & ReAct unified multi-specialist coordination
- LLMCompiler dependency-aware DAG scheduling with bounded concurrency
- PaperQA2 grounded literature synthesis via Lithoscribe
- COPASI / BASICO executable kinetic ODE modeling via Vivarix
- CellAgent single-cell transcriptomics analytics via Lumicell
- Reflexion deterministic verification and bounded correction
- Mem0 isolated persistent memory with true BM25 retrieval
- BioDiscoveryAgent evidence-backed hypothesis prioritization
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional
from phronesis.utils.llm.llm_factory import DEFAULT_ORCHESTRATOR_MODEL


class ClinicalOrchestrationResult:
    """Structured result returned by the orchestrator."""

    def __init__(
        self,
        response: str,
        thoughts: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        selected_agents: list[str],
        duration_seconds: float,
        model_used: str,
        framework_used: str = "authoritative-react-compiler",
    ) -> None:
        self.response = response
        self.thoughts = thoughts
        self.steps = steps
        self.selected_agents = selected_agents
        self.duration_seconds = duration_seconds
        self.model_used = model_used
        self.framework_used = framework_used

    def to_dict(self) -> dict[str, Any]:
        return {
            "response": self.response,
            "thoughts": self.thoughts,
            "steps": self.steps,
            "selected_agents": self.selected_agents,
            "duration_seconds": round(self.duration_seconds, 2),
            "model_used": self.model_used,
            "framework_used": self.framework_used,
        }


def sanitize_agent_names_and_content(text: str) -> str:
    """
    Ensures all legacy agent and project names are replaced with official
    Phronesis multi-agent system names, and strips unselected empty placeholders.
    """
    if not text:
        return text

    legacy_mappings = [
        (r"(?i)\bTalk2KnowledgeGraphs?\b", "Synapta"),
        (r"(?i)\bTalk2BioModels?\b", "Vivarix"),
        (r"(?i)\bTalk2Cells?\b", "Lumicell"),
        (r"(?i)\bTalk2Scholars?\b", "Lithoscribe"),
        (r"(?i)\bTalk2AI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b", "Phronesis"),
        (r"(?i)\bAI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b", "Phronesis"),
    ]
    for pattern, repl in legacy_mappings:
        text = re.sub(pattern, repl, text)

    # Strip empty placeholder sections
    text = re.sub(
        r"(?i)(?:^|\n)#{1,4}\s*[^#\n]*(?:Synapta|Vivarix|Lumicell|Lithoscribe)[^\n]*\n+No domain-specific findings were collected[^\n]*\n*",
        "\n",
        text,
    )

    # Strip robotic boilerplate disclaimers at the end of responses
    text = re.sub(
        r"(?i)(?:^|\n)\*?_?All agent references follow the official naming convention[^\n]*\*?_?",
        "",
        text,
    )

    return text.strip()


async def run_orchestration(
    query: str,
    thread_id: str,
    model_name: Optional[str] = None,
    framework: str = "authoritative-react-compiler",
    enable_web_search: bool = True,
    api_key: Optional[str] = None,
    event_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
    user_id: str = "default_user",
) -> ClinicalOrchestrationResult:
    """
    Executes authoritative clinical investigation connecting Synapta, Vivarix,
    Lumicell, Lithoscribe, and WebSurfer with empirical evidence and Reflexion verification.
    """
    from ...agents.authoritative_orchestrator import get_authoritative_orchestrator

    selected_model = model_name or DEFAULT_ORCHESTRATOR_MODEL
    orch = get_authoritative_orchestrator()

    res = await orch.execute_inquiry(
        query=query,
        thread_id=thread_id,
        model_name=selected_model,
        enable_web_search=enable_web_search,
        api_key=api_key,
        event_callback=event_callback,
        user_id=user_id,
    )

    return ClinicalOrchestrationResult(
        response=res.response,
        thoughts=res.thoughts,
        steps=res.steps,
        selected_agents=res.selected_agents,
        duration_seconds=res.duration_seconds,
        model_used=res.model_used,
        # Report the framework that actually ran; `framework` is a caller preference only.
        framework_used=res.framework_used or framework,
    )
