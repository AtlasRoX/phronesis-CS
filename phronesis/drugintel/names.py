"""
Drug-mention and drug-intent detection for routing the ChEMBL and openFDA sources.

Detection only proposes names; every name is then resolved by exact ChEMBL match, so a wrong guess
ends up as "unresolved" rather than as data about the wrong molecule.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

MAX_DRUGS = 2

CHEMBL_ID_RE = re.compile(r"\bCHEMBL\d{1,9}\b", re.IGNORECASE)

# INN stems (WHO stem book). The stem must follow at least three letters, so bare words never match.
_INN_STEMS = (
    "tinib|rafenib|ciclib|parib|lisib|asib|degib|zumab|ximab|umab|mab|platin|taxel|rubicin|tecan|"
    "lutamide|gliptin|gliflozin|glutide|navir|previr|sartan|vastatin|prazole"
)
INN_SUFFIX_RE = re.compile(rf"\b([a-z][a-z\-]{{2,}}(?:{_INN_STEMS}))\b", re.IGNORECASE)

# Common drugs whose names carry no recognisable stem, plus a few brand names ChEMBL lists as synonyms.
CURATED_DRUGS = frozenset({
    "aspirin", "metformin", "warfarin", "tamoxifen", "cisplatin", "methotrexate", "fluorouracil",
    "gemcitabine", "capecitabine", "cyclophosphamide", "lenalidomide", "thalidomide", "pomalidomide",
    "sotorasib", "adagrasib", "venetoclax", "everolimus", "sirolimus", "rapamycin", "temozolomide",
    "letrozole", "anastrozole", "exemestane", "fulvestrant", "abiraterone", "bortezomib", "carfilzomib",
    "acetaminophen", "paracetamol", "ibuprofen", "naproxen", "celecoxib", "clopidogrel", "heparin",
    "apixaban", "rivaroxaban", "dabigatran", "lisinopril", "amlodipine", "digoxin", "amiodarone",
    "levothyroxine", "prednisone", "dexamethasone", "hydroxychloroquine", "azithromycin", "vancomycin",
    "ciprofloxacin", "amoxicillin", "penicillin", "morphine", "fentanyl", "vincristine", "etoposide",
    "bleomycin", "hydroxyurea", "allopurinol", "colchicine", "isotretinoin", "tretinoin", "rosiglitazone",
    "pioglitazone", "sildenafil", "finasteride", "valproate", "lithium", "clozapine", "haloperidol",
    "gleevec", "glivec", "keytruda", "opdivo", "tagrisso", "herceptin", "avastin", "humira", "lumakras",
    "krazati", "tarceva", "iressa", "xalkori", "coumadin", "lipitor", "ozempic", "revlimid", "imbruvica",
})

# Words that end in an INN stem but are not drugs.
_NOT_DRUGS = frozenset({"chembl"})

DRUG_INTENT_RE = re.compile(
    r"adverse|side effects?|safety|toxicit|faers|boxed|black[- ]box|label warnings?|contraindicat|"
    r"mechanism of action|\bmoa\b|ic50|\bki\b|\bkd\b|potency|bioactivit|chembl|inhibitors? (?:of|for)|"
    r"drugs? (?:targeting|against|for)|approved (?:drugs?|therap)|\bprotacs?\b|degraders?|small[- ]molecules?|"
    r"chemical probes?|(?:compounds?|ligands?|binders?) (?:targeting|against|for|that|which)",
    re.IGNORECASE,
)

SAFETY_INTENT_RE = re.compile(
    r"adverse|side effects?|safety|toxicit|faers|boxed|black[- ]box|label|warnings?|contraindicat|"
    r"pharmacovigilance|post[- ]marketing|\behrs?\b|electronic health records?|real[- ]world (?:safety|evidence|data)",
    re.IGNORECASE,
)

# Safety intent strong enough to query openFDA for candidate drugs when no drug is named. Narrower than
# SAFETY_INTENT_RE: a bare "label" or "warning" does not ask for adverse-event data.
CANDIDATE_SAFETY_INTENT_RE = re.compile(
    r"adverse|side effects?|safety|toxicit|faers|pharmacovigilance|post[- ]marketing|\behrs?\b|"
    r"electronic health records?|real[- ]world (?:safety|evidence|data)|label warnings?|boxed warnings?|black[- ]box",
    re.IGNORECASE,
)

# Questions that ask for EHR / real-world clinical data, which no source here provides (FAERS is not EHR data).
EHR_INTENT_RE = re.compile(r"\behrs?\b|electronic health records?|real[- ]world", re.IGNORECASE)

# FDA label or approval questions: openFDA is the only source that can show an NDA/BLA label.
LABEL_INTENT_RE = re.compile(
    r"\bfda\b|approv|\blabel|prescribing information|\bindicat|\bdailymed\b|\bnda\b|\bbla\b|regulator",
    re.IGNORECASE,
)

# Regulatory-status questions strong enough to look up FDA approvals for candidate drugs when no drug is named
# ("FDA-approved indications for KRAS G12C inhibitors"). Narrower than LABEL_INTENT_RE: "which approved drugs
# target KRAS" is a mechanism question (ChEMBL answers it), and a bare "indicates" must not start openFDA calls.
REGULATORY_INTENT_RE = re.compile(
    r"\bfda\b|\bema\b|regulator|accelerated approval|approval (?:status|history|dates?)|approved indications?|"
    r"prescribing information|\bdailymed\b|label(?:ed)? indications?|\bnda\b|\bbla\b",
    re.IGNORECASE,
)

# A protein variant written as reference residue, position, alternate residue ("G12C", "V600E").
ALLELE_RE = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY])(\d{1,4})([ACDEFGHIKLMNPQRSTVWY])\b")

_CURATED_RE = re.compile(r"\b(" + "|".join(sorted(CURATED_DRUGS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def extract_drug_mentions(query: str) -> List[str]:
    """Up to MAX_DRUGS drug mentions in order of appearance: upper-case CHEMBL ids, lower-case names."""
    hits: List[Tuple[int, str]] = []
    for m in CHEMBL_ID_RE.finditer(query or ""):
        hits.append((m.start(), m.group(0).upper()))
    for m in INN_SUFFIX_RE.finditer(query or ""):
        name = m.group(1).lower()
        if len(name) >= 6 and name not in _NOT_DRUGS:
            hits.append((m.start(), name))
    for m in _CURATED_RE.finditer(query or ""):
        hits.append((m.start(), m.group(1).lower()))
    out: List[str] = []
    for _, name in sorted(hits):
        if name not in out:
            out.append(name)
    return out[:MAX_DRUGS]


def split_mentions(mentions: List[str]) -> Tuple[List[str], List[str]]:
    """(names, chembl_ids) from a mixed mention list."""
    ids = [m.upper() for m in mentions if CHEMBL_ID_RE.fullmatch(m)]
    names = [m for m in mentions if not CHEMBL_ID_RE.fullmatch(m)]
    return names, ids


def _entity_query(ent: Any) -> Optional[Tuple[str, str]]:
    """(value sent to ChEMBL, the user's words) for a resolved drug entity: its ChEMBL id when known."""
    chembl_id = (getattr(ent, "ids", None) or {}).get("chembl_id")
    label = chembl_id or getattr(ent, "label", None) or getattr(ent, "mention", None)
    if not label:
        return None
    value = label.upper() if CHEMBL_ID_RE.fullmatch(str(label)) else str(label).lower()
    said = str(getattr(ent, "mention", None) or getattr(ent, "label", None) or label).strip()
    return value, said


def drug_mentions_for(ctx: Any) -> List[str]:
    """Drug mentions for a query context: resolved drug entities first, else the query text."""
    out: List[str] = []
    for ent in list(getattr(ctx, "drugs", None) or []):
        pair = _entity_query(ent)
        if pair and pair[0] not in out:
            out.append(pair[0])
    if out:
        return out[:MAX_DRUGS]
    return extract_drug_mentions(getattr(ctx, "clean_query", None) or getattr(ctx, "raw_query", "") or "")


def drug_display_names(ctx: Any) -> Dict[str, str]:
    """{value sent to ChEMBL: the user's own words}, so a drug queried by its ChEMBL id (MRTX1133 ->
    CHEMBL5081048, which has no ChEMBL preferred name) is still shown as the user named it."""
    out: Dict[str, str] = {}
    for ent in list(getattr(ctx, "drugs", None) or []):
        pair = _entity_query(ent)
        if pair and pair[1] and not CHEMBL_ID_RE.fullmatch(pair[1]):
            out.setdefault(pair[0], pair[1])
    return out


DEGRADER_RE = re.compile(r"\bprotacs?\b|degrader|targeted protein degradation|molecular glue", re.IGNORECASE)


def has_degrader_intent(query: str) -> bool:
    return bool(DEGRADER_RE.search(query or ""))


def has_drug_intent(query: str) -> bool:
    return bool(DRUG_INTENT_RE.search(query or ""))


def has_safety_intent(query: str) -> bool:
    return bool(SAFETY_INTENT_RE.search(query or ""))


def has_candidate_safety_intent(query: str) -> bool:
    return bool(CANDIDATE_SAFETY_INTENT_RE.search(query or ""))


def has_ehr_intent(query: str) -> bool:
    return bool(EHR_INTENT_RE.search(query or ""))


def has_label_intent(query: str) -> bool:
    return bool(LABEL_INTENT_RE.search(query or ""))


def has_regulatory_intent(query: str) -> bool:
    return bool(REGULATORY_INTENT_RE.search(query or ""))


def query_alleles(query: str) -> List[str]:
    """Protein variants named in the question ("KRAS G12C" -> ["G12C"]), upper case, in order."""
    out: List[str] = []
    for m in ALLELE_RE.finditer(query or ""):
        allele = m.group(0).upper()
        if allele not in out:
            out.append(allele)
    return out


def normalize_target_symbol(symbol: Optional[str]) -> Optional[str]:
    """'KRAS G12C' -> 'KRAS'; None when nothing usable is left."""
    if not symbol:
        return None
    token = str(symbol).strip().split()[0] if str(symbol).strip() else ""
    token = re.sub(r"[^A-Za-z0-9\-]", "", token).upper()
    return token or None


def display_names(mentions: List[str]) -> str:
    return " and ".join(mentions) if mentions else "the named drugs"


# International (INN / British) names -> the US adopted names that FAERS and FDA labels record. FAERS returns 0
# reports for "mesalazine" but 63,000 for "mesalamine", so openfda_drug_safety retries with the US name.
US_DRUG_NAMES: Dict[str, str] = {
    "mesalazine": "mesalamine",
    "paracetamol": "acetaminophen",
    "salbutamol": "albuterol",
    "levosalbutamol": "levalbuterol",
    "adrenaline": "epinephrine",
    "noradrenaline": "norepinephrine",
    "glibenclamide": "glyburide",
    "ciclosporin": "cyclosporine",
    "rifampicin": "rifampin",
    "frusemide": "furosemide",
    "lignocaine": "lidocaine",
    "pethidine": "meperidine",
    "aciclovir": "acyclovir",
    "valaciclovir": "valacyclovir",
    "colecalciferol": "cholecalciferol",
    "colestyramine": "cholestyramine",
    "dexamfetamine": "dextroamphetamine",
    "amfetamine": "amphetamine",
    "metamfetamine": "methamphetamine",
    "isoprenaline": "isoproterenol",
    "orciprenaline": "metaproterenol",
    "glyceryl trinitrate": "nitroglycerin",
    "hydroxycarbamide": "hydroxyurea",
    "tioguanine": "thioguanine",
    "chlorphenamine": "chlorpheniramine",
    "phenobarbitone": "phenobarbital",
    "thiopentone": "thiopental",
    "beclometasone": "beclomethasone",
    "flumetasone": "flumethasone",
    "sodium cromoglicate": "cromolyn sodium",
    "cromoglicic acid": "cromolyn",
    "benzylpenicillin": "penicillin g",
    "phenoxymethylpenicillin": "penicillin v",
    "ethinylestradiol": "ethinyl estradiol",
    "oestradiol": "estradiol",
    "eformoterol": "formoterol",
    "torasemide": "torsemide",
    "amethocaine": "tetracaine",
    "dicycloverine": "dicyclomine",
    "bendrofluazide": "bendroflumethiazide",
    "mitozantrone": "mitoxantrone",
    "levomepromazine": "methotrimeprazine",
    "benzatropine": "benztropine",
    "chlormethine": "mechlorethamine",
    "hyoscine": "scopolamine",
    "glycopyrronium bromide": "glycopyrrolate",
    "suxamethonium": "succinylcholine",
    "tetracosactide": "cosyntropin",
    "cefalexin": "cephalexin",
    "cefalotin": "cephalothin",
    "amoxycillin": "amoxicillin",
    "clomifene": "clomiphene",
    "methylthioninium chloride": "methylene blue",
    "ergometrine": "ergonovine",
    "dimeticone": "dimethicone",
}


def us_drug_name(name: Optional[str]) -> Optional[str]:
    """The US adopted name for an international drug name ("mesalazine" -> "mesalamine"), or None."""
    key = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    return US_DRUG_NAMES.get(key)
