"""
Background research jobs (feature 13): submit a question, leave, and reattach to its event stream later.

Routes (all scoped to the authenticated user; another user's job id is a 404):
- POST /api/jobs (body = the /api/chat request fields) -> {job_id, run_id, status}
- GET /api/jobs, GET /api/jobs/{id}
- GET /api/jobs/{id}/events?after=N: SSE that replays the ordered event log after seq N, then follows it live
  with a comment heartbeat; `stream=false` returns the replay as JSON. Each frame carries `id: <seq>`, so
  `Last-Event-ID` also works for reattaching. While a client is attached the job's run is interactive
  (approval requests wait for an answer through /api/agent/runs/{run_id}/approve).
- POST /api/jobs/{id}/cancel, POST /api/jobs/{id}/resume, DELETE /api/jobs/{id}

Durable execution after LangGraph persistence (https://docs.langchain.com/oss/python/langgraph/persistence).
The request body is validated with the chat endpoint's own ChatRequest model (imported lazily, because
app.backend.main imports this router while it is still loading).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.backend.auth import get_current_user
from phronesis.agentic.jobs import JOB_ID_RE, STATUSES, JobError, get_job_manager, public_job

logger = logging.getLogger("phronesis.backend.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

JobIdPath = Path(..., min_length=4, max_length=64)


def _heartbeat_s() -> float:
    try:
        return max(0.05, float(os.getenv("PHRONESIS_SSE_HEARTBEAT_S", "15")))
    except ValueError:
        return 15.0


def _check_id(job_id: str) -> None:
    if not JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=404, detail="Job not found")


def _job_error(exc: JobError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def request_from_body(body: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    """Validate a ChatRequest-shaped body; returns (stored request, runtime api key). Raises 422 on bad input."""
    from app.backend import main as backend_main

    try:
        req = backend_main.ChatRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_input=False))) from exc
    request = {
        "message": req.message,
        "thread_id": req.thread_id,
        "model": req.model,
        "enable_web_search": req.enable_web_search if req.enable_web_search is not None else True,
        "dataset_ids": list(req.dataset_ids or []),
        "framework": backend_main._request_framework(req),
        "project_id": req.project_id,
        "agent_options": req.agent_options.to_runtime() if req.agent_options else {},
    }
    return {k: v for k, v in request.items() if v is not None}, req.api_key


@router.post("", status_code=201)
async def create_job(body: Dict[str, Any] = Body(...), user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    request, api_key = request_from_body(body)
    try:
        job = await get_job_manager().submit(user_id, request, api_key=api_key)
    except JobError as exc:
        raise _job_error(exc) from exc
    return {"job_id": job["job_id"], "run_id": job["run_id"], "status": job["status"], "thread_id": job["thread_id"]}


@router.get("")
async def list_jobs(
    status: Optional[str] = Query(default=None, max_length=32),
    thread_id: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(get_current_user),
) -> Dict[str, Any]:
    if status and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")
    jobs = get_job_manager().list(user_id, limit=limit, status=status, thread_id=thread_id)
    return {"jobs": [public_job(j, include_result=False) for j in jobs], "count": len(jobs)}


@router.get("/{job_id}")
async def get_job(job_id: str = JobIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_id(job_id)
    job = get_job_manager().get(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job)


@router.get("/{job_id}/events")
async def job_events(
    job_id: str = JobIdPath,
    after: int = Query(default=0, ge=0),
    stream: bool = Query(default=True),
    limit: int = Query(default=1000, ge=1, le=10_000),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID", max_length=20),
    user_id: str = Depends(get_current_user),
):
    _check_id(job_id)
    manager = get_job_manager()
    if manager.get(job_id, user_id, with_result=False) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if last_event_id and last_event_id.isdigit() and not after:
        after = int(last_event_id)
    if not stream:
        events = manager.events(job_id, after=after, limit=limit)
        return {"job_id": job_id, "events": events, "last_seq": events[-1]["seq"] if events else after}

    heartbeat = _heartbeat_s()

    async def frames() -> AsyncGenerator[str, None]:
        manager.attach_watcher(job_id)
        try:
            async for ev in manager.follow(job_id, after=after, heartbeat_s=heartbeat):
                if ev is None:
                    yield ": ping\n\n"
                    continue
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"
        finally:
            manager.detach_watcher(job_id)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str = JobIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_id(job_id)
    job = await get_job_manager().cancel(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job, include_result=False)


@router.post("/{job_id}/resume")
async def resume_job(job_id: str = JobIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_id(job_id)
    try:
        job = await get_job_manager().resume(job_id, user_id)
    except JobError as exc:
        raise _job_error(exc) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job, include_result=False)


@router.delete("/{job_id}")
async def delete_job(job_id: str = JobIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_id(job_id)
    if not await get_job_manager().delete(job_id, user_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job_id": job_id}
