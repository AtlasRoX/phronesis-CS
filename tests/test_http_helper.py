"""phronesis.utils.http: retries, Retry-After, typed errors, cache and limiter (offline, httpx.MockTransport)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import List

import httpx
import pytest

from phronesis.utils.http import (
    ANY_STATUS,
    HttpError,
    get_json,
    post_json,
    request,
    retry_after_seconds,
)
from phronesis.utils.http_limits import TTLCache

URL = "https://api.example.org/v1/thing"


class _Sleeps:
    def __init__(self) -> None:
        self.calls: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _CountingLimiter:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "_CountingLimiter":
        self.entered += 1
        return self

    async def __aexit__(self, *exc) -> None:
        self.exited += 1


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sequence(*responses):
    """A handler answering with each item in turn (an exception class is raised instead)."""
    calls: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item = responses[min(len(calls), len(responses) - 1)]
        calls.append(request)
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("boom", request=request)
        return item

    return handler, calls


# -- retry_after_seconds --------------------------------------------------------------------------------------------

def test_retry_after_seconds_value_default_and_cap():
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "2"})) == 2.0
    assert retry_after_seconds(httpx.Response(429)) == 1.0
    assert retry_after_seconds(httpx.Response(429), default=0.5) == 0.5
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "120"}), cap=3.0) == 3.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "-4"})) == 0.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"}), default=1.5) == 1.5
    assert retry_after_seconds({"Retry-After": "4"}) == 4.0
    assert retry_after_seconds("2.5") == 2.5
    assert retry_after_seconds(None, default=7.0) == 7.0


def test_retry_after_seconds_http_date():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 15.0 <= retry_after_seconds(future, cap=60.0) <= 20.0
    assert retry_after_seconds(future, cap=5.0) == 5.0
    past = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=5), usegmt=True)
    assert retry_after_seconds(past) == 0.0


# -- retries --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_500_then_success():
    handler, calls = _sequence(httpx.Response(500, text="err"), httpx.Response(200, json={"ok": 1}))
    sleeps = _Sleeps()
    async with _client(handler) as client:
        assert await get_json(client, URL, retries=1, backoff_s=0.25, sleep=sleeps) == {"ok": 1}
    assert len(calls) == 2 and sleeps.calls == [0.25]


@pytest.mark.asyncio
async def test_retries_exhausted_raise_http_error():
    handler, calls = _sequence(httpx.Response(503))
    sleeps = _Sleeps()
    async with _client(handler) as client:
        with pytest.raises(HttpError) as err:
            await get_json(client, URL, retries=2, backoff_s=1.0, sleep=sleeps)
    assert err.value.kind == "http" and err.value.status == 503 and err.value.exhausted
    assert err.value.attempts == 3 and len(calls) == 3
    assert sleeps.calls == [1.0, 2.0]  # exponential backoff


@pytest.mark.asyncio
async def test_status_outside_retry_on_fails_at_once():
    handler, calls = _sequence(httpx.Response(403), httpx.Response(200, json={}))
    async with _client(handler) as client:
        with pytest.raises(HttpError) as err:
            await get_json(client, URL, retries=3, sleep=_Sleeps())
    assert err.value.status == 403 and not err.value.exhausted and len(calls) == 1
    assert err.value.response is not None and err.value.response.status_code == 403


@pytest.mark.asyncio
async def test_429_honours_retry_after_seconds():
    handler, calls = _sequence(httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=[1]))
    sleeps = _Sleeps()
    async with _client(handler) as client:
        assert await get_json(client, URL, backoff_s=0.1, sleep=sleeps) == [1]
    assert sleeps.calls == [3.0] and len(calls) == 2


@pytest.mark.asyncio
async def test_429_honours_retry_after_http_date_capped():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
    handler, _ = _sequence(httpx.Response(429, headers={"Retry-After": when}), httpx.Response(200, json={}))
    sleeps = _Sleeps()
    async with _client(handler) as client:
        await get_json(client, URL, retry_after_cap_s=4.0, sleep=sleeps)
    assert sleeps.calls == [4.0]


@pytest.mark.asyncio
async def test_retry_after_can_be_ignored():
    handler, _ = _sequence(httpx.Response(429, headers={"Retry-After": "9"}), httpx.Response(200, json={}))
    sleeps = _Sleeps()
    async with _client(handler) as client:
        await get_json(client, URL, backoff_s=0.5, respect_retry_after=False, sleep=sleeps)
    assert sleeps.calls == [0.5]


@pytest.mark.asyncio
async def test_retry_on_predicate():
    handler, calls = _sequence(httpx.Response(520), httpx.Response(200, json={"x": 1}))
    async with _client(handler) as client:
        body = await get_json(client, URL, retry_on=lambda r: r.status_code >= 500, sleep=_Sleeps())
    assert body == {"x": 1} and len(calls) == 2


@pytest.mark.asyncio
async def test_exhausted_status_in_accept_is_returned():
    handler, calls = _sequence(httpx.Response(502, text="bad gateway"))
    async with _client(handler) as client:
        resp = await request(client, "GET", URL, retries=1, accept=ANY_STATUS, sleep=_Sleeps())
    assert resp.status_code == 502 and len(calls) == 2


# -- typed errors ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_kind_and_retry():
    handler, calls = _sequence(httpx.ReadTimeout, httpx.Response(200, json={"ok": True}))
    async with _client(handler) as client:
        assert await get_json(client, URL, sleep=_Sleeps()) == {"ok": True}
    assert len(calls) == 2

    handler, calls = _sequence(httpx.ReadTimeout)
    async with _client(handler) as client:
        with pytest.raises(HttpError) as err:
            await get_json(client, URL, retry_timeouts=False, sleep=_Sleeps())
    assert err.value.kind == "timeout" and err.value.status is None and len(calls) == 1
    assert isinstance(err.value.cause, httpx.ReadTimeout)


@pytest.mark.asyncio
async def test_transport_kind():
    handler, calls = _sequence(httpx.ConnectError)
    async with _client(handler) as client:
        with pytest.raises(HttpError) as err:
            await get_json(client, URL, retries=1, sleep=_Sleeps())
    assert err.value.kind == "transport" and isinstance(err.value.cause, httpx.ConnectError) and len(calls) == 2


@pytest.mark.asyncio
async def test_bad_json_is_bad_body():
    handler, _ = _sequence(httpx.Response(200, text="<html>not json</html>"))
    async with _client(handler) as client:
        with pytest.raises(HttpError) as err:
            await get_json(client, URL)
        assert err.value.kind == "bad_body" and err.value.status == 200
        assert await get_json(client, URL, expect_json=False) is None


# -- cache, limiter, POST -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_skips_the_request():
    handler, calls = _sequence(httpx.Response(200, json={"n": 1}))
    cache = TTLCache(ttl_s=60)
    async with _client(handler) as client:
        first = await get_json(client, URL, params={"q": "a"}, cache=cache)
        second = await get_json(client, URL, params={"q": "a"}, cache=cache)
        await get_json(client, URL, params={"q": "b"}, cache=cache)
    assert first == second == {"n": 1}
    assert len(calls) == 2 and len(cache) == 2


@pytest.mark.asyncio
async def test_explicit_cache_key_and_failures_not_cached():
    handler, calls = _sequence(httpx.Response(500), httpx.Response(200, json={"v": 2}))
    cache = TTLCache(ttl_s=60)
    async with _client(handler) as client:
        with pytest.raises(HttpError):
            await get_json(client, URL, retries=0, cache=cache, cache_key="k")
        assert len(cache) == 0
        assert await get_json(client, URL, retries=0, cache=cache, cache_key="k") == {"v": 2}
        assert await get_json(client, URL, retries=0, cache=cache, cache_key="k") == {"v": 2}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_limiter_entered_per_attempt():
    handler, _ = _sequence(httpx.Response(503), httpx.Response(200, json={}))
    limiter = _CountingLimiter()
    async with _client(handler) as client:
        await get_json(client, URL, limiter=limiter, sleep=_Sleeps())
    assert limiter.entered == limiter.exited == 2


@pytest.mark.asyncio
async def test_post_json_sends_body_and_params():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"], seen["body"], seen["q"] = req.method, req.content, req.url.params.get("q")
        return httpx.Response(200, json={"data": {"ok": 1}})

    async with _client(handler) as client:
        out = await post_json(client, URL, json={"query": "{x}"}, params={"q": "1"}, headers={"X-Test": "1"})
    assert out == {"data": {"ok": 1}}
    assert seen["method"] == "POST" and b'"query"' in seen["body"] and seen["q"] == "1"


@pytest.mark.asyncio
async def test_timeout_argument_overrides_client_default():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["timeout"] = req.extensions.get("timeout")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=9.0) as client:
        await get_json(client, URL)
        assert seen["timeout"]["read"] == 9.0
        await get_json(client, URL, timeout=2.0)
        assert seen["timeout"]["read"] == 2.0
