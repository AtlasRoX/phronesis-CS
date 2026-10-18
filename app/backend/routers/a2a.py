"""
Agent2Agent (A2A) protocol v1.0.0 endpoint (feature 17), backed by background jobs (feature 13).

Spec: https://a2a-protocol.org/latest/specification/
- `GET /.well-known/agent-card.json`: the Agent Card (name, description, url, version, capabilities,
  default input/output modes, skills, bearer security scheme). Public, like any discovery document.
- `POST /a2a`: JSON-RPC 2.0 binding with the v1.0 PascalCase methods `SendMessage`, `GetTask`, `ListTasks`
  and `CancelTask`; the pre-1.0 names `message/send`, `tasks/get`, `tasks/list` and `tasks/cancel` are aliases.

A message becomes a job (its text parts are the question; `contextId` is the thread). A Task is the job seen
through A2A: id = job id, contextId = thread id, status.state in submitted | working | input-required |
completed | failed | canceled, and the answer as a text artifact once completed. Runs are non-interactive
(plan approvals are automatic), so `input-required` only appears while a REST client watching the job's
events holds an approval open. `configuration.blocking: true` waits for the result (bounded by
`PHRONESIS_A2A_BLOCKING_TIMEOUT_S`, default 300 s). Every method is scoped to the authenticated user; another
user's task id is TaskNotFound (-32001). Streaming and push notifications are not offered.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.backend.auth import get_current_user
from app.backend.routers.mcp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RpcError,
    _server_info,
    dispatch_rpc,
    parse_body,
)
from phronesis.agentic import config as agent_config
from phronesis.agentic.jobs import JOB_ID_RE, JobError, get_job_manager

router = APIRouter(tags=["a2a"])

A2A_VERSION = "1.0.0"
TASK_NOT_FOUND, TASK_NOT_CANCELABLE = -32001, -32002

_STATE = {
    "queued": "submitted",
    "running": "working",
    "waiting_approval": "input-required",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "canceled",
    "interrupted": "failed",
}

_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "biomedical-research",
        "name": "Biomedical research question",
        "description": (
            "Answers a biomedical research question with literature, target-disease associations, clinical trials, "
            "drug mechanisms and variant evidence; every claim is checked against the retrieved evidence and cited."
        ),
        "tags": ["biomedical", "literature", "drug discovery", "target assessment", "clinical trials"],
        "examples": [
            "What diseases is KRAS associated with, and which drugs target it?",
            "Summarise the clinical trial evidence for osimertinib in EGFR-mutant NSCLC.",
        ],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "text/markdown"],
    },
    {
        "id": "model-simulation",
        "name": "Kinetic model simulation",
        "description": "Simulates an SBML kinetic model from BioModels (time course, steady state, parameter scan) and explains the result.",
        "tags": ["systems biology", "SBML", "COPASI", "simulation"],
        "examples": ["Simulate BIOMD0000000010 for 200 seconds and report the MAPK-PP endpoint."],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "text/markdown"],
    },
]


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> Dict[str, Any]:
    base = _base_url(request)
    info = _server_info()
    return {
        "protocolVersion": A2A_VERSION,
        "name": "Phronesis",
        "description": (
            "Biomedical research agent: plans and runs evidence searches across literature, knowledge graphs, trials, "
            "drug and variant databases and kinetic models, then returns a verified, cited answer."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "supportedInterfaces": [{"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}],
        "provider": {"organization": "Phronesis", "url": base},
        "version": info["version"],
        "documentationUrl": f"{base}/docs",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "text/markdown"],
        "skills": _SKILLS,
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "The Phronesis API bearer token (static token or JWT)."}},
        "security": [{"bearer": []}],
    }


def _text_of(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or message.get("content") or []
    texts: List[str] = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if isinstance(part.get("text"), str) and kind in (None, "text"):
            texts.append(part["text"])
    return "\n".join(t.strip() for t in texts if t.strip())


def _agent_message(text: str, task_id: str, context_id: str) -> Dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": f"msg_{uuid.uuid4().hex[:12]}",
        "taskId": task_id,
        "contextId": context_id,
        "parts": [{"kind": "text", "text": text}],
    }


def task_view(job: Dict[str, Any]) -> Dict[str, Any]:
    state = _STATE.get(job["status"], "unknown")
    status: Dict[str, Any] = {"state": state, "timestamp": job.get("updated_at")}
    task: Dict[str, Any] = {
        "kind": "task",
        "id": job["job_id"],
        "contextId": job["thread_id"],
        "status": status,
        "metadata": {"job_status": job["status"], "run_id": job.get("run_id"), "framework": job.get("framework")},
    }
    result = job.get("result") or {}
    if state == "completed" and result:
        report = result.get("verification_report") or {}
        task["artifacts"] = [
            {
                "artifactId": f"{job['job_id']}-answer",
                "name": "answer",
                "parts": [
                    {"kind": "text", "text": str(result.get("response", ""))},
                    {
                        "kind": "data",
                        "data": {
                            "task_status": result.get("task_status"),
                            "verification": {"passed": report.get("passed"), "verification_score": report.get("verification_score")},
                            "citations": result.get("citations") or {},
                        },
                    },
                ],
            }
        ]
    elif state in ("failed", "canceled") and job.get("error"):
        hint = " Resume it with POST /api/jobs/{id}/resume.".replace("{id}", job["job_id"]) if job["status"] == "interrupted" else ""
        status["message"] = _agent_message(f"{job['error']}{hint}", job["job_id"], job["thread_id"])
    return task


def _job_or_404(user_id: str, task_id: Any) -> Dict[str, Any]:
    if not isinstance(task_id, str) or not JOB_ID_RE.match(task_id):
        raise RpcError(TASK_NOT_FOUND, "Task not found")
    job = get_job_manager().get(task_id, user_id)
    if job is None:
        raise RpcError(TASK_NOT_FOUND, "Task not found")
    return job


async def _send_message(user_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    message = params.get("message")
    text = _text_of(message)
    if not text:
        raise RpcError(INVALID_PARAMS, "The message needs at least one text part")
    if len(text) > 20_000:
        raise RpcError(INVALID_PARAMS, "The message text is longer than 20000 characters")
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    context_id = message.get("contextId") if isinstance(message, dict) else None
    if not isinstance(context_id, str) or not context_id.strip() or len(context_id) > 200:
        context_id = f"a2a_{uuid.uuid4().hex[:12]}"
    framework = agent_config.resolve_framework(str(metadata.get("framework") or ""), agent_config.AGENTIC)
    request = {"message": text, "thread_id": context_id, "framework": framework, "enable_web_search": bool(metadata.get("enable_web_search", True))}
    manager = get_job_manager()
    try:
        job = await manager.submit(user_id, request, origin="a2a")
    except JobError as exc:
        raise RpcError(INTERNAL_ERROR if exc.status_code != 422 else INVALID_PARAMS, str(exc)) from exc
    config = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
    if config.get("blocking"):
        timeout = float(os.getenv("PHRONESIS_A2A_BLOCKING_TIMEOUT_S", "300") or 300)
        await manager.wait(job["job_id"], timeout)
    return task_view(manager.get(job["job_id"], user_id) or job)


async def _list_tasks(user_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        size = max(1, min(100, int(params.get("pageSize") or 50)))
    except (TypeError, ValueError):
        raise RpcError(INVALID_PARAMS, "pageSize must be an integer") from None
    context_id = params.get("contextId") if isinstance(params.get("contextId"), str) else None
    jobs = get_job_manager().list(user_id, limit=size, thread_id=context_id)
    tasks = [task_view(j) for j in jobs]
    wanted = params.get("status")
    if isinstance(wanted, str) and wanted:
        tasks = [t for t in tasks if t["status"]["state"] == wanted]
    return {"tasks": tasks, "nextPageToken": "", "pageSize": size, "totalSize": len(tasks)}


async def _cancel_task(user_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    job = _job_or_404(user_id, params.get("id"))
    if job["status"] in ("succeeded", "failed", "cancelled"):
        raise RpcError(TASK_NOT_CANCELABLE, f"Task is already {_STATE[job['status']]}")
    updated = await get_job_manager().cancel(job["job_id"], user_id, reason="cancelled through A2A")
    return task_view(get_job_manager().get(job["job_id"], user_id) or updated or job)


async def _handle(user_id: str, method: str, params: Dict[str, Any]) -> Any:
    if method in ("SendMessage", "message/send"):
        return await _send_message(user_id, params)
    if method in ("GetTask", "tasks/get"):
        return task_view(_job_or_404(user_id, params.get("id")))
    if method in ("ListTasks", "tasks/list"):
        return await _list_tasks(user_id, params)
    if method in ("CancelTask", "tasks/cancel"):
        return await _cancel_task(user_id, params)
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method[:80]}")


@router.post("/a2a")
async def a2a_endpoint(request: Request, user_id: str = Depends(get_current_user)) -> Response:
    payload, error = await parse_body(request)
    if error is not None:
        return error

    async def handler(method: str, req_id: Any, params: Dict[str, Any]) -> Any:
        return await _handle(user_id, method, params)

    return await dispatch_rpc(payload, handler)


@router.get("/a2a")
async def a2a_get() -> Response:
    return JSONResponse({"detail": "POST JSON-RPC requests here; the Agent Card is at /.well-known/agent-card.json"}, status_code=405, headers={"Allow": "POST"})

