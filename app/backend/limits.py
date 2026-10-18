"""Per-user concurrency limits shared by the chat and re-run endpoints."""

from __future__ import annotations

import asyncio
import weakref
from typing import Dict

from fastapi import HTTPException


class UserConcurrencyLimiter:
    """Non-blocking per-user slot counter: a caller over the limit gets 429 instead of queueing.

    Entries are removed when a user's count drops to zero, so the table stays bounded by active users.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._active: Dict[str, int] = {}

    def try_acquire(self, user_id: str) -> bool:
        current = self._active.get(user_id, 0)
        if current >= self.limit:
            return False
        self._active[user_id] = current + 1
        return True

    def acquire_or_429(self, user_id: str, detail: str) -> None:
        if not self.try_acquire(user_id):
            raise HTTPException(status_code=429, detail=detail)

    def release(self, user_id: str) -> None:
        current = self._active.get(user_id, 0)
        if current <= 1:
            self._active.pop(user_id, None)
        else:
            self._active[user_id] = current - 1

    def active(self, user_id: str) -> int:
        return self._active.get(user_id, 0)


class UserSemaphores:
    """One semaphore per user, held only while someone references it (no unbounded growth)."""

    def __init__(self, value: int = 1) -> None:
        self.value = value
        self._items: "weakref.WeakValueDictionary[str, asyncio.Semaphore]" = weakref.WeakValueDictionary()

    def get(self, user_id: str) -> asyncio.Semaphore:
        sem = self._items.get(user_id)
        if sem is None:
            sem = asyncio.Semaphore(self.value)
            self._items[user_id] = sem
        return sem

    def __len__(self) -> int:
        return len(self._items)
