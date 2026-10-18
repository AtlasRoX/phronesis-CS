"""
FastAPI Backend for Phronesis Clinical R&D Multi-Agent Assistant.
Serves Next.js frontend with REST & Server-Sent Events (SSE) streaming.
"""

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Callable, List, Optional

from dotenv import load_dotenv

# Load environment variables from .env. PHRONESIS_LOAD_DOTENV=0 skips it: the test suite sets it so a developer's
# .env (keys, auth mode, data paths) never leaks into pytest runs (audit TST-003).
if os.getenv("PHRONESIS_LOAD_DOTENV", "1").strip().lower() not in ("0", "false", "no", "off"):
    load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.background import BackgroundTask
from phronesis.agents.authoritative_orchestrator import (
    get_authoritative_orchestrator,
)
from phronesis.utils.llm.llm_factory import (
    DEFAULT_ORCHESTRATOR_MODEL,
    get_supported_models,
    is_allowed_model,
    usable_key,
)
from phronesis.utils.biocontext import BioContextMCPClient
from phronesis.utils.memory import get_clinical_memory_engine

from app.backend.auth import auth_disabled, get_current_user
from app.backend.limits import UserConcurrencyLimiter
from app.backend.security import SecurityMiddleware, cors_origins, insecure_allowed
from app.backend.routers.datasets import router as datasets_router
from app.backend.routers.export import router as export_router
from phronesis.agentic import config as agent_config
from phronesis.config import get_bool, validate_environment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("phronesis.backend")


def _package_version(default: str = "1.0.0") -> str:
    try:
        return importlib.metadata.version("phronesis")
    except importlib.metadata.PackageNotFoundError:
        return default


