"""
Async ChEMBL REST client. Exact-match lookups only (never /molecule/search.json?q=, which is fuzzy),
always with only= projections, and one retry (after a short backoff) on 5xx, timeout or transport error.

The EBI API is intermittently slow (requests that hang for 40 s) and returns sporadic 500s, so each request
has a 5 s connect / 12 s read timeout and one retry; the registry tool timeouts (drugs extension) leave room
for that. Molecule, target and mechanism lookups are cached for 6 h, and the release for 6 h, when the client
talks to the real API (not to an injected test transport). A failure always raises ChemblError, never an
empty answer, so callers can report "unknown" instead of "none".

Names (molecule and target preferred names) are fetched by a batch request first; when it fails, omits ids, or has
not answered within NAME_HEDGE_S, per-id requests start in parallel (each with its own retry), and whichever answers
first is used, within NAME_DEADLINE_S. Each name found is cached per id, so a later batch with other ids reuses it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import httpx

from ..utils.http import HttpError, get_json as http_get_json
from ..utils.http_limits import TTLCache

logger = logging.getLogger(__name__)

CHEMBL_BASE = "https://www.ebi.ac.uk/chembl/api/data"
COMPOUND_URL = "https://www.ebi.ac.uk/chembl/explore/compound/{id}"
TARGET_URL = "https://www.ebi.ac.uk/chembl/explore/target/{id}"
ASSAY_URL = "https://www.ebi.ac.uk/chembl/explore/assay/{id}"

MOLECULE_FIELDS = (
    "molecule_chembl_id,pref_name,max_phase,molecule_type,first_approval,black_box_warning,"
    "withdrawn_flag,molecule_hierarchy,molecule_synonyms"
)
MECHANISM_FIELDS = (
    "molecule_chembl_id,parent_molecule_chembl_id,mechanism_of_action,action_type,target_chembl_id,max_phase"
)
# Names only: the molecule_synonyms projection makes molecule.json several times slower on the EBI API.
MOLECULE_NAME_FIELDS = "molecule_chembl_id,pref_name,max_phase,molecule_type,first_approval"
TARGET_FIELDS = "target_chembl_id,pref_name"
ACTIVITY_FIELDS = (
    "activity_id,assay_chembl_id,assay_description,assay_type,document_chembl_id,molecule_chembl_id,"
    "target_chembl_id,target_organism,standard_type,standard_relation,standard_value,standard_units,"
    "pchembl_value,data_validity_comment,parent_molecule_chembl_id"
)
TARGET_ACTIVITY_FIELDS = ACTIVITY_FIELDS + ",molecule_pref_name"

DEFAULT_TIMEOUT_S = 12.0  # read/write/pool timeout per request (simple EBI requests can take 10-15 s)
CONNECT_TIMEOUT_S = 5.0
RETRY_BACKOFF_S = 1.0
CACHE_TTL_S = 6 * 3600
_RELEASE_CACHE = TTLCache(maxsize=8, ttl_s=CACHE_TTL_S)
_LOOKUP_CACHE = TTLCache(maxsize=2048, ttl_s=CACHE_TTL_S)
# Lookups whose answers only change with a ChEMBL release (activities are not cached: their bodies are large).
_CACHEABLE_PATHS = ("molecule.json", "molecule/", "target.json", "target/", "mechanism.json")
# Name lookups: per-id requests start when the batch has not answered after NAME_HEDGE_S, and the whole lookup
# returns what it has after NAME_DEADLINE_S (the profile tool timeout leaves room for the mechanism request too).
NAME_HEDGE_S = 6.0
NAME_DEADLINE_S = 25.0
NAME_CONCURRENCY = 6


class ChemblError(Exception):
    """An upstream ChEMBL failure (5xx after retry, timeout, unexpected status).

    kind: "timeout" (no response in time), "http" (an error status), "transport" (connection failed) or
    "bad_body" (not JSON)."""

    def __init__(self, message: str, kind: str = "http") -> None:
        super().__init__(message)
        self.kind = kind


def _retryable(resp: httpx.Response) -> bool:
    """Any 5xx and 429 are retried once (timeouts and connection errors too); other statuses fail at once."""
    return resp.status_code >= 500 or resp.status_code == 429


def _chembl_error(exc: HttpError, path: str) -> ChemblError:
    if exc.kind == "timeout":
        return ChemblError(f"ChEMBL request timed out for {path}: {type(exc.cause).__name__}", kind="timeout")
    if exc.kind == "transport":
        return ChemblError(f"ChEMBL request failed for {path}: {type(exc.cause).__name__}: {exc.cause}", kind="transport")
    if exc.kind == "bad_body":
        return ChemblError(f"ChEMBL returned a non-JSON body for {path}", kind="bad_body")
    return ChemblError(f"ChEMBL HTTP {exc.status} for {path}", kind="http")


class ChemblClient:
    def __init__(
        self,
        base_url: str = CHEMBL_BASE,
        timeout_s: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        cache: Optional[TTLCache] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s or float(os.getenv("PHRONESIS_DRUGINTEL_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
        self.transport = transport
        # An injected transport is a test fixture: never share cached answers with it.
        self.cache = cache if cache is not None else (_LOOKUP_CACHE if transport is None else None)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s, connect=min(CONNECT_TIMEOUT_S, self.timeout_s)),
            transport=self.transport,
            headers={"Accept": "application/json", "User-Agent": "Phronesis/1.0 (research assistant)"},
            follow_redirects=True,
        )

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        """GET a ChEMBL path; returns (status, json or None). 404 is returned, other errors raise ChemblError."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        cache_key = None
        if self.cache is not None and path.startswith(_CACHEABLE_PATHS):
            cache_key = (self.base_url, path, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            async with self._client() as client:
                body = await http_get_json(
                    client, url, params=params, retries=1, backoff_s=RETRY_BACKOFF_S, retry_on=_retryable,
                    respect_retry_after=False, accept=(200,),
                )
        except HttpError as exc:
            if exc.status == 404:
                if cache_key is not None:
                    self.cache.set(cache_key, (404, None))
                return 404, None
            error = _chembl_error(exc, path)
            if exc.exhausted:
                logger.warning("%s (after one retry)", error)
            raise error from exc.cause
        if cache_key is not None:
            self.cache.set(cache_key, (200, body))
        return 200, body

    # -- molecules -------------------------------------------------------------
    async def molecules_by_pref_name(self, name: str) -> List[Dict[str, Any]]:
        _, data = await self._get("molecule.json", {"pref_name__iexact": name, "only": MOLECULE_FIELDS})
        return list((data or {}).get("molecules") or [])

    async def molecules_by_synonym(self, name: str) -> List[Dict[str, Any]]:
        _, data = await self._get(
            "molecule.json", {"molecule_synonyms__molecule_synonym__iexact": name, "only": MOLECULE_FIELDS}
        )
        return list((data or {}).get("molecules") or [])

    async def molecule(self, chembl_id: str) -> Optional[Dict[str, Any]]:
        """One molecule by id, or None when ChEMBL returns 404 (an invalid id)."""
        status, data = await self._get(f"molecule/{chembl_id}.json", {"only": MOLECULE_FIELDS})
        return None if status == 404 else data

    async def molecules_by_ids(self, chembl_ids: List[str]) -> List[Dict[str, Any]]:
        if not chembl_ids:
            return []
        _, data = await self._get(
            "molecule.json",
            {"molecule_chembl_id__in": ",".join(chembl_ids), "only": MOLECULE_FIELDS, "limit": max(20, len(chembl_ids))},
        )
        return list((data or {}).get("molecules") or [])

    async def molecule_names_by_ids(self, chembl_ids: List[str]) -> List[Dict[str, Any]]:
        """Name, max phase, type and first approval for molecule ids (no synonyms, so it stays fast), in id order.

        Batch first, then per-id requests for the ids the batch did not return (failure, timeout or omission);
        see _rows_by_ids. Raises ChemblError only when no request answered at all."""

        async def batch(ids: List[str]) -> List[Dict[str, Any]]:
            _, data = await self._get(
                "molecule.json",
                {"molecule_chembl_id__in": ",".join(ids), "only": MOLECULE_NAME_FIELDS, "limit": max(20, len(ids))},
            )
            return list((data or {}).get("molecules") or [])

        async def single(cid: str) -> Optional[Dict[str, Any]]:
            status, data = await self._get(f"molecule/{cid}.json", {"only": MOLECULE_NAME_FIELDS})
            return None if status == 404 else data

        found = await self._rows_by_ids(chembl_ids, "molecule_name", "molecule_chembl_id", batch, single)
        return [found[i] for i in dict.fromkeys(chembl_ids) if i in found]

    async def _rows_by_ids(
        self,
        ids: List[str],
        namespace: str,
        id_field: str,
        batch: Callable[[List[str]], Awaitable[List[Dict[str, Any]]]],
        single: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    ) -> Dict[str, Dict[str, Any]]:
        """{id: row} for ids, hedging a batch request with per-id requests.

        The batch starts first; per-id requests (NAME_CONCURRENCY at a time, each retried once by _get) start for
        the ids still missing when the batch fails, omits them, or has not answered after NAME_HEDGE_S. A batch
        that answers later still fills in every id it returns. After NAME_DEADLINE_S what was found is returned.
        A 404 per id is an answer (no such record). Raises the first ChemblError only when nothing answered."""
        wanted = list(dict.fromkeys(str(x) for x in ids if x))
        found: Dict[str, Dict[str, Any]] = {}
        if not wanted:
            return found
        if self.cache is not None:
            for cid in wanted:
                row = self.cache.get((namespace, self.base_url, cid))
                if row is not None:
                    found[cid] = row
        missing = [cid for cid in wanted if cid not in found]
        if not missing:
            return found

        answered: Set[str] = set()  # ids with an answer (a row or a 404)
        failed: Set[str] = set()  # ids whose own request failed after its retry (not requested again)
        errors: List[ChemblError] = []
        any_answer = False

        def keep(row: Dict[str, Any]) -> None:
            cid = str(row.get(id_field) or "")
            if cid in missing and cid not in found:
                found[cid] = row
                answered.add(cid)
                if self.cache is not None:
                    self.cache.set((namespace, self.base_url, cid), row)

        sem = asyncio.Semaphore(NAME_CONCURRENCY)

        async def one(cid: str) -> Optional[Dict[str, Any]]:
            async with sem:
                if cid in found:  # the batch answered while this request waited for a slot
                    return found[cid]
                return await single(cid)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + NAME_DEADLINE_S
        hedge_at = loop.time() + NAME_HEDGE_S
        batch_task: Optional["asyncio.Future[List[Dict[str, Any]]]"] = asyncio.ensure_future(batch(missing))
        singles: Dict["asyncio.Future[Optional[Dict[str, Any]]]", str] = {}
        try:
            while True:
                pending_ids = [cid for cid in missing if cid not in answered]
                if not pending_ids:
                    break
                now = loop.time()
                # Per-id requests once the batch is done (failed or incomplete) or slow.
                if batch_task is None or now >= hedge_at:
                    running = set(singles.values())
                    for cid in pending_ids:
                        if cid not in running and cid not in failed:
                            singles[asyncio.ensure_future(one(cid))] = cid
                tasks = set(singles) | ({batch_task} if batch_task is not None else set())
                if not tasks:
                    break
                if now >= deadline:
                    logger.warning("ChEMBL %s lookup: no answer for %s within %.0fs", namespace,
                                   ", ".join(pending_ids), NAME_DEADLINE_S)
                    break
                wake = deadline if (batch_task is None or now >= hedge_at) else min(deadline, hedge_at)
                done, _ = await asyncio.wait(tasks, timeout=max(0.0, wake - now), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is batch_task:
                        batch_task = None
                        try:
                            rows = task.result()
                        except ChemblError as exc:
                            errors.append(exc)
                            logger.warning("ChEMBL %s batch lookup failed (%s); trying ids one by one", namespace, exc)
                            continue
                        any_answer = True
                        for row in rows:
                            keep(row)
                        continue
                    cid = singles.pop(task)
                    try:
                        row = task.result()
                    except ChemblError as exc:
                        errors.append(exc)
                        failed.add(cid)
                        continue
                    any_answer = True
                    answered.add(cid)
                    if row is not None:
                        keep({**row, id_field: row.get(id_field) or cid})
        finally:
            for task in [*singles, *([batch_task] if batch_task is not None else [])]:
                task.cancel()
        if not any_answer and not found and errors:
            raise errors[0]
        return found

    # -- mechanisms ------------------------------------------------------------
    async def mechanisms_for_parent(self, parent_chembl_id: str) -> List[Dict[str, Any]]:
        """Mechanisms by parent: they are often recorded on a salt form (imatinib's live on CHEMBL1642)."""
        _, data = await self._get(
            "mechanism.json", {"parent_molecule_chembl_id": parent_chembl_id, "only": MECHANISM_FIELDS, "limit": 50}
        )
        return list((data or {}).get("mechanisms") or [])

    async def approved_mechanisms_for_target(self, target_chembl_id: str, limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
        _, data = await self._get(
            "mechanism.json",
            {"target_chembl_id": target_chembl_id, "max_phase": 4, "only": MECHANISM_FIELDS, "limit": limit},
        )
        rows = list((data or {}).get("mechanisms") or [])
        total = int(((data or {}).get("page_meta") or {}).get("total_count") or len(rows))
        return rows, total

    # -- targets ---------------------------------------------------------------
    async def human_target_for_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        _, data = await self._get(
            "target.json",
            {
                "target_components__target_component_synonyms__component_synonym__iexact": symbol,
                "organism": "Homo sapiens",
                "target_type": "SINGLE PROTEIN",
                "only": TARGET_FIELDS,
            },
        )
        return list((data or {}).get("targets") or [])

    async def targets_by_ids(self, target_ids: List[str]) -> Dict[str, Optional[str]]:
        """{target id: preferred name}: batch first, then target/{id}.json per missing id (see _rows_by_ids)."""

        async def batch(ids: List[str]) -> List[Dict[str, Any]]:
            _, data = await self._get(
                "target.json",
                {"target_chembl_id__in": ",".join(ids), "only": TARGET_FIELDS, "limit": max(20, len(ids))},
            )
            return list((data or {}).get("targets") or [])

        async def single(tid: str) -> Optional[Dict[str, Any]]:
            status, data = await self._get(f"target/{tid}.json", {"only": TARGET_FIELDS})
            return None if status == 404 else data

        found = await self._rows_by_ids(target_ids, "target_name", "target_chembl_id", batch, single)
        return {tid: row.get("pref_name") for tid, row in found.items()}

    # -- activities ------------------------------------------------------------
    async def activities(self, molecule_chembl_id: str, target_chembl_id: str, limit: int = 10) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Activities by parent molecule: measurements are often recorded on a salt form (imatinib mesylate,
        CHEMBL1642), so filtering on molecule_chembl_id alone drops them."""
        _, data = await self._get(
            "activity.json",
            {
                "parent_molecule_chembl_id": molecule_chembl_id,
                "target_chembl_id": target_chembl_id,
                "standard_type__in": "IC50,Ki,Kd",
                "pchembl_value__isnull": "false",
                "order_by": "-pchembl_value",
                "limit": limit,
                "only": ACTIVITY_FIELDS,
            },
        )
        rows = list((data or {}).get("activities") or [])
        total = ((data or {}).get("page_meta") or {}).get("total_count")
        return rows, (int(total) if total is not None else None)

    async def activities_for_target(self, target_chembl_id: str, limit: int = 200) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Measured activities of any compound against a target, highest pChEMBL first. Used when no drug is named,
        so a target without approved drugs (MYC) still shows the compounds ChEMBL records against it."""
        _, data = await self._get(
            "activity.json",
            {
                "target_chembl_id": target_chembl_id,
                "standard_type__in": "IC50,Ki,Kd,EC50",
                "pchembl_value__isnull": "false",
                "order_by": "-pchembl_value",
                "limit": limit,
                "only": TARGET_ACTIVITY_FIELDS,
            },
        )
        rows = list((data or {}).get("activities") or [])
        total = ((data or {}).get("page_meta") or {}).get("total_count")
        return rows, (int(total) if total is not None else None)

    # -- release ---------------------------------------------------------------
    async def release(self) -> Optional[str]:
        """chembl_db_version from /status.json, cached for 6 h; None when the status call fails (not cached)."""
        cached = _RELEASE_CACHE.get(self.base_url)
        if cached is not None:
            return cached
        try:
            _, data = await self._get("status.json")
            version = (data or {}).get("chembl_db_version")
        except ChemblError as exc:
            logger.warning("ChEMBL status lookup failed: %s", exc)
            return None
        if version:
            _RELEASE_CACHE.set(self.base_url, version)
        return version


def clear_release_cache() -> None:
    _RELEASE_CACHE.clear()


def clear_lookup_cache() -> None:
    _LOOKUP_CACHE.clear()
