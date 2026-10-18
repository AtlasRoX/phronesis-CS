"""
Phronesis as a Model Context Protocol server (feature 17): JSON-RPC 2.0 over Streamable HTTP at POST /mcp.

Specs:
- MCP 2025-11-25 (https://modelcontextprotocol.io/specification/2025-11-25/): the `initialize` handshake,
  `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
- MCP 2026-07-28 (https://modelcontextprotocol.io/specification/2026-07-28/): `server/discover` and per-request
  version negotiation through the `MCP-Protocol-Version` header (or `_meta["io.modelcontextprotocol/
  protocolVersion"]`). An unsupported version is rejected with -32600.

Tools:
- `phronesis_research`: the full orchestrator, non-interactive (plan approvals are automatic), with a
  `framework` argument (default agentic-plan-execute). Returns the answer text plus structuredContent
  {answer, citations, verification, task_status, framework, thread_id}.
- An allowlist of read-only registry tools (`PHRONESIS_MCP_TOOLS`, comma-separated; default below), listed
  only when registered, with `inputSchema` = the tool's pydantic `model_json_schema()`.

Security, after the MCP threat taxonomy of Hou et al. 2025 (arXiv:2503.23278):
- a fixed allowlist: the server never exposes a tool because a registry or client asked for it, and tools
  that write, upload, dock, generate or execute code are refused even when listed in the environment;
- static descriptions: tool descriptions come from this file (or the code-defined registry entry,
  neutralised) and never from upstream data, so no tool-description injection or rug-pull at runtime;
- the same bearer authentication as the REST API, Origin validation against the CORS allowlist (DNS
  rebinding), and a per-user concurrency limit on tools/call (`PHRONESIS_MCP_MAX_CONCURRENT`, default 2);
- no sampling, roots or elicitation requests to the client, and no session state.

Errors: -32700 parse error, -32600 invalid request, -32601 method not found, -32602 invalid params (also an
unknown tool name), -32603 internal error. A tool that runs and fails returns `isError: true`.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.backend.auth import get_current_user
from app.backend.limits import UserConcurrencyLimiter
from phronesis.agentic import config as agent_config
from phronesis.agentic.guardrails import neutralise
from phronesis.provenance.rerun import UNSAFE_TOOL_RE, tool_replay_refusal

logger = logging.getLogger("phronesis.backend.mcp")

router = APIRouter(tags=["mcp"])

SUPPORTED_VERSIONS: Tuple[str, ...] = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
HANDSHAKE_VERSION = "2025-11-25"
VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

RESEARCH_TOOL = "phronesis_research"

DEFAULT_TOOLS: Tuple[str, ...] = (
    "synapta_opentargets_search",
    "synapta_opentargets_associations",
    "synapta_kegg_pathways",
    "lithoscribe_literature_synthesis",
    "lithoscribe_clinical_trials_search",
    "chembl_resolve_drugs",
    "chembl_drug_profile",
    "opentargets_disease_targets",
    "synapta_variant_intelligence",
    "primekg_query",
    # Structure and omics tools; each is listed only when registered.
    "structure_alphafold_prediction",
    "structure_pdb_search",
    "gwas_catalog_associations",
    "gtex_median_expression",
    "string_interaction_partners",
    "reactome_pathways",
    "pubchem_compound_properties",
    "preprints_recent",
)

# Refused whatever PHRONESIS_MCP_TOOLS says: writes, uploads, code execution and generative/docking calls. The same
# policy object guards the run re-play endpoints (SEC-001), so the two entry points cannot drift apart.
_DENY_RE = UNSAFE_TOOL_RE

_STATIC_DESCRIPTIONS: Dict[str, str] = {
    RESEARCH_TOOL: (
        "Answer a biomedical research question with the full Phronesis pipeline: literature, target-disease "
        "evidence, trials, drugs and (when asked) kinetic model simulation, with a verified, cited answer."
    ),
    "synapta_opentargets_search": "Search Open Targets for a gene, target or disease by name; returns matching ids.",
    "synapta_opentargets_associations": "Top disease associations (Open Targets overall association score) for an Ensembl gene id.",
    "synapta_kegg_pathways": "KEGG pathways that contain a gene symbol.",
    "lithoscribe_literature_synthesis": "Search the biomedical literature and return ranked passages with PMIDs and DOIs.",
    "lithoscribe_clinical_trials_search": "Search ClinicalTrials.gov (API v2) for registered trials and posted results.",
    "chembl_resolve_drugs": "Resolve drug names to ChEMBL molecule ids.",
    "chembl_drug_profile": "ChEMBL mechanisms of action, bioactivity and approved drugs for drugs or a target.",
    "opentargets_disease_targets": "Rank the targets associated with a disease (Open Targets).",
    "synapta_variant_intelligence": "Variant evidence: ClinVar classifications, CIViC evidence, cBioPortal prevalence, gnomAD frequency.",
    "primekg_query": "Query the local PrimeKG biomedical knowledge graph.",
}

_INSTRUCTIONS = (
    "Phronesis answers biomedical research questions with cited, verified evidence. Use phronesis_research for "
    "full questions; the other tools are read-only lookups against public databases."
)

_mcp_slots = UserConcurrencyLimiter(int(os.getenv("PHRONESIS_MCP_MAX_CONCURRENT", "2") or 2))


class ResearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=20_000, description="The research question")
    framework: Optional[Literal[agent_config.ALL_FRAMEWORKS]] = Field(  # type: ignore[valid-type]
        default=None, description="Orchestration framework id; default agentic-plan-execute"
    )
    enable_web_search: bool = Field(default=True, description="Allow web search in addition to the curated sources")
    thread_id: Optional[str] = Field(default=None, max_length=200, description="Continue an earlier thread")


# Seams for tests ---------------------------------------------------------------------------------------------
def _get_registry() -> Any:
    from phronesis.agents import authoritative_orchestrator as orch_module

    return orch_module.get_tool_registry()


async def _default_research_runner(user_id: str, args: ResearchArgs) -> Dict[str, Any]:
    from phronesis.agents.authoritative_orchestrator import get_authoritative_orchestrator

    timeout = float(os.getenv("PHRONESIS_MCP_RESEARCH_TIMEOUT_S", "900") or 900)
    orch = get_authoritative_orchestrator()
    res = await asyncio.wait_for(
        orch.execute_inquiry(
            query=args.question,
            thread_id=args.thread_id or f"mcp_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            enable_web_search=args.enable_web_search,
            framework=agent_config.resolve_framework(args.framework, agent_config.AGENTIC),
            interactive=False,
        ),
        timeout=timeout,
    )
    return res.to_dict()


research_runner: Callable[[str, ResearchArgs], Awaitable[Dict[str, Any]]] = _default_research_runner


def _server_info() -> Dict[str, Any]:
    try:
        version = importlib.metadata.version("phronesis")
    except importlib.metadata.PackageNotFoundError:
        version = "1.0.0"
    return {"name": "phronesis", "title": "Phronesis biomedical research", "version": version}


def _capabilities() -> Dict[str, Any]:
    return {"tools": {"listChanged": False}}


# JSON-RPC helpers (shared with the A2A router) -------------------------------------------------------------
class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def rpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def valid_envelope(msg: Any) -> bool:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return False
    rid = msg.get("id")
    return rid is None or (isinstance(rid, (str, int)) and not isinstance(rid, bool))


MAX_BATCH = 50
MAX_RPC_BODY_BYTES = 1_000_000


async def parse_body(request: Request) -> Tuple[Optional[Any], Optional[JSONResponse]]:
    """The JSON body, read in chunks and abandoned as soon as it passes MAX_RPC_BODY_BYTES (SEC-004)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RPC_BODY_BYTES:
        return None, JSONResponse(rpc_error(None, INVALID_REQUEST, "Request body too large"), status_code=413)
    chunks: List[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_RPC_BODY_BYTES:
            return None, JSONResponse(rpc_error(None, INVALID_REQUEST, "Request body too large"), status_code=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        return json.loads(raw or b""), None
    except (ValueError, UnicodeDecodeError):
        return None, JSONResponse(rpc_error(None, PARSE_ERROR, "Parse error"), status_code=400)


async def dispatch_rpc(payload: Any, handler: Callable[[str, Any, Dict[str, Any]], Awaitable[Any]]) -> Response:
    """Run one message or a batch through handler(method, id, params); notifications get no response."""
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    if batch and not messages:
        return JSONResponse(rpc_error(None, INVALID_REQUEST, "Empty batch"), status_code=400)
    if batch and len(messages) > MAX_BATCH:
        # SEC-012: refuse the whole batch instead of silently dropping members past the limit (JSON-RPC requires
        # one response per request, so a partial answer would lose calls without telling the client).
        return JSONResponse(
            rpc_error(None, INVALID_REQUEST, f"Batch too large: at most {MAX_BATCH} requests per batch", {"max_batch": MAX_BATCH}),
            status_code=400,
        )
    responses: List[Dict[str, Any]] = []
    for msg in messages:
        if not valid_envelope(msg):
            responses.append(rpc_error(msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), (str, int)) else None, INVALID_REQUEST, "Invalid Request"))
            continue
        is_notification = "id" not in msg
        params = msg.get("params")
        if params is not None and not isinstance(params, (dict, list)):
            if not is_notification:
                responses.append(rpc_error(msg.get("id"), INVALID_PARAMS, "params must be an object"))
            continue
        try:
            result = await handler(msg["method"], msg.get("id"), params if isinstance(params, dict) else {})
            if not is_notification:
                responses.append(rpc_result(msg.get("id"), result))
        except RpcError as exc:
            if not is_notification:
                responses.append(rpc_error(msg.get("id"), exc.code, exc.message, exc.data))
        except Exception:  # noqa: BLE001
            request_id = uuid.uuid4().hex[:12]
            logger.error("JSON-RPC method %s failed [request %s]", msg.get("method"), request_id, exc_info=True)
            if not is_notification:
                responses.append(rpc_error(msg.get("id"), INTERNAL_ERROR, f"Internal error (request id {request_id})"))
    if not responses:
        return Response(status_code=202)
    return JSONResponse(responses if batch else responses[0])


