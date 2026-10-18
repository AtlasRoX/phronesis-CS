"""Report export, run manifest and re-run endpoints (owned by EXPORT, feature 9)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from app.backend.auth import get_current_user
from app.backend.limits import UserSemaphores
from phronesis.agentic import config as agent_config
from phronesis.utils.llm.llm_factory import is_allowed_model
from phronesis.export import ExportRequest, build_report_model
from phronesis.provenance.run_manifest import RunManifest, client_view, utc_now
from phronesis.provenance.run_store import get_run_store, valid_run_id

logger = logging.getLogger("phronesis.backend.export")

router = APIRouter(tags=["export"])

MAX_CLIENT_TOOL_CALLS = 40


class ExportFormat(str, Enum):
    docx = "docx"
    pdf = "pdf"
    md = "md"
    json = "json"
    bundle = "bundle"


_MEDIA_TYPES = {
    ExportFormat.docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ExportFormat.pdf: "application/pdf",
    ExportFormat.md: "text/markdown; charset=utf-8",
    ExportFormat.json: "application/json",
    ExportFormat.bundle: "application/zip",
}
_EXTENSIONS = {
    ExportFormat.docx: "docx",
    ExportFormat.pdf: "pdf",
    ExportFormat.md: "md",
    ExportFormat.json: "json",
    ExportFormat.bundle: "zip",
}
_LIBRARIES = {ExportFormat.docx: "python-docx", ExportFormat.pdf: "fpdf2", ExportFormat.bundle: "python-docx/fpdf2"}


class RerunRequest(BaseModel):
    mode: Literal["tools", "full"] = "tools"
    model: Optional[str] = Field(default=None, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_model(self) -> "RerunRequest":
        if self.model and not self.api_key and not is_allowed_model(self.model):
            raise ValueError(
                f"Unsupported model {self.model[:80]!r}: choose one listed by /api/models or supply your own API key."
            )
        return self


class ClientRerunRequest(BaseModel):
    mode: Literal["tools"] = "tools"
    manifest: RunManifest


# Seams for tests: the registry and orchestrator the chat endpoints use.
def _get_registry() -> Any:
    from phronesis.agents import authoritative_orchestrator as orch_module

    return orch_module.get_tool_registry()


def _get_orchestrator() -> Any:
    from phronesis.agents.authoritative_orchestrator import get_authoritative_orchestrator

    return get_authoritative_orchestrator()


# Held only while a re-run references it, so the table does not grow with every user ever seen.
_rerun_locks = UserSemaphores(1)


def _rerun_lock(user_id: str) -> asyncio.Semaphore:
    sem = _rerun_locks.get(user_id)
    if sem.locked():
        raise HTTPException(status_code=429, detail="A re-run is already in progress for this user.")
    return sem


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:48].rstrip("-") or "answer"


def _filename(question: str, fmt: ExportFormat) -> str:
    return f"phronesis-{_slug(question)}-{datetime.now(timezone.utc):%Y%m%d}.{_EXTENSIONS[fmt]}"


def _render(fmt: ExportFormat, report: Any) -> bytes:
    from phronesis import export as exporter

    if fmt is ExportFormat.docx:
        return exporter.render_docx(report)
    if fmt is ExportFormat.pdf:
        return exporter.render_pdf(report)
    if fmt is ExportFormat.md:
        return exporter.render_markdown(report)
    if fmt is ExportFormat.bundle:
        return exporter.render_bundle(report)
    payload = {
        "report": report.model_dump(mode="json"),
        "manifest": client_view(report.manifest) if report.manifest else None,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


@router.post("/api/export/{fmt}")
async def export_message(fmt: ExportFormat, req: ExportRequest, user_id: str = Depends(get_current_user)) -> Response:
    run_id = str((req.message.run_manifest or {}).get("run_id") or "")
    stored = await asyncio.to_thread(get_run_store().load, user_id, run_id) if valid_run_id(run_id) else None
    try:
        report = build_report_model(req.question, req.message, stored_manifest=stored)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        # Posted message JSON with unexpected shapes (e.g. a hand-edited run_manifest).
        logger.info("Export request could not be read: %s", exc)
        raise HTTPException(status_code=422, detail=f"The message could not be read for export: {type(exc).__name__}") from exc
    try:
        body = await asyncio.to_thread(_render, fmt, report)
    except ImportError as exc:
        lib = _LIBRARIES.get(fmt, "a renderer")
        raise HTTPException(status_code=501, detail=f"{fmt.value} export needs {lib}, which is not available: {exc}") from exc
    filename = _filename(req.question, fmt)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
        "X-Phronesis-Provenance": report.provenance,
    }
    return Response(content=body, media_type=_MEDIA_TYPES[fmt], headers=headers)


async def _load_owned(user_id: str, run_id: str) -> Dict[str, Any]:
    manifest = await asyncio.to_thread(get_run_store().load, user_id, run_id) if valid_run_id(run_id) else None
    if manifest is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return manifest


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    return client_view(await _load_owned(user_id, run_id))


@router.post("/api/runs/rerun")
async def rerun_client_manifest(req: ClientRerunRequest, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Replay a manifest from a downloaded bundle (SEC-001).

    When the manifest's run id is one of the caller's stored runs, the stored manifest is replayed instead of the
    posted one. Otherwise the posted calls must pass the replay policy shared with the MCP endpoint: registered,
    not a write/upload/code-execution/generative/docking/calibration/export tool, and no server file named in the
    inputs except a typed dataset path inside the configured dataset roots. A single refused call rejects the
    whole request (400); nothing is executed.
    """
    from phronesis.provenance.rerun import ReplayPolicyError, check_replayable, diff_runs, replay_tools, without_calls

    posted = req.manifest.model_dump(mode="json")
    posted_calls = posted.get("tool_calls") or []
    if len(posted_calls) > MAX_CLIENT_TOOL_CALLS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_CLIENT_TOOL_CALLS} tool calls can be replayed.")
    registry = _get_registry()
    try:
        check_replayable(posted, registry, client_supplied=True)
    except ReplayPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = str(posted.get("run_id") or "")
    stored = await asyncio.to_thread(get_run_store().load, user_id, run_id) if valid_run_id(run_id) else None
    manifest = stored or posted
    client_supplied = stored is None
    refused = check_replayable(manifest, registry, client_supplied=client_supplied)
    manifest = without_calls(manifest, refused)

    started = utc_now()
    async with _rerun_lock(user_id):
        new = await replay_tools(manifest, registry, user_id=user_id)
    report = diff_runs(
        manifest, new, mode="tools", provenance="client_supplied" if client_supplied else "server_store", started_at=started
    )
    if not client_supplied:
        report.notes.append("The posted manifest matched one of your stored runs; the stored manifest was replayed.")
    _note_refused(report, refused)
    return report.model_dump(mode="json")


