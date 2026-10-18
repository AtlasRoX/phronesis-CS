"""
Publication integrity status (retractions, withdrawals, expressions of concern, corrections) for
Lithoscribe articles, combined from two independent sources:

- EuropePMC core metadata: pubType 'Retracted Publication' and commentCorrectionList entries
  ('Retraction in', 'Expression of concern in', 'Erratum in', ...). No extra request.
- Crossref works ``updated-by`` (publisher deposits plus the integrated Retraction Watch database),
  fetched with ONE batched filter request per turn. Only ``updated-by`` judges an article: ``update-to``
  also appears on some original papers (e.g. the Surgisphere Lancet paper) and never marks a notice.

A failed Crossref call (timeout, 429, empty or non-JSON body) is reported as 'unavailable', never as
'no retraction'. DOIs are sent in pages of CROSSREF_PAGE_SIZE; when only some pages fail the batch is 'partial'
and the DOIs that were not checked are listed. An article no source answered for stays 'unchecked': EuropePMC
counts as a source only for records it indexes for corrections (MEDLINE records, or records carrying correction
data), so a preprint or PMC-only record without a Crossref answer is never reported as 'not retracted'.

A partial retraction is its own status ('partially_retracted'), ranked above an expression of concern: the article
stays in the evidence but must be cited with that caveat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Set

import httpx
from pydantic import BaseModel, Field

from ..utils.http_limits import get_limiter
from .evidence_grading import EvidenceGrade, pub_types

logger = logging.getLogger(__name__)

CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_TIMEOUT_S = 6.0
CROSSREF_PAGE_SIZE = 50  # DOIs per filter request

NoticeKind = Literal[
    "retraction", "partial_retraction", "withdrawal", "removal", "expression_of_concern", "correction", "erratum"
]
StatusName = Literal[
    "retracted", "withdrawn", "partially_retracted", "expression_of_concern", "corrected", "none", "unchecked"
]


class IntegrityNotice(BaseModel):
    kind: NoticeKind
    source: Literal["europepmc", "crossref_publisher", "crossref_retraction_watch"]
    notice_pmid: Optional[str] = None
    notice_doi: Optional[str] = None
    date: Optional[str] = None  # Crossref updated.date-time
    reference: Optional[str] = None  # EuropePMC commentCorrection.reference
    record_id: Optional[str] = None  # Retraction Watch record-id (via Crossref)


class IntegrityStatus(BaseModel):
    status: StatusName = "unchecked"
    notices: List[IntegrityNotice] = Field(default_factory=list)
    sources_checked: List[Literal["europepmc", "crossref"]] = Field(default_factory=list)
    crossref_error: Optional[str] = None


class GradedArticle(BaseModel):
    article_key: str
    source: str = ""
    pmid: Optional[str] = None
    doi: Optional[str] = None
    pmcid: Optional[str] = None
    title: str = ""
    journal: str = ""
    year: str = ""
    grade: EvidenceGrade
    integrity: IntegrityStatus
    is_integrity_notice: bool = False
    excluded_reason: Optional[str] = None  # 'retracted' | 'withdrawn' | 'integrity_notice'


class CrossrefBatchResult(BaseModel):
    status: Literal["checked", "partial", "unavailable", "skipped", "disabled"]
    error: Optional[str] = None
    found: Set[str] = Field(default_factory=set)  # lowercased DOIs Crossref returned
    notices: Dict[str, List[IntegrityNotice]] = Field(default_factory=dict)  # lowercased DOI -> notices
    requested: int = 0  # distinct DOIs asked for
    unchecked: List[str] = Field(default_factory=list)  # lowercased DOIs no successful request covered

    def check_label(self) -> str:
        if self.status == "unavailable":
            return f"unavailable: {self.error or 'unknown error'}"
        if self.status == "partial":
            # Starts with "unavailable" so every consumer that warns on an unavailable check warns here too.
            return f"unavailable for {len(self.unchecked)} of {self.requested} DOIs: {self.error or 'unknown error'}"
        if self.status == "skipped":
            return f"skipped: {self.error or 'no DOIs'}"
        return self.status

    def covers(self, doi: Optional[str]) -> bool:
        """True when a successful Crossref request included this DOI."""
        key = (doi or "").strip().lower()
        return bool(key) and self.status in ("checked", "partial") and key not in self.unchecked


# EuropePMC commentCorrection type -> notice kind
_EPMC_CC_KINDS = {
    "retraction in": "retraction",
    "partial retraction in": "partial_retraction",
    "expression of concern in": "expression_of_concern",
    "erratum in": "erratum",
    "correction in": "correction",
    "corrected and republished in": "correction",
}

_CROSSREF_KINDS = {
    "retraction": "retraction",
    "partial_retraction": "partial_retraction",
    "withdrawal": "withdrawal",
    "removal": "removal",
    "expression_of_concern": "expression_of_concern",
    "correction": "correction",
    "corrigendum": "correction",
    "erratum": "erratum",
}

_KIND_STATUS = {
    "retraction": "retracted",
    "withdrawal": "withdrawn",
    "removal": "withdrawn",
    # Part of the findings withdrawn: not an erratum. Kept in the evidence but flagged like an expression of concern.
    "partial_retraction": "partially_retracted",
    "expression_of_concern": "expression_of_concern",
    "correction": "corrected",
    "erratum": "corrected",
}
_STATUS_RANK = {
    "retracted": 5, "withdrawn": 5, "partially_retracted": 4, "expression_of_concern": 3, "corrected": 2, "none": 1,
    "unchecked": 0,
}
# Statuses that keep an article in the evidence but require the citing sentence to carry the caveat.
CAVEAT_STATUSES = frozenset({"partially_retracted", "expression_of_concern"})

EXCLUDING_STATUSES = frozenset({"retracted", "withdrawn"})


def _status_from_notices(notices: Iterable[IntegrityNotice]) -> str:
    best = "none"
    for n in notices:
        status = _KIND_STATUS.get(n.kind, "none")
        if _STATUS_RANK[status] > _STATUS_RANK[best]:
            best = status
    return best


def _comment_corrections(article: Dict[str, Any]) -> List[Any]:
    ccl = article.get("commentCorrectionList")
    entries = (ccl or {}).get("commentCorrection") if isinstance(ccl, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    return list(entries or [])


def europepmc_covers(article: Dict[str, Any]) -> bool:
    """Whether EuropePMC metadata can answer the integrity question for this record. MEDLINE (source MED)
    records carry PubMed's comment/correction links; other records (PMC-only, preprints) only when they list
    correction data or a 'Retracted Publication' type. Without coverage, EuropePMC's silence is not evidence of
    'no retraction'."""
    if str(article.get("source") or "").upper() == "MED":
        return True
    if _comment_corrections(article):
        return True
    return any(p.lower() == "retracted publication" for p in pub_types(article))


def europepmc_integrity(article: Dict[str, Any]) -> IntegrityStatus:
    """Integrity status from EuropePMC core metadata alone. sources_checked is empty (status 'unchecked') when
    EuropePMC has no integrity coverage for the record (europepmc_covers)."""
    notices: List[IntegrityNotice] = []
    for cc in _comment_corrections(article):
        if not isinstance(cc, dict):
            continue
        kind = _EPMC_CC_KINDS.get(str(cc.get("type") or "").strip().lower())
        if not kind:
            continue
        cc_id = str(cc.get("id") or "").strip()
        notices.append(
            IntegrityNotice(
                kind=kind,  # type: ignore[arg-type]
                source="europepmc",
                notice_pmid=cc_id if cc_id.isdigit() and str(cc.get("source") or "MED").upper() == "MED" else None,
                notice_doi=_doi_in(cc.get("reference")),
                reference=str(cc.get("reference")) if cc.get("reference") else None,
            )
        )
    types = {p.lower() for p in pub_types(article)}
    if "retracted publication" in types and not any(n.kind == "retraction" for n in notices):
        notices.append(IntegrityNotice(kind="retraction", source="europepmc", reference="EuropePMC publication type: Retracted Publication"))
    if not notices and not europepmc_covers(article):
        return IntegrityStatus(status="unchecked", notices=[], sources_checked=[])
    return IntegrityStatus(status=_status_from_notices(notices), notices=notices, sources_checked=["europepmc"])  # type: ignore[arg-type]


_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def _doi_in(text: Any) -> Optional[str]:
    m = _DOI_RE.search(str(text or ""))
    return m.group(1).rstrip(".,;)").lower() if m else None


def normalize_update_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def parse_crossref_updates(items: Iterable[Dict[str, Any]]) -> Dict[str, List[IntegrityNotice]]:
    """Crossref work items -> {lowercased DOI: notices from its updated-by entries}. update-to is ignored."""
    out: Dict[str, List[IntegrityNotice]] = {}
    for item in items or []:
        if not isinstance(item, dict) or not item.get("DOI"):
            continue
        doi = str(item["DOI"]).lower()
        notices = out.setdefault(doi, [])
        for upd in item.get("updated-by") or []:
            if not isinstance(upd, dict):
                continue
            kind = _CROSSREF_KINDS.get(normalize_update_type(upd.get("type")))
            if not kind:
                continue
            source = "crossref_retraction_watch" if str(upd.get("source") or "").lower() == "retraction-watch" else "crossref_publisher"
            notices.append(
                IntegrityNotice(
                    kind=kind,  # type: ignore[arg-type]
                    source=source,  # type: ignore[arg-type]
                    notice_doi=str(upd.get("DOI")).lower() if upd.get("DOI") else None,
                    date=(upd.get("updated") or {}).get("date-time") if isinstance(upd.get("updated"), dict) else None,
                    record_id=str(upd.get("record-id")) if upd.get("record-id") else None,
                )
            )
    return out


def merge_integrity(epmc: Optional[IntegrityStatus], crossref: Optional[CrossrefBatchResult], doi: Optional[str]) -> IntegrityStatus:
    """Combine both sources. Precedence: retracted/withdrawn > partially_retracted > expression_of_concern >
    corrected > none."""
    notices: List[IntegrityNotice] = list(epmc.notices) if epmc else []
    sources: List[str] = list(epmc.sources_checked) if epmc else []
    error: Optional[str] = None
    key = (doi or "").strip().lower()
    if crossref is not None:
        if crossref.status == "unavailable" or (crossref.status == "partial" and key and key in crossref.unchecked):
            error = crossref.error or "unavailable"
        elif crossref.covers(key) and key in crossref.found:
            sources.append("crossref")
            notices.extend(crossref.notices.get(key, []))
    if not sources:
        return IntegrityStatus(status="unchecked", notices=notices, sources_checked=[], crossref_error=error)
    return IntegrityStatus(
        status=_status_from_notices(notices),  # type: ignore[arg-type]
        notices=notices,
        sources_checked=sources,  # type: ignore[arg-type]
        crossref_error=error,
    )


class CrossrefIntegrityClient:
    """One batched Crossref filter request per call. Errors are captured into the result, never raised."""

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_s: float = CROSSREF_TIMEOUT_S,
        mailto: Optional[str] = None,
    ) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.mailto = mailto if mailto is not None else os.getenv("PHRONESIS_CROSSREF_MAILTO", "").strip()

    def _user_agent(self) -> str:
        base = "Phronesis-Lithoscribe/1.0 (integrity check)"
        return f"{base} (mailto:{self.mailto})" if self.mailto else base

    async def async_check_dois(self, dois: Iterable[Optional[str]]) -> CrossrefBatchResult:
        """Check every DOI, in pages of CROSSREF_PAGE_SIZE sent concurrently (the shared Crossref limiter spaces
        them). A DOI containing a comma cannot be expressed in the filter and is reported unchecked, never
        silently dropped."""
        requested = list(dict.fromkeys(str(d).strip().lower() for d in dois if d and str(d).strip()))
        wanted = [d for d in requested if "," not in d]  # a comma would split the filter value
        unsendable = [d for d in requested if "," in d]
        if not requested:
            return CrossrefBatchResult(status="skipped", error="no DOIs")
        pages = [wanted[i:i + CROSSREF_PAGE_SIZE] for i in range(0, len(wanted), CROSSREF_PAGE_SIZE)]
        results = await asyncio.gather(*(self._check_page(page) for page in pages))
        found: Set[str] = set()
        notices: Dict[str, List[IntegrityNotice]] = {}
        unchecked: List[str] = list(unsendable)
        errors: List[str] = []
        failed_pages = False
        for page, res in zip(pages, results):
            if res.status == "checked":
                found |= res.found
                notices.update(res.notices)
            else:
                failed_pages = True
                unchecked.extend(page)
                errors.append(res.error or "unknown error")
        error = "; ".join(dict.fromkeys(errors)) or None
        if not pages or len(unchecked) == len(requested):
            return CrossrefBatchResult(status="unavailable", error=error, requested=len(requested), unchecked=unchecked)
        return CrossrefBatchResult(
            status="partial" if failed_pages else "checked", error=error, found=found, notices=notices,
            requested=len(requested), unchecked=unchecked,
        )

    async def _check_page(self, wanted: List[str]) -> CrossrefBatchResult:
        """One Crossref filter request for at most CROSSREF_PAGE_SIZE DOIs."""
        params = {
            "filter": ",".join(f"doi:{d}" for d in wanted),
            "select": "DOI,updated-by,type,title",
            "rows": str(CROSSREF_PAGE_SIZE),
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(self.timeout_s),
                headers={"User-Agent": self._user_agent(), "Accept": "application/json"},
            ) as client:
                async with get_limiter("crossref", 0.2):
                    resp = await client.get(CROSSREF_WORKS_URL, params=params)
        except httpx.TimeoutException:
            return CrossrefBatchResult(status="unavailable", error="timeout")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Crossref integrity check failed: %s", exc)
            return CrossrefBatchResult(status="unavailable", error=type(exc).__name__)
        if resp.status_code != 200:
            return CrossrefBatchResult(status="unavailable", error=f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
            items = payload["message"]["items"]
            if not isinstance(items, list):
                raise ValueError("items is not a list")
        except Exception:  # noqa: BLE001
            return CrossrefBatchResult(status="unavailable", error="invalid response")
        notices = parse_crossref_updates(items)
        return CrossrefBatchResult(status="checked", found=set(notices), notices=notices)


def notice_year(notice: Dict[str, Any]) -> Optional[str]:
    """Year of a notice from its Crossref date or the EuropePMC reference text ('Lancet. 2010 Feb 6;...')."""
    date = str(notice.get("date") or "")
    if re.match(r"^\d{4}", date):
        return date[:4]
    m = re.search(r"\b(19|20)\d{2}\b", str(notice.get("reference") or ""))
    return m.group(0) if m else None


def source_names(notices: Iterable[Dict[str, Any]]) -> List[str]:
    names = []
    for n in notices:
        label = {
            "europepmc": "EuropePMC",
            "crossref_retraction_watch": "Crossref/Retraction Watch",
            "crossref_publisher": "Crossref (publisher)",
        }.get(str(n.get("source")))
        if label and label not in names:
            names.append(label)
    return names