def _allowed_origins() -> List[str]:
    raw = ",".join(
        [os.getenv("PHRONESIS_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"), os.getenv("PHRONESIS_MCP_ALLOWED_ORIGINS", "")]
    )
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def check_origin(request: Request) -> Optional[JSONResponse]:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in _allowed_origins():
        return JSONResponse(rpc_error(None, INVALID_REQUEST, "Origin not allowed"), status_code=403)
    return None


# Tools ---------------------------------------------------------------------------------------------------
def allowlisted_names() -> List[str]:
    raw = os.getenv("PHRONESIS_MCP_TOOLS")
    names = [n.strip() for n in raw.split(",") if n.strip()] if raw else list(DEFAULT_TOOLS)
    return [n for n in dict.fromkeys(names) if n != RESEARCH_TOOL and not _DENY_RE.search(n)]


def exposed_entries(registry: Any) -> List[Any]:
    out = []
    for name in allowlisted_names():
        entry = registry.get_entry(name) if registry is not None else None
        # Registry tags marking per-user data or code execution are refused too (shared replay policy).
        if entry is not None and tool_replay_refusal(name, entry) is None:
            out.append(entry)
    return out


def _description(entry: Any) -> str:
    return _STATIC_DESCRIPTIONS.get(entry.name) or neutralise(str(entry.description or entry.name))[:600]


