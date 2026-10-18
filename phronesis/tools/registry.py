"""
Biomedical Tool Registry for Phronesis, inspired by ToolUniverse (2025).
Provides a searchable, validated, and resilient tool registry with canonical entity identifiers,
strict timeouts, typed error states, and health monitoring.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Optional, Type
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AdapterType(str, Enum):
    DIRECT_REST = "DIRECT_REST"
    DIRECT_GRAPHQL = "DIRECT_GRAPHQL"
    LOCAL_COMPUTE = "LOCAL_COMPUTE"
    REMOTE_MCP = "REMOTE_MCP"


class ErrorType(str, Enum):
    TIMEOUT = "TIMEOUT"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    NOT_FOUND = "NOT_FOUND"
    SIMULATION_FAILED = "SIMULATION_FAILED"
    UPSTREAM_API_ERROR = "UPSTREAM_API_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DATASET_REQUIRED = "DATASET_REQUIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolResult(BaseModel):
    """Standardized output model for every biomedical tool execution."""
    success: bool = Field(..., description="Whether tool execution completed successfully without defect")
    tool_name: str = Field(..., description="Name of the executed tool")
    data: Any = Field(default=None, description="Structured output payload or execution artifact")
    canonical_ids: Dict[str, str] = Field(
        default_factory=dict,
        description="Canonical identifiers resolved (e.g. ensembl_id, uniprot_id, biomodel_id, efo_id, pmid, doi)",
    )
    error: Optional[str] = Field(default=None, description="Human-readable error description if failed")
    error_type: Optional[ErrorType] = Field(default=None, description="Typed failure classification")
    execution_time_seconds: float = Field(default=0.0, description="Measured wall-clock execution duration")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Execution metadata: adapter type, cache status, solver diagnostics, provenance",
    )

    def to_summary(self) -> str:
        """Return concise string summary for LLM context or logs."""
        if not self.success:
            return f"[{self.tool_name} FAILED ({self.error_type})]: {self.error}"
        id_str = ", ".join(f"{k}={v}" for k, v in self.canonical_ids.items()) if self.canonical_ids else "None"
        return f"[{self.tool_name} SUCCESS ({self.execution_time_seconds:.2f}s)]: IDs: ({id_str})"


class ToolUniverseEntry(BaseModel):
    """Registry entry metadata for a biomedical tool."""
    name: str = Field(..., description="Unique tool name")
    description: str = Field(..., description="Detailed description of tool capabilities")
    domain: str = Field(..., description="Specialist agent domain: synapta | vivarix | lumicell | lithoscribe | general")
    adapter_type: AdapterType = Field(..., description="Direct API adapter vs Local engine vs MCP")
    input_schema: Type[BaseModel] = Field(..., description="Pydantic input schema class")
    output_schema: Optional[Type[BaseModel]] = Field(default=None, description="Expected structured output schema")
    canonical_id_types: List[str] = Field(default_factory=list, description="IDs handled, e.g. ['ensembl_id', 'uniprot_id']")
    timeout_seconds: float = Field(default=30.0, description="Per-execution timeout cutoff")
    tags: List[str] = Field(default_factory=list, description="Searchable functional tags")
    # True: the executor returns an awaitable (e.g. a lambda wrapping an async client call) and runs on the event
    # loop; False: it is synchronous and runs in a worker thread; None: detected from the callable.
    is_async: Optional[bool] = Field(default=None, description="Executor returns an awaitable")


# Native solvers (COPASI, Scanpy) run in their own bounded pool: a call that outlives its timeout cannot be
# interrupted, so it must not be able to exhaust the default executor that everything else shares.
_NATIVE_POOL: Optional[ThreadPoolExecutor] = None


def _native_pool() -> ThreadPoolExecutor:
    global _NATIVE_POOL
    if _NATIVE_POOL is None:
        workers = max(1, int(os.getenv("PHRONESIS_NATIVE_WORKERS", "4") or 4))
        _NATIVE_POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="phronesis-native")
    return _NATIVE_POOL


_NETWORK_POOL: Optional[ThreadPoolExecutor] = None


def _network_pool() -> ThreadPoolExecutor:
    """The bounded pool for synchronous networked tools (REST, GraphQL, MCP). A call that times out keeps its thread
    until the socket gives up; a dedicated pool keeps such threads from exhausting asyncio's default executor."""
    global _NETWORK_POOL
    if _NETWORK_POOL is None:
        try:
            workers = int(os.getenv("PHRONESIS_NETWORK_WORKERS", "16") or 16)
        except ValueError:
            workers = 16
        _NETWORK_POOL = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="phronesis-net")
    return _NETWORK_POOL