def _preload_primekg() -> None:
    """Load the local PrimeKG graph in the background so the first question does not wait for it."""
    if os.getenv("PHRONESIS_PRIMEKG_PRELOAD", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from phronesis.kgintel.store import store_available
    except Exception as exc:  # noqa: BLE001
        logger.warning("PrimeKG preload unavailable: %s", exc)
        return
    if not store_available():
        logger.info("PrimeKG store not built; run `phronesis-primekg build` to enable knowledge-graph analyses")
        return

    def _load() -> None:
        try:
            from phronesis.kgintel.graph import get_primekg

            kg = get_primekg()
            logger.info("PrimeKG loaded: %s nodes, %s edges", f"{kg.n:,}", f"{kg.n_edges:,}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("PrimeKG preload failed: %s", exc)

    threading.Thread(target=_load, name="primekg-preload", daemon=True).start()


def _check_config() -> List[str]:
    """Log every configuration problem as a warning; PHRONESIS_STRICT_CONFIG=1 turns them into a startup error.

    Weak bearer secrets (SEC-009) are reported by validate_environment too."""
    problems = validate_environment()
    for problem in problems:
        logger.warning("Configuration: %s", problem)
    if problems and get_bool("PHRONESIS_STRICT_CONFIG"):
        raise RuntimeError(f"{len(problems)} configuration problem(s) (PHRONESIS_STRICT_CONFIG=1); see the warnings above")
    return problems


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    _check_config()
    _preload_primekg()
    job_manager = None
    try:
        from phronesis.agentic.jobs import get_job_manager

        job_manager = get_job_manager()  # marks jobs left running by a previous process as interrupted
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("phronesis.backend").warning("Job manager unavailable: %s", exc)
    yield
    shutdown = getattr(job_manager, "shutdown", None)
    if callable(shutdown):
        try:
            result = shutdown()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("phronesis.backend").warning("Job manager shutdown failed: %s", exc)


app = FastAPI(
    title="Phronesis Clinical R&D API",
    description="Multi-Agent Orchestrator backend powered by NVIDIA NIM Nemotron and BioContext MCP",
    version=_package_version(),
    lifespan=_lifespan,
)

# CORS: explicit origin allow-list (wildcard origins are dropped: with credentials they would admit any page)
_cors_origins = cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version", "Mcp-Session-Id", "Last-Event-ID"],
    expose_headers=["Content-Disposition"],
)
# Outermost: body-size limits, Origin check on state-changing requests, Host allow-list and loopback-only clients
# when authentication is disabled (app/backend/security.py; SEC-002, SEC-004, CFG-001).
app.add_middleware(SecurityMiddleware)

app.include_router(datasets_router)
app.include_router(export_router)

# Agentic platform routers (run control, jobs, projects, MCP, A2A). A router that fails to import is logged
# and skipped so one broken feature never takes the API down.
# Routers that failed are listed in /api/health; PHRONESIS_STRICT_ROUTERS=1 makes such a failure fatal at startup.
DISABLED_ROUTERS: dict[str, str] = {}
for _router_module in (
    "app.backend.routers.agent_runs",
    "app.backend.routers.jobs",
    "app.backend.routers.projects",
    "app.backend.routers.mcp",
    "app.backend.routers.a2a",
):
    try:
        app.include_router(importlib.import_module(_router_module).router)
    except Exception as _router_exc:  # noqa: BLE001
        if os.getenv("PHRONESIS_STRICT_ROUTERS", "").strip().lower() in ("1", "true", "yes", "on"):
            raise RuntimeError(f"Router {_router_module} failed to load (PHRONESIS_STRICT_ROUTERS=1)") from _router_exc
        DISABLED_ROUTERS[_router_module] = type(_router_exc).__name__
        logging.getLogger("phronesis.backend").warning(
            "Router %s disabled: %s", _router_module, _router_exc, exc_info=True
        )

if auth_disabled():
    logger.warning(
        "PHRONESIS_AUTH_MODE=disabled: all requests run as a single local user. Only loopback clients are served%s.",
        " (PHRONESIS_ALLOW_INSECURE=1: other clients too; publish the port on 127.0.0.1 only)" if insecure_allowed() else "",
    )

_DATASET_ID_RE = re.compile(r"ds_[0-9a-f]{12}")

MAX_MESSAGE_CHARS = 20_000
MAX_THREAD_ID_CHARS = 200
SSE_HEARTBEAT_SECONDS = float(os.getenv("PHRONESIS_SSE_HEARTBEAT_S", "15"))
HEALTH_CACHE_SECONDS = float(os.getenv("PHRONESIS_HEALTH_CACHE_S", "60"))

# Each orchestration runs many tools and model calls; one user cannot start an unbounded number at once.
_chat_slots = UserConcurrencyLimiter(int(os.getenv("PHRONESIS_MAX_CONCURRENT_CHATS_PER_USER", "2")))
_CHAT_BUSY_DETAIL = "You already have the maximum number of questions running; wait for one to finish."
# Title generation calls the model too; over this per-user limit a request gets the heuristic title instead.
_title_slots = UserConcurrencyLimiter(int(os.getenv("PHRONESIS_MAX_CONCURRENT_TITLES_PER_USER", "2")))


def _internal_error(request_id: str) -> str:
    """Client-facing text for an unexpected failure: no exception text, which can carry keys or upstream URLs."""
    return f"The request failed on the server (request id {request_id}). The details are in the server log."


class AgentBudgetOptions(BaseModel):
    """Per-request budget limits; they can only tighten the server's own limits."""
    max_tool_calls: Optional[int] = Field(default=None, ge=1, le=200)
    max_llm_calls: Optional[int] = Field(default=None, ge=1, le=120)
    max_tokens: Optional[int] = Field(default=None, ge=1000, le=2_000_000)
    max_seconds: Optional[float] = Field(default=None, ge=0, le=7200)  # 0 = no time limit
    max_draft_rounds: Optional[int] = Field(default=None, ge=0, le=5)


class AgentOptions(BaseModel):
    """Agentic settings for one request."""
    effort_level: Optional[str] = Field(
        default="medium",
        description="Reasoning & deliberation effort level: 'low', 'medium', 'high', 'ultra'",
    )
    require_plan_approval: bool = Field(default=False, description="Pause for the user's approval before running the plan")
    allow_clarification: bool = Field(default=True, description="Ask a clarifying question when the request is ambiguous")
    enable_counter_evidence: bool = True
    enable_hypotheses: bool = True
    enable_code_analysis: bool = True
    budget: Optional[AgentBudgetOptions] = None

    def to_runtime(self) -> dict[str, Any]:
        data = self.model_dump()
        data["effort_level"] = self.effort_level or "medium"
        data["budget"] = {k: v for k, v in (data.get("budget") or {}).items() if v is not None}
        return data


class ChatRequest(BaseModel):
    """Chat request schema."""
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS, description="User query or clinical prompt")
    thread_id: Optional[str] = Field(None, max_length=MAX_THREAD_ID_CHARS, description="Session thread identifier")
    model: Optional[str] = Field(
        DEFAULT_ORCHESTRATOR_MODEL,
        max_length=200,
        description="Model identifier, e.g. 'nvidia/nemotron-3-super-120b-a12b' or 'gpt-4o'",
    )
    enable_web_search: Optional[bool] = Field(
        True,
        description="Enable free web search and URL surfing",
    )
    api_key: Optional[str] = Field(None, max_length=500, description="Optional runtime API key")
    dataset_ids: Optional[List[str]] = Field(
        default=None, max_length=5, description="Uploaded dataset ids (ds_...) attached to this question"
    )
    framework: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Orchestration framework id (see /api/frameworks); defaults to PHRONESIS_DEFAULT_FRAMEWORK",
    )
    project_id: Optional[str] = Field(default=None, max_length=64, description="Research project (prj_...) for project memory")
    agent_options: Optional[AgentOptions] = Field(default=None, description="Per-request agentic settings")

    @field_validator("framework")
    @classmethod
    def _check_framework(cls, value: Optional[str]) -> Optional[str]:
        if value and value not in agent_config.ALL_FRAMEWORKS:
            raise ValueError(f"Unknown framework {value[:64]!r}; choose one listed by /api/frameworks")
        return value

    @field_validator("project_id")
    @classmethod
    def _check_project_id(cls, value: Optional[str]) -> Optional[str]:
        if value and not re.fullmatch(r"prj_[A-Za-z0-9]{6,40}", value):
            raise ValueError("Invalid project id")
        return value

    @field_validator("dataset_ids")
    @classmethod
    def _check_dataset_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        for item in value or []:
            if not _DATASET_ID_RE.fullmatch(item):
                raise ValueError(f"Invalid dataset id: {item[:40]!r}")
        return value

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: Optional[str]) -> Optional[str]:
        return check_caller_api_key(value)

    @model_validator(mode="after")
    def _check_model(self) -> "ChatRequest":
        # The server's keys only pay for catalog models; any other id needs the caller's own key.
        # (Enforced again in get_llm, where the key is actually resolved: SEC-003.)
        if self.model and not self.api_key and not is_allowed_model(self.model):
            raise ValueError(
                f"Unsupported model {self.model[:80]!r}: choose one listed by /api/models or supply your own API key."
            )
        return self


