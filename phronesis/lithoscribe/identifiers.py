"""Explicit literature identifiers (PMID, PMCID, DOI) named in a question, normalized for exact lookup.

Europe PMC has no "PMID:" search field ("PMID:39369609" returns zero hits); a PMID is fetched with
"EXT_ID:<id> AND SRC:MED", a PMCID with "PMCID:<id>" and a DOI with 'DOI:"<doi>"'. A question that names an identifier
creates a retrieval obligation: the record is fetched by that identifier before any topical search, and a failed lookup
means "not retrieved", never "does not exist".
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# "PMID 39369609", "PMID: 39369609", "pmid=39369609", "PubMed ID 39369609", pubmed.ncbi.nlm.nih.gov/39369609
_PMID_RE = re.compile(
    r"\b(?:PMID|PubMed\s*(?:ID|identifier)?)\s*[:#=]?\s*(\d{1,9})\b"
    r"|pubmed\.ncbi\.nlm\.nih\.gov/(\d{1,9})\b"
    r"|europepmc\.org/(?:abstract|article)/MED/(\d{1,9})\b",
    re.I,
)
_PMCID_RE = re.compile(r"\b(PMC\d{3,10})\b", re.I)
# Crossref's recommended pattern; trailing punctuation is not part of the DOI.
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)

MAX_LITERATURE_IDS = 5


def normalize_literature_id(raw: str) -> Tuple[Optional[str], str]:
    """(normalized id or None, kind): kind is pmid | pmcid | doi | invalid. "PMID:123" -> ("123", "pmid")."""
    text = (raw or "").strip()
    m = re.fullmatch(r"(?:pmid\s*[:#=]?\s*)?(\d{1,9})", text, re.I)
    if m:
        return m.group(1), "pmid"
    m = re.fullmatch(r"(?:pmcid\s*[:#=]?\s*)?(PMC\d{3,10})", text, re.I)
    if m:
        return m.group(1).upper(), "pmcid"
    doi = re.sub(r"^(?:doi\s*[:=]?\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.I).strip().rstrip(".,;:)]}")
    if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return doi.lower(), "doi"
    return None, "invalid"


def europepmc_exact_query(normalized: str, kind: str) -> Optional[str]:
    """The Europe PMC query that returns exactly this record."""
    if kind == "pmid":
        return f"EXT_ID:{normalized} AND SRC:MED"
    if kind == "pmcid":
        return f"PMCID:{normalized}"
    if kind == "doi":
        return f'DOI:"{normalized}"'
    return None


def extract_literature_ids(text: str, limit: int = MAX_LITERATURE_IDS) -> List[str]:
    """Identifiers the text names explicitly, normalized, in order of appearance: PMIDs (only when labeled "PMID" or as
    a PubMed URL, so years and doses are never mistaken for one), PMCIDs and DOIs."""
    found: List[Tuple[int, str]] = []
    for m in _PMID_RE.finditer(text or ""):
        found.append((m.start(), next(g for g in m.groups() if g)))
    for m in _PMCID_RE.finditer(text or ""):
        found.append((m.start(), m.group(1).upper()))
    for m in _DOI_RE.finditer(text or ""):
        doi = m.group(1).rstrip(".,;:)]}").lower()
        found.append((m.start(), doi))
    out: List[str] = []
    for _pos, ident in sorted(found):
        if ident not in out:
            out.append(ident)
    return out[:limit]


def strip_literature_ids(text: str) -> str:
    """The text without its identifiers (and their "PMID" labels), for topical search terms."""
    text = _PMID_RE.sub(" ", text or "")
    text = _PMCID_RE.sub(" ", text)
    text = _DOI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


__all__ = [
    "MAX_LITERATURE_IDS",
    "europepmc_exact_query",
    "extract_literature_ids",
    "normalize_literature_id",
    "strip_literature_ids",
]