def _must_propagate(exc: BaseException) -> bool:
    """Run-level control exceptions (cancellation, budget) are never turned into a failed ToolResult."""
    try:
        from ..agentic.budget import BudgetExhausted
        from ..agentic.control import RunCancelled
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, (RunCancelled, BudgetExhausted))


def _is_async_callable(fn: Callable[..., Any]) -> bool:
    target = fn
    while isinstance(target, functools.partial):
        target = target.func
    return inspect.iscoroutinefunction(target) or inspect.iscoroutinefunction(getattr(target, "__call__", None))


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class BiomedicalToolRegistry:
    """
    Searchable Tool Registry managing all biomedical tools across Phronesis.
    Validates inputs, injects timeouts, enforces structured errors, and tracks health.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, ToolUniverseEntry] = {}
        self._executors: Dict[str, Callable[..., Any]] = {}

    def register(
        self,
        entry: ToolUniverseEntry,
        executor: Callable[..., Any],
    ) -> None:
        """Register a tool and its executable function."""
        self._tools[entry.name] = entry
        self._executors[entry.name] = executor
        logger.debug("Registered tool in ToolUniverse: %s (%s)", entry.name, entry.domain)

    def get_entry(self, name: str) -> Optional[ToolUniverseEntry]:
        return self._tools.get(name)

    def list_tools(self, domain: Optional[str] = None) -> List[ToolUniverseEntry]:
        """List registered tools, optionally filtered by agent domain."""
        if domain:
            return [t for t in self._tools.values() if t.domain.lower() == domain.lower()]
        return list(self._tools.values())

    def search_tools(self, query: str, domain: Optional[str] = None) -> List[ToolUniverseEntry]:
        """Search registered tools by keyword match across name, description, and tags."""
        q = query.lower()
        candidates = self.list_tools(domain)
        matched = []
        for c in candidates:
            score = 0
            if q in c.name.lower():
                score += 3
            if any(q in t.lower() for t in c.tags):
                score += 2
            if q in c.description.lower():
                score += 1
            if score > 0:
                matched.append((score, c))
        matched.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in matched]

    async def execute(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """
        Execute tool with input validation, timeout enforcement, and typed ToolResult output.
        Every call, whatever its outcome, is reported to the active run recorder.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()
        trace: Dict[str, Any] = {}
        res: Optional[ToolResult] = None
        raw_kwargs = dict(kwargs)
        # Arguments that fail the schema only for a trivially fixable reason (a limit above its bound, too many
        # list items, {"name": ...} where a string is expected) are repaired once; the original error stands
        # when the repair does not validate. The adjustment is recorded on the result.
        # Model-generated calls (agents, plans) pass repair_arguments=True; programmatic callers get strict validation.
        adjusted: List[str] = []
        entry = self._tools.get(tool_name)
        if kwargs.pop("_repair_arguments", False) and entry is not None:
            from .arg_repair import repair_arguments

            repaired = repair_arguments(entry.input_schema, kwargs)
            if repaired is not None:
                kwargs, adjusted = repaired
                logger.info("Repaired arguments for %s: %s", tool_name, "; ".join(adjusted))
        raw_kwargs.pop("_repair_arguments", None)
        try:
            from .reliability import execute_with_policies

            res = await execute_with_policies(
                self, tool_name, kwargs, lambda: self._execute_inner(tool_name, kwargs, trace), trace=trace
            )
            if res is None:
                logger.error("Tool %s returned no result; reporting an internal error", tool_name)
                res = ToolResult(
                    success=False,
                    tool_name=tool_name,
                    error="Tool execution returned no result",
                    error_type=ErrorType.INTERNAL_ERROR,
                    execution_time_seconds=time.perf_counter() - t0,
                    metadata={"registry": "ToolUniverse"},
                )
            if adjusted and isinstance(res, ToolResult):
                res = res.model_copy(update={"metadata": {**(res.metadata or {}), "arguments_adjusted": list(adjusted)}})
        except Exception as exc:  # noqa: BLE001
            if _must_propagate(exc):
                raise
            logger.error("Tool %s failed outside its executor: %s", tool_name, exc, exc_info=True)
            res = ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"{type(exc).__name__}: {exc}",
                error_type=ErrorType.INTERNAL_ERROR,
                execution_time_seconds=time.perf_counter() - t0,
                metadata={"registry": "ToolUniverse"},
            )
        finally:
            try:
                from ..provenance.run_manifest import record_tool_call

                record_tool_call(
                    tool_name=tool_name,
                    entry=trace.get("entry"),
                    inputs=trace.get("validated_kwargs"),
                    raw_inputs=raw_kwargs,
                    result=res,
                    started_at=started_at,
                    elapsed_seconds=time.perf_counter() - t0,
                )
            except Exception:  # noqa: BLE001
                # Provenance must never fail a tool call, but a lost record is worth knowing about.
                logger.warning("Could not record the %s tool call in the run manifest", tool_name, exc_info=True)
        assert res is not None  # set on every path that reaches here (control exceptions propagate instead)
        return res

    async def _execute_inner(self, tool_name: str, kwargs: Dict[str, Any], trace: Dict[str, Any]) -> ToolResult:
        entry = self._tools.get(tool_name)
        executor = self._executors.get(tool_name)
        trace["entry"] = entry

        if not entry or not executor:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is not registered in ToolUniverse",
                error_type=ErrorType.NOT_FOUND,
                metadata={"registry": "ToolUniverse"},
            )

        # 1. Validate inputs via Pydantic schema
        try:
            validated_input = entry.input_schema(**kwargs)
            validated_kwargs = validated_input.model_dump()
            trace["validated_kwargs"] = validated_kwargs
        except Exception as val_err:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"Input validation error: {val_err}",
                error_type=ErrorType.VALIDATION_ERROR,
                metadata={"adapter": entry.adapter_type.value},
            )

        # 2. Execute with bounded timeout
        start_t = time.perf_counter()
        try:
            is_async = entry.is_async if entry.is_async is not None else _is_async_callable(executor)
            if is_async:
                raw_out = await asyncio.wait_for(
                    _await_maybe(executor(**validated_kwargs)),
                    timeout=entry.timeout_seconds,
                )
            else:
                # Synchronous executors (COPASI/BASICO, Scanpy) run in a worker thread so they neither block
                # the event loop nor escape the timeout; on cancellation the awaiting task returns promptly
                # while the native solver call finishes in the background (it cannot be interrupted mid-call).
                # LOCAL_COMPUTE tools use their own bounded pool so stuck solvers cannot starve other tools.
                deadline = time.perf_counter() + entry.timeout_seconds
                if entry.adapter_type == AdapterType.LOCAL_COMPUTE:
                    call = functools.partial(contextvars.copy_context().run, executor, **validated_kwargs)
                    pending = asyncio.get_running_loop().run_in_executor(_native_pool(), call)
                else:
                    # Networked sync tools get their own bounded pool: a timed-out call's thread keeps running until
                    # its socket gives up, and must not exhaust the loop's default executor.
                    call = functools.partial(contextvars.copy_context().run, executor, **validated_kwargs)
                    pending = asyncio.get_running_loop().run_in_executor(_network_pool(), call)
                call_res = await asyncio.wait_for(pending, timeout=entry.timeout_seconds)
                if inspect.isawaitable(call_res):
                    raw_out = await asyncio.wait_for(
                        call_res,
                        timeout=max(0.001, deadline - time.perf_counter()),
                    )
                else:
                    raw_out = call_res

            elapsed = time.perf_counter() - start_t

            # If executor already returned ToolResult, merge metadata
            if isinstance(raw_out, ToolResult):
                raw_out.execution_time_seconds = elapsed
                raw_out.metadata["adapter"] = entry.adapter_type.value
                if entry.output_schema and raw_out.success and raw_out.data is not None:
                    try:
                        if isinstance(raw_out.data, dict):
                            entry.output_schema.model_validate(raw_out.data)
                    except Exception as out_err:
                        raw_out.success = False
                        raw_out.error = f"Output validation failed: {out_err}"
                        raw_out.error_type = ErrorType.VALIDATION_ERROR
                return raw_out

            # ORC-001: an executor that follows the error-as-data convention (BioContext client) reports a
            # transport failure as a dict with upstream_failed=True. That is a failed call, not "nothing found":
            # it must be retried, counted by the breaker, never cached, and shown as an unavailable source.
            if isinstance(raw_out, dict) and raw_out.get("upstream_failed") is True:
                try:
                    failed_type = ErrorType(str(raw_out.get("error_type") or "UPSTREAM_API_ERROR"))
                except ValueError:
                    failed_type = ErrorType.UPSTREAM_API_ERROR
                return ToolResult(
                    success=False,
                    tool_name=tool_name,
                    data=raw_out,
                    error=str(raw_out.get("error") or "upstream request failed"),
                    error_type=failed_type,
                    execution_time_seconds=elapsed,
                    metadata={"adapter": entry.adapter_type.value},
                )

            # Extract any canonical IDs in data if dict
            canonical_ids: Dict[str, str] = {}
            if isinstance(raw_out, dict):
                for id_key in ("ensembl_id", "uniprot_id", "biomodel_id", "efo_id", "pmid", "doi", "chembl_id"):
                    if id_key in raw_out and raw_out[id_key]:
                        canonical_ids[id_key] = str(raw_out[id_key])

            res = ToolResult(
                success=True,
                tool_name=tool_name,
                data=raw_out,
                canonical_ids=canonical_ids,
                execution_time_seconds=elapsed,
                metadata={"adapter": entry.adapter_type.value},
            )

            if entry.output_schema and res.data is not None:
                try:
                    if isinstance(res.data, dict):
                        entry.output_schema.model_validate(res.data)
                except Exception as out_err:
                    res.success = False
                    res.error = f"Output validation failed: {out_err}"
                    res.error_type = ErrorType.VALIDATION_ERROR

            return res

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start_t
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"Tool execution exceeded timeout cutoff of {entry.timeout_seconds}s",
                error_type=ErrorType.TIMEOUT,
                execution_time_seconds=elapsed,
                metadata={"adapter": entry.adapter_type.value},
            )
        except Exception as exec_err:
            elapsed = time.perf_counter() - start_t
            logger.error("Error executing %s: %s", tool_name, exec_err, exc_info=True)
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=str(exec_err),
                error_type=ErrorType.INTERNAL_ERROR,
                execution_time_seconds=elapsed,
                metadata={"adapter": entry.adapter_type.value},
            )


