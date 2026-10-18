"""
Identifier families checked generically by the verifier and harvested into memory.

A family's ids are 'observed' when they appear in a successful tool output, a retrieved passage,
canonical_ids (canonical_keys), verified memory, the query context, or the user's own question.
Cited ids that were never observed are CRITICAL IDENTIFIER defects, hedged or not.
This module imports nothing from phronesis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Set, Tuple


@dataclass(frozen=True)
class IdentifierFamily:
    name: str
    pattern: re.Pattern
    normalize: Callable[[str], str]
    label: str
    canonical_keys: Tuple[str, ...]
    correction: str
    severity: str = "CRITICAL"


def _identity(s: str) -> str:
    return s


def _upper(s: str) -> str:
    return s.upper()


def _lower(s: str) -> str:
    return s.lower()


def _ontology(s: str) -> str:
    return s.replace(":", "_")


def _strip_version(s: str) -> str:
    return s.split(".", 1)[0].upper()


def _compact_upper(s: str) -> str:
    return re.sub(r"[\s-]", "", s).upper()


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_RE = re.compile(_UUID, re.IGNORECASE)


def _uuid_only(s: str) -> str:
    m = _UUID_RE.search(s)
    return m.group(0).lower() if m else s.lower()


FAMILIES: Tuple[IdentifierFamily, ...] = (
    IdentifierFamily(
        name="nct",
        pattern=re.compile(r"\bNCT\d{8}\b", re.IGNORECASE),
        normalize=_upper,
        label="NCT",
        canonical_keys=("nct_ids",),
        correction="Cite only ClinicalTrials.gov NCT numbers that appear in the retrieved registry records or passages.",
    ),
    IdentifierFamily(
        name="chembl",
        pattern=re.compile(r"\bCHEMBL\d{1,9}\b", re.IGNORECASE),
        normalize=_upper,
        label="CHEMBL",
        canonical_keys=("chembl_ids", "chembl_id"),
        correction="Cite only ChEMBL identifiers returned by an executed tool in this investigation.",
    ),
    IdentifierFamily(
        name="hgnc",
        pattern=re.compile(r"\bHGNC:\d{1,6}\b"),
        normalize=_identity,
        label="HGNC",
        canonical_keys=("hgnc_ids", "hgnc_id"),
        correction="Cite only HGNC identifiers resolved by an executed tool in this investigation.",
    ),
    IdentifierFamily(
        name="ontology",
        pattern=re.compile(r"\b(?:EFO|MONDO|Orphanet|HP|DOID)[_:]\d{4,9}\b"),
        normalize=_ontology,
        label="ONTOLOGY",
        canonical_keys=("ontology_ids", "efo_id"),
        correction="Cite only disease ontology identifiers (MONDO/EFO/Orphanet/HP/DOID) present in the evidence.",
    ),
    IdentifierFamily(
        name="rsid",
        pattern=re.compile(r"\brs\d{3,12}\b"),
        normalize=_lower,
        label="RSID",
        canonical_keys=("rsids",),
        correction="Cite only dbSNP rsIDs present in the evidence or the researcher's question.",
    ),
    IdentifierFamily(
        name="clinvar",
        pattern=re.compile(r"\bVCV\d{9}(?:\.\d+)?\b"),
        normalize=_strip_version,
        label="CLINVAR",
        canonical_keys=("clinvar_vcv",),
        correction="Cite only ClinVar VCV accessions returned by the variant lookup.",
    ),
    IdentifierFamily(
        name="civic",
        pattern=re.compile(r"\b(?:EID|AID)\d{1,6}\b"),
        normalize=_identity,
        label="CIVIC",
        canonical_keys=("civic_ids",),
        correction="Cite only CIViC evidence (EID) or assertion (AID) identifiers returned by the variant lookup.",
    ),
    IdentifierFamily(
        name="geo",
        pattern=re.compile(r"\bGS[EM]\d{1,9}\b"),
        normalize=_identity,
        label="GEO",
        canonical_keys=("geo_accessions",),
        correction="Cite only GEO accessions present in the evidence or confirmed for an attached dataset.",
    ),
    IdentifierFamily(
        name="drugbank",
        pattern=re.compile(r"\bDB\d{5}\b"),
        normalize=_upper,
        label="DRUGBANK",
        canonical_keys=("drugbank_ids", "drugbank_id"),
        correction="Cite only DrugBank identifiers returned by an executed tool (PrimeKG) in this investigation.",
    ),
    IdentifierFamily(
        name="fda_application",
        pattern=re.compile(r"\b(?:NDA|ANDA|BLA)[\s-]?\d{6}\b"),
        normalize=_compact_upper,
        label="FDA_APPLICATION",
        canonical_keys=("application_numbers",),
        correction="Cite only FDA application numbers (NDA/ANDA/BLA) shown on the retrieved openFDA label.",
    ),
    IdentifierFamily(
        # A bare UUID is too generic; only one presented as a DailyMed / label set id is checked.
        name="dailymed_setid",
        pattern=re.compile(
            r"(?:\bset[\s_-]?id\b[\"'`*\s:=(]{0,6}|\bsetid=|\bDailyMed\b[^\n.]{0,60}?)" + _UUID, re.IGNORECASE
        ),
        normalize=_uuid_only,
        label="DAILYMED_SETID",
        canonical_keys=("set_ids",),
        correction="Cite only DailyMed label set ids returned by the openFDA label lookup.",
    ),
    IdentifierFamily(
        name="dataset",
        pattern=re.compile(r"\bds_[0-9a-f]{12}\b"),
        normalize=_identity,
        label="DATASET",
        canonical_keys=("dataset_ids",),
        correction="Refer only to uploaded dataset ids that were attached to this question.",
    ),
)

_BY_NAME: Dict[str, IdentifierFamily] = {f.name: f for f in FAMILIES}


def family_names() -> List[str]:
    return [f.name for f in FAMILIES]


def get_family(name: str) -> IdentifierFamily:
    return _BY_NAME[name]


def extract_family_ids(text: str) -> Dict[str, Set[str]]:
    """Normalized ids of every family found in text (every family key is present)."""
    out: Dict[str, Set[str]] = {f.name: set() for f in FAMILIES}
    if not text:
        return out
    for fam in FAMILIES:
        for m in fam.pattern.finditer(text):
            out[fam.name].add(fam.normalize(m.group(0)))
    return out


def normalize_ids(name: str, ids) -> Set[str]:
    """Normalize ids of one family, splitting comma-separated strings and dropping blanks."""
    fam = _BY_NAME.get(name)
    out: Set[str] = set()
    if ids is None:
        return out
    items = [ids] if isinstance(ids, str) else list(ids)
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.add(fam.normalize(part) if fam else part)
    return out
