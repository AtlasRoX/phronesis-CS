"""
Free Web Search and Internet Surfing Tool for Phronesis.
Powered by DuckDuckGo and asynchronous HTTP web scraping; requires no API keys. A SearXNG instance
(PHRONESIS_SEARXNG_URL) or a Tavily API key (TAVILY_API_KEY) is tried first when configured.

DuckDuckGo answers automated traffic it distrusts with an anti-bot challenge (HTTP 202, "anomaly" page) on both
its HTML and Lite endpoints. That is a provider failure, not an empty result: when every provider failed, the
search returns a failed ToolResult ("Web search unavailable: ...") instead of "0 results".

Every request has its own timeout and the whole search-and-surf call has a budget that fits inside the
registry's tool timeout, so results already found are returned instead of being lost to a late fetch.
Surfing only fetches public http(s) addresses (no loopback, private, link-local or reserved hosts, checked on
every redirect hop), reads at most MAX_RESPONSE_BYTES, and parses HTML off the event loop.
"""

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("phronesis.tools.web_search")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
# The registry kills websurfer_search at 20 s; leave headroom for validation and serialization.
DEFAULT_BUDGET_SECONDS = 17.0
# Longer queries are cut at a word boundary: a pasted research brief matches nothing on a search engine.
MAX_QUERY_CHARS = 250
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})
# DuckDuckGo's anti-bot interstitial ("Unfortunately, bots use DuckDuckGo too").
_DDG_CHALLENGE_RE = re.compile(r"anomaly-modal|challenge-form|bots use DuckDuckGo", re.I)


def clean_search_query(query: str) -> str:
    """One line of plain text for a search engine: Unicode hyphens as "-", no control characters, whitespace
    collapsed, at most MAX_QUERY_CHARS (cut at a word boundary)."""
    text = (query or "").translate(_DASHES)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())
    if len(text) > MAX_QUERY_CHARS:
        cut = text[:MAX_QUERY_CHARS]
        text = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return text


def _attempt(provider: str, status: str, detail: str = "") -> dict[str, str]:
    """One provider's outcome: ok (results), empty (answered, nothing found), blocked or error."""
    return {"provider": provider, "status": status, **({"detail": detail[:200]} if detail else {})}


def _ddg_blocked(resp: httpx.Response) -> bool:
    return resp.status_code == 202 or bool(_DDG_CHALLENGE_RE.search(resp.text[:20000]))


class UnsafeURLError(ValueError):
    """The URL points at a non-public address or uses a scheme other than http(s)."""


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        or addr.is_multicast or addr.is_unspecified
    )


