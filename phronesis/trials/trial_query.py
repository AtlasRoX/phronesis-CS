"""
Deterministic ClinicalTrials.gov query planning (no LLM). The registry's query.term ANDs every word, so a
natural-language question matches nothing ("what clinical trials exist for KRAS G12C inhibitors" -> 0, while
"KRAS G12C" -> 224); plans are therefore built from extracted entities only: NCT IDs, drugs (INN stems and
development codes), the named target, up to three conditions (coordinated phrases such as "ovarian and prostate
cancers" are expanded, one registry search each) and clinical-setting terms (adjuvant, resected, stage IIB).
QueryContext entities are preferred when the entity resolver supplied them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .clinical_trials import MAX_CONDITIONS, SETTING_TERMS, build_strategies

logger = logging.getLogger(__name__)

_NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)

_CLINICAL_INTENT_RE = re.compile(
    r"\b(?:clinical[\s\-]+trials?|trials?|phase\s*(?:[1-4]|i{1,3}|iv)[ab]?|nct\d{8}|response\s+rates?|"
    r"(?:objective|overall|complete|partial)\s+response|orr|pfs|"
    r"(?:overall|progression[\s\-]free|disease[\s\-]free|event[\s\-]free|median)\s+survival|survival\s+benefit|"
    # A bare "landscape" is usually genomic ("mutational landscape"), so only clinical landscapes count.
    r"efficacy|approv(?:al|als|ed)|(?:clinical|trials?|treatment|therapeutic|development|competitive|pipeline)\s+landscape|recruiting|enrol(?:l)?ment|pivotal|randomi[sz]ed|"
    r"placebo[\s\-]controlled|primary\s+endpoints?|hazard\s+ratio)\b",
    re.IGNORECASE,
)

_INN_SUFFIXES = (
    "mab", "tinib", "nib", "rasib", "ciclib", "parib", "lisib", "rafenib", "platin", "taxel", "rubicin", "cept",
    "stat", "tide", "leucel", "degib", "zomib", "limus", "tecan", "vedotin", "citabine", "trexate", "mustine",
)
# A 3-letter stem is enough ("ola-parib", "ixa-zomib"); the stopwords below guard words like "concept".
_INN_RE = re.compile(r"\b([a-z][a-z]{2,}(?:" + "|".join(sorted(_INN_SUFFIXES, key=len, reverse=True)) + r"))\b", re.IGNORECASE)
_INN_STOPWORDS = {
    "concept", "except", "accept", "intercept", "precept", "percept", "incept", "thermostat", "rheostat",
    "homeostat", "hemostat", "haemostat", "peptide", "polypeptide", "nucleotide", "oligonucleotide", "dinucleotide",
    "neuropeptide", "propeptide", "dipeptide", "tripeptide", "glycopeptide", "lipopeptide", "riptide", "aptamer",
}
_INN_STOP_ENDINGS = ("peptide", "nucleotide")

_SPACED_CODE_PREFIXES = {"AMG", "MK", "LY", "JNJ", "BAY", "ABBV", "GSK", "PF", "RG", "RO", "SAR", "TAK", "BI", "DS", "BMS", "AZD", "INCB", "ARQ", "TNO"}
_DEV_CODE_RE = re.compile(r"\b([A-Z]{2,5})(-|\s)?(\d{3,6})\b")
_DEV_CODE_STOP_PREFIXES = {
    "NCT", "GSE", "GSM", "GPL", "GDS", "PMC", "PMID", "CD", "SLC", "HLA", "ISO", "COVID", "SARS", "IL", "ENSG",
    "ENST", "ENSP", "BIOMD", "HSA", "MMU", "KEGG", "DOID", "HP", "EFO", "MONDO", "CVCL", "DSM", "ICD", "ECOG",
    "RS", "SNP", "VCV", "RCV", "SCV", "EID", "AID", "CHEMBL", "HGNC", "GO", "UMLS", "MESH", "PDB", "OMIM", "CA",
    "MIR", "LINC", "ZNF", "KIAA", "OR", "TMEM", "CCDC", "FAM", "ANKRD", "CYP", "UGT", "ABC", "KRT",
}

_CONDITION_HEAD = (
    r"(?:cancers?|carcinomas?|[a-z\-]*(?:carcinoma|blastoma|sarcoma|glioma|lymphoma|melanoma|myeloma|mesothelioma)s?|"
    r"tumou?rs?|neoplasms?|malignanc(?:y|ies)|leuka?emias?|diseases?|syndromes?|diabetes|fibrosis|arthritis)"
)
_CONDITION_ABBREV_RE = re.compile(
    r"\b(NSCLC|SCLC|CRC|mCRC|AML|CLL|CML|HCC|RCC|TNBC|GBM|MDS|mCRPC|CRPC|HNSCC|ESCC|PDAC|NASH|MASH|COPD|IPF)\b"
)
_CONDITION_RE = re.compile(r"((?:[A-Za-z][A-Za-z'\-]*\s+){0,4})(" + _CONDITION_HEAD + r")\b", re.IGNORECASE)
_CONDITION_STOP = {
    "a", "an", "the", "in", "for", "of", "with", "on", "and", "or", "to", "at", "by", "from", "about", "against",
    "into", "vs", "versus", "what", "which", "who", "how", "are", "is", "were", "was", "do", "does", "there", "any",
    "exist", "exists", "existing", "trial", "trials", "clinical", "study", "studies", "patients", "patient",
    "people", "treating", "treat", "treatment", "therapy", "therapies", "mutant", "mutated", "mutation",
    "mutations", "positive", "negative", "driven", "amplified", "rearranged", "wild-type", "wildtype", "harboring",
    "harbouring", "bearing", "carrying", "inhibitor", "inhibitors", "drug", "drugs", "stable", "progressive",
    "residual", "minimal", "measurable", "my", "our", "this", "that", "these", "those", "recruiting", "ongoing",
    "completed", "phase", "status", "current", "latest", "new", "novel", "first-line", "second-line", "line",
}

_PHASE_TOKEN = r"(?:[1-4]|iv|i{1,3})"
_PHASE_RE = re.compile(rf"\b(early\s+)?phase\s*({_PHASE_TOKEN})(?:\s*(?:/|-|and|or|to)\s*(?:phase\s*)?({_PHASE_TOKEN}))?\b", re.IGNORECASE)
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}

_STATUS_PATTERNS = (
    (re.compile(r"\bnot\s+yet\s+recruiting\b", re.I), ["NOT_YET_RECRUITING"]),
    (re.compile(r"\bactive,?\s+not\s+recruiting\b", re.I), ["ACTIVE_NOT_RECRUITING"]),
    (re.compile(r"(?<!not\s)(?<!yet\s)\b(?:currently\s+)?recruiting\b", re.I), ["RECRUITING"]),
    (re.compile(r"\b(?:ongoing|open|active)\s+(?:clinical\s+)?(?:trials?|studies)\b", re.I),
     ["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"]),
    (re.compile(r"\bcompleted\s+(?:clinical\s+)?(?:trials?|studies)\b", re.I), ["COMPLETED"]),
    (re.compile(r"\b(?:terminated|stopped)\s+(?:clinical\s+)?(?:trials?|studies)\b", re.I), ["TERMINATED"]),
    (re.compile(r"\bwithdrawn\s+(?:clinical\s+)?(?:trials?|studies)\b", re.I), ["WITHDRAWN"]),
)


def has_clinical_intent(query: str) -> bool:
    return bool(_CLINICAL_INTENT_RE.search(query or ""))


def extract_nct_ids(query: str) -> List[str]:
    out: List[str] = []
    for m in _NCT_RE.findall(query or ""):
        nct = m.upper()
        if nct not in out:
            out.append(nct)
    return out[:20]


def extract_drugs(query: str) -> List[str]:
    """INN-stem drug names (lower-cased) and development codes (AZD9291, BMS-986507), in order of mention."""
    found: List[tuple] = []
    for m in _INN_RE.finditer(query or ""):
        word = m.group(1).lower()
        if word in _INN_STOPWORDS or word.endswith(_INN_STOP_ENDINGS):
            continue
        found.append((m.start(), word))
    for m in _DEV_CODE_RE.finditer(query or ""):
        prefix, sep, digits = m.group(1), m.group(2), m.group(3)
        if prefix in _DEV_CODE_STOP_PREFIXES:
            continue
        if sep and sep.isspace():
            # "AMG 510" is a code; "COHORT 2019" or "TABLE 123" is not.
            if prefix not in _SPACED_CODE_PREFIXES or re.fullmatch(r"(?:19|20)\d{2}", digits):
                continue
            sep = " "
        found.append((m.start(), f"{prefix}{sep or ''}{digits}"))
    out: List[str] = []
    for _, name in sorted(found):
        if name.lower() not in {o.lower() for o in out}:
            out.append(name)
    return out


def extract_condition(query: str) -> Optional[str]:
    """A condition phrase ending in a disease head noun ('non-small cell lung cancer'), or a disease abbreviation."""
    q = query or ""
    for m in _CONDITION_RE.finditer(q):
        words = m.group(1).split()
        kept: List[str] = []
        for w in reversed(words):
            wl = w.lower().strip("'")
            # Stop at function words, gene symbols and alterations ('EGFR', 'G12C-mutant').
            if wl in _CONDITION_STOP or re.search(r"\d", w) or (w.isupper() and len(w) > 1) or wl.endswith(("-mutant", "-mutated", "-positive", "-negative", "-driven")):
                break
            kept.insert(0, w)
        phrase = " ".join(kept + [m.group(2)])
        if phrase.lower() in {"disease", "diseases", "syndrome", "syndromes"}:
            continue
        return phrase
    m = _CONDITION_ABBREV_RE.search(q)
    return m.group(1) if m else None


_HEAD_FULL_RE = re.compile(_CONDITION_HEAD, re.IGNORECASE)
_HEAD_SCAN_RE = re.compile(r"\b(" + _CONDITION_HEAD + r")\b", re.IGNORECASE)
_TRAILING_WORDS_RE = re.compile(r"((?:[A-Za-z][A-Za-z'\-]*\s+){0,4})$")
_COORD_PREFIX_RE = re.compile(
    r"((?:[A-Za-z][A-Za-z'\-]*\s*,\s*){0,3}[A-Za-z][A-Za-z'\-]*)\s*,?\s+(?:and/or|and|or|&)\s+$", re.IGNORECASE
)
_NO_SINGULAR = ("sis", "tis", "betes", "ss")


def _singular_head(head: str) -> str:
    """'cancers' -> 'cancer', 'malignancies' -> 'malignancy'; 'fibrosis' and 'diabetes' are unchanged."""
    low = head.lower()
    if low.endswith("ies"):
        return head[:-3] + "y"
    if low.endswith("s") and not low.endswith(_NO_SINGULAR):
        return head[:-1]
    return head


def _modifier_ok(word: str) -> bool:
    wl = word.lower().strip("'")
    return bool(
        len(wl) >= 3
        and wl not in _CONDITION_STOP
        and not re.search(r"\d", word)
        and not (word.isupper() and len(word) > 1)
        and not _HEAD_FULL_RE.fullmatch(wl)
    )


def extract_conditions(query: str, limit: int = MAX_CONDITIONS) -> List[str]:
    """
    Every condition phrase in order of mention, at most `limit`. Coordinated modifiers sharing one head noun are
    expanded: 'ovarian and prostate cancers' -> ['ovarian cancer', 'prostate cancer'], 'breast, ovarian or
    pancreatic cancer' -> three conditions. A lone phrase is returned as extract_condition() gives it.
    """
    q = query or ""
    found: List[tuple] = []
    prev_end = 0
    # Head nouns are scanned one by one, so 'ovarian cancer and prostate cancer' yields both phrases (a single
    # regex match would swallow the first into the second's modifier window).
    for h in _HEAD_SCAN_RE.finditer(q):
        prefix = q[prev_end:h.start()]
        prev_end = h.end()
        window = _TRAILING_WORDS_RE.search(prefix)
        words = window.group(1).split() if window else []
        kept: List[str] = []
        for w in reversed(words):
            wl = w.lower().strip("'")
            if wl in _CONDITION_STOP or re.search(r"\d", w) or (w.isupper() and len(w) > 1) or wl.endswith(("-mutant", "-mutated", "-positive", "-negative", "-driven")):
                break
            kept.insert(0, w)
        head = h.group(1)
        phrase = " ".join(kept + [head])
        if phrase.lower() in {"disease", "diseases", "syndrome", "syndromes"}:
            continue
        start = h.start() - len(" ".join(kept)) - 1 if kept else h.start()
        # The text before a one-word modifier: 'ovarian and ' in 'ovarian and prostate cancers'.
        modifiers: List[str] = []
        if len(kept) == 1:
            coord = _COORD_PREFIX_RE.search(prefix[: max(0, len(prefix) - len(kept[0]) - 1)])
            if coord:
                modifiers = [w.strip() for w in coord.group(1).split(",")]
                modifiers = [w for w in modifiers if w and " " not in w and _modifier_ok(w)]
        if modifiers:
            single = _singular_head(head)
            for mod in modifiers:
                found.append((start, f"{mod} {single}"))
            found.append((start, f"{kept[0]} {single}"))
        else:
            found.append((start, phrase))
    for m in _CONDITION_ABBREV_RE.finditer(q):
        found.append((m.start(), m.group(1)))
    out: List[str] = []
    for _, phrase in sorted(found, key=lambda x: x[0]):
        if phrase.lower() not in {o.lower() for o in out}:
            out.append(phrase)
    return out[:limit]


_SETTING_RES = [(term, re.compile(pattern, re.IGNORECASE)) for term, (pattern, _alts, _core) in SETTING_TERMS.items()]
_STAGE_SEP = r"(?:/|,|-|\u2013|\band\b|\bor\b|\bto\b)"
_STAGE_RE = re.compile(
    rf"\bstage\s+((?:IV|I{{1,3}})[A-D]?(?:\s*{_STAGE_SEP}\s*(?:stage\s+)?(?:(?:IV|I{{1,3}})[A-D]?|[A-D])\b)*)",
    re.IGNORECASE,
)
_STAGE_PART_RE = re.compile(r"^(IV|I{1,3})?([A-D]?)$", re.IGNORECASE)


def extract_stages(query: str) -> List[str]:
    """'stage IIB/IIC' -> ['stage IIB', 'stage IIC']; 'stage IIB/C' -> the same; 'stage III' -> ['stage III']."""
    out: List[str] = []
    for m in _STAGE_RE.finditer(query or ""):
        base = ""
        for part in re.split(rf"\s*{_STAGE_SEP}\s*", m.group(1), flags=re.IGNORECASE):
            part = re.sub(r"(?i)^stage\s+", "", part.strip())
            pm = _STAGE_PART_RE.match(part) if part else None
            if not pm or not (pm.group(1) or pm.group(2)):
                continue
            numeral = (pm.group(1) or base).upper()
            if not numeral:
                continue
            base = numeral
            term = f"stage {numeral}{pm.group(2).upper()}"
            if term not in out:
                out.append(term)
    return out


def extract_setting_terms(query: str) -> List[str]:
    """Clinical-setting terms in the question (adjuvant, resected, first-line, metastatic, stage IIB, randomized)."""
    q = query or ""
    found = [(m.start(), term) for term, rx in _SETTING_RES for m in [rx.search(q)] if m]
    out = [term for _, term in sorted(found)]
    return out + [s for s in extract_stages(q) if s not in out]


def extract_phases(query: str) -> List[str]:
    out: List[str] = []
    for m in _PHASE_RE.finditer(query or ""):
        early, first, second = m.group(1), m.group(2).lower(), (m.group(3) or "").lower()
        for i, tok in enumerate(t for t in (first, second) if t):
            num = _ROMAN.get(tok, tok)
            name = "EARLY_PHASE1" if early and i == 0 and num == "1" else f"PHASE{num}"
            if name not in out:
                out.append(name)
    return out


def extract_statuses(query: str) -> List[str]:
    out: List[str] = []
    for pattern, statuses in _STATUS_PATTERNS:
        if pattern.search(query or ""):
            out.extend(s for s in statuses if s not in out)
    return out


@dataclass
class TrialQueryPlan:
    label: str
    term: Optional[str] = None
    intervention: Optional[str] = None
    condition: Optional[str] = None
    conditions: List[str] = field(default_factory=list)  # every condition searched (one search each) when > 1
    setting_terms: List[str] = field(default_factory=list)
    nct_ids: List[str] = field(default_factory=list)
    phases: List[str] = field(default_factory=list)
    statuses: List[str] = field(default_factory=list)
    drugs: List[str] = field(default_factory=list)
    strategies: List[Dict[str, Any]] = field(default_factory=list)
    entity_source: str = "query_text"  # query_text | query_context
    trial_names: List[str] = field(default_factory=list)  # trials the question names ("SOLO-1"): listed/fetched first

    def to_tool_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        for key in ("term", "intervention", "condition"):
            value = getattr(self, key)
            if value:
                kwargs[key] = value
        for key in ("nct_ids", "phases", "statuses", "setting_terms", "trial_names"):
            value = getattr(self, key)
            if value:
                kwargs[key] = list(value)
        extra = [c for c in self.conditions if c != self.condition]
        if extra:
            kwargs["conditions"] = extra
        return kwargs


def _ctx_labels(ctx: Any, attr: str) -> List[str]:
    try:
        entities = list(getattr(ctx, attr) or [])
    except Exception:  # noqa: BLE001
        logger.debug("Could not read %s of the query context", attr, exc_info=True)
        return []
    out: List[str] = []
    for e in entities:
        label = str(getattr(e, "label", "") or getattr(e, "mention", "") or "").strip()
        if label and label.lower() not in {o.lower() for o in out}:
            out.append(label)
    return out


# "KRAS-mutant pancreatic cancer": the gene is a search term, not part of the condition.
_MUTANT_GENE_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{1,9})(?:[\s\-]+[A-Z]\d{1,4}[A-Z]?)?[\s\-]+(?:mutant|mutated|mutations?|driven|altered|positive|"
    r"amplified|rearranged)\b"
)


def _ctx_disease_conditions(ctx: Any) -> List[str]:
    """Registry conditions for the resolved diseases, in the user's words. The resolver's ontology label can be a
    narrow sibling of what was asked ('pancreatic cancer' -> 'exocrine pancreatic carcinoma' finds 1 KRAS trial
    instead of ~190), and ClinicalTrials.gov expands the plain wording through its own synonyms. The mention is
    used when it reads as a condition (a disease head noun or a known abbreviation; gene and alteration words
    such as 'KRAS-mutant' are stripped); otherwise the label."""
    try:
        entities = list(getattr(ctx, "diseases") or [])
    except Exception:  # noqa: BLE001
        logger.debug("Could not read the diseases of the query context", exc_info=True)
        return []
    out: List[str] = []
    for e in entities:
        mention = str(getattr(e, "mention", "") or "").strip()
        label = str(getattr(e, "label", "") or "").strip()
        from_mention = extract_condition(mention) if mention else None
        # An abbreviation-only mention ('PDAC') reads better as the label ('pancreatic ductal adenocarcinoma').
        if from_mention and _HEAD_SCAN_RE.search(from_mention):
            words = from_mention.split()
            condition = " ".join([*words[:-1], _singular_head(words[-1])])
        else:
            condition = label or mention
        if condition and condition.lower() not in {o.lower() for o in out}:
            out.append(condition)
    return out


def _mutant_gene(text: str) -> Optional[str]:
    m = _MUTANT_GENE_RE.search(text or "")
    return m.group(1) if m else None


def plan_trial_query(query: str, primary_target: Optional[str] = None, ctx: Any = None) -> Optional[TrialQueryPlan]:
    """A search plan when the question has clinical intent, names an NCT ID, or names a drug; otherwise None.
    Also None when nothing searchable (drug, target or NCT ID) was identified."""
    q = query or ""
    nct_ids = extract_nct_ids(q)
    ctx_drugs = _ctx_labels(ctx, "drugs") if ctx is not None else []
    drugs = ctx_drugs or extract_drugs(q)
    intent = has_clinical_intent(q) or bool(ctx is not None and {"clinical", "clinical_trials"} & set(getattr(ctx, "intents", set()) or set()))
    if not (intent or nct_ids or drugs):
        return None

    if nct_ids:
        shown = ", ".join(nct_ids[:3]) + (f" +{len(nct_ids) - 3} more" if len(nct_ids) > 3 else "")
        plan = TrialQueryPlan(label=shown, nct_ids=nct_ids, entity_source="query_text")
    else:
        target = (primary_target or "").strip() or None
        if target is None and ctx is not None:
            target = getattr(ctx, "primary_display", None) or None
        ctx_diseases = _ctx_disease_conditions(ctx) if ctx is not None else []
        if target is None:
            # "KRAS-mutant pancreatic cancer" with no resolved gene: KRAS is the term, the cancer the condition.
            mentions = [str(getattr(e, "mention", "") or "") for e in (getattr(ctx, "diseases", None) or [])] if ctx is not None else []
            target = next((g for g in (_mutant_gene(t) for t in [*mentions, q]) if g), None)
        if not drugs and not target:
            return None
        # A generic 'cancer' next to 'ovarian cancer' adds noise, not a second condition.
        from ..agents.query_planner import drop_generic_disease_names

        text_conditions = drop_generic_disease_names(extract_conditions(q))
        # Several diseases (resolved, or a coordinated phrase such as 'ovarian and prostate cancers') are each
        # searched; a single one keeps the resolver's label when there is one.
        if len(ctx_diseases) > 1:
            conditions = ctx_diseases[:MAX_CONDITIONS]
        elif len(text_conditions) > 1:
            conditions = text_conditions
        else:
            single = ctx_diseases[0] if ctx_diseases else (text_conditions[0] if text_conditions else extract_condition(q))
            conditions = [single] if single else []
        condition = conditions[0] if conditions else None
        intervention = " OR ".join(drugs[:3]) if drugs else None
        anchor = "/".join(drugs[:3]) if drugs else target
        plan = TrialQueryPlan(
            label=" + ".join(x for x in (anchor, ", ".join(conditions)) if x),
            term=target,
            intervention=intervention,
            condition=condition,
            conditions=conditions if len(conditions) > 1 else [],
            setting_terms=extract_setting_terms(q),
            phases=extract_phases(q),
            statuses=extract_statuses(q),
            drugs=drugs[:3],
            entity_source="query_context" if (ctx_drugs or ctx_diseases) else "query_text",
        )
    plan.strategies = [
        {"name": name, "params": params}
        for name, params in build_strategies(plan.term, plan.intervention, plan.condition, plan.nct_ids)
    ]
    if not plan.nct_ids:
        plan.trial_names = named_trials(q, ctx)
    return plan


def named_trials(query: str, ctx: Any = None) -> List[str]:
    """Trial names the question mentions ("SOLO-1", "KEYNOTE-716"), as the literature search finds them; [] when the
    extractor is unavailable."""
    try:
        from ..agents.authoritative_orchestrator import question_trial_names
        from ..agents.search_plan import clean_trial_names

        text = (getattr(ctx, "raw_query", None) or query) if ctx is not None else query
        names = clean_trial_names(question_trial_names(ctx, text or ""))
        if ctx is None:
            # Without resolved genes, 'BRCA2' looks like a trial name; keep only separated names ('SOLO-1', 'SOLO 1').
            names = [n for n in names if "-" in n or " " in n]
        return names[:6]
    except Exception:  # noqa: BLE001 - prioritising named trials is an optimisation, never a failure
        logger.debug("Trial-name extraction failed", exc_info=True)
        return []