def _note_refused(report: Any, refused: list) -> None:
    for item in refused[:20]:
        report.notes.append(f"Not replayed: {item.get('tool_name')} (call {item.get('seq')}): {item.get('reason')}")


def _rerun_framework(original: Dict[str, Any]) -> Optional[str]:
    """The framework the original run asked for: the recorded request first, else the framework it finished with."""
    request = original.get("request") if isinstance(original.get("request"), dict) else {}
    for value in (request.get("requested_framework"), request.get("framework"), original.get("framework")):
        if isinstance(value, str) and value in agent_config.ALL_FRAMEWORKS:
            return value
    return None


@router.post("/api/runs/{run_id}/rerun")
async def rerun_stored(run_id: str, req: RerunRequest, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    from phronesis.provenance.rerun import check_replayable, diff_runs, replay_tools, without_calls

    original = await _load_owned(user_id, run_id)
    store = get_run_store()
    started = utc_now()
    if req.mode == "tools":
        registry = _get_registry()
        # The stored run's own calls; tools outside the replay policy are reported, not re-executed.
        refused = check_replayable(original, registry, client_supplied=False)
        replayable = without_calls(original, refused)
        async with _rerun_lock(user_id):
            new = await replay_tools(replayable, registry, user_id=user_id)
        report = diff_runs(replayable, new, mode="tools", provenance="server_store", started_at=started)
        _note_refused(report, refused)
        return report.model_dump(mode="json")

    # SEC-003: the stored model id is re-checked here; server keys only pay for catalog models.
    model_id = req.model or original.get("model_id") or None
    if model_id and not req.api_key and not is_allowed_model(model_id):
        raise HTTPException(
            status_code=400,
            detail=f"The run used {str(model_id)[:80]!r}, which needs your own API key to re-run; supply api_key or choose a model from /api/models.",
        )
    request = original.get("request") if isinstance(original.get("request"), dict) else {}
    routing = original.get("routing") or {}
    dataset_ids = [d for d in (request.get("dataset_ids") or routing.get("dataset_ids") or []) if isinstance(d, str)]
    framework = _rerun_framework(original)
    agent_options = request.get("agent_options") if isinstance(request.get("agent_options"), dict) else {}
    project_id = request.get("project_id") if isinstance(request.get("project_id"), str) else None
    orch = _get_orchestrator()
    async with _rerun_lock(user_id):
        result = await orch.execute_inquiry(
            original.get("query") or "",
            thread_id=f"rerun:{run_id}:{uuid.uuid4().hex[:8]}",
            **({"model_name": model_id} if model_id else {}),
            enable_web_search=bool(original.get("enable_web_search", True)),
            api_key=req.api_key,
            user_id=user_id,
            dataset_ids=dataset_ids or None,
            persist_memory=False,
            recall_memory=False,
            # PRV-004: the original configuration, not the classic pipeline by default.
            **({"framework": framework} if framework else {}),
            **({"agent_options": dict(agent_options)} if agent_options else {}),
            **({"project_id": project_id} if project_id else {}),
            interactive=False,
        )
    new_manifest = getattr(result, "run_manifest", None) or {}
    new_run_id = str(new_manifest.get("run_id") or "")
    stored_new = await asyncio.to_thread(store.load, user_id, new_run_id) if valid_run_id(new_run_id) else None
    original_response = await asyncio.to_thread(store.load_response, user_id, run_id)
    report = diff_runs(
        original,
        stored_new or new_manifest,
        mode="full",
        provenance="server_store",
        original_response=original_response,
        new_response=getattr(result, "response", None),
        started_at=started,
    )
    report.notes.extend(_full_rerun_notes(original, stored_new or new_manifest, framework, req.model))
    return report.model_dump(mode="json")


def _full_rerun_notes(original: Dict[str, Any], new: Dict[str, Any], framework: Optional[str], model_override: Optional[str]) -> list:
    """What a full re-run could not reproduce, so the diff is not blamed on model non-determinism alone."""
    notes = []
    request = original.get("request") if isinstance(original.get("request"), dict) else {}
    if not request:
        notes.append(
            "This run was stored before request settings were recorded: agent options and project memory could not be "
            f"restored; the re-run used the framework {framework or 'default'}."
        )
    elif request.get("project_id"):
        notes.append("Project memory was recalled as it is now, not as it was when the original run was made.")
    if request.get("interactive"):
        notes.append("The original run was interactive; the re-run approved plans automatically.")
    if model_override and model_override != original.get("model_id"):
        notes.append(f"The re-run used the model {model_override!r} instead of the original {original.get('model_id')!r}.")
    before = (original.get("llm_params") or {}).get("served_models") or []
    after = (new.get("llm_params") or {}).get("served_models") or []
    if before and after and sorted(before) != sorted(after):
        notes.append(f"The models that answered differ: original {', '.join(before)}; re-run {', '.join(after)}.")
    new_fw, old_fw = new.get("framework"), original.get("framework")
    if new_fw and old_fw and new_fw != old_fw:
        notes.append(f"The re-run finished with the framework {new_fw!r}; the original finished with {old_fw!r}.")
    return notes