def check_caller_api_key(value: Optional[str]) -> Optional[str]:
    """A caller key that looks like a template value ('your-key', 'sk-...') is refused (422) instead of being
    silently replaced by the server's key (SEC-003). Blank means no key."""
    if value is None or not value.strip():
        return None
    if usable_key(value) is None:
        raise ValueError("The API key looks like a placeholder; supply a real key or leave it empty.")
    return value.strip()


_TITLE_PREAMBLE_RES = [
    re.compile(r"^(?:hello|hi|hey|good\s+(?:morning|afternoon|evening))\b[,\s.:!]*", re.I),
    re.compile(r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:help\s+me\s+)?(?:to\s+)?(?:explain|tell\s+me\s+(?:about)?|summarize|analyze|find|give\s+me|show\s+me|list|search|describe|discuss|compare|evaluate|check|provide|run|simulate|look\s+up)\b[,\s:]*", re.I),
    re.compile(r"^(?:please\s+)?(?:explain|tell\s+me\s+(?:about)?|summarize|analyze|find|give\s+me|show\s+me|list|search|describe|discuss|compare|evaluate|check|provide|run|simulate|look\s+up)\b[,\s:]*", re.I),
    re.compile(r"^(?:what|how|why|when|where|who)\s+(?:is|are|do|does|did|can|could|would|should|to|about|was|were)\s+", re.I),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+(?:know|understand|learn|find|see|analyze|investigate|explore))\s+(?:about\s+)?", re.I),
    re.compile(r"^(?:i'm\s+looking\s+for|i\s+am\s+looking\s+for|tell\s+me\s+about|give\s+me\s+a\s+summary\s+of|can\s+we\s+look\s+at|about)\s+", re.I),
]

