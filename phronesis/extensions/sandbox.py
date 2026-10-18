"""
Extension module owned by WP5 (feature 10, sandboxed code analysis). Registers the registry tool
`analysis_run_python` (domain "analysis", LOCAL_COMPUTE).

The tool runs one cell of Python over datasets attached to the current question, through the layered sandbox in
phronesis.agentic.sandbox (AST policy, isolated subprocess, scrubbed environment, network stub, time and memory
limits). It refuses unless PHRONESIS_SANDBOX=on, and it reads only datasets bound to the run by the analyst phase
(SandboxScope): a planner cannot point it at another user's upload or at a server path. CodeAct (Wang et al., ICML
2024, arXiv:2402.01030); the threat model is in docs/agentic/sandbox-security.md.
"""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field, field_validator

TOOL_NAME = "analysis_run_python"


class AnalysisRunInput(BaseModel):
    code: str = Field(..., min_length=1, max_length=20000, description="Python analysis code; set `result` to the answer values")
    dataset_ids: List[str] = Field(default_factory=list, max_length=5, description="Attached dataset ids (ds_...) the code reads")
    timeout_s: int = Field(default=60, ge=5, le=110)

    @field_validator("dataset_ids")
    @classmethod
    def _ids(cls, value: List[str]) -> List[str]:
        from phronesis.agentic.sandbox import DATASET_ID_RE

        bad = [v for v in value if not DATASET_ID_RE.match(str(v))]
        if bad:
            raise ValueError(f"dataset ids must look like ds_<12 hex>: {bad[:3]}")
        return value


def run_analysis(code: str, dataset_ids: List[str], timeout_s: int = 60) -> Any:
    """Executor: resolve the bound attachments, run the cell, and return a ToolResult carrying the run report."""
    from phronesis.agentic.sandbox import current_scope, resolve_inputs, sandbox_enabled, sandbox_memory_mb, sandbox_timeout_s
    from phronesis.agentic.sandbox.runner import run_code
    from phronesis.tools.registry import ErrorType, ToolResult

    if not sandbox_enabled():
        return ToolResult(
            success=False,
            tool_name=TOOL_NAME,
            data={"status": "disabled"},
            error="Code analysis is disabled on this server (set PHRONESIS_SANDBOX=on to enable it).",
            error_type=ErrorType.VALIDATION_ERROR,
        )
    inputs, errors = resolve_inputs(current_scope(), dataset_ids)
    if errors:
        return ToolResult(
            success=False,
            tool_name=TOOL_NAME,
            data={"status": "error", "errors": errors},
            error="; ".join(errors)[:400],
            error_type=ErrorType.DATASET_REQUIRED,
        )
    report = run_code(
        code,
        {i.name: i.path for i in inputs},
        timeout_s=min(int(timeout_s), sandbox_timeout_s()),
        memory_mb=sandbox_memory_mb(),
    )
    report["inputs"] = [{"dataset_id": i.dataset_id, "name": i.name, "kind": i.kind} for i in inputs]
    status = report.get("status")
    if status == "ok":
        return ToolResult(success=True, tool_name=TOOL_NAME, data=report, metadata={"sandbox": "subprocess"})
    error_type = {
        "timeout": ErrorType.TIMEOUT,
        "policy_violation": ErrorType.VALIDATION_ERROR,
    }.get(str(status), ErrorType.INTERNAL_ERROR)
    detail = report.get("error") or status
    if report.get("violations"):
        detail = f"{detail}: " + "; ".join(report["violations"][:5])
    return ToolResult(success=False, tool_name=TOOL_NAME, data=report, error=str(detail)[:600], error_type=error_type)


def register_tools(registry: Any) -> None:
    """Registered only when PHRONESIS_SANDBOX=on, so planners never see a tool that would refuse every call."""
    from phronesis.agentic.sandbox import sandbox_enabled
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    if not sandbox_enabled():
        return
    registry.register(
        ToolUniverseEntry(
            name=TOOL_NAME,
            description=(
                "Run Python analysis code (pandas, numpy, scipy, statsmodels, scikit-learn, matplotlib, anndata, "
                "scanpy) over the CSV/TSV/h5ad datasets attached to this question, in an isolated sandbox with no "
                "network. Set `result` to the computed values; open matplotlib figures are returned as PNG."
            ),
            domain="analysis",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=AnalysisRunInput,
            canonical_id_types=["dataset_id"],
            timeout_seconds=120.0,
            tags=["analysis", "statistics", "code", "sandbox", "pandas", "codeact", "plot"],
        ),
        executor=run_analysis,
    )
