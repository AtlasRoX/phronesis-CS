"""
Upstream data-source versions and BioModels pins for the run manifest (owned by EXPORT, feature 9).

Each probe is cached for an hour per process. A failed probe is recorded as {"value": None, "error": ...};
no version is ever guessed. Probes are off under pytest (and with PHRONESIS_PROVENANCE_PROBES=0) so tests
never touch the network; tests pass an httpx transport to exercise the parsers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

OPENTARGETS_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
KEGG_INFO_URL = "https://rest.kegg.jp/info/pathway"
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
BIOMODELS_FILES = "https://www.biomodels.org/model/files/{acc}?format=json"
# www.ebi.ac.uk/biomodels/{acc} redirects here twice; requests still follow redirects.
BIOMODELS_META = "https://www.biomodels.org/{acc}?format=json"
CTGOV_VERSION_URL = "https://clinicaltrials.gov/api/v2/version"
CHEMBL_STATUS_URL = "https://www.ebi.ac.uk/chembl/api/data/status.json"
OPENFDA_EVENT_URL = "https://api.fda.gov/drug/event.json"

SOURCE_KEYS = ("opentargets", "kegg", "europepmc", "clinicaltrials", "chembl", "openfda")
CACHE_TTL_S = 3600.0
FAILURE_TTL_S = 300.0  # a failing upstream (KEGG asks for light traffic) is retried at most every 5 minutes
PROBE_TIMEOUT_S = 8.0

_ACCESSION_RE = re.compile(r"^(?:BIOMD|MODEL)\d{10}$")
_KEGG_UPDATE_RE = re.compile(r"Last update (\d{4}/\d{2}/\d{2})")
_OT_META_QUERY = "{ meta { name apiVersion { x y z } dataVersion { year month iteration } } }"

_cache: Dict[str, Tuple[float, Any]] = {}


def probes_enabled() -> bool:
    flag = os.getenv("PHRONESIS_PROVENANCE_PROBES", "").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def clear_cache() -> None:
    _cache.clear()


def _cached(key: str) -> Optional[Any]:
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    return None


def _store(key: str, value: Any, ttl_s: float = CACHE_TTL_S) -> Any:
    _cache[key] = (time.monotonic() + ttl_s, value)
    return value


def _failure(exc: BaseException | str) -> Dict[str, Any]:
    if isinstance(exc, str):
        return {"value": None, "error": exc[:200]}
    return {"value": None, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def _client(transport: Optional[httpx.AsyncBaseTransport]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=PROBE_TIMEOUT_S,
        follow_redirects=True,
        transport=transport,
        headers={"User-Agent": "Phronesis-RunManifest/1.0"},
    )


# -----------------------------------------------------------------------------
# Data-source versions
# -----------------------------------------------------------------------------

def parse_opentargets_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    meta = ((payload or {}).get("data") or {}).get("meta") or {}
    api = meta.get("apiVersion") or {}
    data = meta.get("dataVersion") or {}
    api_version = ".".join(str(api[k]) for k in ("x", "y", "z") if api.get(k) is not None) or None
    data_parts = [str(data[k]) for k in ("year", "month", "iteration") if data.get(k) is not None]
    data_version = ".".join(data_parts) or None
    if not data_version:
        return _failure("Open Targets meta carried no dataVersion")
    return {"value": data_version, "api": api_version, "data": data_version, "name": meta.get("name")}


def parse_kegg_info(text: str) -> Dict[str, Any]:
    m = _KEGG_UPDATE_RE.search(text or "")
    if not m:
        return _failure("KEGG info carried no 'Last update' line")
    return {"value": m.group(1), "last_update": m.group(1)}


def parse_europepmc_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    version = (payload or {}).get("version")
    if not version:
        return _failure("EuropePMC response carried no version")
    return {"value": str(version), "version": str(version)}


def parse_ctgov_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    """/api/v2/version: {"apiVersion": "2.0.3", "dataTimestamp": "2026-10-13T09:00:05"}; the data timestamp pins the
    registry snapshot the trial search saw."""
    stamp = (payload or {}).get("dataTimestamp")
    if not stamp:
        return _failure("ClinicalTrials.gov version carried no dataTimestamp")
    return {"value": str(stamp), "data_timestamp": str(stamp), "api": (payload or {}).get("apiVersion")}


def parse_chembl_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    version = (payload or {}).get("chembl_db_version")
    if not version:
        return _failure("ChEMBL status carried no chembl_db_version")
    return {"value": str(version), "release_date": (payload or {}).get("chembl_release_date")}


def parse_openfda_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    updated = ((payload or {}).get("meta") or {}).get("last_updated")
    if not updated:
        return _failure("openFDA meta carried no last_updated")
    return {"value": str(updated), "last_updated": str(updated)}


async def _probe_ctgov(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(CTGOV_VERSION_URL)
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_ctgov_version(resp.json())


async def _probe_chembl(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(CHEMBL_STATUS_URL, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_chembl_status(resp.json())


async def _probe_openfda(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(OPENFDA_EVENT_URL, params={"limit": 1})
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_openfda_meta(resp.json())


async def _probe_opentargets(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.post(OPENTARGETS_GRAPHQL, json={"query": _OT_META_QUERY})
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_opentargets_meta(resp.json())


async def _probe_kegg(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(KEGG_INFO_URL)
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_kegg_info(resp.text)


async def _probe_europepmc(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(
        EUROPEPMC_SEARCH,
        params={"query": "EXT_ID:10712587 AND SRC:MED", "format": "json", "resultType": "lite", "pageSize": 1},
    )
    if resp.status_code != 200:
        return _failure(f"HTTP {resp.status_code}")
    return parse_europepmc_version(resp.json())


_PROBES = {
    "opentargets": _probe_opentargets,
    "kegg": _probe_kegg,
    "europepmc": _probe_europepmc,
    "clinicaltrials": _probe_ctgov,
    "chembl": _probe_chembl,
    "openfda": _probe_openfda,
}


async def get_source_versions(transport: Optional[httpx.AsyncBaseTransport] = None) -> Dict[str, Dict[str, Any]]:
    """Versions of the upstream sources the agents query; each value is cached for an hour."""
    if transport is None and not probes_enabled():
        return {k: _failure("version probes disabled") for k in SOURCE_KEYS}
    out: Dict[str, Dict[str, Any]] = {}
    missing = []
    for key in SOURCE_KEYS:
        hit = _cached(f"src:{key}") if transport is None else None
        if hit is not None:
            out[key] = hit
        else:
            missing.append(key)
    if missing:
        async with _client(transport) as client:
            results = await asyncio.gather(*(_PROBES[k](client) for k in missing), return_exceptions=True)
        for key, res in zip(missing, results):
            value = _failure(res) if isinstance(res, BaseException) else res
            out[key] = value
            if transport is None:
                _store(f"src:{key}", value, CACHE_TTL_S if value.get("value") is not None else FAILURE_TTL_S)
    return {k: out[k] for k in SOURCE_KEYS}


# -----------------------------------------------------------------------------
# BioModels pin
# -----------------------------------------------------------------------------

def vivarix_cache_path(accession: str) -> Path:
    from ..utils.paths import data_dir

    # Same location as VivarixSimulationEngine._cache_dir, so the pin finds the file Vivarix downloaded.
    root = data_dir("biomodels_cache") or Path(tempfile.gettempdir()) / "phronesis_biomodels_cache"
    return root / f"{accession}.cps"


def _local_cache_sha256(accession: str) -> Optional[str]:
    path = vivarix_cache_path(accession)
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _json_or_none(resp: httpx.Response) -> Optional[Any]:
    if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or "").lower():
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def resolve_biomodel_pin(
    accession: str, transport: Optional[httpx.AsyncBaseTransport] = None
) -> Dict[str, Any]:
    """Latest revision, main SBML file and its sha256 from BioModels, plus the sha256 of the local .cps cache."""
    now = datetime.now(timezone.utc).isoformat()
    pin: Dict[str, Any] = {
        "accession": accession,
        "latest_revision": None,
        "main_file": None,
        "main_file_sha256": None,
        "local_cache_sha256": None,
        "resolved_at": now,
        "error": None,
    }
    if not _ACCESSION_RE.match(accession or ""):
        pin["error"] = "not a BioModels accession"
        return pin
    try:
        pin["local_cache_sha256"] = await asyncio.to_thread(_local_cache_sha256, accession)
    except OSError as exc:
        pin["error"] = f"local cache unreadable: {type(exc).__name__}"

    if transport is None and not probes_enabled():
        pin["error"] = "BioModels probes disabled"
        return pin
    cache_key = f"bm:{accession}"
    hit = _cached(cache_key) if transport is None else None
    if hit is not None:
        return {**pin, **hit, "local_cache_sha256": pin["local_cache_sha256"]}

    errors = []
    remote: Dict[str, Any] = {}
    try:
        async with _client(transport) as client:
            files_resp, meta_resp = await asyncio.gather(
                client.get(BIOMODELS_FILES.format(acc=accession)),
                client.get(BIOMODELS_META.format(acc=accession)),
                return_exceptions=True,
            )
    except Exception as exc:  # noqa: BLE001
        files_resp = meta_resp = exc

    if isinstance(files_resp, BaseException):
        errors.append(f"files: {type(files_resp).__name__}")
    else:
        files = _json_or_none(files_resp)
        main = (files or {}).get("main") or []
        if main and isinstance(main[0], dict) and main[0].get("sha256sum"):
            remote["main_file"] = main[0].get("name")
            remote["main_file_sha256"] = main[0].get("sha256sum")
        else:
            errors.append(f"files: HTTP {files_resp.status_code}")

    if isinstance(meta_resp, BaseException):
        errors.append(f"metadata: {type(meta_resp).__name__}")
    else:
        meta = _json_or_none(meta_resp)
        revisions = ((meta or {}).get("history") or {}).get("revisions") or []
        # Revision numbers are not contiguous (1, 2, 4), so take the last entry rather than counting.
        last = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
        if isinstance(last.get("version"), int):
            remote["latest_revision"] = last["version"]
        else:
            errors.append(f"metadata: HTTP {meta_resp.status_code}")

    pin.update(remote)
    if errors:
        pin["error"] = "; ".join(([pin["error"]] if pin["error"] else []) + errors)
    elif transport is None:
        _store(cache_key, remote)
    return pin