# -----------------------------------------------------------------------------
# STANDARD TOOL INPUT SCHEMAS & REGISTRATION
# -----------------------------------------------------------------------------

class SynaptaSearchInput(BaseModel):
    query: str
    size: int = Field(default=5, ge=1, le=50)


class SynaptaAssocInput(BaseModel):
    ensembl_id: str = Field(..., pattern=r"^ENSG\d{11}$")
    size: int = Field(default=10, ge=1, le=50)


class SynaptaKeggInput(BaseModel):
    target_symbol: str


# A BioModels accession, or the dataset id of an uploaded SBML model (never a path or free text).
VIVARIX_MODEL_ID = r"^(?:BIOMD\d{10}|MODEL\d{10}|ds_[0-9a-f]{12})$"


class VivarixSimInput(BaseModel):
    biomodel_id: str = Field(default="BIOMD0000000010", pattern=VIVARIX_MODEL_ID)
    duration: float = Field(default=60.0, ge=1.0, le=5000.0, description="seconds")
    intervals: int = Field(default=40, ge=5, le=500)
    parameter_overrides: Optional[Dict[str, float]] = Field(default=None, max_length=20)
    sbml_path: Optional[str] = None
    model_label: Optional[str] = None
    model_provenance: Optional[Dict[str, Any]] = None


