"""
EvidenceSource protocol: a pluggable Stage 1 evidence gatherer (registry, database, API) that the
orchestrator runs next to Synapta, Lithoscribe and WebSurfer, and that contributes its own evidence
XML, identifiers, citations, artifact, fallback text and scope notes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from ..agents.query_context import QueryContext, SourceDecision

if TYPE_CHECKING:
    from ..tools.registry import ToolResult

logger = logging.getLogger(__name__)


class CitationRecord(BaseModel):
    type: str  # pmid|doi|nct|chembl|biomodel|ensembl|uniprot|kegg|clinvar|rsid|civic|geo|url|dailymed
    id: str
    title: Optional[str] = None
    url: Optional[str] = None
    passage: Optional[str] = None
    section: Optional[str] = None
    authors: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[str] = None
    evidence_level: Optional[str] = None
    retracted: Optional[bool] = None
    source_agent: str = ""
    source_tool: str = ""


class SourceRunState:
    """Per-inquiry shared memo so several sources can await one upstream call (e.g. a ChEMBL resolve), and so a
    source can use another source's result (source_result) without running it twice."""

    def __init__(self, registry: Any, ctx: QueryContext) -> None:
        self.registry = registry
        self.ctx = ctx
        self._tasks: Dict[str, "asyncio.Task[Any]"] = {}

    async def once(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """The first caller starts factory() as a task; every caller awaits that same task's result."""
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._tasks[key] = task
        return await asyncio.shield(task)

    async def run_source_once(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """One run() per source per inquiry (see share_source_run). The caller that starts it awaits the task
        directly, so cancelling that caller (a stopped turn) cancels the run; later callers share its result."""
        memo = f"source:{key}"
        task = self._tasks.get(memo)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._tasks[memo] = task
            return await task
        return await asyncio.shield(task)

    async def source_result(
        self, key: str, *, requester: Optional["EvidenceSource"] = None, timeout_s: Optional[float] = None
    ) -> Optional["ToolResult"]:
        """The result of another registered stage-1 source that runs this turn, awaited without running it a
        second time (the orchestrator runs all stage-1 sources concurrently, so this is how a source can depend
        on an earlier one). None when that source does not run this turn, is not registered, is not ordered
        before `requester` (dependencies must point to a lower `order`, so they cannot form a cycle), raised,
        or did not finish within timeout_s. Waiting never cancels the other source's run."""
        if not self.ctx.should_run(key):
            return None
        from . import get_registered_source

        src = get_registered_source(key)
        if src is None or (requester is not None and (src.order, src.key) >= (requester.order, requester.key)):
            return None
        fut = asyncio.ensure_future(src.run(self.ctx, self.registry, self))
        try:
            if timeout_s is None:
                return await asyncio.shield(fut)
            return await asyncio.wait_for(asyncio.shield(fut), timeout_s)
        except asyncio.TimeoutError:
            logger.warning("Source %s did not finish within %ss for a dependent source", key, timeout_s)
            return None
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if fut.cancelled() and current is not None and not current.cancelling():
                return None  # the other source's run was cancelled, not this one
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s failed for a dependent source: %s", key, exc)
            return None


def share_source_run(src: "EvidenceSource") -> None:
    """Make src.run() run once per inquiry (per SourceRunState), so the orchestrator's own call and any
    dependent source's source_result() share one run. Applied by register_source; idempotent."""
    if getattr(src.run, "_shared_source_run", False):
        return
    inner = src.run

    async def run(ctx: QueryContext, registry: Any, state: SourceRunState) -> "ToolResult":
        if isinstance(state, SourceRunState) and state.ctx is ctx:
            return await state.run_source_once(src.key, lambda: inner(ctx, registry, state))
        return await inner(ctx, registry, state)

    run._shared_source_run = True  # type: ignore[attr-defined]
    src.run = run  # type: ignore[method-assign]


class EvidenceSource:
    key: str = ""  # tool_results key and step id, e.g. "clinical_trials"
    agent: str = ""  # Phronesis agent label, e.g. "Lithoscribe"
    action: str = ""  # steps[] action text
    required_tools: Tuple[str, ...] = ()  # skipped (not an error) when any is not registered
    stage: int = 1  # only stage 1 is supported
    order: int = 100  # ordering for the run and the evidence block

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        return SourceDecision(run=False, reason="not applicable")

    def running_title(self, ctx: QueryContext) -> str:
        return f"Running {self.key}"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> "ToolResult":
        raise NotImplementedError

    def describe(self, result: "ToolResult", ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return f"{self.key} failed", f"{err_type}: {str(result.error or '')[:240]}"
        return f"{self.key} finished", ""

    def evidence_xml(self, result: "ToolResult") -> str:
        """Only called for successful results; must escape every value."""
        return ""

    def allowed_identifiers(self, result: "ToolResult") -> Dict[str, Set[str]]:
        """family name -> ids this result legitimately contains (merged into the verifier's observed sets)."""
        return {}

    def citations(self, result: "ToolResult") -> List[CitationRecord]:
        return []

    def artifact(self, result: "ToolResult") -> Optional[Tuple[str, Any]]:
        """(execution_artifacts key, compact JSON-safe value); dict values for one key are shallow-merged."""
        return None

    def fallback_markdown(self, result: "ToolResult") -> str:
        return ""

    def scope_notes(self, ctx: QueryContext, result: Optional["ToolResult"]) -> List[str]:
        """result is None when the source did not run."""
        return []

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        return None
