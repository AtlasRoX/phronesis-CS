"""
BioContext MCP & Direct Biomedical API Client for Phronesis.

Provides unified async and sync interfaces for:
- OpenTargets Platform (GraphQL: Entity Search vs Target-Disease Association Evidence)
- Europe PMC (Literature search, citation traversal, and abstract/passage retrieval)
- KEGG Pathway REST API
- UniProtKB (Verified protein metadata and function)
- Ensembl REST API (Gene coordinates and canonical ENSG mappings)
- BioModels EMBL-EBI REST API (SBML model search and download)

Adheres to ToolUniverse (2025) and PaperQA2 standards: canonical entity IDs,
validated schemas, real association scores, non-blocking async HTTP, and observed health.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, TypeVar
import httpx
import requests

logger = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_BIOCONTEXT_URL = os.getenv("BIOCONTEXT_MCP_URL", "https://mcp.biocontext.ai/mcp/")
TIMEOUT_SECONDS = float(os.getenv("BIOCONTEXT_TIMEOUT", "25.0"))
OPENTARGETS_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"
EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
BIOMODELS_SEARCH_URL = "https://www.ebi.ac.uk/biomodels/search"
UNIPROT_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
ENSEMBL_REST_URL = "https://rest.ensembl.org"
KEGG_REST_URL = "https://rest.kegg.jp"


def _loop_running_in_this_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_coroutine_sync(make_coro: Callable[[], Coroutine[Any, Any, T]], timeout: Optional[float] = None) -> T:
    """Run the coroutine made by `make_coro` to completion from synchronous code.

    With no loop running in this thread it is run with asyncio.run. With one running (a sync wrapper called from
    async code), it cannot be scheduled on that loop and waited for — the loop is blocked by the wait, which is a
    deadlock — so it runs in a fresh worker thread on its own event loop. Raises TimeoutError after `timeout`."""
    if not _loop_running_in_this_thread():
        return asyncio.run(make_coro())
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="biocontext-sync")
    try:
        return pool.submit(lambda: asyncio.run(make_coro())).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False)


def upstream_failure(base: Dict[str, Any], exc: BaseException, **empty: Any) -> Dict[str, Any]:
    """The error-as-data payload of a call that never got an answer from the upstream (ORC-001).

    Transport failures (connect/read errors, timeouts, 5xx, a GraphQL error body) are not "nothing found": the
    payload carries ``upstream_failed=True`` and an ``error_type`` (TIMEOUT or UPSTREAM_API_ERROR) so the tool
    registry turns it into a failed ToolResult (retried, counted by the breaker, never cached) and coverage
    reports the source as unavailable. The exception class name is kept in ``error`` so transport failures stay
    recognisable. ``empty`` holds the usual empty collections (``results=[]``) for legacy dict readers."""
    error_type = "TIMEOUT" if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)) else "UPSTREAM_API_ERROR"
    return {**base, **empty, "error": f"{type(exc).__name__}: {exc}", "error_type": error_type, "upstream_failed": True}


class UpstreamPayloadError(RuntimeError):
    """An upstream answered 2xx with an error body (e.g. a GraphQL ``errors`` list and no ``data``)."""


def _ot_datatype_scores(rows: Any) -> Dict[str, float]:
    """{datatype id: score rounded to 4 dp} of an Open Targets ``datatypeScores`` list (KG-013)."""
    out: Dict[str, float] = {}
    for dt in rows or []:
        if not isinstance(dt, dict) or not dt.get("id"):
            continue
        try:
            out[str(dt["id"])] = round(float(dt["score"]), 4)
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _ot_datatype_score(scores: Dict[str, float], *ids: str) -> Optional[float]:
    """The score of the first datatype id present (current id first, legacy ids after); None when OT returned none
    of them, so an absent datatype is never reported as a measured 0.0 (KG-013)."""
    for i in ids:
        if i in scores:
            return scores[i]
    return None


class BioContextMCPClient:
    """
    Unified client for biomedical databases and services.
    Provides non-blocking async methods as primary path, with sync wrappers.
    """

    def __init__(self, endpoint_url: Optional[str] = None) -> None:
        self.endpoint_url = endpoint_url or DEFAULT_BIOCONTEXT_URL
        self._sync_session = requests.Session()
        self._sync_session.headers.update({
            "User-Agent": "Phronesis-BioContext-Client/2.0",
            "Accept": "application/json",
        })

    def _get_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": "Phronesis-BioContext-Client/2.0",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=10.0),
            follow_redirects=True,
        )

    # -------------------------------------------------------------------------
    # HEALTH & ADAPTER READINESS (ToolUniverse Specification)
    # -------------------------------------------------------------------------
    async def async_check_service_readiness(self) -> Dict[str, Any]:
        """
        Verify live connectivity, measured latency, and active protocol adapters.
        Does NOT return fake constant status; measures observed network health.
        """
        readiness: Dict[str, Any] = {
            "mcp_remote_url": self.endpoint_url,
            "mcp_remote_active": False,
            "direct_adapters": {},
            "timestamp": time.time(),
        }

        async with self._get_async_client() as client:
            # 1. Check MCP remote server (if reachable)
            try:
                ping_res = await client.post(
                    self.endpoint_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                    timeout=3.0,
                )
                readiness["mcp_remote_active"] = ping_res.status_code in (200, 204)
            except Exception:
                readiness["mcp_remote_active"] = False

            # 2. Check OpenTargets GraphQL
            ot_t0 = time.perf_counter()
            try:
                ot_res = await client.post(
                    OPENTARGETS_GRAPHQL_URL,
                    json={"query": 'query { search(queryString: "cancer") { total } }'},
                    timeout=5.0,
                )
                readiness["direct_adapters"]["opentargets"] = {
                    "status": "ready" if ot_res.status_code == 200 else "degraded",
                    "latency_ms": round((time.perf_counter() - ot_t0) * 1000, 1),
                }
            except Exception as e:
                readiness["direct_adapters"]["opentargets"] = {"status": "unreachable", "error": str(e)}

            # 3. Check EuropePMC
            epmc_t0 = time.perf_counter()
            try:
                epmc_res = await client.get(
                    EUROPEPMC_SEARCH_URL,
                    params={"query": "cancer", "format": "json", "pageSize": 1},
                    timeout=5.0,
                )
                readiness["direct_adapters"]["europepmc"] = {
                    "status": "ready" if epmc_res.status_code == 200 else "degraded",
                    "latency_ms": round((time.perf_counter() - epmc_t0) * 1000, 1),
                }
            except Exception as e:
                readiness["direct_adapters"]["europepmc"] = {"status": "unreachable", "error": str(e)}

            # 4. Check UniProt
            uni_t0 = time.perf_counter()
            try:
                uni_res = await client.get(
                    UNIPROT_SEARCH_URL,
                    params={"query": "P04637", "format": "json", "size": 1},
                    timeout=5.0,
                )
                readiness["direct_adapters"]["uniprot"] = {
                    "status": "ready" if uni_res.status_code == 200 else "degraded",
                    "latency_ms": round((time.perf_counter() - uni_t0) * 1000, 1),
                }
            except Exception as e:
                readiness["direct_adapters"]["uniprot"] = {"status": "unreachable", "error": str(e)}

        return readiness

    def is_cloud_mcp_available(self) -> bool:
        """Synchronous ping check for legacy callers."""
        try:
            resp = self._sync_session.post(
                self.endpoint_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                timeout=4.0,
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # OPENTARGETS: ENTITY SEARCH vs ASSOCIATION EVIDENCE (Critical Distinction)
    # -------------------------------------------------------------------------
    async def async_query_opentargets_search(self, query: str, size: int = 5) -> Dict[str, Any]:
        """
        Search OpenTargets for entities (targets, diseases, drugs).
        Returns candidate Ensembl IDs, EFO IDs, or ChEMBL IDs.
        """
        query_str = """
        query SearchQuery($queryString: String!) {
            search(queryString: $queryString) {
                total
                hits {
                    id
                    name
                    entity
                    description
                }
            }
        }
        """
        async with self._get_async_client() as client:
            try:
                resp = await client.post(
                    OPENTARGETS_GRAPHQL_URL,
                    json={"query": query_str, "variables": {"queryString": query}},
                )
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or (payload.get("errors") and not payload.get("data")):
                    raise UpstreamPayloadError(f"OpenTargets GraphQL error: {str((payload or {}).get('errors'))[:300]}")
                data = (payload.get("data") or {}).get("search") or {}
                hits = (data.get("hits") or [])[:size]
                return {
                    "source": "OpenTargets (Search)",
                    "query": query,
                    "total_hits": data.get("total", len(hits)),
                    "results": hits,
                }
            except Exception as e:
                logger.warning("OpenTargets search failed: %s", e)
                return upstream_failure({"source": "OpenTargets (Search)", "query": query}, e, results=[])

    async def async_query_opentargets_associations(
        self,
        ensembl_id: str,
        size: int = 10,
    ) -> Dict[str, Any]:
        """
        Retrieve REAL quantified target-disease association scores from OpenTargets.
        Extracts overall score and datatype breakdowns (genetic, somatic, pathway, literature).
        """
        query_str = """
        query TargetAssociations($ensemblId: String!, $size: Int!) {
            target(ensemblId: $ensemblId) {
                id
                approvedSymbol
                approvedName
                biotype
                associatedDiseases(page: { size: $size, index: 0 }) {
                    count
                    rows {
                        disease {
                            id
                            name
                            description
                        }
                        score
                        datatypeScores {
                            id
                            score
                        }
                    }
                }
            }
        }
        """
        async with self._get_async_client() as client:
            try:
                resp = await client.post(
                    OPENTARGETS_GRAPHQL_URL,
                    json={"query": query_str, "variables": {"ensemblId": ensembl_id, "size": size}},
                )
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or (payload.get("errors") and not payload.get("data")):
                    raise UpstreamPayloadError(f"OpenTargets GraphQL error: {str((payload or {}).get('errors'))[:300]}")
                target_data = (payload.get("data") or {}).get("target") or {}
                if not target_data:
                    # A real answer from OpenTargets: the id is unknown there (not an outage).
                    return {
                        "source": "OpenTargets (Associations)",
                        "ensembl_id": ensembl_id,
                        "error": f"No target found for Ensembl ID {ensembl_id}",
                        "error_type": "NOT_FOUND",
                        "associations": [],
                    }

                assoc_data = target_data.get("associatedDiseases", {})
                rows = assoc_data.get("rows", [])
                associations = []
                for row in rows:
                    disease_obj = row.get("disease", {})
                    dt_scores = _ot_datatype_scores(row.get("datatypeScores"))
                    associations.append({
                        "disease_id": disease_obj.get("id"),
                        "disease_name": disease_obj.get("name"),
                        "overall_association_score": round(row.get("score", 0.0), 4),
                        # KG-013: OT datatype ids are singular ("somatic_mutation") and drug evidence is "clinical"
                        # ("known_drug" in older releases); a datatype absent from the row is None, never 0.0.
                        "genetic_association_score": _ot_datatype_score(dt_scores, "genetic_association"),
                        "somatic_mutation_score": _ot_datatype_score(dt_scores, "somatic_mutation", "somatic_mutations"),
                        "known_drug_score": _ot_datatype_score(dt_scores, "clinical", "known_drug"),
                        "literature_score": _ot_datatype_score(dt_scores, "literature"),
                        "datatype_scores": dt_scores,
                    })

                return {
                    "source": "OpenTargets (Associations)",
                    "ensembl_id": ensembl_id,
                    "approved_symbol": target_data.get("approvedSymbol"),
                    "approved_name": target_data.get("approvedName"),
                    "total_associated_diseases": assoc_data.get("count", len(associations)),
                    "associations": associations,
                }
            except Exception as e:
                logger.warning("OpenTargets association query failed: %s", e)
                return upstream_failure(
                    {"source": "OpenTargets (Associations)", "ensembl_id": ensembl_id}, e, associations=[]
                )

    def query_opentargets(self, query: str, query_type: str = "search", size: int = 5) -> Dict[str, Any]:
        """Synchronous wrapper for backwards compatibility. Async callers should await the async_* methods."""
        try:
            if query_type == "associations" and query.startswith("ENSG"):
                return _run_coroutine_sync(
                    lambda: self.async_query_opentargets_associations(query, size=size),
                    timeout=TIMEOUT_SECONDS * 2,
                )
            if _loop_running_in_this_thread():
                # Inside an active loop: the blocking requests path avoids nesting event loops.
                return self._sync_query_opentargets_fallback(query, size=size)
            return _run_coroutine_sync(lambda: self.async_query_opentargets_search(query, size=size),
                                       timeout=TIMEOUT_SECONDS * 2)
        except Exception:  # noqa: BLE001
            logger.debug("OpenTargets async path failed; using the sync fallback", exc_info=True)
            return self._sync_query_opentargets_fallback(query, size=size)

    def _sync_query_opentargets_fallback(self, query: str, size: int = 5) -> Dict[str, Any]:
        query_str = """
        query SearchQuery($queryString: String!) {
            search(queryString: $queryString) {
                total
                hits { id name entity description }
            }
        }
        """
        try:
            resp = self._sync_session.post(
                OPENTARGETS_GRAPHQL_URL,
                json={"query": query_str, "variables": {"queryString": query}},
                timeout=TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {}).get("search", {})
            hits = data.get("hits", [])[:size]
            return {
                "source": "OpenTargets (BioContext)",
                "query": query,
                "total_hits": data.get("total", len(hits)),
                "results": hits,
            }
        except Exception as e:
            return {"source": "OpenTargets (BioContext)", "query": query, "error": str(e), "results": []}

    # -------------------------------------------------------------------------
    # EUROPE PMC & LITERATURE PASSAGE SYNTHESIS (PaperQA2 / OpenScholar)
    # -------------------------------------------------------------------------
    async def async_query_europepmc(self, query: str, limit: int = 5, sort: Optional[str] = None) -> Dict[str, Any]:
        """Query Europe PMC search with structured metadata. sort is a Europe PMC sort ('CITED desc'); default relevance."""
        params = {
            "query": query,
            "format": "json",
            "pageSize": limit,
            "resultType": "core",
        }
        if sort:
            params["sort"] = sort
        async with self._get_async_client() as client:
            try:
                resp = await client.get(EUROPEPMC_SEARCH_URL, params=params)
                resp.raise_for_status()
                result_list = resp.json().get("resultList", {}).get("result", [])
                articles = []
                for art in result_list:
                    # The core result type has no top-level pubType or journalTitle, and PMC/PPR records have no
                    # PMID: their id is a PMC/PPR accession, so it is never used as one.
                    pub_type_list = art.get("pubTypeList") or {}
                    pub_types = pub_type_list.get("pubType") or [] if isinstance(pub_type_list, dict) else []
                    if isinstance(pub_types, str):
                        pub_types = [pub_types]
                    articles.append({
                        "id": art.get("id"),
                        "source": art.get("source", ""),
                        "title": art.get("title", ""),
                        "authorString": art.get("authorString", ""),
                        "journalTitle": art.get("journalTitle") or ((art.get("journalInfo") or {}).get("journal") or {}).get("title") or "",
                        "pubYear": art.get("pubYear", ""),
                        "firstPublicationDate": art.get("firstPublicationDate", ""),
                        "doi": art.get("doi", ""),
                        "pmcid": art.get("pmcid", ""),
                        "pmid": art.get("pmid"),
                        "abstractText": art.get("abstractText", ""),
                        "pubType": art.get("pubType") or "; ".join(str(p) for p in pub_types),
                        "pubTypeList": pub_type_list,
                        "meshHeadingList": art.get("meshHeadingList") or {},
                        "commentCorrectionList": art.get("commentCorrectionList") or {},
                        "isOpenAccess": art.get("isOpenAccess", "N") == "Y",
                    })
                return {
                    "source": "EuropePMC",
                    "query": query,
                    "count": len(articles),
                    "articles": articles,
                }
            except Exception as e:
                logger.warning("EuropePMC async query failed: %s", e)
                return {"source": "EuropePMC", "query": query, "error": str(e), "articles": []}

    async def async_fetch_europepmc_fulltext(self, pmcid: str) -> Optional[str]:
        """Fetch full-text XML for an open-access publication via Europe PMC."""
        clean_pmcid = pmcid.strip()
        if not clean_pmcid.startswith("PMC"):
            return None
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{clean_pmcid}/fullTextXML"
        async with self._get_async_client() as client:
            try:
                resp = await client.get(url, timeout=10.0, headers={"Accept": "application/xml"})
                if resp.status_code == 200 and resp.text:
                    return resp.text
            except Exception as e:
                logger.debug("Full-text XML retrieval for %s failed: %s", clean_pmcid, e)
        return None

    def query_europepmc(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Synchronous wrapper."""
        try:
            resp = self._sync_session.get(
                EUROPEPMC_SEARCH_URL,
                params={"query": query, "format": "json", "pageSize": limit, "resultType": "lite"},
                timeout=TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            result_list = resp.json().get("resultList", {}).get("result", [])
            articles = []
            for art in result_list:
                articles.append({
                    "id": art.get("id"),
                    "title": art.get("title"),
                    "authorString": art.get("authorString"),
                    "journalTitle": art.get("journalTitle"),
                    "pubYear": art.get("pubYear"),
                    "doi": art.get("doi"),
                    "pmcid": art.get("pmcid"),
                })
            return {"source": "EuropePMC (BioContext)", "query": query, "count": len(articles), "articles": articles}
        except Exception as e:
            return {"source": "EuropePMC (BioContext)", "query": query, "error": str(e), "articles": []}

    # -------------------------------------------------------------------------
    # BIOMODELS DATABASE (Computational Systems Biology & Provenance)
    # -------------------------------------------------------------------------
    async def async_query_biomodels_search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """
        Search EMBL-EBI BioModels repository for curated ODE models.
        Returns verified model accession (e.g. BIOMD0000000010), name, and publication.
        """
        params = {"query": query, "format": "json", "numResults": limit}
        async with self._get_async_client() as client:
            try:
                resp = await client.get(BIOMODELS_SEARCH_URL, params=params)
                resp.raise_for_status()
                models_data = resp.json().get("models", [])
                models = []
                for m in models_data[:limit]:
                    models.append({
                        "accession": m.get("id"),
                        "name": m.get("name", ""),
                        "format": m.get("format", "SBML"),
                        "submission_id": m.get("submissionId"),
                        "publication_id": m.get("publicationId"),
                        "authors": m.get("authors", []),
                        "description": m.get("description", ""),
                    })
                return {
                    "source": "BioModels (EMBL-EBI)",
                    "query": query,
                    "count": len(models),
                    "models": models,
                }
            except Exception as e:
                logger.warning("BioModels search failed: %s", e)
                return {"source": "BioModels (EMBL-EBI)", "query": query, "error": str(e), "models": []}

    # -------------------------------------------------------------------------
    # UNIPROTKB & ENSEMBL (Canonical Protein and Genomic IDs)
    # -------------------------------------------------------------------------
    async def async_query_uniprot(self, query: str, limit: int = 5, reviewed_only: bool = True) -> Dict[str, Any]:
        search_query = query
        if reviewed_only and "reviewed:" not in search_query:
            search_query = f"{search_query} AND reviewed:true"

        params = {
            "query": search_query,
            "format": "json",
            "size": limit,
            "fields": "accession,id,protein_name,gene_names,organism_name,length,cc_function",
        }
        async with self._get_async_client() as client:
            try:
                resp = await client.get(UNIPROT_SEARCH_URL, params=params)
                resp.raise_for_status()
                results = resp.json().get("results", [])
                proteins = []
                for r in results:
                    desc = r.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                    genes = [g.get("geneName", {}).get("value", "") for g in r.get("genes", []) if "geneName" in g]
                    proteins.append({
                        "accession": r.get("primaryAccession"),
                        "entry_id": r.get("uniProtkbId"),
                        "protein_name": desc,
                        "gene_names": genes,
                        "organism": r.get("organism", {}).get("scientificName"),
                        "length": r.get("sequence", {}).get("length"),
                    })
                return {"source": "UniProt", "query": query, "count": len(proteins), "proteins": proteins}
            except Exception as e:
                return {"source": "UniProt", "query": query, "error": str(e), "proteins": []}

    def query_uniprot(self, query: str, limit: int = 5, reviewed_only: bool = True) -> Dict[str, Any]:
        search_query = query
        if reviewed_only and "reviewed:" not in search_query:
            search_query = f"{search_query} AND reviewed:true"
        params = {
            "query": search_query,
            "format": "json",
            "size": limit,
            "fields": "accession,id,protein_name,gene_names,organism_name,length,cc_function",
        }
        try:
            resp = self._sync_session.get(UNIPROT_SEARCH_URL, params=params, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            proteins = []
            for r in results:
                desc = r.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                genes = [g.get("geneName", {}).get("value", "") for g in r.get("genes", []) if "geneName" in g]
                proteins.append({
                    "accession": r.get("primaryAccession"),
                    "entry_id": r.get("uniProtkbId"),
                    "protein_name": desc,
                    "gene_names": genes,
                    "organism": r.get("organism", {}).get("scientificName"),
                    "length": r.get("sequence", {}).get("length"),
                })
            return {"source": "UniProt (BioContext)", "query": query, "count": len(proteins), "proteins": proteins}
        except Exception as e:
            return {"source": "UniProt (BioContext)", "query": query, "error": str(e), "proteins": []}

    async def async_query_ensembl(self, gene_symbol: str, species: str = "human") -> Dict[str, Any]:
        url = f"{ENSEMBL_REST_URL}/lookup/symbol/{species}/{gene_symbol}"
        async with self._get_async_client() as client:
            try:
                resp = await client.get(url, headers={"Content-Type": "application/json"})
                resp.raise_for_status()
                data = resp.json()
                return {
                    "source": "Ensembl",
                    "gene_symbol": gene_symbol,
                    "id": data.get("id"),
                    "display_name": data.get("display_name"),
                    "biotype": data.get("biotype"),
                    "description": data.get("description"),
                    "seq_region_name": data.get("seq_region_name"),
                    "start": data.get("start"),
                    "end": data.get("end"),
                    "strand": data.get("strand"),
                }
            except Exception as e:
                return {"source": "Ensembl", "gene_symbol": gene_symbol, "error": str(e)}

    def query_ensembl(self, gene_symbol: str, species: str = "human") -> Dict[str, Any]:
        url = f"{ENSEMBL_REST_URL}/lookup/symbol/{species}/{gene_symbol}"
        try:
            resp = self._sync_session.get(url, headers={"Content-Type": "application/json"}, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
            return {
                "source": "Ensembl (BioContext)",
                "gene_symbol": gene_symbol,
                "id": data.get("id"),
                "display_name": data.get("display_name"),
                "biotype": data.get("biotype"),
                "description": data.get("description"),
                "seq_region_name": data.get("seq_region_name"),
                "start": data.get("start"),
                "end": data.get("end"),
                "strand": data.get("strand"),
            }
        except Exception as e:
            return {"source": "Ensembl (BioContext)", "gene_symbol": gene_symbol, "error": str(e)}

    # -------------------------------------------------------------------------
    # KEGG & PANGLAODB
    # -------------------------------------------------------------------------
    async def async_query_kegg(self, operation: str, argument: str) -> Dict[str, Any]:
        url = f"{KEGG_REST_URL}/{operation}/{argument}"
        async with self._get_async_client() as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                lines = [l.strip() for l in resp.text.strip().split("\n") if l.strip()]
                entries = []
                for line in lines[:10]:
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        entries.append({"id": parts[0], "description": parts[1]})
                    else:
                        entries.append({"entry": line})
                return {"source": "KEGG", "operation": operation, "argument": argument, "results": entries}
            except Exception as e:
                return {"source": "KEGG", "operation": operation, "argument": argument, "error": str(e), "results": []}

    async def async_query_kegg_gene_pathways(self, target_symbol: str, organism: str = "hsa", limit: int = 10) -> Dict[str, Any]:
        """
        Resolve a gene symbol to its KEGG pathway memberships.
        KEGG pathway names do not contain gene symbols, so `find/pathway/<SYMBOL>` is empty for most genes;
        the correct route is find/<org>/<SYMBOL> -> link/pathway/<gene_id> -> list/pathway/<org> for names.
        """
        symbol = target_symbol.strip().split()[0].upper()
        base = {"source": "KEGG", "operation": "gene_pathways", "argument": symbol, "organism": organism}
        async with self._get_async_client() as client:
            try:
                find_resp = await client.get(f"{KEGG_REST_URL}/find/{organism}/{symbol}")
                find_resp.raise_for_status()
                gene_id = None
                for line in find_resp.text.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) < 2:
                        continue
                    # Description starts with "SYMBOL, ALIAS, ...; name" - require an exact primary-symbol match.
                    primary = parts[1].split(";")[0].split(",")[0].strip().upper()
                    if primary == symbol:
                        gene_id = parts[0].strip()
                        break
                if not gene_id:
                    # KEGG answered; the symbol has no exact gene entry (a legitimate "no match", not an outage).
                    return {
                        **base, "gene_id": None, "results": [], "error_type": "NOT_FOUND",
                        "error": f"No exact KEGG {organism} gene match for {symbol}",
                    }

                link_resp = await client.get(f"{KEGG_REST_URL}/link/pathway/{gene_id}")
                link_resp.raise_for_status()
                pathway_ids = []
                for line in link_resp.text.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1].startswith("path:"):
                        pathway_ids.append(parts[1].removeprefix("path:"))

                names: Dict[str, str] = {}
                if pathway_ids:
                    list_resp = await client.get(f"{KEGG_REST_URL}/list/pathway/{organism}")
                    list_resp.raise_for_status()
                    for line in list_resp.text.strip().split("\n"):
                        parts = line.split("\t")
                        if len(parts) >= 2:
                            names[parts[0].strip()] = parts[1].strip()

                entries = [{"id": pid, "description": names.get(pid, pid)} for pid in pathway_ids[:limit]]
                return {**base, "gene_id": gene_id, "results": entries}
            except Exception as e:
                logger.warning("KEGG gene-pathway lookup failed for %s: %s", symbol, e)
                return upstream_failure(base, e, results=[])

    def query_kegg(self, operation: str, argument: str) -> Dict[str, Any]:
        url = f"{KEGG_REST_URL}/{operation}/{argument}"
        try:
            resp = self._sync_session.get(url, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            lines = [l.strip() for l in resp.text.strip().split("\n") if l.strip()]
            entries = []
            for line in lines[:10]:
                parts = line.split("\t")
                if len(parts) >= 2:
                    entries.append({"id": parts[0], "description": parts[1]})
                else:
                    entries.append({"entry": line})
            return {"source": "KEGG (BioContext)", "operation": operation, "argument": argument, "results": entries}
        except Exception as e:
            return {"source": "KEGG (BioContext)", "operation": operation, "argument": argument, "error": str(e), "results": []}

    def query_kegg_gene_pathways(self, target_symbol: str, organism: str = "hsa", limit: int = 10) -> Dict[str, Any]:
        """Synchronous wrapper of `async_query_kegg_gene_pathways` (find/<org> -> link/pathway -> list/pathway)."""
        try:
            return _run_coroutine_sync(
                lambda: self.async_query_kegg_gene_pathways(target_symbol, organism=organism, limit=limit),
                timeout=TIMEOUT_SECONDS * 3,
            )
        except Exception as e:  # noqa: BLE001
            return upstream_failure(
                {"source": "KEGG", "operation": "gene_pathways", "argument": target_symbol, "organism": organism},
                e, results=[],
            )

    def query_panglaodb(self, gene_or_cell: str, limit: int = 25) -> Dict[str, Any]:
        """Cell-type markers from the bundled offline PanglaoDB snapshot (ORC-016).

        This used to return an Ensembl lookup labelled as a PanglaoDB annotation. It now reads the PanglaoDB table
        of the local marker database (phronesis/lumicell/markers/cell_markers.db, built by
        scripts/build_cellmarker_db.py): a gene symbol returns the cell types it marks, otherwise the term is
        matched against cell-type names and their markers are returned. Without the database the status is
        "unavailable" and nothing is presented as an annotation."""
        import sqlite3
        from pathlib import Path

        term = str(gene_or_cell or "").strip()
        db_path = Path(__file__).resolve().parents[2] / "lumicell" / "markers" / "cell_markers.db"
        base: Dict[str, Any] = {"source": "PanglaoDB (local offline snapshot)", "query": term, "results": []}
        if not term:
            return {**base, "status": "no_match", "error": "empty query"}
        if not db_path.is_file():
            return {**base, "status": "unavailable", "error": "the local PanglaoDB marker database is not installed"}
        cols = "symbol, cell_type, organ, canonical_marker, sensitivity_human, specificity_human"
        try:
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT {cols} FROM panglaodb_human WHERE symbol = ? "
                    "ORDER BY canonical_marker DESC, sensitivity_human DESC LIMIT ?",
                    (term.upper(), limit),
                ).fetchall()
                match = "gene_symbol"
                if not rows:
                    rows = conn.execute(
                        f"SELECT {cols} FROM panglaodb_human WHERE cell_type LIKE ? "
                        "ORDER BY canonical_marker DESC, sensitivity_human DESC LIMIT ?",
                        (f"%{term}%", limit),
                    ).fetchall()
                    match = "cell_type"
            finally:
                conn.close()
        except sqlite3.Error as e:
            return {**base, "status": "unavailable", "error": f"{type(e).__name__}: {e}"}
        results = [
            {
                "symbol": r["symbol"], "cell_type": r["cell_type"], "organ": r["organ"],
                "canonical_marker": bool(r["canonical_marker"]),
                "sensitivity_human": r["sensitivity_human"], "specificity_human": r["specificity_human"],
            }
            for r in rows
        ]
        if not results:
            return {**base, "status": "no_match"}
        return {
            **base, "status": "ok", "matched_on": match, "results": results,
            "note": (
                "Columns as distributed by PanglaoDB: sensitivity_human is the fraction of cells of that type "
                "expressing the gene; in the distributed file a LOW specificity_human value means the gene is rarely "
                "expressed in other cell types. Rows are ordered canonical markers first, then by sensitivity."
            ),
        }
