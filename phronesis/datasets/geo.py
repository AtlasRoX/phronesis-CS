"""
GEO series lookup through NCBI E-utilities (esearch db=gds, then esummary). The accession is checked
locally first, and every failure degrades to status 'lookup_failed' so it never blocks an upload.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from ..utils.http import HttpError, get_json as http_get_json
from ..utils.http_limits import TTLCache, get_limiter
from .models import GeoVerification

logger = logging.getLogger(__name__)

GSE_RE = re.compile(r"^GSE\d{1,9}$")
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TIMEOUT_S = 10.0
MAX_SAMPLES = 50

_CACHE = TTLCache(maxsize=512, ttl_s=24 * 3600)

# Tests swap this for a factory that returns a client built on httpx.MockTransport.
_client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
    timeout=TIMEOUT_S, headers={"User-Agent": "Phronesis/2.0 (dataset provenance check)"}
)

_ORGANISM_ALIASES = {
    "human": "homo sapiens",
    "mouse": "mus musculus",
    "rat": "rattus norvegicus",
    "zebrafish": "danio rerio",
    "fruit fly": "drosophila melanogaster",
    "yeast": "saccharomyces cerevisiae",
}


def organism_matches(declared: Optional[str], taxon: Optional[str]) -> Optional[bool]:
    """Compare the declared organism with GEO's taxon field ('Homo sapiens', or several separated by '; ')."""
    if not declared or not taxon:
        return None
    d = declared.strip().lower()
    d = _ORGANISM_ALIASES.get(d, d)
    taxa = [t.strip().lower() for t in re.split(r"[;,]", taxon) if t.strip()]
    return d in taxa


def clear_cache() -> None:
    _CACHE.clear()


async def _get_json(client: httpx.AsyncClient, endpoint: str, params: dict) -> Any:
    query = {**params, "retmode": "json", "tool": "phronesis"}
    # The shared limiter spaces calls for the keyed rate when NCBI_API_KEY is set, so the key must be sent.
    if os.getenv("NCBI_EMAIL"):
        query["email"] = os.environ["NCBI_EMAIL"]
    if os.getenv("NCBI_API_KEY"):
        query["api_key"] = os.environ["NCBI_API_KEY"]
    try:
        return await http_get_json(
            client, f"{EUTILS}/{endpoint}", params=query, retries=0, limiter=get_limiter("ncbi_eutils", 0.34)
        )
    except HttpError as exc:
        # lookup_geo records the exception's class name (ReadTimeout, HTTPStatusError, JSONDecodeError, ...).
        if exc.kind == "http" and exc.response is not None:
            exc.response.raise_for_status()
        if exc.cause is not None:
            raise exc.cause from None
        raise


async def _fetch(accession: str) -> GeoVerification:
    async with _client_factory() as client:
        # The gse[ETYP] filter is required: [ACCN] alone also matches every GSM sample of the series.
        search = await _get_json(client, "esearch.fcgi", {"db": "gds", "term": f"{accession}[ACCN] AND gse[ETYP]"})
        result = search.get("esearchresult") or {}
        ids = result.get("idlist") or []
        if str(result.get("count", "0")) == "0" or not ids:
            return GeoVerification(status="not_found", accession=accession)
        uid = str(ids[0])
        summary = await _get_json(client, "esummary.fcgi", {"db": "gds", "id": uid})
    doc = (summary.get("result") or {}).get(uid) or {}
    if str(doc.get("accession", "")).upper() != accession:
        return GeoVerification(status="not_found", accession=accession)
    n_samples = doc.get("n_samples")
    return GeoVerification(
        status="verified",
        accession=accession,
        title=doc.get("title") or None,
        taxon=doc.get("taxon") or None,
        n_samples=int(n_samples) if isinstance(n_samples, (int, str)) and str(n_samples).isdigit() else None,
        pdat=doc.get("pdat") or None,
        pubmedids=[str(p) for p in (doc.get("pubmedids") or []) if str(p).isdigit()],
        bioproject=doc.get("bioproject") or None,
        samples=[str(s.get("accession")) for s in (doc.get("samples") or [])[:MAX_SAMPLES] if isinstance(s, dict) and s.get("accession")],
    )


async def lookup_geo(accession: Optional[str], declared_organism: Optional[str] = None) -> GeoVerification:
    """Verify a GEO series accession; never raises."""
    if not accession:
        return GeoVerification(status="not_requested")
    acc = accession.strip().upper()
    if not GSE_RE.match(acc):
        return GeoVerification(status="not_found", accession=acc[:16], error="not a GEO series accession")
    cached = _CACHE.get(acc)
    if cached is None:
        try:
            cached = await _fetch(acc)
        except Exception as exc:  # noqa: BLE001  timeouts, 429/5xx, malformed JSON
            logger.info("GEO lookup for %s failed: %s", acc, exc)
            return GeoVerification(
                status="lookup_failed",
                accession=acc,
                checked_at=datetime.now(timezone.utc),
                error=type(exc).__name__,
            )
        _CACHE.set(acc, cached)
    out = cached.model_copy(deep=True)
    out.checked_at = datetime.now(timezone.utc)
    if out.status == "verified":
        out.organism_matches = organism_matches(declared_organism, out.taxon)
    return out
