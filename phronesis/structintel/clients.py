"""
Async HTTP clients for protein structure and chemistry evidence (feature 12).

- AlphaFold DB (Varadi et al., NAR 2022, doi:10.1093/nar/gkab1061; NAR 2024, doi:10.1093/nar/gkad1011):
  GET https://alphafold.ebi.ac.uk/api/prediction/{UniProt accession} returns a JSON array of models (the canonical
  sequence as AF-<acc>-F1 plus isoforms). We keep the model id (modelEntityId, older entryId), the version, the
  file URLs and globalMetricValue, the mean pLDDT. Bands follow AlphaFold (Jumper et al., Nature 2021,
  doi:10.1038/s41586-021-03819-2): >90 very high, 70-90 confident, 50-70 low, <50 very low.
- RCSB PDB (Burley et al., NAR 2023, doi:10.1093/nar/gkac1077): the Search API v2 (POST .../rcsbsearch/v2/query, a
  terminal text query on rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers
  .database_accession, return_type entry; 204 means no hits) and the Data API entry endpoint
  (GET https://data.rcsb.org/rest/v1/core/entry/{id}: struct.title, exptl.method, resolution_combined and the
  nonpolymer bound components).
- NVIDIA NIM, only with NVIDIA_API_KEY: DiffDock (Corso et al., ICLR 2023, arXiv:2210.01776) at
  POST https://health.api.nvidia.com/v1/biology/mit/diffdock {protein, ligand, ligand_file_type, num_poses} and
  MolMIM (Reidenbach et al. 2022, arXiv:2208.09016) at POST .../v1/biology/nvidia/molmim/generate {smi, algorithm,
  num_molecules, property_name, min_similarity}. ESMFold is not wired: its request body is unverified.

Clients accept an injected httpx transport (tests); answers from the real API are cached for 6 h.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from ..utils.http import ANY_STATUS, HttpError, request as http_request
from ..utils.http_limits import TTLCache, get_limiter

logger = logging.getLogger(__name__)

AFDB_PREDICTION_URL = "https://alphafold.ebi.ac.uk/api/prediction/{accession}"
AFDB_ENTRY_PAGE = "https://alphafold.ebi.ac.uk/entry/{accession}"
RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_ENTRY_URL = "https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
RCSB_ENTRY_PAGE = "https://www.rcsb.org/structure/{pdb_id}"
RCSB_FILE_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
NIM_DIFFDOCK_URL = "https://health.api.nvidia.com/v1/biology/mit/diffdock"
NIM_MOLMIM_URL = "https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate"

UNIPROT_ACCESSION_RE = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$")
UNIPROT_IN_TEXT_RE = re.compile(r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b")
PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
AF_MODEL_ID_RE = re.compile(r"\bAF-[A-Z0-9]{6,10}(?:-\d+)?-F\d+\b")

_USER_AGENT = "Phronesis/1.0 (structure intelligence; +https://alphafold.ebi.ac.uk)"
_CACHE = TTLCache(maxsize=512, ttl_s=6 * 3600)
MAX_PDB_TEXT_BYTES = 6 * 1024 * 1024


class StructureClientError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # timeout | upstream | auth | invalid


def plddt_band(value: Optional[float]) -> Optional[str]:
    """AlphaFold confidence band of a pLDDT value (Jumper et al. 2021)."""
    if value is None:
        return None
    v = float(value)
    if v > 90:
        return "very high"
    if v >= 70:
        return "confident"
    if v >= 50:
        return "low"
    return "very low"


def nvidia_api_key() -> str:
    return (os.getenv("NVIDIA_API_KEY", "") or "").strip()


def _round(value: Any, digits: int = 2) -> Optional[float]:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def summarize_alphafold(entries: Any, accession: str) -> Optional[Dict[str, Any]]:
    """The canonical model for the accession (AF-<acc>-F1 when present), compacted."""
    if not isinstance(entries, list):
        entries = [entries] if isinstance(entries, dict) else []
    models = [e for e in entries if isinstance(e, dict)]
    if not models:
        return None

    def model_id(e: Dict[str, Any]) -> str:
        return str(e.get("modelEntityId") or e.get("entryId") or "")

    canonical = f"AF-{accession}-F1"
    chosen = next((e for e in models if model_id(e) == canonical), None) or next(
        (e for e in models if str(e.get("uniprotAccession") or "") == accession), models[0]
    )
    mean = _round(chosen.get("globalMetricValue"))
    fractions = {
        band: _round(chosen.get(key), 3)
        for band, key in (
            ("very_high", "fractionPlddtVeryHigh"),
            ("confident", "fractionPlddtConfident"),
            ("low", "fractionPlddtLow"),
            ("very_low", "fractionPlddtVeryLow"),
        )
        if chosen.get(key) is not None
    }
    return {
        "model_id": model_id(chosen),
        "version": chosen.get("latestVersion"),
        "mean_plddt": mean,
        "plddt_band": plddt_band(mean),
        "plddt_fractions": fractions or None,
        "url": AFDB_ENTRY_PAGE.format(accession=accession),
        "pdb_url": chosen.get("pdbUrl"),
        "cif_url": chosen.get("cifUrl"),
        "pae_image_url": chosen.get("paeImageUrl"),
        "gene": chosen.get("gene"),
        "description": chosen.get("uniprotDescription"),
        "organism": chosen.get("organismScientificName"),
        "sequence_range": [chosen.get("uniprotStart") or chosen.get("sequenceStart"), chosen.get("uniprotEnd") or chosen.get("sequenceEnd")],
        "tool_used": chosen.get("toolUsed"),
        "created": chosen.get("modelCreatedDate"),
        "n_models": len(models),
    }


def pdb_search_query(accession: str, rows: int = 10) -> Dict[str, Any]:
    return {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                        "operator": "exact_match",
                        "value": accession,
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_name",
                        "operator": "exact_match",
                        "value": "UniProt",
                    },
                },
            ],
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": max(1, min(10, int(rows)))},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }


def summarize_entry(doc: Dict[str, Any], pdb_id: str) -> Dict[str, Any]:
    info = doc.get("rcsb_entry_info") if isinstance(doc.get("rcsb_entry_info"), dict) else {}
    exptl = doc.get("exptl") if isinstance(doc.get("exptl"), list) else []
    methods = [str(e.get("method")) for e in exptl if isinstance(e, dict) and e.get("method")]
    resolution = info.get("resolution_combined")
    if isinstance(resolution, list):
        resolution = resolution[0] if resolution else None
    accession = doc.get("rcsb_accession_info") if isinstance(doc.get("rcsb_accession_info"), dict) else {}
    pid = str(doc.get("rcsb_id") or pdb_id).upper()
    return {
        "pdb_id": pid,
        "title": str((doc.get("struct") or {}).get("title") or "")[:300] or None,
        "method": ", ".join(methods) or info.get("experimental_method"),
        "resolution": _round(resolution, 2),
        "ligands": [str(x) for x in (info.get("nonpolymer_bound_components") or [])][:20],
        "release_date": str(accession.get("initial_release_date") or "")[:10] or None,
        "polymer_entity_count": info.get("polymer_entity_count"),
        "url": RCSB_ENTRY_PAGE.format(pdb_id=pid),
    }


def summarize_diffdock(payload: Any) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    poses = data.get("ligand_positions") or data.get("ligand_poses") or []
    conf = data.get("position_confidence") or data.get("confidence") or []
    confidences = [c for c in (_round(x, 3) for x in (conf if isinstance(conf, list) else [conf])) if c is not None]
    return {
        "status": data.get("status") or ("success" if poses else "unknown"),
        "num_poses": len(poses) if isinstance(poses, list) else 0,
        "confidences": confidences[:20],
        "top_confidence": max(confidences) if confidences else None,
    }


def summarize_molmim(payload: Any, property_name: str) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    molecules = data.get("molecules", payload)
    if isinstance(molecules, str):
        try:
            molecules = json.loads(molecules)
        except ValueError:
            molecules = []
    out: List[Dict[str, Any]] = []
    for m in molecules if isinstance(molecules, list) else []:
        if isinstance(m, dict):
            smiles = m.get("sample") or m.get("smiles") or m.get("smi")
            if smiles:
                out.append({"smiles": str(smiles)[:300], "score": _round(m.get("score"), 4)})
        elif isinstance(m, str):
            out.append({"smiles": m[:300], "score": None})
    return {"property_name": property_name, "molecules": out[:30]}


class StructureClient:
    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_s: float = 20.0,
        api_key: Optional[str] = None,
    ) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self._api_key = api_key
        # An injected transport is a test fixture: never share cached answers with it.
        self.cache: Optional[TTLCache] = _CACHE if transport is None else None

    @property
    def api_key(self) -> str:
        return self._api_key if self._api_key is not None else nvidia_api_key()

    def _client(self, timeout: Optional[float] = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(timeout or self.timeout_s, connect=10.0),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def _request(self, method: str, url: str, *, timeout: Optional[float] = None, **kwargs: Any) -> httpx.Response:
        """One request, no retry; every status is returned (callers map it). Failures raise StructureClientError."""
        try:
            async with self._client(timeout) as client:
                return await http_request(client, method, url, retries=0, accept=ANY_STATUS, **kwargs)
        except HttpError as exc:
            if exc.kind == "timeout":
                raise StructureClientError("timeout", f"{url.split('/')[2]} timed out") from exc.cause
            raise StructureClientError("upstream", f"{url.split('/')[2]}: {type(exc.cause).__name__}") from exc.cause
        except httpx.HTTPError as exc:  # request errors that are not transport errors (redirect loops, decoding)
            raise StructureClientError("upstream", f"{url.split('/')[2]}: {type(exc).__name__}") from exc

    def _cached(self, key: Any) -> Any:
        return self.cache.get(key) if self.cache is not None else None

    def _store(self, key: Any, value: Any) -> None:
        if self.cache is not None:
            self.cache.set(key, value)

    # UniProt ------------------------------------------------------------------------------------------------------
    async def uniprot_accession(self, symbol: str, taxon: int = 9606) -> Optional[str]:
        """The reviewed (Swiss-Prot) human accession for an exact gene symbol, e.g. KRAS -> P01116; None if none."""
        sym = (symbol or "").strip()
        if not sym or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]{0,19}", sym):
            return None
        key = ("uniprot_accession", sym.upper(), taxon)
        cached = self._cached(key)
        if cached is not None:
            return cached or None
        params = {
            "query": f"gene_exact:{sym} AND organism_id:{taxon} AND reviewed:true",
            "fields": "accession", "format": "json", "size": "1",
        }
        resp = await self._request("GET", "https://rest.uniprot.org/uniprotkb/search", params=params)
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"rest.uniprot.org returned HTTP {resp.status_code}")
        results = (resp.json() or {}).get("results") or []
        acc = str((results[0] or {}).get("primaryAccession") or "") if results else ""
        self._store(key, acc)
        return acc or None

    # AlphaFold DB ----------------------------------------------------------------------------------------------
    async def alphafold_prediction(self, accession: str) -> Optional[Dict[str, Any]]:
        acc = accession.strip().upper()
        if not UNIPROT_ACCESSION_RE.match(acc):
            raise StructureClientError("invalid", f"not a UniProt accession: {accession[:20]}")
        key = ("afdb", acc)
        hit = self._cached(key)
        if hit is not None:
            return hit or None
        resp = await self._request("GET", AFDB_PREDICTION_URL.format(accession=acc))
        if resp.status_code in (400, 404):
            self._store(key, {})
            return None
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"AlphaFold DB returned HTTP {resp.status_code}")
        try:
            summary = summarize_alphafold(resp.json(), acc)
        except ValueError as exc:
            raise StructureClientError("upstream", "AlphaFold DB returned invalid JSON") from exc
        self._store(key, summary or {})
        return summary

    # RCSB PDB --------------------------------------------------------------------------------------------------
    async def pdb_search(self, accession: str, rows: int = 10) -> Dict[str, Any]:
        acc = accession.strip().upper()
        key = ("rcsb_search", acc, rows)
        hit = self._cached(key)
        if hit is not None:
            return hit
        resp = await self._request("POST", RCSB_SEARCH_URL, json=pdb_search_query(acc, rows))
        if resp.status_code == 204:
            out: Dict[str, Any] = {"total_count": 0, "pdb_ids": []}
        elif resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError as exc:
                raise StructureClientError("upstream", "RCSB search returned invalid JSON") from exc
            ids = [str(r.get("identifier")).upper() for r in body.get("result_set") or [] if isinstance(r, dict) and r.get("identifier")]
            out = {"total_count": int(body.get("total_count") or len(ids)), "pdb_ids": [i for i in ids if PDB_ID_RE.match(i)][: max(1, min(10, rows))]}
        else:
            raise StructureClientError("upstream", f"RCSB search returned HTTP {resp.status_code}")
        self._store(key, out)
        return out

    async def pdb_entry(self, pdb_id: str) -> Optional[Dict[str, Any]]:
        pid = pdb_id.strip().upper()
        if not PDB_ID_RE.match(pid):
            raise StructureClientError("invalid", f"not a PDB id: {pdb_id[:12]}")
        key = ("rcsb_entry", pid)
        hit = self._cached(key)
        if hit is not None:
            return hit or None
        async with get_limiter("rcsb", 0.05):
            resp = await self._request("GET", RCSB_ENTRY_URL.format(pdb_id=pid))
        if resp.status_code == 404:
            self._store(key, {})
            return None
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"RCSB entry {pid} returned HTTP {resp.status_code}")
        try:
            out = summarize_entry(resp.json(), pid)
        except ValueError as exc:
            raise StructureClientError("upstream", "RCSB entry returned invalid JSON") from exc
        self._store(key, out)
        return out

    async def pdb_entries(self, pdb_ids: List[str]) -> List[Dict[str, Any]]:
        sem = asyncio.Semaphore(4)

        async def one(pid: str) -> Optional[Dict[str, Any]]:
            async with sem:
                try:
                    return await self.pdb_entry(pid)
                except StructureClientError as exc:
                    logger.info("RCSB entry %s failed: %s", pid, exc)
                    return {"pdb_id": pid, "url": RCSB_ENTRY_PAGE.format(pdb_id=pid), "error": str(exc)[:120]}

        results = await asyncio.gather(*(one(p) for p in pdb_ids))
        return [r for r in results if r]

    async def fetch_structure_text(self, url: str) -> str:
        if not url.startswith(("https://alphafold.ebi.ac.uk/files/", "https://files.rcsb.org/download/")):
            raise StructureClientError("invalid", "structure files are fetched only from AlphaFold DB or RCSB")
        resp = await self._request("GET", url, timeout=60.0)
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"structure file returned HTTP {resp.status_code}")
        if len(resp.content) > MAX_PDB_TEXT_BYTES:
            raise StructureClientError("invalid", "structure file is too large to dock")
        return resp.text

    # NVIDIA NIM ------------------------------------------------------------------------------------------------
    def _nim_headers(self) -> Dict[str, str]:
        key = self.api_key
        if not key:
            raise StructureClientError("auth", "NVIDIA_API_KEY is not set")
        return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}

    async def diffdock(self, protein_pdb: str, ligand: str, ligand_file_type: str = "txt", num_poses: int = 5) -> Dict[str, Any]:
        body = {"protein": protein_pdb, "ligand": ligand, "ligand_file_type": ligand_file_type, "num_poses": max(1, min(20, int(num_poses)))}
        resp = await self._request("POST", NIM_DIFFDOCK_URL, json=body, headers=self._nim_headers(), timeout=180.0)
        if resp.status_code in (401, 403):
            raise StructureClientError("auth", f"NVIDIA NIM DiffDock rejected the API key (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"NVIDIA NIM DiffDock returned HTTP {resp.status_code}")
        try:
            return summarize_diffdock(resp.json())
        except ValueError as exc:
            raise StructureClientError("upstream", "DiffDock returned invalid JSON") from exc

    async def molmim_generate(
        self,
        smi: str,
        num_molecules: int = 10,
        property_name: str = "QED",
        min_similarity: float = 0.3,
        algorithm: str = "CMA-ES",
    ) -> Dict[str, Any]:
        body = {
            "smi": smi,
            "algorithm": algorithm,
            "num_molecules": max(1, min(100, int(num_molecules))),
            "property_name": property_name,
            "min_similarity": max(0.0, min(0.7, float(min_similarity))),
        }
        resp = await self._request("POST", NIM_MOLMIM_URL, json=body, headers=self._nim_headers(), timeout=120.0)
        if resp.status_code in (401, 403):
            raise StructureClientError("auth", f"NVIDIA NIM MolMIM rejected the API key (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise StructureClientError("upstream", f"NVIDIA NIM MolMIM returned HTTP {resp.status_code}")
        try:
            return summarize_molmim(resp.json(), property_name)
        except ValueError as exc:
            raise StructureClientError("upstream", "MolMIM returned invalid JSON") from exc


def _default_factory() -> StructureClient:
    return StructureClient()


# Tests replace this with a factory that injects an httpx.MockTransport.
_client_factory = _default_factory


def get_client() -> StructureClient:
    return _client_factory()


def clear_cache() -> None:
    _CACHE.clear()