_ACRONYMS = {
    "DNA", "RNA", "mRNA", "tRNA", "siRNA", "shRNA", "snRNA", "lncRNA",
    "KRAS", "EGFR", "TP53", "BRCA1", "BRCA2", "BRAF", "MYC", "HER2", "HER3",
    "ALK", "ROS1", "RET", "MET", "NTRK", "NTRK1", "NTRK2", "NTRK3",
    "PIK3CA", "PTEN", "AKT1", "CDK4", "CDK6", "RB1", "APC", "VHL",
    "PD-1", "PD-L1", "CTLA-4", "LAG-3", "TIM-3", "TIGIT",
    "NSCLC", "SCLC", "CRC", "PDAC", "HCC", "RCC", "TNBC", "GBM", "AML", "CML", "ALL", "CLL",
    "ODE", "SBML", "SED-ML", "PDB", "GEO", "MCP", "API", "DAG",
    "ChEMBL", "PrimeKG", "CIViC", "ClinVar", "gnomAD", "COSMIC", "TCGA",
    "UniProt", "OpenTargets", "KEGG", "PubMed", "Ensembl",
    "G12D", "G12C", "G12V", "G12A", "G12S", "G12R", "Q61H", "Q61K", "Q61L", "T790M", "C797S", "V600E",
}
_LOWER_ACRONYMS = {a.lower(): a for a in _ACRONYMS}
_MINOR_WORDS = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "from", "by", "with", "in", "of", "vs", "versus"}


def _heuristic_title(text: str, max_chars: int = 50) -> str:
    clean = re.sub(r"```[\s\S]*?```", " ", text)
    clean = re.sub(r"https?://\S+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return "Clinical Investigation"

    stripped = clean
    for _ in range(3):
        changed = False
        for pat in _TITLE_PREAMBLE_RES:
            m = pat.search(stripped)
            if m:
                stripped = stripped[m.end():].strip()
                changed = True
        if re.match(r"^(?:the|a|an)\s+", stripped, re.I) and len(stripped) > 8:
            stripped = re.sub(r"^(?:the|a|an)\s+", "", stripped, flags=re.I).strip()
            changed = True
        if not changed:
            break

    target = stripped if len(stripped) >= 3 else clean
    target = re.sub(r"^[\"\'`\s\-_=]+", "", target)
    target = re.sub(r"[?!:;.,\"\'`\s\-_=]+$", "", target).strip()
    if not target:
        return "Clinical Investigation"

    words = target.split()
    formatted = []
    for idx, w in enumerate(words):
        lw = w.lower()
        if lw in _LOWER_ACRONYMS:
            formatted.append(_LOWER_ACRONYMS[lw])
        elif "-" in w:
            parts = [
                _LOWER_ACRONYMS.get(p.lower(), p.capitalize()) if p else ""
                for p in w.split("-")
            ]
            formatted.append("-".join(parts))
        elif idx > 0 and lw in _MINOR_WORDS:
            formatted.append(lw)
        elif any(c.isupper() for c in w[1:]) and any(c.islower() for c in w):
            formatted.append(w)
        else:
            formatted.append(w.capitalize())

    res = ""
    for w in formatted:
        cand = f"{res} {w}" if res else w
        if len(cand) > max_chars:
            if len(res.split()) >= 2:
                break
            res = cand[:max_chars].rstrip(",.:;_- ")
            break
        res = cand

    res = re.sub(r"\s+(?:in|of|and|for|on|with|to|at|by|a|the|vs)$", "", res, flags=re.I).strip()
    return res or "Clinical Investigation"


class TitleRequest(BaseModel):
    """Request schema for dynamic conversation title generation."""
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS, description="User query or prompt")
    model: Optional[str] = Field(None, max_length=200, description="Optional model identifier")
    api_key: Optional[str] = Field(None, max_length=500, description="Optional runtime API key")

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: Optional[str]) -> Optional[str]:
        return check_caller_api_key(value)

    @model_validator(mode="after")
    def _check_model(self) -> "TitleRequest":
        # SEC-003: the title route used to accept any model on the server's keys.
        if self.model and not self.api_key and not is_allowed_model(self.model):
            raise ValueError(
                f"Unsupported model {self.model[:80]!r}: choose one listed by /api/models or supply your own API key."
            )
        return self


class TitleResponse(BaseModel):
    """Response schema for dynamic conversation title generation."""
    title: str = Field(..., description="Intelligent, concise dynamic conversation title")
    method: str = Field(..., description="'ai' if generated by LLM, 'heuristic' if extracted by pattern")


class ResetRequest(BaseModel):
    """Reset session request schema."""
    thread_id: str = Field(..., min_length=1, max_length=MAX_THREAD_ID_CHARS)


# Upstream readiness probes are cached so the unauthenticated health route cannot be used to drive outbound traffic.
_health_cache: dict[str, Any] = {"at": 0.0, "readiness": None}