def tool_definitions(registry: Any) -> List[Dict[str, Any]]:
    tools = [
        {
            "name": RESEARCH_TOOL,
            "title": "Phronesis research",
            "description": _STATIC_DESCRIPTIONS[RESEARCH_TOOL],
            "inputSchema": ResearchArgs.model_json_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        }
    ]
    for entry in exposed_entries(registry):
        try:
            schema = entry.input_schema.model_json_schema()
        except Exception:  # noqa: BLE001
            schema = {"type": "object"}
        tools.append(
            {
                "name": entry.name,
                "title": entry.name.replace("_", " "),
                "description": _description(entry),
                "inputSchema": schema,
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
            }
        )
    return tools


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _text_result(text: str, structured: Optional[Dict[str, Any]] = None, is_error: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        out["structuredContent"] = structured
    return out


async def _call_research(user_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        args = ResearchArgs.model_validate(arguments)
    except ValidationError as exc:
        return _text_result(f"Invalid arguments: {exc.errors(include_url=False)[:3]}", is_error=True)
    try:
        result = await research_runner(user_id, args)
    except asyncio.TimeoutError:
        return _text_result("The research run timed out.", is_error=True)
    report = result.get("verification_report") or {}
    structured = _json_safe(
        {
            "answer": result.get("response", ""),
            "citations": result.get("citations") or (result.get("execution_artifacts") or {}).get("citation_map") or {},
            "verification": {
                "passed": report.get("passed"),
                "verification_score": report.get("verification_score"),
                "defects": [
                    {k: d.get(k) for k in ("defect_type", "claim", "description") if isinstance(d, dict)}
                    for d in (report.get("defects") or [])[:10]
                    if isinstance(d, dict)
                ],
            },
            "task_status": result.get("task_status"),
            "framework": result.get("framework_used"),
            "thread_id": result.get("thread_id"),
        }
    )
    failed = ((result.get("task_status") or {}).get("task_status")) in ("failed", "cancelled")
    return _text_result(str(result.get("response", "")), structured, is_error=failed)


async def _call_registry(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    registry = _get_registry()
    if any(not isinstance(k, str) for k in arguments):
        return _text_result("Invalid arguments: keys must be strings", is_error=True)
    try:
        res = await registry.execute(name, **arguments)
    except TypeError as exc:  # e.g. an argument named like execute()'s own parameters
        return _text_result(f"Invalid arguments: {str(exc)[:300]}", is_error=True)
    if not getattr(res, "success", False):
        err_type = getattr(getattr(res, "error_type", None), "value", None) or "ERROR"
        return _text_result(f"{name} failed ({err_type}): {str(getattr(res, 'error', '') or '')[:1000]}", {"error_type": err_type}, is_error=True)
    data = _json_safe(res.data)
    structured = data if isinstance(data, dict) else {"result": data}
    text = json.dumps(structured, ensure_ascii=False)
    if len(text) > 20_000:
        text = text[:20_000] + " ... (truncated; the full result is in structuredContent)"
    return _text_result(text, structured)


async def _handle(user_id: str, method: str, req_id: Any, params: Dict[str, Any]) -> Any:
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    meta_version = meta.get(VERSION_META_KEY)
    if meta_version is not None and meta_version not in SUPPORTED_VERSIONS:
        raise RpcError(INVALID_REQUEST, f"Unsupported protocol version {str(meta_version)[:32]}", {"supported": list(SUPPORTED_VERSIONS)})
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else HANDSHAKE_VERSION
        return {"protocolVersion": version, "capabilities": _capabilities(), "serverInfo": _server_info(), "instructions": _INSTRUCTIONS}
    if method.startswith("notifications/"):
        return None  # initialized, cancelled, ...: nothing to do for a stateless server
    if method == "ping":
        return {}
    if method == "server/discover":
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": _capabilities(),
            "serverInfo": _server_info(),
            "instructions": _INSTRUCTIONS,
        }
    if method == "tools/list":
        return {"tools": tool_definitions(_get_registry())}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if params.get("arguments") is not None else {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "tools/call needs a tool name and an arguments object")
        if name != RESEARCH_TOOL and name not in {e.name for e in exposed_entries(_get_registry())}:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name[:80]}")
        if not _mcp_slots.try_acquire(user_id):
            return _text_result("Too many tool calls are running for this user; try again when one finishes.", is_error=True)
        try:
            if name == RESEARCH_TOOL:
                return await _call_research(user_id, arguments)
            return await _call_registry(name, arguments)
        finally:
            _mcp_slots.release(user_id)
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method[:80]}")


@router.post("/mcp")
async def mcp_endpoint(request: Request, user_id: str = Depends(get_current_user)) -> Response:
    bad_origin = check_origin(request)
    if bad_origin is not None:
        return bad_origin
    header_version = request.headers.get("mcp-protocol-version")
    if header_version and header_version not in SUPPORTED_VERSIONS:
        return JSONResponse(
            rpc_error(None, INVALID_REQUEST, f"Unsupported MCP-Protocol-Version {header_version[:32]}", {"supported": list(SUPPORTED_VERSIONS)}),
            status_code=400,
        )
    payload, error = await parse_body(request)
    if error is not None:
        return error

    async def handler(method: str, req_id: Any, params: Dict[str, Any]) -> Any:
        return await _handle(user_id, method, req_id, params)

    return await dispatch_rpc(payload, handler)


@router.get("/mcp")
async def mcp_get() -> Response:
    """No server-initiated SSE stream is offered (Streamable HTTP allows 405 here)."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
