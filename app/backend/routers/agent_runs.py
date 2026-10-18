"""
Run control for agentic runs in progress (feature 15, human in the loop): approve plans, steer, cancel.

The first SSE event of an agentic run (chat stream or job events) is
`{"type":"run_control","run_id","framework","interactive"}`; approval phases then send
`{"type":"approval_request","run_id","approval_id","kind","payload"}`. The client answers here. Co-planning
and action guards after Magentic-UI (Mozannar et al. 2025, arXiv:2507.22358).

Every route is scoped to the authenticated user: another user's run id is a 404, exactly like an unknown one.
Controls live in this process only (phronesis.agentic.control).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.backend.auth import get_current_user
from phronesis.agentic.control import ApprovalDecision, RunControl, get_control

router = APIRouter(prefix="/api/agent", tags=["agent"])

RUN_ID_RE = re.compile(r"^run_[0-9a-f]{16}$")
RunIdPath = Path(..., min_length=5, max_length=64)


class ApproveRequest(BaseModel):
    approval_id: str = Field(..., min_length=4, max_length=64, pattern=r"^apr_[0-9a-f]{6,32}$")
    approved: bool
    payload: Optional[Dict[str, Any]] = Field(default=None, description="The edited plan (e.g. sources switched off)")
    message: str = Field(default="", max_length=2000)


class SteerRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


def _control(run_id: str, user_id: str) -> RunControl:
    control = get_control(run_id, user_id) if RUN_ID_RE.match(run_id or "") else None
    if control is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return control


@router.get("/runs/{run_id}")
async def get_run(run_id: str = RunIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    """The run's control state: pending approvals, decisions so far, queued steering, cancelled."""
    return _control(run_id, user_id).to_dict()


@router.post("/runs/{run_id}/approve")
async def approve_run(req: ApproveRequest, run_id: str = RunIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    control = _control(run_id, user_id)
    if len(str(req.payload or "")) > 50_000:
        raise HTTPException(status_code=413, detail="The edited payload is too large")
    ok = control.resolve(req.approval_id, ApprovalDecision(approved=req.approved, payload=req.payload, message=req.message, source="user"))
    if not ok:
        raise HTTPException(status_code=409, detail="No pending approval with that id (already answered or timed out)")
    return {"success": True, "run_id": control.run_id, "approval_id": req.approval_id, "approved": req.approved}


@router.post("/runs/{run_id}/steer")
async def steer_run(req: SteerRequest, run_id: str = RunIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Queue an instruction; it is read before synthesis and after each draft (a steer during drafting triggers one rewrite)."""
    control = _control(run_id, user_id)
    if control.cancelled:
        raise HTTPException(status_code=409, detail="The run was cancelled")
    control.steer(req.message)
    return {"success": True, "run_id": control.run_id, "queued_steering": len(control.peek_steering())}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(req: Optional[CancelRequest] = None, run_id: str = RunIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    control = _control(run_id, user_id)
    control.cancel((req.reason if req and req.reason else "") or "cancelled by the user")
    return {"success": True, "run_id": control.run_id, "cancelled": True}


@router.get("/reliability")
async def reliability(user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Tool reliability telemetry (circuit breakers, retries, cache) when the reliability layer provides it."""
    try:
        from phronesis.tools import reliability as reliability_module
    except Exception:  # noqa: BLE001
        return {"available": False}
    snapshot = getattr(reliability_module, "reliability_snapshot", None)
    if not callable(snapshot):
        return {"available": False}
    try:
        data = snapshot()
    except Exception:  # noqa: BLE001
        return {"available": False, "error": "snapshot failed"}
    return {"available": True, **(data if isinstance(data, dict) else {"snapshot": data})}