async def _service_readiness() -> dict[str, Any]:
    cached = _health_cache["readiness"]
    if isinstance(cached, dict) and time.monotonic() - float(_health_cache["at"]) < HEALTH_CACHE_SECONDS:
        return dict(cached)
    readiness = await BioContextMCPClient().async_check_service_readiness()
    _health_cache.update(at=time.monotonic(), readiness=readiness)
    return dict(readiness) if isinstance(readiness, dict) else {}


@app.get("/api/health/live")
async def liveness_check() -> dict[str, Any]:
    """Cheap liveness probe: no upstream calls."""
    return {"status": "alive", "service": "Phronesis Clinical R&D API"}


_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I)


def _redact_url(url: Any) -> Optional[str]:
    """scheme://host[:port]/path with user info, query and fragment removed (they can carry credentials)."""
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", "")) if parts.scheme else "<configured>"
    except ValueError:
        return "<configured>"


def _public_adapters(readiness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Adapter name -> status word only (no URLs, no exception text)."""
    out: dict[str, dict[str, Any]] = {}
    for name, value in (readiness.get("direct_adapters") or {}).items():
        status = value.get("status") if isinstance(value, dict) else None
        out[str(name)[:40]] = {"status": status if status in ("ready", "degraded", "unreachable") else "unknown"}
    return out


def _detailed_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    """Readiness for authenticated callers: latencies and error types, with URLs reduced to scheme/host/path."""
    adapters: dict[str, Any] = {}
    for name, value in (readiness.get("direct_adapters") or {}).items():
        if not isinstance(value, dict):
            continue
        item = {k: value[k] for k in ("status", "latency_ms") if k in value}
        if value.get("error"):
            item["error"] = _URL_RE.sub(lambda m: _redact_url(m.group(0)) or "<url>", str(value["error"]))[:200]
        adapters[str(name)[:40]] = item
    return {
        "mcp_remote_url": _redact_url(readiness.get("mcp_remote_url")),
        "mcp_remote_active": bool(readiness.get("mcp_remote_active")),
        "direct_adapters": adapters,
    }


def _provider_flags() -> dict[str, dict[str, bool]]:
    return {
        "nvidia_nim": {"available": usable_key(os.getenv("NVIDIA_API_KEY")) is not None or usable_key(os.getenv("NVIDIA_NIM_API_KEY")) is not None},
        "openai": {"available": usable_key(os.getenv("OPENAI_API_KEY")) is not None},
    }


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    """Public status: booleans and status words only (SEC-005). Configured URLs, upstream error text and the
    vector-store location are served to authenticated callers by /api/health/details."""
    observed_readiness = await _service_readiness()
    return {
        "status": "healthy",
        "service": "Phronesis Clinical R&D API",
        "llm_providers": _provider_flags(),
        "mcp_connectivity": {
            "remote_mcp_active": bool(observed_readiness.get("mcp_remote_active", False)),
            "direct_adapters": _public_adapters(observed_readiness),
        },
        "auth": {"mode": "disabled" if auth_disabled() else "bearer"},
        # Module name -> exception type only: the message can carry paths or configuration.
        "disabled_routers": dict(DISABLED_ROUTERS),
        # Count only: the messages can quote configured values.
        "config_problems": len(validate_environment()),
    }


@app.get("/api/health/details")
async def health_details(user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Diagnostics for authenticated callers: adapter latencies and error types, configured endpoints with
    credentials, query strings and fragments removed."""
    observed_readiness = await _service_readiness()
    milvus_uri = os.getenv("MILVUS_URI")
    return {
        "status": "healthy",
        "llm_providers": {
            **{k: {**v} for k, v in _provider_flags().items()},
            "default_orchestrator": DEFAULT_ORCHESTRATOR_MODEL,
        },
        "vector_store": {"uri": _redact_url(milvus_uri) if milvus_uri else "Milvus Lite (embedded)"},
        "biocontext_service_readiness": _detailed_readiness(observed_readiness),
        "disabled_routers": dict(DISABLED_ROUTERS),
        "config_problems": len(validate_environment()),
    }


@app.get("/api/models")
async def list_models() -> dict[str, Any]:
    """Return supported models catalog for selection in Next.js UI."""
    return {"models": get_supported_models()}


def _request_framework(req: "ChatRequest") -> str:
    """The framework a chat request runs: its own choice, else PHRONESIS_DEFAULT_FRAMEWORK, else Auto routing."""
    return agent_config.resolve_framework(req.framework, os.getenv("PHRONESIS_DEFAULT_FRAMEWORK") or agent_config.AUTO)


def _agent_kwargs(req: "ChatRequest", interactive: bool) -> dict[str, Any]:
    return {
        "framework": _request_framework(req),
        "project_id": req.project_id,
        "agent_options": req.agent_options.to_runtime() if req.agent_options else {},
        "interactive": interactive,
    }


@app.get("/api/frameworks")
async def list_frameworks() -> dict[str, Any]:
    """Orchestration frameworks the backend implements."""
    default = agent_config.resolve_framework(None, os.getenv("PHRONESIS_DEFAULT_FRAMEWORK") or agent_config.AUTO)
    return {
        "default": default,
        "frameworks": [
            {"id": fid, "description": agent_config.FRAMEWORK_DESCRIPTIONS.get(fid, ""), "agentic": agent_config.is_agentic(fid)}
            for fid in agent_config.ALL_FRAMEWORKS
        ],
    }


@app.get("/api/effort-levels")
async def list_effort_levels() -> dict[str, Any]:
    """Available reasoning and thinking effort levels (low, medium, high, ultra)."""
    return {
        "default": agent_config.EFFORT_MEDIUM,
        "levels": list(agent_config.EFFORT_PROFILES.values()),
    }


@app.get("/api/agents")
async def list_agents() -> dict[str, Any]:
    """Return metadata and capabilities for all 4 clinical domain agents."""
    return {
        "orchestrator": {
            "name": "Clinical R&D Orchestrator",
            "role": "Supervisor",
            "description": "Deconstructs complex clinical inquiries and routes tasks across all 4 domain agents and BioContext MCP.",
        },
        "agents": [
            {
                "id": "t2kg",
                "name": "Synapta",
                "domain": "Knowledge Graphs & Disease Targets",
                "description": "Queries the full PrimeKG knowledge graph locally (129k nodes, 4M edges): drug indications, contraindications, targets and side effects, disease genes and phenotypes, mechanism paths, network-proximity repurposing, phenotype-ranked differential diagnosis, polypharmacy and gene-set enrichment; normalizes entity names and provides ChEMBL drug data plus ClinVar classifications, CIViC evidence, cBioPortal per-study prevalence and gnomAD germline allele frequency.",
                "tools": [
                    "PrimeKG full-graph analytics (local)",
                    "PrimeKG Multimodal Subgraph Extraction",
                    "BioContext OpenTargets",
                    "BioContext KEGG",
                    "HGNC/OpenTargets entity normalization",
                    "ChEMBL drug, mechanism and bioactivity",
                    "ClinVar/CIViC/cBioPortal/gnomAD variant evidence",
                ],
            },
            {
                "id": "t2b",
                "name": "Vivarix",
                "domain": "Kinetic Modeling & Systems Biology",
                "description": "Simulates kinetic ODE biological models, time-series parameter scans, SBML/SED-ML dynamics, and steady-state evaluations.",
                "tools": [
                    "SimulateModelTool",
                    "SearchModelsTool",
                    "SteadyStateTool",
                    "CustomPlotterTool",
                    "BioContext UniProt",
                    "Uploaded SBML models",
                ],
            },
            {
                "id": "scp",
                "name": "Lumicell",
                "domain": "Single-Cell Transcriptomics",
                "description": "Analyzes Single Cell Portal (SCP) studies, AnnData expression matrices, cell clusters, and cellular biomarkers.",
                "tools": [
                    "search_studies",
                    "display_studies",
                    "BioContext PanglaoDB",
                    "BioContext Ensembl",
                    "Uploaded .h5ad/.csv datasets",
                ],
            },
            {
                "id": "s2",
                "name": "Lithoscribe",
                "domain": "Biomedical Literature & Clinical Trials",
                "description": "Retrieves and synthesizes research papers, PubMed/EuropePMC articles, Semantic Scholar citations, and clinical trial outcomes.",
                "tools": [
                    "search_tool",
                    "get_single_paper_recommendations",
                    "BioContext EuropePMC",
                    "ClinicalTrials.gov v2 (registry + posted results)",
                    "openFDA FAERS and labels",
                ],
            },
        ],
    }


class MemorySearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    thread_id: Optional[str] = Field(None, max_length=MAX_THREAD_ID_CHARS)
    top_k: Optional[int] = Field(5, ge=1, le=50)


ThreadIdPath = Path(..., min_length=1, max_length=MAX_THREAD_ID_CHARS)


@app.post("/api/reset")
async def reset_session(req: ResetRequest, user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Clear working memory for exactly the given thread_id (caller's memories only)."""
    await asyncio.to_thread(get_clinical_memory_engine().clear_thread_memory, req.thread_id, user_id)
    return {"success": True, "thread_id": req.thread_id, "message": "Session and thread memory reset"}


@app.get("/api/memory/stats")
async def memory_stats_endpoint(user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Return clinical memory metrics for the authenticated user."""
    engine = get_clinical_memory_engine()
    return await asyncio.to_thread(engine.get_memory_stats, user_id)


@app.get("/api/memory/thread/{thread_id}")
async def get_thread_memory_endpoint(thread_id: str = ThreadIdPath, user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Retrieve the authenticated user's dialogue history for a thread."""
    engine = get_clinical_memory_engine()
    history = await asyncio.to_thread(engine.get_conversation_history, thread_id, user_id)
    return {"thread_id": thread_id, "turns": history, "count": len(history)}


@app.get("/api/memory/entities")
async def get_entities_vault_endpoint(user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Retrieve biological entities referenced in the authenticated user's memories."""
    engine = get_clinical_memory_engine()
    entities = await asyncio.to_thread(engine.get_entity_vault, user_id)
    return {"entities": entities, "count": len(entities)}


@app.post("/api/memory/search")
async def search_memory_endpoint(req: MemorySearchRequest, user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """BM25 search across the authenticated user's historical memory chunks and findings."""
    engine = get_clinical_memory_engine()
    results = await asyncio.to_thread(engine.search_memory, req.query, req.thread_id, user_id, None, req.top_k or 5)
    return {"query": req.query, "results": results, "count": len(results)}


@app.delete("/api/memory/thread/{thread_id}")
async def clear_thread_memory_endpoint(thread_id: str = ThreadIdPath, user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Clear the authenticated user's memory for exactly this thread id."""
    engine = get_clinical_memory_engine()
    await asyncio.to_thread(engine.clear_thread_memory, thread_id, user_id)
    return {"success": True, "thread_id": thread_id, "message": "Thread memory cleared"}


@app.delete("/api/memory/clear")
async def clear_all_memory_endpoint(user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """Clear all memories belonging to the authenticated user."""
    engine = get_clinical_memory_engine()
    await asyncio.to_thread(engine.clear_all_memory, user_id)
    return {"success": True, "message": "All of your clinical memory was cleared"}


@app.post("/api/chat/title")
async def generate_chat_title(
    req: TitleRequest,
    user_id: str = Depends(get_current_user),
) -> TitleResponse:
    """
    Generate an intelligent, concise (3-6 words) title for a clinical conversation.
    Uses orchestrator LLM when available, falling back seamlessly to biomedical heuristic title extraction.
    """
    query = req.message.strip()
    if not query:
        return TitleResponse(title="Clinical Investigation", method="heuristic")
    # Per-user limit on model calls for titles (same limiter as chat, its own slots): over it, no model call.
    if not _title_slots.try_acquire(user_id):
        logger.info("Title requests over the per-user limit; returning the heuristic title")
        return TitleResponse(title=_heuristic_title(query), method="heuristic")
    try:
        return await _ai_or_heuristic_title(req, query)
    finally:
        _title_slots.release(user_id)


async def _ai_or_heuristic_title(req: TitleRequest, query: str) -> TitleResponse:
    # Fast AI title generation attempt (timeout 4.5s)
    try:
        from phronesis.utils.llm.llm_factory import get_llm
        from langchain_core.messages import HumanMessage, SystemMessage

        model_id = req.model or DEFAULT_ORCHESTRATOR_MODEL
        llm = get_llm(model_name=model_id, api_key=req.api_key, temperature=0.0, max_tokens=30)

        system_prompt = (
            "You are a clinical bioinformatics assistant. Given the user's prompt, generate an ultra-concise, "
            "professional title of 3 to 6 words capturing the core scientific, therapeutic, or biological topic. "
            "Do NOT include conversational filler, quotes, punctuation marks, or prefixes like 'Discussion on' or 'Inquiry about'. "
            "Preserve standard capitalization for biomedical entities (e.g. KRAS, EGFR, NSCLC, TP53, mRNA). "
            "Return ONLY the plain title text."
        )

        response = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=query[:600]),
            ]),
            timeout=4.5,
        )

        raw_title = str(response.content).strip().strip('"\'`')
        clean_title = re.sub(r"[\r\n]+.*$", "", raw_title)
        clean_title = re.sub(r"[?!:;.,\"\'`]+$", "", clean_title).strip()
        if clean_title and 3 <= len(clean_title) <= 70:
            return TitleResponse(title=clean_title, method="ai")
    except Exception as exc:
        logger.debug("AI title generation skipped or unavailable, using heuristic: %s", exc)

    return TitleResponse(title=_heuristic_title(query), method="heuristic")


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, user_id: str = Depends(get_current_user)) -> dict[str, Any]:
    """
    Standard JSON chat endpoint returning agent reasoning steps,
    transparent thoughts, and synthesized clinical response.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    model_id = req.model or DEFAULT_ORCHESTRATOR_MODEL

    _chat_slots.acquire_or_429(user_id, _CHAT_BUSY_DETAIL)
    try:
        orch = get_authoritative_orchestrator()
        orch_res = await orch.execute_inquiry(
            query=req.message,
            thread_id=thread_id,
            model_name=model_id,
            enable_web_search=req.enable_web_search if req.enable_web_search is not None else True,
            api_key=req.api_key,
            user_id=user_id,
            dataset_ids=req.dataset_ids or [],
            **_agent_kwargs(req, interactive=False),
        )
        return orch_res.to_dict()

    except Exception as e:
        request_id = uuid.uuid4().hex[:12]
        logger.error("Chat orchestration failed [request %s]", request_id, exc_info=True)
        raise HTTPException(
            status_code=500, detail=_internal_error(request_id), headers={"X-Request-ID": request_id}
        ) from e
    finally:
        _chat_slots.release(user_id)


async def sse_frames(queue: "asyncio.Queue[dict[str, Any]]", heartbeat_s: float) -> AsyncGenerator[str, None]:
    """Serialize queued events as SSE frames until done/error, sending a comment ping while the queue is idle.

    Stages can run for over a minute without an event; the ping keeps proxies with idle timeouts from
    closing the stream.
    """
    while True:
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
        except asyncio.TimeoutError:
            yield ": ping\n\n"
            continue
        yield f"data: {json.dumps(ev)}\n\n"
        if ev.get("type") in ("done", "error"):
            return


def _release_once(user_id: str) -> Callable[[], None]:
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            _chat_slots.release(user_id)

    return release


@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, user_id: str = Depends(get_current_user)) -> StreamingResponse:
    """
    Server-Sent Events (SSE) streaming endpoint. Event types, in order of arrival:
    init, then any mix of thought (activity step upsert, keyed by data.id), reasoning_delta
    (the model's own reasoning tokens), content_delta (answer tokens), content_reset
    (drop streamed answer text before a revision), and finally done or error.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    model_id = req.model or DEFAULT_ORCHESTRATOR_MODEL
    framework_id = _request_framework(req)

    _chat_slots.acquire_or_429(user_id, _CHAT_BUSY_DETAIL)
    release = _release_once(user_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_thought_event(event: dict[str, Any]) -> None:
            await queue.put(event)

        yield f"data: {json.dumps({'type': 'init', 'thread_id': thread_id, 'model': model_id, 'framework': framework_id})}\n\n"

        async def worker() -> None:
            try:
                orch = get_authoritative_orchestrator()
                orch_res = await orch.execute_inquiry(
                    query=req.message,
                    thread_id=thread_id,
                    model_name=model_id,
                    enable_web_search=req.enable_web_search if req.enable_web_search is not None else True,
                    api_key=req.api_key,
                    event_callback=on_thought_event,
                    user_id=user_id,
                    dataset_ids=req.dataset_ids or [],
                    **_agent_kwargs(req, interactive=True),
                )
                await queue.put({"type": "done", "result": orch_res.to_dict()})
            except Exception:
                request_id = uuid.uuid4().hex[:12]
                logger.error("Streaming worker error [request %s]", request_id, exc_info=True)
                await queue.put({"type": "error", "detail": _internal_error(request_id), "request_id": request_id})

        task = asyncio.create_task(worker())

        try:
            async for frame in sse_frames(queue, SSE_HEARTBEAT_SECONDS):
                yield frame
        finally:
            if not task.done():
                logger.info("Client disconnected from chat stream. Cancelling worker task %s.", task)
                task.cancel()
            release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        # Also frees the slot when the client disconnects before the generator ever starts.
        background=BackgroundTask(release),
    )


if __name__ == "__main__":
    import uvicorn
    # Development entry point: loopback only. Containers bind 0.0.0.0 inside and publish on 127.0.0.1 (compose).
    uvicorn.run("app.backend.main:app", host="127.0.0.1", port=8000, reload=True)