class VivarixSteadyStateInput(BaseModel):
    biomodel_id: str = Field(default="BIOMD0000000010", pattern=VIVARIX_MODEL_ID)
    sbml_path: Optional[str] = None
    model_label: Optional[str] = None
    model_provenance: Optional[Dict[str, Any]] = None
    parameter_overrides: Optional[Dict[str, float]] = Field(default=None, max_length=20)


class VivarixParamScanInput(BaseModel):
    biomodel_id: str = Field(default="BIOMD0000000010", pattern=VIVARIX_MODEL_ID)
    parameter_name: str = Field(default="V1", min_length=1, max_length=200)
    start_val: float = Field(default=1.0, allow_inf_nan=False)
    end_val: float = Field(default=10.0, allow_inf_nan=False)
    num_steps: int = Field(default=5, ge=2, le=50, description="points, both endpoints included")
    target_species: Optional[str] = Field(default="Erk2-PP")
    duration: float = Field(default=60.0, ge=1.0, le=5000.0, description="seconds per point")
    intervals: int = Field(default=100, ge=5, le=500)
    spacing: Literal["linear", "log"] = "linear"
    report_all_species: bool = False
    sbml_path: Optional[str] = None
    model_label: Optional[str] = None
    model_provenance: Optional[Dict[str, Any]] = None


