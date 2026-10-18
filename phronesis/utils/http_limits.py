"""
Process-wide politeness helpers for upstream APIs: named minimum-interval limiters and a small TTL cache.

Limiter conventions (share one name per upstream so features never exceed its published limit):
  "ncbi_eutils"    0.34 s (0.11 s when NCBI_API_KEY is set; ClinVar, dbSNP and GEO share it)
  "gnomad"         2.0 s
  "crossref"       0.2 s
  "clinicaltrials" 0.0 s
  "openfda"        0.0 s
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Hashable, Optional


class MinIntervalLimiter:
    """Async context manager enforcing a minimum spacing between call starts, across tasks and event loops."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _reserve(self) -> float:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self.min_interval_s
            return start - now

    async def __aenter__(self) -> "MinIntervalLimiter":
        if self.min_interval_s > 0:
            delay = self._reserve()
            if delay > 0:
                await asyncio.sleep(delay)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


_LIMITERS: Dict[str, MinIntervalLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def get_limiter(name: str, min_interval_s: float) -> MinIntervalLimiter:
    """Return the singleton limiter for name; the first caller's interval wins (ncbi_eutils honors NCBI_API_KEY)."""
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(name)
        if lim is None:
            interval = min_interval_s
            if name == "ncbi_eutils" and os.getenv("NCBI_API_KEY"):
                interval = min(interval, 0.11)
            lim = MinIntervalLimiter(interval)
            _LIMITERS[name] = lim
        return lim


class TTLCache:
    """Thread-safe LRU cache whose entries expire ttl_s seconds after they were set."""

    def __init__(self, maxsize: int = 1024, ttl_s: float = 3600) -> None:
        self.maxsize = max(1, int(maxsize))
        self.ttl_s = float(ttl_s)
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
