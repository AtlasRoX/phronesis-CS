"""The one alternative strategy for an exact PMID lookup that Europe PMC could not answer: NCBI E-utilities efetch.

The record is returned in the Europe PMC article shape the synthesis engine already handles (title, structured
abstract, journal, year, DOI, publication types and "Erratum in"/"Retraction in" links), so a paper fetched this way
is graded, chunked and cited like any other. A failure raises: the caller records it and says the paper was not
retrieved, which is not proof that it does not exist.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import httpx

from ..utils.http_limits import get_limiter

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TIMEOUT_S = 8.0

# PubMed CommentsCorrections RefType -> the Europe PMC commentCorrection type the integrity code reads.
_REF_TYPES = {
    "ErratumIn": "Erratum in",
    "RetractionIn": "Retraction in",
    "ExpressionOfConcernIn": "Expression of concern in",
    "RepublishedIn": "Corrected and republished in",
    "UpdateIn": "Update in",
}


def _text(el: Optional[ET.Element]) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_efetch(xml_text: str) -> Optional[Dict[str, Any]]:
    """The first PubmedArticle of an efetch reply as a Europe PMC-shaped record, or None when there is none."""
    root = ET.fromstring(xml_text)
    art = root.find(".//PubmedArticle")
    if art is None:
        return None
    pmid = _text(art.find(".//MedlineCitation/PMID"))
    parts: List[str] = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = ab.get("Label")
        body = _text(ab)
        if body:
            parts.append(f"{label}: {body}" if label else body)
    doi = next((_text(e) for e in art.findall(".//ELocationID") if e.get("EIdType") == "doi"), "")
    if not doi:
        doi = next((_text(e) for e in art.findall(".//ArticleIdList/ArticleId") if e.get("IdType") == "doi"), "")
    pmcid = next((_text(e) for e in art.findall(".//ArticleIdList/ArticleId") if e.get("IdType") == "pmc"), "")
    pub_types = [_text(p) for p in art.findall(".//PublicationTypeList/PublicationType") if _text(p)]
    authors = [
        " ".join(x for x in (_text(a.find("LastName")), _text(a.find("Initials"))) if x)
        for a in art.findall(".//AuthorList/Author")
    ]
    corrections = []
    for cc in art.findall(".//CommentsCorrectionsList/CommentsCorrections"):
        kind = _REF_TYPES.get(cc.get("RefType") or "")
        if kind:
            corrections.append({"source": "MED", "id": _text(cc.find("PMID")), "reference": _text(cc.find("RefSource")), "type": kind})
    return {
        "id": pmid,
        "source": "MED",
        "pmid": pmid,
        "doi": doi,
        "pmcid": pmcid,
        "title": _text(art.find(".//ArticleTitle")),
        "authorString": ", ".join(a for a in authors if a),
        "journalTitle": _text(art.find(".//Journal/Title")),
        "pubYear": _text(art.find(".//Journal/JournalIssue/PubDate/Year")) or _text(art.find(".//ArticleDate/Year")),
        "abstractText": " ".join(parts),
        "pubType": "; ".join(pub_types),
        "pubTypeList": {"pubType": pub_types},
        "commentCorrectionList": {"commentCorrection": corrections} if corrections else {},
        "isOpenAccess": False,
        "_retrieved_via": "ncbi_efetch",
    }


async def fetch_pubmed_record(pmid: str) -> Optional[Dict[str, Any]]:
    """One efetch request for a PMID (rate-limited with the other NCBI calls); None when PubMed has no such record."""
    params = {"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "phronesis"}
    if os.getenv("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    if os.getenv("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    async with get_limiter("ncbi_eutils", 0.34):
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(EFETCH_URL, params=params)
            resp.raise_for_status()
    return parse_efetch(resp.text)


__all__ = ["fetch_pubmed_record", "parse_efetch"]