class LumicellPipelineInput(BaseModel):
    dataset_path: Optional[str] = None
    target_gene: Optional[str] = None
    dataset_name: Optional[str] = None
    max_pct_mt: float = Field(default=20.0, ge=0.0, le=100.0)
    n_neighbors: Optional[int] = Field(default=None, ge=2, le=100)
    resolution: float = Field(default=0.5, ge=0.05, le=3.0)


class LithoscribeSynthesisInput(BaseModel):
    query: str
    max_articles: int = Field(default=8, ge=1, le=50)
    check_integrity: bool = True
    # Planned EuropePMC searches ({"query", "sort", "tier", "label"}); when absent the query is decomposed.
    search_queries: Optional[List[Dict[str, Any]]] = None
    keywords: Optional[List[str]] = None  # words that rank retrieved passages
    pinned_ids: Optional[List[str]] = Field(
        default=None,
        max_length=10,
        description=(
            "Exact identifiers to fetch first (PMID like '39369609', PMCID like 'PMC1234567', or DOI); use this, "
            "not a free-text 'PMID ...' query, to retrieve a named paper. Identifiers in the query are also fetched."
        ),
    )


class WebSurferSearchInput(BaseModel):
    query: str
    max_results: int = Field(default=3, ge=1, le=20)


def register_default_biomedical_tools(registry: BiomedicalToolRegistry) -> None:
    """Register all builtin biomedical tools into the ToolUniverse registry."""
    from ..utils.biocontext import BioContextMCPClient
    from ..vivarix.engine import VivarixSimulationEngine
    from ..lumicell.engine import LumicellAnalysisEngine
    from ..lithoscribe.synthesis_engine import LiteratureSynthesisEngine
    from ..tools.web_search import FreeWebSearchClient

    biocontext = BioContextMCPClient()
    vivarix = VivarixSimulationEngine()
    lumicell = LumicellAnalysisEngine()
    lithoscribe = LiteratureSynthesisEngine(biocontext)
    web_search = FreeWebSearchClient()

    # 1. Synapta Search
    registry.register(
        ToolUniverseEntry(
            name="synapta_opentargets_search",
            description="Query OpenTargets GraphQL entity search for genes and targets.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_GRAPHQL,
            input_schema=SynaptaSearchInput,
            canonical_id_types=["ensembl_id"],
            timeout_seconds=20.0,
            tags=["genetics", "targets", "search"],
            is_async=True,
        ),
        executor=lambda query, size=5: biocontext.async_query_opentargets_search(query, size=size),
    )

    # 2. Synapta Associations
    registry.register(
        ToolUniverseEntry(
            name="synapta_opentargets_associations",
            description="Query OpenTargets target-disease association scores by Ensembl ID.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_GRAPHQL,
            input_schema=SynaptaAssocInput,
            canonical_id_types=["ensembl_id", "efo_id"],
            timeout_seconds=20.0,
            tags=["genetics", "diseases", "associations"],
            is_async=True,
        ),
        executor=lambda ensembl_id, size=10: biocontext.async_query_opentargets_associations(ensembl_id, size=size),
    )

    # 3. Synapta KEGG Pathways
    registry.register(
        ToolUniverseEntry(
            name="synapta_kegg_pathways",
            description="Query KEGG REST API for biological pathway memberships.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=SynaptaKeggInput,
            canonical_id_types=["kegg_id"],
            timeout_seconds=15.0,
            tags=["pathways", "kegg"],
            is_async=True,
        ),
        executor=lambda target_symbol: biocontext.async_query_kegg_gene_pathways(target_symbol),
    )

    # 4. Vivarix ODE Time Course
    registry.register(
        ToolUniverseEntry(
            name="vivarix_simulate_time_course",
            description="Simulate kinetic ODE time-course trajectory via COPASI / BASICO.",
            domain="vivarix",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=VivarixSimInput,
            canonical_id_types=["biomodel_id"],
            timeout_seconds=45.0,
            tags=["simulation", "ode", "copasi", "kinetics"],
        ),
        executor=lambda biomodel_id="BIOMD0000000010", duration=60.0, intervals=40, parameter_overrides=None, sbml_path=None, model_label=None, model_provenance=None: vivarix.simulate_time_course(
            biomodel_id=biomodel_id, duration=duration, intervals=intervals, parameter_overrides=parameter_overrides,
            sbml_path=sbml_path, model_label=model_label, model_provenance=model_provenance,
        ),
    )

    # 5. Vivarix Steady State
    registry.register(
        ToolUniverseEntry(
            name="vivarix_compute_steady_state",
            description="Compute steady-state concentrations and reaction fluxes via COPASI.",
            domain="vivarix",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=VivarixSteadyStateInput,
            canonical_id_types=["biomodel_id"],
            timeout_seconds=30.0,
            tags=["steady_state", "copasi", "fluxes"],
        ),
        executor=lambda biomodel_id="BIOMD0000000010", sbml_path=None, model_label=None, model_provenance=None, parameter_overrides=None: vivarix.compute_steady_state(
            biomodel_id=biomodel_id, sbml_path=sbml_path, model_label=model_label, model_provenance=model_provenance,
            parameter_overrides=parameter_overrides,
        ),
    )

    # 6. Vivarix Parameter Scan
    registry.register(
        ToolUniverseEntry(
            name="vivarix_parameter_scan",
            description="Scan model parameter across values to compute sensitivity curves.",
            domain="vivarix",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=VivarixParamScanInput,
            canonical_id_types=["biomodel_id"],
            timeout_seconds=120.0,
            tags=["parameter_scan", "sensitivity", "copasi"],
        ),
        executor=lambda biomodel_id="BIOMD0000000010", parameter_name="V1", start_val=1.0, end_val=10.0, num_steps=5, target_species="Erk2-PP", duration=60.0, intervals=100, spacing="linear", report_all_species=False, sbml_path=None, model_label=None, model_provenance=None: vivarix.run_parameter_scan(
            biomodel_id=biomodel_id, parameter_name=parameter_name, start_val=start_val, end_val=end_val, num_steps=num_steps, target_species=target_species,
            duration=duration, intervals=intervals, spacing=spacing, report_all_species=report_all_species,
            sbml_path=sbml_path, model_label=model_label, model_provenance=model_provenance,
        ),
    )

    # 7. Lumicell Single-Cell Analysis
    registry.register(
        ToolUniverseEntry(
            name="lumicell_analyze_single_cell",
            description="Analyze verified AnnData single-cell dataset (QC, PCA, Leiden clustering, Wilcoxon markers).",
            domain="lumicell",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=LumicellPipelineInput,
            canonical_id_types=["target_gene"],
            # pbmc3k: ~8 s warm, ~30 s on the first call of a process (numba JIT); 50k-cell inputs take minutes.
            timeout_seconds=180.0,
            tags=["single_cell", "scanpy", "anndata", "clustering"],
        ),
        executor=lambda dataset_path=None, target_gene=None, dataset_name=None, max_pct_mt=20.0, n_neighbors=None, resolution=0.5: lumicell.execute_single_cell_pipeline(
            dataset_path=dataset_path, target_gene=target_gene, dataset_name=dataset_name, max_pct_mt=max_pct_mt, n_neighbors=n_neighbors, resolution=resolution
        ),
    )

    # 8. Lithoscribe Literature Synthesis
    registry.register(
        ToolUniverseEntry(
            name="lithoscribe_literature_synthesis",
            description=(
                "Query EuropePMC with query decomposition and passage extraction; named papers are fetched by exact "
                "PMID/PMCID/DOI (pinned_ids) before any topical search."
            ),
            domain="lithoscribe",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=LithoscribeSynthesisInput,
            canonical_id_types=["pmid", "doi"],
            timeout_seconds=40.0,
            tags=["literature", "europepmc", "citations", "paperqa2"],
            is_async=True,
        ),
        executor=lambda query, max_articles=8, check_integrity=True, search_queries=None, keywords=None, pinned_ids=None: (
            lithoscribe.execute_literature_synthesis(
                query, max_articles=max_articles, check_integrity=check_integrity,
                search_queries=search_queries, keywords=keywords, pinned_ids=pinned_ids,
            )
        ),
    )

    # 9. WebSurfer Live Web Search
    registry.register(
        ToolUniverseEntry(
            name="websurfer_search",
            description="Perform live asynchronous web search and article retrieval.",
            domain="websurfer",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=WebSurferSearchInput,
            canonical_id_types=["url"],
            timeout_seconds=20.0,
            tags=["web", "search", "live_verification"],
            is_async=True,
        ),
        executor=lambda query, max_results=3: web_search.search_and_surf(query, max_results=max_results),
    )

    # Feature extensions register their own tools (each failure is logged and skipped).
    from ..extensions import load_extensions, register_extension_tools

    load_extensions()
    register_extension_tools(registry)