async def _check_public_url(url: str) -> None:
    """Raise UnsafeURLError unless every address the host resolves to is public."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError(f"unsupported URL: {url[:120]}")
    host = parsed.hostname
    try:
        ips = [host] if _looks_like_ip(host) else [
            info[4][0] for info in await asyncio.get_running_loop().getaddrinfo(host, parsed.port or None, type=socket.SOCK_STREAM)
        ]
    except socket.gaierror as err:
        raise UnsafeURLError(f"cannot resolve {host}: {err}") from err
    if not ips or not all(_is_public_ip(str(ip).split("%")[0]) for ip in ips):
        raise UnsafeURLError(f"{host} resolves to a non-public address")


def _proxy_configured(url: str) -> bool:
    """True when an environment proxy carries this request (the peer is then the proxy, not the host)."""
    try:
        scheme = urllib.parse.urlsplit(url).scheme
        proxies = urllib.request.getproxies()
        host = urllib.parse.urlsplit(url).hostname or ""
        return bool(proxies.get(scheme) or proxies.get("all")) and not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001
        return False


def _check_connected_peer(resp: httpx.Response, url: str) -> None:
    """Raise UnsafeURLError when the socket the response came over is connected to a non-public address.

    _check_public_url resolves the host before the request, and the HTTP client resolves it again to connect: a
    DNS-rebinding host can answer the first lookup with a public address and the second with 127.0.0.1. The
    connected peer is what was actually reached, so it is checked after the response headers arrive and before
    the body is read. Requests through an environment proxy connect to the proxy and are not checked here."""
    stream = resp.extensions.get("network_stream")
    get_extra = getattr(stream, "get_extra_info", None)
    if not callable(get_extra):
        return  # no socket information (a custom or mock transport)
    try:
        peer = get_extra("server_addr")
    except Exception:  # noqa: BLE001
        logger.debug("No peer address for %s", url[:120], exc_info=True)
        return
    if not peer:
        return
    ip = str(peer[0] if isinstance(peer, (tuple, list)) else peer).split("%")[0]
    try:
        public = _is_public_ip(ip)
    except ValueError:
        return  # not an IP address (a Unix socket path)
    if not public and not _proxy_configured(url):
        host = urllib.parse.urlsplit(url).hostname or url[:120]
        raise UnsafeURLError(f"{host} connected to a non-public address ({ip})")


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def _read_capped(resp: httpx.Response, limit: int = MAX_RESPONSE_BYTES) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > limit:
            chunks.append(chunk[: max(0, limit - (size - len(chunk)))])
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


def _parse_ddg_html(html: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")
    for item in soup.select(".result__body"):
        title_el = item.select_one(".result__title a")
        snippet_el = item.select_one(".result__snippet")
        if title_el and snippet_el:
            href_val = title_el.get("href")
            href = href_val if isinstance(href_val, str) else ""
            # Unwrap DuckDuckGo redirect if present
            uddg_match = re.search(r"uddg=([^&]+)", href)
            actual_url = urllib.parse.unquote(uddg_match.group(1)) if uddg_match else href
            results.append({
                "title": title_el.get_text(strip=True),
                "url": actual_url,
                "snippet": snippet_el.get_text(strip=True),
            })
            if len(results) >= max_results:
                break
    return results


def _parse_ddg_lite(html: str, max_results: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select(".result-link")
    snippets = soup.select(".result-snippet")
    return [
        {
            "title": link.get_text(strip=True),
            "url": str(link.get("href") or ""),
            "snippet": snippets[i].get_text(strip=True) if i < len(snippets) else "",
        }
        for i, link in enumerate(links[:max_results])
    ]


def _extract_article(html: str, url: str, max_chars: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url
    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "svg", "noscript", "form"]):
        tag.decompose()
    # Gather meaningful paragraphs
    paragraphs = [t for t in (p.get_text(strip=True) for p in soup.find_all(["p", "h2", "h3", "li"])) if len(t) > 35]
    content = "\n\n".join(paragraphs)
    if len(content) > max_chars:
        content = content[:max_chars] + "... [Content clipped for brevity]"
    return {"title": title, "url": url, "content": content or "No readable text content extracted."}


class FreeWebSearchClient:
    """
    High-performance, free web search and URL surfing client.
    Does not require any external paid API keys.
    """

    def __init__(self, timeout: float = 6.0, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.timeout = timeout
        self._transport = transport  # tests inject an httpx.MockTransport for surf_url
        self.headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _timeout(self, remaining: Optional[float]) -> float:
        return max(0.5, min(self.timeout, remaining)) if remaining is not None else self.timeout

    async def search(self, query: str, max_results: int = 5, *, deadline: Optional[float] = None) -> list[dict[str, str]]:
        """
        Search the web (configured Tavily / SearXNG first, then DuckDuckGo HTML, then DuckDuckGo Lite).
        Returns top titles, URLs, and text snippets; search_with_status also says what each provider did.
        """
        results, _ = await self.search_with_status(query, max_results, deadline=deadline)
        return results

    async def search_with_status(
        self, query: str, max_results: int = 5, *, deadline: Optional[float] = None
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """(results, attempts): the first provider's results, and each tried provider's outcome
        (_attempt: ok / empty / blocked / error). Providers are tried in order while time is left."""
        clean_query = clean_search_query(query)
        if not clean_query:
            return [], []

        def remaining() -> Optional[float]:
            return None if deadline is None else deadline - time.monotonic()

        providers: list[tuple[str, Any]] = []
        if _tavily_key():
            providers.append(("Tavily", self._tavily))
        if os.getenv("PHRONESIS_SEARXNG_URL", "").strip():
            providers.append(("SearXNG", self._searxng))
        providers += [("DuckDuckGo HTML", self._ddg_html), ("DuckDuckGo Lite", self._ddg_lite)]
        attempts: list[dict[str, str]] = []
        for i, (name, fn) in enumerate(providers):
            left = remaining()
            if i and left is not None and left <= 2.0:
                break
            try:
                results, attempt = await fn(clean_query, max_results, self._timeout(left))
            except Exception as err:  # noqa: BLE001 - one provider failing never ends the search
                results, attempt = [], _attempt(name, "error", f"{type(err).__name__}: {err}")
            attempt["provider"] = name
            attempts.append(attempt)
            if attempt["status"] in ("blocked", "error"):
                logger.warning("Web search provider %s %s: %s", name, attempt["status"], attempt.get("detail", ""))
            if results:
                return results, attempts
        return [], attempts

    async def _ddg_html(self, query: str, max_results: int, timeout: float) -> tuple[list[dict[str, str]], dict[str, str]]:
        async with httpx.AsyncClient(headers=self.headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        return await _ddg_outcome(resp, _parse_ddg_html, max_results)

    async def _ddg_lite(self, query: str, max_results: int, timeout: float) -> tuple[list[dict[str, str]], dict[str, str]]:
        async with httpx.AsyncClient(headers=self.headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get("https://lite.duckduckgo.com/lite/", params={"q": query})
        return await _ddg_outcome(resp, _parse_ddg_lite, max_results)

    async def _searxng(self, query: str, max_results: int, timeout: float) -> tuple[list[dict[str, str]], dict[str, str]]:
        """A SearXNG instance's JSON API (its settings must enable the json format)."""
        base = os.getenv("PHRONESIS_SEARXNG_URL", "").strip().rstrip("/")
        async with httpx.AsyncClient(headers=self.headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
        if resp.status_code in (403, 429):
            return [], _attempt("", "blocked", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            return [], _attempt("", "error", f"HTTP {resp.status_code}")
        rows = (resp.json() or {}).get("results") or []
        results = [
            {"title": _plain(r.get("title")), "url": str(r.get("url") or ""), "snippet": _plain(r.get("content"))}
            for r in rows if isinstance(r, dict) and str(r.get("url") or "").startswith("http")
        ][:max_results]
        return results, _attempt("", "ok" if results else "empty")

    async def _tavily(self, query: str, max_results: int, timeout: float) -> tuple[list[dict[str, str]], dict[str, str]]:
        """The Tavily search API (TAVILY_API_KEY): an LLM-oriented search returning extracted page content."""
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_tavily_key()}"}
        body = {"query": query, "max_results": max(1, min(20, max_results)), "search_depth": "basic",
                "include_answer": False, "include_raw_content": False}
        async with httpx.AsyncClient(headers=headers, timeout=max(timeout, 8.0)) as client:
            resp = await client.post("https://api.tavily.com/search", json=body)
        if resp.status_code in (401, 403):
            return [], _attempt("", "error", f"HTTP {resp.status_code}: the Tavily API key was rejected")
        if resp.status_code in (429, 432, 433):
            return [], _attempt("", "blocked", f"HTTP {resp.status_code}: Tavily rate or usage limit reached")
        if resp.status_code != 200:
            return [], _attempt("", "error", f"HTTP {resp.status_code}")
        rows = (resp.json() or {}).get("results") or []
        results = [
            {"title": _plain(r.get("title")), "url": str(r.get("url") or ""), "snippet": _plain(r.get("content"))[:800]}
            for r in rows if isinstance(r, dict) and str(r.get("url") or "").startswith("http")
        ][:max_results]
        return results, _attempt("", "ok" if results else "empty")

    async def surf_url(self, url: str, max_chars: int = 3500, *, timeout: Optional[float] = None) -> dict[str, Any]:
        """
        Surfs to a public URL, extracts clean main article text, strips ads and navigation.
        """
        if not url or not url.startswith("http"):
            return {"url": url, "error": "Invalid URL"}

        current = url
        hops: list[str] = []  # each redirect target, in order: the page's publisher is the last hop's host
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=timeout or self.timeout, follow_redirects=False,
                transport=getattr(self, "_transport", None),
            ) as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    await _check_public_url(current)
                    async with client.stream("GET", current) as resp:
                        _check_connected_peer(resp, current)  # DNS rebinding: the address actually reached
                        if resp.is_redirect and resp.headers.get("location"):
                            current = urllib.parse.urljoin(current, resp.headers["location"])
                            hops.append(current)
                            continue
                        if resp.status_code != 200:
                            return {"url": url, "error": f"HTTP status {resp.status_code}"}
                        ctype = resp.headers.get("content-type", "")
                        if ctype and "html" not in ctype and "text" not in ctype:
                            return {"url": url, "error": f"Unsupported content type {ctype.split(';')[0]}"}
                        html = await _read_capped(resp)
                        page = await asyncio.to_thread(_extract_article, html, current, max_chars)
                        page["status"] = resp.status_code
                        page["requested_url"] = url
                        page["final_url"] = current
                        if hops:
                            page["redirects"] = hops
                        return page
                return {"url": url, "error": "Too many redirects"}
        except UnsafeURLError as err:
            logger.info("Refused to surf %s: %s", url, err)
            return {"url": url, "error": f"Refused: {err}"}
        except Exception as err:
            logger.warning("Web surfing error for %s: %s", url, err)
            return {"url": url, "error": str(err)}

    async def search_and_surf(
        self, query: str, max_results: int = 3, surf_top: bool = True, budget_seconds: float = DEFAULT_BUDGET_SECONDS
    ) -> Any:
        """
        Convenience method: searches the web, and if surf_top is True and time remains in the budget,
        surfs and extracts full text from the highest-ranked result. Search results are kept even if the
        page fetch fails or is skipped.

        Returns {"query", "results", "top_page", "providers"}. When no provider answered (each was blocked
        or failed), returns a failed ToolResult (UPSTREAM_API_ERROR, "Web search unavailable: ...") instead,
        so the run reports that web search failed rather than that the web has nothing.
        """
        deadline = time.monotonic() + budget_seconds
        search_results, attempts = await self.search_with_status(query, max_results=max_results, deadline=deadline)
        if not search_results and attempts and all(a["status"] in ("blocked", "error") for a in attempts):
            return _unavailable(query, attempts)
        surfed_page: Optional[dict[str, Any]] = None

        left = deadline - time.monotonic()
        if surf_top and search_results and left > 1.5:
            top_url = search_results[0].get("url", "")
            if top_url and top_url.startswith("http"):
                try:
                    surfed_page = await asyncio.wait_for(
                        self.surf_url(top_url, max_chars=2500, timeout=self._timeout(left - 0.5)), timeout=left - 0.5
                    )
                except asyncio.TimeoutError:
                    surfed_page = {"url": top_url, "error": "Page fetch skipped: search budget exhausted"}

        return {
            "query": clean_search_query(query),
            "results": search_results,
            "top_page": surfed_page,
            "providers": attempts,
        }


