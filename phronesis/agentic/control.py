"""
Run control for human-in-the-loop work: plan approval, clarification answers, steering messages and
cancellation for a run in progress.

A run gets a RunControl when it starts; its run_id is sent to the client in a "run_control" event and the
client answers through /api/agent/runs/{run_id}/... Controls are per process and per user: another user's
run_id resolves to nothing.

Approval fails closed (AGT-005). A run that was never interactive (plain JSON /api/chat, MCP, a job nobody ever
watched) has no human to ask: its requests are approved automatically (source "auto") and recorded as such. Once a
human could answer (the run started interactive, or a watcher attached), an unanswered request is a refusal:
- no answer within the timeout: approved=False, source "timeout";
- the last watcher left (set_interactive(False)) or a request arrives while nobody is watching: approved=False,
  source "unattended";
- the wait itself failed: approved=False, source "error".
The approval phases then withhold the guarded (expensive or irreversible) work instead of running it unasked.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


class RunCancelled(RuntimeError):
    """Raised at a checkpoint after the user cancelled the run."""


@dataclass
class ApprovalDecision:
    approved: bool
    payload: Optional[Dict[str, Any]] = None  # an edited plan or other edited payload, when the user changed it
    message: str = ""
    source: str = "user"  # user | auto | timeout | unattended | error (only "user" and "auto" can approve)


@dataclass
class _Pending:
    approval_id: str
    kind: str
    payload: Dict[str, Any]
    future: "asyncio.Future[ApprovalDecision]"
    created: float = field(default_factory=time.time)


class RunControl:
    def __init__(self, user_id: str, interactive: bool, run_id: Optional[str] = None) -> None:
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
        self.user_id = user_id
        self.interactive = interactive
        # True once a human could answer (the run started interactive or a watcher attached): from then on an
        # unanswered request is a refusal, never an automatic approval.
        self.human_attached = bool(interactive)
        self.cancelled = False
        self.cancel_reason = ""
        self._steering: List[str] = []
        self._pending: Dict[str, _Pending] = {}
        self.decisions: List[Dict[str, Any]] = []
        self.created = time.time()

    # Cancellation --------------------------------------------------------------------------------------------
    def cancel(self, reason: str = "cancelled by the user") -> None:
        self.cancelled = True
        self.cancel_reason = reason
        for p in list(self._pending.values()):
            if not p.future.done():
                p.future.set_result(ApprovalDecision(approved=False, message=reason, source="user"))

    def checkpoint(self) -> None:
        if self.cancelled:
            raise RunCancelled(self.cancel_reason or "cancelled")

    # Steering ------------------------------------------------------------------------------------------------
    def steer(self, message: str) -> None:
        message = (message or "").strip()
        if message:
            self._steering.append(message[:2000])

    def drain_steering(self) -> List[str]:
        out, self._steering = self._steering, []
        return out

    def peek_steering(self) -> List[str]:
        return list(self._steering)

    # Approval ------------------------------------------------------------------------------------------------
    def pending(self) -> List[Dict[str, Any]]:
        return [
            {"approval_id": p.approval_id, "kind": p.kind, "payload": p.payload, "created": p.created}
            for p in self._pending.values()
            if not p.future.done()
        ]

    @property
    def needs_human_decision(self) -> bool:
        """Guarded work needs an explicit decision: the run is interactive or a human was attached to it."""
        return self.interactive or self.human_attached

    def _refuse(self, kind: str, source: str, message: str) -> ApprovalDecision:
        self.decisions.append({"kind": kind, "approved": False, "source": source, "message": message})
        return ApprovalDecision(approved=False, message=message, source=source)

    async def request_approval(
        self,
        kind: str,
        payload: Dict[str, Any],
        timeout_seconds: float = 300.0,
        announce: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> ApprovalDecision:
        """Wait for the user's decision. A never-interactive run approves automatically; once a human could answer,
        a timeout, the watcher leaving or a failed wait refuse (fail closed).

        announce(approval_id) is awaited after the request is registered and before waiting, so the caller can
        send the {"type": "approval_request", "approval_id": ...} event with an id the API already accepts.
        Edited payloads come back in the shape they were sent, with `run: false` on each switched-off
        source (kind "plan") or node (kind "dag").
        """
        if not self.interactive:
            if self.human_attached:
                return self._refuse(kind, "unattended", "nobody is watching the run; the guarded work was not run")
            decision = ApprovalDecision(approved=True, message="non-interactive run; approved automatically", source="auto")
            self.decisions.append({"kind": kind, "approved": True, "source": "auto"})
            return decision
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        fut: "asyncio.Future[ApprovalDecision]" = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = _Pending(approval_id, kind, payload, fut)
        if announce is not None:
            try:
                await announce(approval_id)
            except Exception:  # noqa: BLE001
                pass  # the request stays answerable through GET /api/agent/runs/{run_id}
        try:
            decision = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            decision = ApprovalDecision(
                approved=False, message=f"no answer within {timeout_seconds:.0f}s; the guarded work was not run", source="timeout"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed wait is a refusal, never an approval
            decision = ApprovalDecision(
                approved=False, message=f"approval could not be obtained ({type(exc).__name__}); the guarded work was not run",
                source="error",
            )
        finally:
            self._pending.pop(approval_id, None)
        self.decisions.append({"kind": kind, "approved": decision.approved, "source": decision.source, "message": decision.message})
        return decision

    def set_interactive(self, interactive: bool, reason: str = "nobody is watching; the guarded work was not run") -> None:
        """Switch interactivity (a job gaining or losing its watcher). Losing the last watcher refuses pending
        requests (source "unattended"): the work they guard is withheld rather than run without a decision."""
        self.interactive = interactive
        if interactive:
            self.human_attached = True
            return
        for p in list(self._pending.values()):
            if not p.future.done():
                p.future.set_result(ApprovalDecision(approved=False, message=reason, source="unattended"))

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        p = self._pending.get(approval_id)
        if p is None or p.future.done():
            return False
        p.future.set_result(decision)
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "interactive": self.interactive,
            "cancelled": self.cancelled,
            "pending": self.pending(),
            "decisions": list(self.decisions),
            "queued_steering": len(self._steering),
        }


_CONTROLS: Dict[str, RunControl] = {}
_MAX_AGE_SECONDS = 6 * 3600


def create_control(user_id: str, interactive: bool, run_id: Optional[str] = None) -> RunControl:
    now = time.time()
    for rid, c in list(_CONTROLS.items()):
        if now - c.created > _MAX_AGE_SECONDS:
            _CONTROLS.pop(rid, None)
    control = RunControl(user_id, interactive, run_id)
    _CONTROLS[control.run_id] = control
    return control


def get_control(run_id: str, user_id: str) -> Optional[RunControl]:
    c = _CONTROLS.get(run_id)
    return c if c is not None and c.user_id == user_id else None


def release_control(run_id: str) -> None:
    _CONTROLS.pop(run_id, None)