# Global singleton instance
_GLOBAL_REGISTRY: Optional[BiomedicalToolRegistry] = None
_REGISTRY_COMPLETE = False
_REGISTRY_LAST_ATTEMPT = 0.0
_REGISTRY_RETRY_SECONDS = 60.0


def get_tool_registry() -> BiomedicalToolRegistry:
    """
    Return the global ToolUniverse registry singleton with default tools registered.
    If registration failed part-way (e.g. an optional dependency was briefly unavailable), it is retried on a
    later call, at most once per minute, instead of serving a half-populated registry for the process lifetime.
    """
    global _GLOBAL_REGISTRY, _REGISTRY_COMPLETE, _REGISTRY_LAST_ATTEMPT
    if _GLOBAL_REGISTRY is not None and (_REGISTRY_COMPLETE or time.monotonic() - _REGISTRY_LAST_ATTEMPT < _REGISTRY_RETRY_SECONDS):
        return _GLOBAL_REGISTRY
    _REGISTRY_LAST_ATTEMPT = time.monotonic()
    candidate = BiomedicalToolRegistry()
    try:
        register_default_biomedical_tools(candidate)
        _REGISTRY_COMPLETE = True
    except Exception as e:
        logger.warning("Failed to register default biomedical tools (will retry): %s", e)
        if _GLOBAL_REGISTRY is not None and len(_GLOBAL_REGISTRY.list_tools()) >= len(candidate.list_tools()):
            return _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = candidate
    return _GLOBAL_REGISTRY

