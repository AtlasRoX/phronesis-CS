"""
One async HTTP helper for the upstream API clients: retries with backoff, Retry-After, typed errors, an optional
TTL cache and an optional rate limiter.

- request() sends one request (with retries) and returns the httpx.Response; get_json() / post_json() parse the
  body as JSON and can cache it.
- Failures raise HttpError, whose kind is "timeout" (no response in time), "http" (a status outside `accept`),
  "transport" (connection failed) or "bad_body" (not JSON). `cause` is the underlying httpx/JSON exception and
  `response` the last response (status errors and bad bodies), so a client can map an HttpError onto its own
  exception class and messages.
- Retries: a response whose status is in `retry_on` (a set of codes, or a predicate on the response), a timeout
  (retry_timeouts) or a transport error (retry_transport) is retried up to `retries` times. Attempt n waits
  backoff_s * 2**n; a retried response's Retry-After (seconds or an HTTP-date, capped at retry_after_cap_s)
  replaces that wait unless respect_retry_after is False. A retryable status on the last attempt is returned when
  it is in `accept`, otherwise it raises HttpError(kind="http") with exhausted=True.
- `limiter` (e.g. http_limits.get_limiter(...)) is entered around every attempt; `cache` (http_limits.TTLCache)
  stores parsed JSON bodies (never None) under cache_key, by default (method, url, sorted params) for GETs.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Container, Hashable, Mapping, Optional, Union

import httpx

from .http_limits import TTLCache

__all__ = [
    "HttpError",
    "request",
    "get_json",
    "post_json",
    "retry_after_seconds",
    "DEFAULT_RETRY_ON",
    "SUCCESS",
    "ANY_STATUS",
    "BELOW_400",
    "MAX_RETRY_AFTER_S",
]

DEFAULT_RETRY_ON = (429, 500, 502, 503, 504)
SUCCESS = range(200, 300)  # the default `accept`
ANY_STATUS = range(100, 600)  # accept every status (the caller inspects the response)
BELOW_400 = range(100, 400)
MAX_RETRY_AFTER_S = 30.0

RetryOn = Union[Container[int], Callable[[httpx.Response], bool]]
Sleep = Callable[[float], Awaitable[Any]]


class HttpError(Exception):
    """A failed upstream request. kind: "timeout" | "http" | "transport" | "bad_body"."""

    def __init__(
        self,
        kind: str,
        status: Optional[int],
        message: str,
        *,
        response: Optional[httpx.Response] = None,
        cause: Optional[BaseException] = None,
        attempts: int = 1,
        exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message
        self.response = response
        self.cause = cause
        self.attempts = attempts
        self.exhausted = exhausted  # the failure was retryable but no retry was left


def retry_after_seconds(
    source: Union[httpx.Response, Mapping[str, str], str, None],
    default: float = 1.0,
    cap: float = MAX_RETRY_AFTER_S,
) -> float:
    """Seconds to wait from a Retry-After value (delta-seconds or an HTTP-date), clamped to [0, cap].

    source is a response, a headers mapping or the raw header value. A missing or unparseable value gives
    `default` (not capped)."""
    if isinstance(source, httpx.Response):
        raw = source.headers.get("retry-after")
    elif isinstance(source, Mapping):
        raw = source.get("retry-after", source.get("Retry-After"))
    else:
        raw = source
    if raw is None:
        return default
    try:
        return max(0.0, min(cap, float(raw)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(str(raw).strip())
    except (TypeError, ValueError, IndexError):
        return default
    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(cap, delta)) if math.isfinite(delta) else default


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except Exception:  # noqa: BLE001  a malformed URL still gets a readable message
        return url


def _should_retry(retry_on: RetryOn, resp: httpx.Response) -> bool:
    if callable(retry_on):
        return retry_on(resp)
    return resp.status_code in retry_on


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    json: Any = None,
    content: Any = None,
    retries: int = 1,
    backoff_s: float = 1.0,
    retry_on: RetryOn = DEFAULT_RETRY_ON,
    retry_timeouts: bool = True,
    retry_transport: bool = True,
    respect_retry_after: bool = True,
    retry_after_cap_s: float = MAX_RETRY_AFTER_S,
    timeout: Any = None,
    limiter: Any = None,
    accept: Container[int] = SUCCESS,
    sleep: Optional[Sleep] = None,
) -> httpx.Response:
    """Send one request with retries; return the response (status in `accept`) or raise HttpError."""
    attempts = max(0, retries) + 1
    send_timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
    host = _host(url)
    for attempt in range(attempts):
        last = attempt == attempts - 1
        delay = backoff_s * (2 ** attempt)
        try:
            if limiter is not None:
                async with limiter:
                    resp = await client.request(
                        method, url, params=params, headers=headers, json=json, content=content, timeout=send_timeout
                    )
            else:
                resp = await client.request(
                    method, url, params=params, headers=headers, json=json, content=content, timeout=send_timeout
                )
        except httpx.TimeoutException as exc:
            if retry_timeouts and not last:
                await _sleep(sleep, delay)
                continue
            raise HttpError(
                "timeout", None, f"{host} timed out ({type(exc).__name__})",
                cause=exc, attempts=attempt + 1, exhausted=retry_timeouts,
            ) from exc
        except httpx.TransportError as exc:
            if retry_transport and not last:
                await _sleep(sleep, delay)
                continue
            raise HttpError(
                "transport", None, f"{host} unreachable ({type(exc).__name__}: {exc})",
                cause=exc, attempts=attempt + 1, exhausted=retry_transport,
            ) from exc
        retryable = _should_retry(retry_on, resp)
        if retryable and not last:
            wait = retry_after_seconds(resp, default=delay, cap=retry_after_cap_s) if respect_retry_after else delay
            await _sleep(sleep, wait)
            continue
        if resp.status_code not in accept:
            raise HttpError(
                "http", resp.status_code, f"HTTP {resp.status_code} from {host}",
                response=resp, attempts=attempt + 1, exhausted=retryable,
            )
        return resp
    raise AssertionError("unreachable")  # pragma: no cover


async def _sleep(sleep: Optional[Sleep], seconds: float) -> None:
    # asyncio.sleep is looked up per call so a test that patches it (or passes `sleep`) controls the wait.
    await (sleep or asyncio.sleep)(seconds)


def _parse(resp: httpx.Response, url: str, expect_json: bool) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        if not expect_json:
            return None
        raise HttpError(
            "bad_body", resp.status_code, f"{_host(url)} returned a non-JSON body (HTTP {resp.status_code})",
            response=resp, cause=exc,
        ) from exc


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    cache: Optional[TTLCache],
    cache_key: Optional[Hashable],
    expect_json: bool,
    **kwargs: Any,
) -> Any:
    if cache is not None and cache_key is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    resp = await request(client, method, url, **kwargs)
    body = _parse(resp, url, expect_json)
    if cache is not None and cache_key is not None and body is not None:
        cache.set(cache_key, body)
    return body


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    retries: int = 1,
    backoff_s: float = 1.0,
    retry_on: RetryOn = DEFAULT_RETRY_ON,
    timeout: Any = None,
    cache: Optional[TTLCache] = None,
    cache_key: Optional[Hashable] = None,
    limiter: Any = None,
    expect_json: bool = True,
    **options: Any,
) -> Any:
    """GET url and return its JSON body (see request() for retries and errors; `options` are its other keywords).

    expect_json=False returns None for a body that is not JSON instead of raising HttpError("bad_body")."""
    if cache is not None and cache_key is None:
        cache_key = ("GET", url, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
    return await _json_request(
        client, "GET", url, cache=cache, cache_key=cache_key, expect_json=expect_json,
        params=params, headers=headers, retries=retries, backoff_s=backoff_s, retry_on=retry_on,
        timeout=timeout, limiter=limiter, **options,
    )


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: Any = None,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    retries: int = 1,
    backoff_s: float = 1.0,
    retry_on: RetryOn = DEFAULT_RETRY_ON,
    timeout: Any = None,
    cache: Optional[TTLCache] = None,
    cache_key: Optional[Hashable] = None,
    limiter: Any = None,
    expect_json: bool = True,
    **options: Any,
) -> Any:
    """POST a JSON body and return the JSON answer; cached only when both cache and cache_key are given."""
    return await _json_request(
        client, "POST", url, cache=cache, cache_key=cache_key, expect_json=expect_json,
        json=json, params=params, headers=headers, retries=retries, backoff_s=backoff_s, retry_on=retry_on,
        timeout=timeout, limiter=limiter, **options,
    )