async def _ddg_outcome(resp: httpx.Response, parse: Any, max_results: int) -> tuple[list[dict[str, str]], dict[str, str]]:
    if _ddg_blocked(resp):
        return [], _attempt("", "blocked", f"anti-bot challenge (HTTP {resp.status_code}); DuckDuckGo is rate-limiting this host")
    if resp.status_code != 200:
        return [], _attempt("", "error", f"HTTP {resp.status_code}")
    results = await asyncio.to_thread(parse, resp.text[:MAX_RESPONSE_BYTES], max_results)
    return results, _attempt("", "ok" if results else "empty")


def _tavily_key() -> str:
    return (os.getenv("TAVILY_API_KEY") or "").strip()


def _plain(text: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _unavailable(query: str, attempts: list[dict[str, str]]) -> Any:
    """The failed ToolResult of a search no provider answered."""
    from .registry import ErrorType, ToolResult  # the registry imports this module lazily; keep it that way

    reasons = "; ".join(f"{a['provider']}: {a['status']}" + (f" ({a['detail']})" if a.get("detail") else "") for a in attempts)
    return ToolResult(
        success=False,
        tool_name="websurfer_search",
        error=f"Web search unavailable: {reasons}",
        error_type=ErrorType.UPSTREAM_API_ERROR,
        data={"query": clean_search_query(query), "results": [], "top_page": None, "providers": attempts},
    )


# Alias for backward compatibility
WebSearch = FreeWebSearchClient

__all__ = ["FreeWebSearchClient", "WebSearch", "UnsafeURLError"]
