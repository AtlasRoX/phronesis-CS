"""
Async openFDA client for FAERS reaction counts, SPL label excerpts and Drugs@FDA application records.

Status mapping: 404 NOT_FOUND is a successful query with zero matches; 403 is a rate-limit or API-key
rejection; 500 parse_exception is a malformed query. Successful responses are cached for 24 h by URL.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from ..utils.http import HttpError, get_json as http_get_json
from ..utils.http_limits import TTLCache, get_limiter

logger = logging.getLogger(__name__)

OPENFDA_BASE = "https://api.fda.gov"
DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"
MAX_LIMIT_WITHOUT_KEY = 999  # limit=1000 returns 403 API_KEY_MISSING without a key
# Per request; a label lookup can make four requests in sequence, under the 35 s registry tool timeout.
DEFAULT_TIMEOUT_S = 10.0

_CACHE = TTLCache(maxsize=512, ttl_s=24 * 3600)


class OpenFdaError(Exception):
    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind  # rate_limit|query|upstream|timeout
        self.status = status


def lucene_term(value: str) -> str:
    """Strip characters with Lucene meaning (quotes, colons, parentheses, operators) from a name."""
    cleaned = re.sub(r"[^A-Za-z0-9 \-]", " ", value or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def faers_search(ingredient: str, brand_names: Optional[list] = None) -> str:
    """FAERS search by active substance (openfda.generic_name misses unharmonized drugs such as osimertinib)."""
    clauses = [f'patient.drug.activesubstance.activesubstancename:"{lucene_term(ingredient)}"']
    for brand in brand_names or []:
        term = lucene_term(brand)
        if term:
            clauses.append(f'patient.drug.medicinalproduct:"{term}"')
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def faers_generic_search(ingredient: str, brand_names: Optional[list] = None) -> str:
    """FAERS search on the openFDA-harmonized generic name: a fallback when the active-substance field has no match
    (some reports record only the product, which openFDA maps to its US generic name)."""
    clauses = [f'patient.drug.openfda.generic_name:"{lucene_term(ingredient)}"']
    for brand in brand_names or []:
        term = lucene_term(brand)
        if term:
            clauses.append(f'patient.drug.medicinalproduct:"{term}"')
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


_NDA_BLA = " AND openfda.application_number:(NDA* OR BLA*)"
_COMBINATION_RE = re.compile(r"\bAND\b|[,/+]", re.IGNORECASE)


def label_search(ingredient: str) -> str:
    """Phrase match on generic_name; it also matches combination products ('SITAGLIPTIN AND METFORMIN ...')."""
    return f'openfda.generic_name:"{lucene_term(ingredient)}"{_NDA_BLA}'


def label_exact_search(generic_name: str) -> str:
    return f'openfda.generic_name.exact:"{generic_name}"{_NDA_BLA}'


def is_combination(generic_name: Optional[str]) -> Optional[bool]:
    """True when an openFDA generic_name lists more than one ingredient; None when it is unknown."""
    return bool(_COMBINATION_RE.search(generic_name)) if generic_name else None


def single_ingredient_name(ingredient: str, terms: List[str]) -> Optional[str]:
    """The generic_name (the INN or the INN plus a salt word) of a single-ingredient product, if any is listed."""
    inn = lucene_term(ingredient).upper()
    candidates = [
        t for t in terms
        if re.fullmatch(r"[A-Z0-9][A-Z0-9 \-]*", t or "") and not is_combination(t) and (t == inn or t.startswith(inn + " "))
    ]
    return next((t for t in candidates if t == inn), candidates[0] if candidates else None)


def label_fallback_search(ingredient: str) -> str:
    term = lucene_term(ingredient)
    return f'spl_product_data_elements:"{term}"' if " " in term else f"spl_product_data_elements:{term}"


RETRY_BACKOFF_S = 0.5


def _lenient_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _error_parts(body: Any) -> Tuple[Any, Optional[str]]:
    """(the body's "error" member, its code) of an openFDA answer."""
    err = body.get("error") if isinstance(body, dict) else None
    code = (err or {}).get("code") if isinstance(err, dict) else None
    return err, code


def _error_detail(err: Any) -> str:
    return str((err or {}).get("details") or (err or {}).get("message") or "")


def _retryable(resp: httpx.Response) -> bool:
    """An upstream 5xx: not a NOT_FOUND answer and not a malformed query (parse_exception)."""
    if resp.status_code < 500:
        return False
    err, code = _error_parts(_lenient_json(resp))
    return code != "NOT_FOUND" and "parse_exception" not in _error_detail(err)


class OpenFdaClient:
    def __init__(
        self,
        base_url: str = OPENFDA_BASE,
        timeout_s: Optional[float] = None,
        api_key: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        cache: Optional[TTLCache] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s or float(os.getenv("PHRONESIS_OPENFDA_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
        self.api_key = api_key if api_key is not None else os.getenv("OPENFDA_API_KEY")
        self.transport = transport
        self.cache = cache if cache is not None else _CACHE

    def clamp_limit(self, limit: int) -> int:
        return max(1, min(int(limit), 1000 if self.api_key else MAX_LIMIT_WITHOUT_KEY))

    async def _get(
        self, path: str, params: Dict[str, Any], reduce: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """(json, found). found is False for 404 NOT_FOUND; other failures raise OpenFdaError.
        reduce() trims a successful body before it is cached and returned (labels reach about 1 MB)."""
        query = dict(params)
        if self.api_key:
            query["api_key"] = self.api_key
        cache_key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s,
                transport=self.transport,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": "Phronesis/1.0"},
            ) as client:
                # One retry (after 0.5 s) on a connection error or an upstream 5xx; timeouts are not retried.
                body = await http_get_json(
                    client, url, params=query, retries=1, backoff_s=RETRY_BACKOFF_S, retry_on=_retryable,
                    retry_timeouts=False, respect_retry_after=False, accept=(200,), expect_json=False,
                    limiter=get_limiter("openfda", 0.0),
                )
        except HttpError as exc:
            if exc.kind == "timeout":
                raise OpenFdaError("timeout", f"openFDA timed out after {self.timeout_s:g}s ({path})") from exc.cause
            if exc.kind == "transport":
                raise OpenFdaError("upstream", f"openFDA unreachable ({type(exc.cause).__name__})") from exc.cause
            resp = exc.response
            assert resp is not None
            err, code = _error_parts(_lenient_json(resp))
            if resp.status_code == 404 or code == "NOT_FOUND":
                return self._not_found(cache_key)
            if resp.status_code in (403, 429):
                label = f"HTTP {resp.status_code}" + (f" {code}" if code else "")
                raise OpenFdaError(
                    "rate_limit",
                    f"openFDA rejected the request ({label}): rate limit reached or API key missing/invalid; "
                    "set OPENFDA_API_KEY for higher limits",
                    resp.status_code,
                ) from None
            detail = _error_detail(err)
            kind = "query" if "parse_exception" in detail else "upstream"
            raise OpenFdaError(
                kind, f"openFDA HTTP {resp.status_code} {code or ''}: {detail[:200]}".strip(), resp.status_code
            ) from None
        if body is None:  # a 200 whose body is not JSON
            body = {}
        _, code = _error_parts(body)
        if code == "NOT_FOUND":
            return self._not_found(cache_key)
        if reduce is not None:
            body = reduce(body)
        out = (body, True)
        self.cache.set(cache_key, out)
        return out

    def _not_found(self, cache_key: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
        out: Tuple[Optional[Dict[str, Any]], bool] = (None, False)
        self.cache.set(cache_key, out)
        return out

    async def reaction_counts(self, search: str, limit: int = 10) -> Tuple[Optional[Dict[str, Any]], bool]:
        return await self._get(
            "drug/event.json",
            {"search": search, "count": "patient.reaction.reactionmeddrapt.exact", "limit": self.clamp_limit(limit)},
        )

    async def total_reports(self, search: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        return await self._get("drug/event.json", {"search": search, "limit": 1}, reduce=_meta_only)

    async def serious_split(self, search: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        return await self._get("drug/event.json", {"search": search, "count": "serious"})

    async def label(self, search: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        return await self._get("drug/label.json", {"search": search, "limit": 1}, reduce=_label_fields)

    async def drugsfda(self, search: str, limit: int = 100) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Drugs@FDA application records (NDA/BLA history) matching a search, trimmed before caching."""
        return await self._get("drug/drugsfda.json", {"search": search, "limit": self.clamp_limit(limit)}, reduce=_drugsfda_fields)

    async def label_generic_names(self, search: str) -> List[str]:
        """Distinct openfda.generic_name values of the labels matching a search, most labels first."""
        body, found = await self._get(
            "drug/label.json", {"search": search, "count": "openfda.generic_name.exact", "limit": 20}
        )
        if not found:
            return []
        return [str(r["term"]) for r in (body or {}).get("results") or [] if r.get("term")]


_LABEL_KEEP = (
    "set_id", "id", "effective_time", "version", "boxed_warning", "warnings_and_cautions", "warnings",
    "contraindications", "indications_and_usage",  # INDICATIONS AND USAGE: the label's approved indications
)
_OPENFDA_KEEP = ("brand_name", "generic_name", "manufacturer_name", "application_number")


def _meta_only(body: Dict[str, Any]) -> Dict[str, Any]:
    return {"meta": body.get("meta") or {}}


def _label_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the label fields the excerpt uses; the rest of an SPL (trial tables, ADR prose) is dropped."""
    results = []
    for r in (body.get("results") or [])[:1]:
        kept = {k: r[k] for k in _LABEL_KEEP if k in r}
        kept["openfda"] = {k: v for k, v in (r.get("openfda") or {}).items() if k in _OPENFDA_KEEP}
        results.append(kept)
    return {"meta": body.get("meta") or {}, "results": results}


_PRODUCT_KEEP = ("brand_name", "active_ingredients", "dosage_form", "route", "marketing_status")
_SUBMISSION_KEEP = (
    "submission_type", "submission_number", "submission_status", "submission_status_date", "review_priority",
    "submission_class_code",
)


def _drugsfda_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the application number, sponsor, products and submission history; drop document links (most of the body)."""
    results = []
    for r in body.get("results") or []:
        if not isinstance(r, dict):
            continue
        results.append({
            "application_number": r.get("application_number"),
            "sponsor_name": r.get("sponsor_name"),
            "products": [{k: p[k] for k in _PRODUCT_KEEP if k in p} for p in r.get("products") or [] if isinstance(p, dict)],
            "submissions": [
                {k: s[k] for k in _SUBMISSION_KEEP if k in s} for s in r.get("submissions") or [] if isinstance(s, dict)
            ],
        })
    return {"meta": body.get("meta") or {}, "results": results}


def clear_cache() -> None:
    _CACHE.clear()
