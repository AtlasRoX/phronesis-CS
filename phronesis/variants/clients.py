"""
Async adapters for ClinVar (NCBI E-utilities), CIViC GraphQL, gnomAD GraphQL and the cBioPortal REST API.

Each client wraps a caller-owned httpx.AsyncClient, so tests inject an httpx.MockTransport. Upstream
politeness is process-wide: ClinVar shares the 'ncbi_eutils' limiter, gnomAD has its own 2 s spacing
(its third rapid call returns an HTML 429), cBioPortal has a 0.1 s spacing, and static cBioPortal/CIViC lookups
(including CIViC evidence and assertions) are cached for 24 h. ClinVar, CIViC and cBioPortal retry once on a 5xx
response or a connection error.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from ..utils.http import ANY_STATUS, HttpError, request as http_request, retry_after_seconds
from ..utils.http_limits import TTLCache, get_limiter
from .models import ClinVarRecord, review_stars

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CIVIC_URL = "https://civicdb.org/api/graphql"
GNOMAD_URL = "https://gnomad.broadinstitute.org/api"
CBIO_BASE = "https://www.cbioportal.org/api"
USER_AGENT = "Phronesis-Synapta/1.0 (variant intelligence)"

_DAY = 24 * 3600
_STATIC_CACHE = TTLCache(maxsize=256, ttl_s=_DAY)  # cBioPortal /studies, gene panels, gene lookups, CIViC gene ids
_GNOMAD_CACHE = TTLCache(maxsize=512, ttl_s=3600)

# Indirection points for tests (no real sleeping or spacing offline).
_sleep = asyncio.sleep


def _limiter(name: str, min_interval_s: float):
    return get_limiter(name, min_interval_s)


def clear_caches() -> None:
    _STATIC_CACHE.clear()
    _GNOMAD_CACHE.clear()


class UpstreamError(Exception):
    """An upstream call failed; `detail` is short and safe to show in the source status line."""

    def __init__(self, detail: str, status: Optional[int] = None, rate_limited: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.rate_limited = rate_limited


def _json(resp: httpx.Response, source: str) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError(f"{source} returned a non-JSON response (HTTP {resp.status_code})", resp.status_code) from exc


RETRY_DELAY_S = 0.5


RETRY_AFTER_CAP_S = 3.0


def _is_5xx(resp: httpx.Response) -> bool:
    return resp.status_code >= 500


async def _send_retrying(
    source: str, http: httpx.AsyncClient, method: str, url: str, *, limiter: Any = None, **kwargs: Any
) -> httpx.Response:
    """Send once more (after RETRY_DELAY_S) on a 5xx response or a connection error, and return the last response
    whatever its status. A timeout is raised as the httpx exception (the per-source budget handles it)."""
    try:
        return await http_request(
            http, method, url, retries=1, backoff_s=RETRY_DELAY_S, retry_on=_is_5xx, retry_timeouts=False,
            respect_retry_after=False, accept=ANY_STATUS, limiter=limiter, sleep=_sleep, **kwargs,
        )
    except HttpError as exc:
        if exc.kind == "timeout" and exc.cause is not None:
            raise exc.cause from None
        raise UpstreamError(f"{source} unreachable ({type(exc.cause).__name__})") from exc.cause


# -----------------------------------------------------------------------------
# ClinVar
# -----------------------------------------------------------------------------

class ClinVarClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    @staticmethod
    def _params(**params: Any) -> Dict[str, Any]:
        out = {**params, "retmode": "json", "tool": "phronesis"}
        if os.getenv("NCBI_EMAIL"):
            out["email"] = os.environ["NCBI_EMAIL"]
        if os.getenv("NCBI_API_KEY"):
            out["api_key"] = os.environ["NCBI_API_KEY"]
        return out

    async def _get(self, path: str, params: Dict[str, Any]) -> Any:
        for attempt in range(2):
            resp = await _send_retrying(
                "ClinVar", self.http, "GET", f"{EUTILS_BASE}/{path}", params=self._params(**params),
                limiter=_limiter("ncbi_eutils", 0.34),
            )
            if resp.status_code == 429:
                if attempt == 0:
                    await _sleep(retry_after_seconds(resp, default=1.0, cap=RETRY_AFTER_CAP_S))
                    continue
                raise UpstreamError("HTTP 429 rate limited by NCBI E-utilities", 429, rate_limited=True)
            if resp.status_code >= 400:
                raise UpstreamError(f"ClinVar HTTP {resp.status_code}", resp.status_code)
            return _json(resp, "ClinVar")
        raise UpstreamError("ClinVar request failed")  # pragma: no cover

    async def esearch(self, term: str, retmax: int = 20) -> List[str]:
        data = await self._get("esearch.fcgi", {"db": "clinvar", "term": term, "retmax": retmax})
        result = (data or {}).get("esearchresult") or {}
        if "ERROR" in result:
            raise UpstreamError(f"ClinVar esearch error: {str(result['ERROR'])[:120]}")
        return [str(i) for i in result.get("idlist") or []]

    async def esummary(self, ids: List[str]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        data = await self._get("esummary.fcgi", {"db": "clinvar", "id": ",".join(ids)})
        result = (data or {}).get("result") or {}
        return [result[uid] for uid in result.get("uids") or [] if isinstance(result.get(uid), dict)]


_TITLE_P_RE = re.compile(r"\(p\.([A-Z][a-z]{2}\d{1,5}(?:[A-Z][a-z]{2}|=|\*|fs\S*))\)")


def parse_clinvar_record(doc: Dict[str, Any]) -> Optional[ClinVarRecord]:
    """Reduce an esummary document to ClinVarRecord fields; germline, oncogenicity and somatic impact stay separate."""
    uid = str(doc.get("uid") or "")
    accession = str(doc.get("accession") or "")
    if not uid or not accession:
        return None
    vset = (doc.get("variation_set") or [{}])[0] or {}
    rsids = sorted({
        f"rs{x['db_id']}" for x in vset.get("variation_xrefs") or []
        if x.get("db_source") == "dbSNP" and str(x.get("db_id") or "").isdigit()
    })
    grch38 = next(
        (loc for loc in vset.get("variation_loc") or [] if loc.get("assembly_name") == "GRCh38" and loc.get("status") == "current"),
        None,
    )

    def cls(key: str) -> Dict[str, Any]:
        c = doc.get(key)
        return c if isinstance(c, dict) else {}

    def traits(c: Dict[str, Any]) -> List[str]:
        return [str(t.get("trait_name")) for t in c.get("trait_set") or [] if t.get("trait_name")]

    germ, onco, somatic = cls("germline_classification"), cls("oncogenicity_classification"), cls("clinical_impact_classification")
    start = None
    if grch38 and str(grch38.get("start") or "").isdigit():
        start = int(grch38["start"])
    return ClinVarRecord(
        variation_id=uid,
        accession=accession,
        title=str(doc.get("title") or ""),
        protein_change=[p.strip() for p in str(doc.get("protein_change") or "").split(",") if p.strip()],
        rsids=rsids,
        genes=[str(g.get("symbol")) for g in doc.get("genes") or [] if g.get("symbol")],
        molecular_consequences=[str(x) for x in doc.get("molecular_consequence_list") or []],
        canonical_spdi=vset.get("canonical_spdi") or None,
        grch38_chr=(grch38 or {}).get("chr") or None,
        grch38_start=start,
        germline_classification=germ.get("description") or None,
        germline_review_status=germ.get("review_status") or None,
        germline_last_evaluated=(str(germ.get("last_evaluated") or "").split(" ")[0] or None),
        germline_traits=traits(germ)[:6],
        germline_review_stars=review_stars(germ.get("review_status")) if germ.get("description") else None,
        oncogenicity_classification=onco.get("description") or None,
        oncogenicity_review_status=onco.get("review_status") or None,
        oncogenicity_traits=traits(onco)[:6],
        oncogenicity_review_stars=review_stars(onco.get("review_status")) if onco.get("description") else None,
        somatic_clinical_impact=somatic.get("description") or None,
        somatic_clinical_impact_review_status=somatic.get("review_status") or None,
        somatic_clinical_impact_traits=traits(somatic)[:6],
        somatic_clinical_impact_review_stars=review_stars(somatic.get("review_status")) if somatic.get("description") else None,
        url=f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{uid}/",
    )



def title_protein_change(title: str) -> Optional[str]:
    """The 3-letter protein change in a ClinVar title ('... (p.Gly12Asp)' -> 'Gly12Asp')."""
    m = _TITLE_P_RE.search(title or "")
    return m.group(1) if m else None


# -----------------------------------------------------------------------------
# CIViC
# -----------------------------------------------------------------------------

_CIVIC_GENE_Q = "query($s:String!){gene(entrezSymbol:$s){id name}}"
_CIVIC_VARIANTS_Q = (
    "query($g:Int!,$n:String!){variants(geneId:$g,name:$n,first:10){totalCount nodes{id name "
    "... on GeneVariant{alleleRegistryId clinvarIds singleVariantMolecularProfileId}}}}"
)
_CIVIC_EVIDENCE_Q = (
    "query($mp:Int!,$first:Int!){evidenceItems(molecularProfileId:$mp,status:ACCEPTED,first:$first,"
    "sortBy:{column:EVIDENCE_RATING,direction:DESC}){totalCount nodes{id name evidenceLevel evidenceType "
    "evidenceDirection significance evidenceRating variantOrigin therapyInteractionType description "
    "disease{name doid} therapies{name} source{citationId sourceType citation publicationYear}}}}"
)
_CIVIC_ASSERTIONS_Q = (
    "query($mp:Int!){assertions(molecularProfileId:$mp,status:ACCEPTED,first:10){totalCount nodes{id name "
    "ampLevel assertionType assertionDirection significance status fdaCompanionTest regulatoryApproval "
    "nccnGuideline{name} disease{name} therapies{name}}}}"
)


class CivicClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _gql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        resp = await _send_retrying("CIViC", self.http, "POST", CIVIC_URL, json={"query": query, "variables": variables})
        if resp.status_code >= 400:
            raise UpstreamError(f"CIViC HTTP {resp.status_code}", resp.status_code)
        body = _json(resp, "CIViC")
        errors = (body or {}).get("errors") or []
        if errors:
            msg = str((errors[0] or {}).get("message") or "unknown error")[:160]
            raise UpstreamError(f"CIViC GraphQL error: {msg}")
        return (body or {}).get("data") or {}

    async def gene_id(self, symbol: str) -> Optional[int]:
        key = ("civic_gene", symbol)
        cached = _STATIC_CACHE.get(key)
        if cached is not None:
            return cached or None
        gene = (await self._gql(_CIVIC_GENE_Q, {"s": symbol})).get("gene")
        gid = int(gene["id"]) if isinstance(gene, dict) and gene.get("id") is not None else 0
        _STATIC_CACHE.set(key, gid)
        return gid or None

    async def _cached_gql(self, key: Tuple[Any, ...], query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        cached = _STATIC_CACHE.get(key)
        if cached is not None:
            return cached
        data = await self._gql(query, variables)
        _STATIC_CACHE.set(key, data)
        return data

    async def variants(self, gene_id: int, name: str) -> List[Dict[str, Any]]:
        data = await self._cached_gql(("civic_variants", gene_id, name), _CIVIC_VARIANTS_Q, {"g": gene_id, "n": name})
        return list(((data.get("variants") or {}).get("nodes")) or [])

    async def evidence(self, molecular_profile_id: int, first: int = 25) -> Tuple[int, List[Dict[str, Any]]]:
        data = await self._cached_gql(
            ("civic_evidence", molecular_profile_id, first), _CIVIC_EVIDENCE_Q, {"mp": molecular_profile_id, "first": first}
        )
        block = data.get("evidenceItems") or {}
        return int(block.get("totalCount") or 0), list(block.get("nodes") or [])

    async def assertions(self, molecular_profile_id: int) -> Tuple[int, List[Dict[str, Any]]]:
        data = await self._cached_gql(("civic_assertions", molecular_profile_id), _CIVIC_ASSERTIONS_Q, {"mp": molecular_profile_id})
        block = data.get("assertions") or {}
        return int(block.get("totalCount") or 0), list(block.get("nodes") or [])


# -----------------------------------------------------------------------------
# gnomAD
# -----------------------------------------------------------------------------

_GNOMAD_Q = (
    "query($id:String!,$ds:DatasetId!){variant(variantId:$id,dataset:$ds){variant_id rsids caid flags "
    "exome{ac an af homozygote_count faf95{popmax popmax_population} filters populations{id ac an}} "
    "genome{ac an af filters} transcript_consequences{gene_symbol transcript_id hgvsp hgvsc major_consequence is_canonical}}}"
)


class GnomadClient:
    RETRY_DELAY_S = 3.0

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def variant(self, variant_id: str, dataset: str = "gnomad_r4") -> Optional[Dict[str, Any]]:
        """The gnomAD variant document, or None when gnomAD has no such variant."""
        key = (variant_id, dataset)
        cached = _GNOMAD_CACHE.get(key)
        if cached is not None:
            return cached.get("variant")
        for attempt in range(2):
            async with _limiter("gnomad", 2.0):
                resp = await self.http.post(GNOMAD_URL, json={"query": _GNOMAD_Q, "variables": {"id": variant_id, "ds": dataset}})
            is_json = "json" in resp.headers.get("content-type", "")
            if resp.status_code == 429 or (resp.status_code >= 500 and not is_json):
                if attempt == 0:
                    await _sleep(self.RETRY_DELAY_S)
                    continue
                raise UpstreamError("rate limited", resp.status_code, rate_limited=resp.status_code == 429)
            if resp.status_code >= 400 and not is_json:
                raise UpstreamError(f"gnomAD HTTP {resp.status_code}", resp.status_code)
            body = _json(resp, "gnomAD")
            errors = (body or {}).get("errors") or []
            variant = ((body or {}).get("data") or {}).get("variant")
            if errors and not variant:
                messages = " ".join(str((e or {}).get("message") or "") for e in errors)
                if "not found" in messages.lower():
                    _GNOMAD_CACHE.set(key, {"variant": None})
                    return None
                raise UpstreamError(f"gnomAD GraphQL error: {messages[:160]}")
            _GNOMAD_CACHE.set(key, {"variant": variant})
            return variant
        raise UpstreamError("rate limited")  # pragma: no cover


# -----------------------------------------------------------------------------
# cBioPortal
# -----------------------------------------------------------------------------

class CbioPortalClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return await _send_retrying(
            "cBioPortal", self.http, "GET", f"{CBIO_BASE}{path}", params=params,
            headers={"Accept": "application/json"}, limiter=_limiter("cbioportal", 0.1),
        )

    async def gene(self, symbol: str) -> Optional[Dict[str, Any]]:
        key = ("cbio_gene", symbol)
        cached = _STATIC_CACHE.get(key)
        if cached is not None:
            return cached or None
        resp = await self._get(f"/genes/{symbol}")
        if resp.status_code == 404:
            _STATIC_CACHE.set(key, {})
            return None
        if resp.status_code >= 400:
            raise UpstreamError(f"cBioPortal HTTP {resp.status_code} on /genes", resp.status_code)
        gene = _json(resp, "cBioPortal")
        _STATIC_CACHE.set(key, gene)
        return gene

    async def studies(self) -> List[Dict[str, Any]]:
        cached = _STATIC_CACHE.get("cbio_studies")
        if cached is not None:
            return cached
        resp = await self._get("/studies", {"projection": "DETAILED"})
        if resp.status_code >= 400:
            raise UpstreamError(f"cBioPortal HTTP {resp.status_code} on /studies", resp.status_code)
        # importDate: when the study's data were loaded into cBioPortal (its data-release provenance, DAT-010).
        keep = ("studyId", "cancerTypeId", "name", "allSampleCount", "sequencedSampleCount", "referenceGenome", "cancerType",
                "importDate")
        studies = [{k: s.get(k) for k in keep} for s in _json(resp, "cBioPortal") or [] if isinstance(s, dict)]
        _STATIC_CACHE.set("cbio_studies", studies)
        return studies

    async def mutations(self, study_id: str, entrez_gene_id: int) -> List[Dict[str, Any]]:
        resp = await self._get(
            f"/molecular-profiles/{study_id}_mutations/mutations",
            {"sampleListId": f"{study_id}_sequenced", "entrezGeneId": entrez_gene_id, "projection": "SUMMARY"},
        )
        if resp.status_code >= 400:
            raise UpstreamError(f"cBioPortal HTTP {resp.status_code} on {study_id} mutations", resp.status_code)
        return list(_json(resp, "cBioPortal") or [])

    async def gene_panel_data(self, study_id: str) -> List[Dict[str, Any]]:
        resp = await _send_retrying(
            "cBioPortal", self.http, "POST", f"{CBIO_BASE}/molecular-profiles/{study_id}_mutations/gene-panel-data/fetch",
            json={"sampleListId": f"{study_id}_sequenced"}, headers={"Accept": "application/json"},
            limiter=_limiter("cbioportal", 0.1),
        )
        if resp.status_code >= 400:
            raise UpstreamError(f"cBioPortal HTTP {resp.status_code} on {study_id} gene panels", resp.status_code)
        return list(_json(resp, "cBioPortal") or [])

    async def gene_panel_genes(self, panel_id: str) -> Set[str]:
        key = ("cbio_panel", panel_id)
        cached = _STATIC_CACHE.get(key)
        if cached is not None:
            return cached
        resp = await self._get(f"/gene-panels/{panel_id}")
        if resp.status_code >= 400:
            raise UpstreamError(f"cBioPortal HTTP {resp.status_code} on gene panel {panel_id}", resp.status_code)
        genes = {str(g.get("hugoGeneSymbol")) for g in (_json(resp, "cBioPortal") or {}).get("genes") or [] if g.get("hugoGeneSymbol")}
        _STATIC_CACHE.set(key, genes)
        return genes


def new_http_client(transport: Optional[httpx.AsyncBaseTransport] = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
