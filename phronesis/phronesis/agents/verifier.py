"""
Clinical Verifier & Reflexion Self-Correction Engine.
Implements Reflexion (Shinn et al., 2023) architecture with strict evidence grounding:
- Deterministic checks for canonical identifiers against actually observed tool outputs
  (BioModels, DOIs, PMIDs, Ensembl, UniProt), including identifiers verified in prior turns
- Elimination of static whitelists: cited models, DOIs, PMIDs, and targets must exist in observed evidence
- Strict numerical consistency checking with tight tolerance (5% relative error) against simulator outputs,
  with exact species-name boundaries, signed values, and concentration-unit conversion
- Sentence-level claim-to-passage grounding for clinical assertions (trial identifiers, metrics, entities)
- Distinction between transient time-course values and validated steady-state equilibria
- Structured defect reporting triggering bounded LLM correction with verifiable provenance
"""

from __future__ import annotations

import html
import json
import logging
import re
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple
from pydantic import BaseModel, Field

from ...extensions.hooks import (
    VerificationContext,
    apply_serialization_filters,
    get_evidence_doc_providers,
    get_identifier_vetoes,
    get_verification_checks,
)
from ...extensions.id_families import FAMILIES, extract_family_ids, normalize_ids
from ...tools.registry import ToolResult
# sentence_spans lives in utils.text and is re-exported here (citations, the orchestrator and tests import it).
from ...utils.source_authority import URL_FIELDS, is_fda_url, is_regulatory_url, record_url
from ...utils.text import TYPOGRAPHY_FOLD, _TABLE_LINE_RE, find_folded, fold_typography, sentence_spans  # noqa: F401

logger = logging.getLogger(__name__)

_LEGACY_ID_KEYS = ("biomodels", "pmids", "dois", "ensembl", "uniprot")


def _pmid_in_passage(pmid: str, passage: str) -> bool:
    """A PMID named as such in passage text ('PMID: 123', 'pubmed/123'); a bare number such as a year is not one."""
    return bool(re.search(rf"(?:pmid|pubmed(?:\.ncbi\.nlm\.nih\.gov)?)\W{{0,4}}{re.escape(pmid)}(?!\d)", passage))


def _doi_in_passage(doi: str, passage: str) -> bool:
    """The whole DOI in passage text, not only the start of a longer DOI."""
    return bool(re.search(rf"(?<![\w./]){re.escape(doi.lower())}(?![\w/]|\.\w)", passage))


class DefectSeverity(str, Enum):
    CRITICAL = "CRITICAL"  # Blatant contradiction, unobserved citation, unsupported clinical claim, material numerical mismatch
    MAJOR = "MAJOR"        # Modeling distinction violation (time-course vs steady-state), missing passage citation
    MINOR = "MINOR"        # Vague phrasing, ungrounded minor detail


class DefectItem(BaseModel):
    category: str  # IDENTIFIER | NUMERICAL | CITATION | CLINICAL_CLAIM | COMPLETION | INTEGRITY | MODELING_DISTINCTION
    severity: DefectSeverity
    description: str
    suggested_correction: str
    claim: Optional[str] = None
    # The answer sentence (or table row) exactly as written, for claim-audit defects; lets a caller remove it.
    sentence: Optional[str] = None


class ClaimEvidence(BaseModel):
    source_key: str
    passage: str = ""  # excerpt the judgment rests on, for reviewers
    passage_id: Optional[str] = None


class ClaimRecord(BaseModel):
    """One audited answer claim. status: supported | contradicted | unsupported | not_checked."""

    claim_id: str
    claim_type: str  # clinical | mechanistic | computational | absence | operational (a tool or lookup outcome)
    claim_text: str
    evidence: List[ClaimEvidence] = Field(default_factory=list)
    status: str
    reason: str = ""
    # For a claim that cites PMID/DOI sources whose retrieved text was checked: False when the support comes from a
    # different document than the one cited (the cited source did not back it). None when nothing was cited or the
    # cited text was not retrieved.
    cited_source_supported: Optional[bool] = None


class VerificationReport(BaseModel):
    passed: bool
    verification_score: float  # 0.0 to 1.0; not a probability of correctness
    defects: List[DefectItem] = Field(default_factory=list)
    verified_identifiers: List[str] = Field(default_factory=list)
    numerical_checks_passed: int = 0
    numerical_checks_failed: int = 0
    failure_lesson: Optional[str] = None
    # Clinical sentences the claim audit grounded: {"sentence", "evidence_key", "passage_id"}
    claim_support: List[Dict[str, Any]] = Field(default_factory=list)
    # Identifiers excluded by registered vetoes (e.g. retracted articles), as "PMID:<id>", "DOI:<doi>", "<LABEL>:<id>"
    vetoed_identifiers: List[str] = Field(default_factory=list)
    # Claim-level audit: every material claim with its evidence and status
    claims: List[ClaimRecord] = Field(default_factory=list)
    contradicted_claims: int = 0
    unsupported_claims: int = 0
    unchecked_claims: int = 0
    # passed, and every material claim was checked and supported; only then may an answer be labeled verified
    fully_verified: bool = False

    def to_feedback_prompt(self) -> str:
        """Generate structured Reflexion feedback prompt to guide bounded LLM correction."""
        lines = [
            "REFLEXION VERIFICATION AUDIT FAILED:",
            f"Overall Score: {self.verification_score:.2f} / 1.00 (Status: UNVERIFIED - DEFECTS PRESENT)",
            "Identified Specific Defects to Fix:",
        ]
        for idx, d in enumerate(self.defects, 1):
            lines.append(f"{idx}. [{d.severity.value}] {d.category}: {d.description}")
            lines.append(f"   Required Fix: {d.suggested_correction}")
        if self.failure_lesson:
            lines.append(f"Historical Failure Lesson: {self.failure_lesson}")
        return "\n".join(lines)


# Molar concentration prefixes relative to 1 mol/L.
_MOLAR_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0}

# Negation and limitation markers. Registered feature checks receive this as VerificationContext.hedge_pattern
# and the steady-state wording check uses it; the built-in claim audit does NOT skip sentences that merely
# contain a negation (see _UNCERTAINTY_PATTERN).
_HEDGE_PATTERN = re.compile(
    r"\b(?:no|not|none|never|without|lack(?:s|ing)?|absent|absence|unavailable|insufficient|unverified|"
    r"unobserved|unknown|cannot|could\s+not|did\s+not|were\s+not|was\s+not|is\s+not|are\s+not|"
    r"hypothetical|fabricat\w*|unsupported)\b|n't\b",
    re.IGNORECASE,
)
# A proposed experiment ("Run a parameter sweep and record the steady-state ...") is not a claim about results.
_SUGGESTION_PATTERN = re.compile(
    r"^[\s\-*•\d.)]*(?:(?:for (?:each|every)|then|next|first|finally|subsequently)\b[^.]{0,80}?\b)?"
    r"(?:run|perform|compute|record|measure|simulate|consider|test|validate|assess|repeat|extend|"
    r"compare|quantify|use|apply)\b|\b(?:would|could|should|suggest\w*|recommend\w*|next experiments?|in future)\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "were", "was", "from", "have", "has", "had", "been", "into",
    "their", "which", "these", "those", "than", "then", "also", "such", "among", "between", "after", "before",
    "during", "within", "across", "over", "under", "about", "when", "where", "while", "both", "more", "most",
    "patients", "patient", "trial", "trials", "phase", "clinical", "study", "studies", "response", "responses",
    "rate", "rates", "overall", "survival", "progression", "free", "objective", "median", "confirmed",
    "demonstrated", "showed", "shown", "reported", "results", "result", "outcome", "outcomes", "efficacy",
    "significant", "statistically", "treatment", "therapy", "data", "evidence", "approved",
    "cancer", "cancers", "tumor", "tumors", "tumour", "tumours", "inhibitor", "inhibitors", "inhibition",
    "cell", "cells", "mutant", "mutation", "mutations", "drug", "drugs", "dose", "months", "weeks", "years",
}

_CLINICAL_CLAIM_PATTERNS = [
    r"phase\s*(?:[123]|i{1,3}|iv)[ab]?\s*(?:clinical\s*)?(?:trial|study)",
    r"clinical\s*trial\s*(?:readouts?|results?|confirmed|outcomes?|progress)",
    r"(?:overall\s*survival|progression[\s\-]free\s*survival|curative\s*efficacy|objective\s*response\s*rate|\borr\b|\bpfs\b|\bos\b\s*(?:of|was|=))",
    r"positive\s*clinical\s*trial\s*progress",
    r"confirmed\s*orr",
    r"cross-domain\s*analysis\s*confirms\s*strong\s*target\s*validation",
    r"fda[\s\-]*approv\w*",
    r"accelerated\s+approval",
    r"\b(?:nda|bla)\s*\d{5,6}\b|drugs@fda",
    r"statistically\s*significant",
    r"\bnct\d{8}\b",
]

# Claims about regulatory status (approval, label, indication) need a regulatory record: an openFDA / Drugs@FDA tool
# record or an fda.gov page. A simulation, a target-association score or a trial paper never certifies an approval.
_REGULATORY_CLAIM_RE = re.compile(
    r"\bfda[\s\-]*(?:approv\w*|label\w*|grant\w*|authori[sz]\w*|clear\w*)|\baccelerated\s+approval|"
    r"\b(?:nda|bla)\s*\d{5,6}\b|drugs@fda|\bfda\s+label|\b(?:approved|indicated)\s+(?:by\s+the\s+(?:us\s+)?fda\s+)?for\b|"
    r"\b(?:regulatory|marketing)\s+approval|\blabel(?:ed|led)?\s+indication",
    re.IGNORECASE,
)
# Tool sources that hold regulatory records (matched against the evidence key, e.g. "tool:openfda",
# "tool:agent.plan.3.openfda"); a web result counts only when its record carries an fda.gov URL.
_REGULATORY_SOURCE_KEYS = ("openfda", "drug_safety", "drugsfda", "drugs_fda", "fda_label")
_WEB_SOURCE_KEYS = ("websurfer", "web_search", "web")
# Clinical outcome claims (efficacy, survival, response, trial results) cannot rest on a simulation or on a
# target/pathway association score: those sources say nothing about patients.
_CLINICAL_OUTCOME_RE = re.compile(
    r"\b(?:orr|pfs|os|dcr|hr)\b|survival|response\s+rate|responders?|hazard\s+ratio|phase\s*(?:[123]|i{1,3}|iv)|"
    r"\btrial\b|randomi[sz]ed|patients|cohort|efficacy|adverse\s+events?|toxicit\w*|approv\w*",
    re.IGNORECASE,
)
_NON_CLINICAL_SOURCE_KEYS = (
    "vivarix", "simulat", "synapta", "opentargets", "kegg", "reactome", "string", "primekg", "gtex", "gwas",
    "depmap", "lumicell", "structure", "alphafold", "calibrat",
)
# "monotherapy" / "single-agent" said of a regimen the same clause names as a combination.
_MONOTHERAPY_RE = re.compile(r"\bmono[\s\-]?therap\w*|\bsingle[\s\-]agent\b", re.IGNORECASE)
_COMBINATION_JOIN_RE = re.compile(r"\s*(?:\+|plus\b|with\b|and\b|combined\s+with\b|in\s+combination\s+with\b|/)\s*", re.IGNORECASE)


def is_regulatory_claim(text: str) -> bool:
    return bool(_REGULATORY_CLAIM_RE.search(text or ""))


def _key_has(key: str, names: Tuple[str, ...]) -> bool:
    k = (key or "").lower()
    return k.startswith("tool:") and any(n in k for n in names)


_FDA_CLAIM_RE = re.compile(r"\bfda\b|drugs@fda|\b(?:nda|bla)\s*\d{5,6}\b", re.IGNORECASE)


def is_regulatory_doc(doc: "_Doc", claim: str = "") -> bool:
    """A regulatory record: an openFDA/Drugs@FDA tool record, or a web page whose own recorded URL (final URL after
    redirects) is on a regulator's host; an FDA claim (or no claim given) needs fda.gov or one of its subdomains. A URL
    quoted in a page's text is a citation, not the page's origin, and origin alone never establishes that the record
    supports a claim: the claim is still checked against the record's text."""
    if _key_has(doc.key, _REGULATORY_SOURCE_KEYS):
        return True
    if not _key_has(doc.key, _WEB_SOURCE_KEYS) or not doc.url:
        return False
    if not claim or _FDA_CLAIM_RE.search(claim):
        return is_fda_url(doc.url)
    return is_regulatory_url(doc.url)


def is_non_clinical_doc(doc: "_Doc") -> bool:
    return _key_has(doc.key, _NON_CLINICAL_SOURCE_KEYS)


_REGIMEN_BREAK_RE = re.compile(r"\b(?:vs\.?|versus|compared|than|or|while|whereas|but|after|before)\b|[,;:]", re.IGNORECASE)


# Sample sizes bound to what they count: "sotorasib (N=102)", "the KRAS G12C subgroup (n=16)", "16 had KRAS G12C",
# "45 patients with KRAS-mutant NSCLC". A size is bound to the words just before "n=", or to the words around a count
# of patients; the binding is what a subgroup/arm denominator check compares, not the bare number.
_N_EQ_RE = re.compile(r"\bn\s*=\s*(\d+)(?:\s*/\s*(\d+))?\b", re.IGNORECASE)
_N_COUNT_RE = re.compile(
    r"(?<![\w.])(\d+)\s+((?:[a-z0-9][\w\-+/]*\s+){0,3}?)(?:patients?|participants?|subjects?|cases)\b"
    r"(?:\s+(?:with|harbou?ring|carrying|who\s+had)\s+((?:[a-z0-9][\w\-+/]*\s*){1,4}))?",
    re.IGNORECASE,
)
_N_HAD_RE = re.compile(r"(?<![\w.])(\d+)\s+(?:had|harbou?red|carried)\s+((?:[a-z0-9][\w\-+/]*\s*){1,3})", re.IGNORECASE)
_BINDING_STOP_RE = re.compile(r"[.;:()\[\]]|\bn\s*=", re.IGNORECASE)
_PROTEIN_CHANGE_RE = re.compile(r"^[a-z]\d{1,4}[a-z]$")
# A judge reason that names a concrete scope error (which subgroup, arm, denominator, endpoint or time point a figure
# belongs to), as opposed to an inconclusive "the excerpt does not clearly say".
_RELATION_SCOPE_RE = re.compile(
    r"\b(?:denominator|sub-?groups?|subsets?|arms?|cohorts?|populations?|endpoints?|time\s*points?|units?|"
    r"treatment\s+lines?|denominators?)\b", re.IGNORECASE)
_RELATION_ERROR_RE = re.compile(
    r"\b(?:mismatch\w*|wrong|incorrect\w*|instead|rather\s+than|refers?\s+to|belongs?\s+to|actually|swapp?\w*|"
    r"mislabel\w*|misattribut\w*|whole|entire|parent|not\s+the)\b", re.IGNORECASE)


def _binding_label(text: str, start: int, end: int) -> str:
    """Up to four words before position `start`, cut at the last clause or bracket boundary."""
    window = text[max(0, start - 60):start].rstrip(" ,")
    if window.endswith(("(", "[")):
        window = window[:-1]  # "subgroup (n=16)": the bracket opens the size, it does not end the label
    cuts = list(_BINDING_STOP_RE.finditer(window))
    if cuts:
        window = window[cuts[-1].end():]
    return " ".join(window.split()[-4:])


def sample_size_bindings(text: str) -> List[Tuple[int, Set[str]]]:
    """(count, words naming what it counts) for each sample size in `text` (lowercased)."""
    low = text.lower()
    out: List[Tuple[int, Set[str]]] = []
    for m in _N_EQ_RE.finditer(low):
        words = set(re.findall(r"[a-z0-9][a-z0-9\-+]*", _binding_label(low, m.start(), m.end())))
        out.append((int(m.group(1)), words))
        if m.group(2):
            out.append((int(m.group(2)), set()))  # "N=102/164": the second arm is not named here
    for m in _N_COUNT_RE.finditer(low):
        label = " ".join(x for x in (m.group(2), m.group(3)) if x)
        out.append((int(m.group(1)), set(re.findall(r"[a-z0-9][a-z0-9\-+]*", label))))
    for m in _N_HAD_RE.finditer(low):
        out.append((int(m.group(1)), set(re.findall(r"[a-z0-9][a-z0-9\-+]*", m.group(2)))))
    return out


def _distinctive(words: Set[str], drugs: Set[str]) -> Set[str]:
    """The words of a binding label that single out a subgroup or arm: a protein change (G12C) or a drug name."""
    return {w for w in words if _PROTEIN_CHANGE_RE.match(w) or w in drugs}


def denominator_mismatch(claim: str, doc_text: str) -> Optional[str]:
    """'n=45 counts <another group> in the source; KRAS G12C is n=16' when the claim gives a subgroup or arm a sample
    size that the source binds to a different group, while the source gives that subgroup or arm its own size."""
    from ...drugintel.names import extract_drug_mentions

    claim_bindings = [(n, w) for n, w in sample_size_bindings(claim) if w]
    if not claim_bindings:
        return None
    drugs = {d.lower() for d in extract_drug_mentions(claim + " " + doc_text[:4000])}
    doc_bindings = sample_size_bindings(doc_text)
    for n, words in claim_bindings:
        key = _distinctive(words, drugs)
        if not key:
            continue
        own = [(dn, dw) for dn, dw in doc_bindings if key & dw]
        if not own or any(dn == n for dn, _ in own):
            continue
        elsewhere = [(dn, dw) for dn, dw in doc_bindings if dn == n and not key & dw]
        if not elsewhere:
            continue
        group = " ".join(sorted(key)).upper() if all(_PROTEIN_CHANGE_RE.match(k) for k in key) else " and ".join(sorted(key))
        other = " ".join(w for w in sorted(elsewhere[0][1]) if w not in _STOPWORDS) or "another group"
        sizes = ", ".join(str(dn) for dn in dict.fromkeys(dn for dn, _ in own))
        return f"n={n} counts {other} in the source, while {group} is n={sizes}"
    return None


def judge_relation_error(verdict: Dict[str, Any]) -> Optional[str]:
    """The judge's concrete scope error (wrong subgroup, arm, denominator, endpoint or time point), or None for an
    inconclusive verdict. A structured ``relation_error`` field is taken as is."""
    explicit = verdict.get("relation_error") or verdict.get("error_type")
    reason = str(verdict.get("reason") or "")
    if explicit:
        return f"{explicit}: {reason}" if reason else str(explicit)
    if _RELATION_SCOPE_RE.search(reason) and _RELATION_ERROR_RE.search(reason):
        return reason
    return None


# A sentence reporting what a tool or lookup did in this run ("the search for PMID X failed", "the correction text was
# not retrieved"): a fact about the run, grounded in its tool outcomes, never in a scientific citation.
_OPERATIONAL_RE = re.compile(
    r"\b(?:search(?:es)?|look[\s\-]?ups?|retrieval|quer(?:y|ies)|fetch(?:es)?|requests?|downloads?|tool\s+calls?|api\s+calls?|"
    r"database\s+calls?)\b[^.;]{0,90}?\b(?:fail(?:ed|s|ure)?|timed?\s*out|errored|returned\s+(?:no|nothing|zero|an?\s+error)|"
    r"did\s+not\s+return|found\s+no(?:thing)?|was\s+unsuccessful|were\s+unsuccessful|w(?:as|ere)\s+(?:not\s+)?skipped)\b|"
    r"\b(?:could|can)\s*not\s+be\s+(?:retrieved|fetched|downloaded|accessed|looked\s+up)\b|"
    r"\b(?:was|were|is|are)\s+not\s+(?:retrieved|fetched|downloaded|accessible)\b|"
    # Correction handling is a run outcome too: "the notice was retrieved ... so corrected content was not applied".
    r"\bcorrect(?:ed|ion)\s+(?:content|text|values?|results?)\s+(?:was|were|is|are)\s+(?:not\s+)?applied\b|"
    r"\bno\s+corrected\s+(?:content|text|values?|results?)\s+(?:was|were)\s+applied\b|"
    # Record linkage the exact lookup reports: "a correction (erratum) exists for PMID X (linked erratum PMID Y)".
    r"\b(?:corrections?|errat(?:um|a)|corrigend(?:um|a))\b[^.;]{0,60}?\b(?:exists?|is\s+linked|was\s+linked|linked)\b",
    re.IGNORECASE,
)


# A line that only locates evidence: "Source: abstract of PMID 39369609; URL: ...", "*Sources:* ...".
_SOURCE_LOCATION_RE = re.compile(
    r"^\s*[*_]{0,2}(?:sources?|supporting\s+source(?:\s+location)?s?|source\s+location|references?)[*_]{0,2}\s*:"
    # "*All figures are taken verbatim from the abstract of PMID X.*": where the numbers come from, no new fact.
    r"|^\s*[*_]{0,2}(?:all\s+)?(?:figures|values|numbers|results|data)\s+(?:are|were)\s+(?:taken|extracted|quoted|"
    r"transcribed)\s+(?:verbatim\s+|directly\s+)?from\b"
    # "**Article:** https://pubmed.ncbi.nlm.nih.gov/39369609/ ...", "Erratum notice: PMID 39984314 (...)": a label
    # pointing at a record.
    r"|^\s*[*_]{0,2}[A-Za-z][\w /\-]{0,30}[*_]{0,2}\s*:[*_]{0,2}\s*(?:<?https?://|PMID:?\s*\d|DOI:?\s*10\.|NCT\d{8})",
    re.IGNORECASE,
)


def is_operational_claim(text: str) -> bool:
    return bool(_OPERATIONAL_RE.search(text or ""))


def regimen_mismatch(text: str) -> Optional[str]:
    """'<A> + <B> is a combination, not monotherapy' when the text calls a regimen monotherapy (or single-agent) and,
    next to that word with no comparator between, names two drugs joined as a combination ("ifebemtinib plus
    garsorasib monotherapy"). "Sotorasib monotherapy versus sotorasib plus panitumumab" compares two regimens and
    passes."""
    from ...drugintel.names import extract_drug_mentions

    low = (text or "").lower()
    names = [n for n in extract_drug_mentions(text or "") if not n.upper().startswith("CHEMBL")]
    spots = sorted((m.start(), m.end(), n) for n in names for m in re.finditer(rf"\b{re.escape(n)}\b", low))
    pairs = [
        (a, b) for a, b in zip(spots, spots[1:])
        if a[2] != b[2] and _COMBINATION_JOIN_RE.fullmatch(low[a[1]:b[0]])
    ]
    for mono in _MONOTHERAPY_RE.finditer(low):
        for a, b in pairs:
            gap = low[b[1]:mono.start()] if mono.start() >= b[1] else low[mono.end():a[0]] if mono.end() <= a[0] else None
            if gap is not None and len(gap) <= 40 and not _REGIMEN_BREAK_RE.search(gap):
                return f"{a[2]} + {b[2]} is a combination regimen, not monotherapy"
    return None


# Header words of a simulation output table ("| Species | Initial (nmol) | Final (nmol) |"). A table without them
# (a drug approval table "| Target | Year |") holds no concentrations, so its bare numbers are not species values.
_SIM_TABLE_HEADER_RE = re.compile(
    r"species|concentration|initial|final|steady|amount|level|simulat|time|model|\b[pnuµμm]?mol\b|\b[pnuµμm]m\b",
    re.IGNORECASE,
)


def in_non_simulation_table(text: str, pos: int) -> bool:
    """The position sits in a markdown table row whose table header names no simulation quantity."""
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    line = text[ls: le if le >= 0 else len(text)]
    if not line.lstrip().startswith("|"):
        return False
    header, start = line, ls
    while start > 0:
        prev_start = text.rfind("\n", 0, start - 1) + 1
        prev = text[prev_start: start - 1]
        if not prev.lstrip().startswith("|"):
            break
        header, start = prev, prev_start
    return not _SIM_TABLE_HEADER_RE.search(header)


def article_unions(docs: List["_Doc"]) -> List["_Doc"]:
    """One extra document per article (PMID/DOI key) with several retrieved passages: their joined text. A paper's
    figures are often spread over its abstract and a results chunk; each passage alone lacks some of them."""
    by_key: Dict[str, List["_Doc"]] = {}
    for d in docs:
        if d.key.startswith(("PMID:", "DOI:")):
            by_key.setdefault(d.key, []).append(d)
    out: List["_Doc"] = []
    for key, parts in by_key.items():
        texts = list(dict.fromkeys(p.text for p in parts))
        if len(texts) > 1:
            out.append(_Doc(key, "\n".join(texts), None, "article"))
    return out

# Explicit uncertainty statements report a limit of this run; they are not checked as findings. A sentence that
# merely contains "no"/"not" ("ORR of 99% with no new safety signals") is still a factual claim.
_UNCERTAINTY_PATTERN = re.compile(
    r"\b(?:unverified|uncertain|unclear|hypothetical|speculative|remains?\s+unknown|is\s+unknown|"
    r"(?:could|can)\s*not\s+(?:be\s+)?(?:verif|confirm|determin|establish|retriev|find|locat|identif|assess)\w*|"
    r"unable\s+to\s+(?:verif|confirm|determin|establish|retriev|find|locat|identif|assess)\w*|"
    r"insufficient\s+(?:evidence|data)|"
    r"evidence\s+(?:is|was|remains)\s+(?:unavailable|insufficient|limited|lacking)|"
    r"(?:not|never)\s+(?:been\s+)?(?:retrieved|returned|located)\b|"
    r"(?:was|were|is|are|been)\s+(?:not\s+)?(?:found|identified)\s+in\s+(?:this|the\s+current|the\s+present)\s+(?:run|search|investigation|evidence)|"
    r"no\s+(?:matching|supporting|relevant|retrieved|posted)\s+(?:records?|sources?|passages?|evidence|data|publications?|papers?|trials?|results?)|"
    r"no\b[^.;]{0,80}?\b(?:was|were|is|are|could\s+be)\s+(?:retrieved|returned|located|available|found|identified)\b|"
    r"not\s+(?:been\s+)?found\b(?!\s+to\b)|"
    r"premise\s+(?:is|remains)\s+(?:unverified|unsupported)|"
    r"(?:do|did|will)\s+not\s+(?:invent|fabricate|speculate)|"
    r"not\s+(?:run|performed|executed|computed|simulated)\b|was\s+not\s+(?:run|performed|executed))",
    re.IGNORECASE,
)

# Statements that a source, or the literature, lacks something ("the cited records do not mention mitapivat").
_ABSENCE_PATTERN = re.compile(
    r"\b(?:does|do|did)\s+not\s+(?P<v1>mention|discuss|describe|report|address|identify|explain|investigate|examine|"
    r"characteri[sz]e|provide|include|reference|name|elucidate|propose|show)\b|"
    r"\b(?:doesn't|don't|didn't)\s+(?P<v2>mention|discuss|describe|report|address|identify|explain|investigate|examine|"
    r"provide|include|reference|name|elucidate|propose|show)\b|"
    r"\b(?:is|are|was|were)\s+(?:not|never)\s+(?P<v3>mentioned|discussed|described|reported|addressed|identified|"
    r"explained|investigated|examined|characteri[sz]ed|referenced|named|elucidated)\b|"
    r"\bwithout\s+(?P<v4>explaining|describing|identifying|reporting|mentioning|addressing|elucidating|investigating|"
    r"proposing|characteri[sz]ing)\b|"
    r"\b(?:makes?|made)\s+no\s+(?P<v5>mention)\b|\bno\s+(?P<v6>mention)\s+(?:of|is\s+made\s+of)\b|"
    r"\b(?:there\s+(?:is|are)\s+no|no\s+(?:published\s+)?(?:studies|reports?|literature|publications?|data)\s+"
    r"(?:have|has|exist|exists|report|reports|describe|describes|show|shows))(?P<v7>)",
    re.IGNORECASE,
)
_MENTION_VERBS = re.compile(r"^(?:mention\w*|discuss\w*|referenc\w*|nam\w*|includ\w*)$", re.IGNORECASE)
_MECHANISM_OBJECT = re.compile(r"\b(?:mechanis\w*|how|why|pathways?|drivers?|basis|explanation|causes?)\b", re.IGNORECASE)
_MECHANISM_CUES = re.compile(
    r"\b(?:mechanis\w*|mediat\w*|interact\w*|regulat\w*|upregulat\w*|downregulat\w*|reprogramm\w*|activat\w*|"
    r"signal(?:ing|ling|s)?|pathways?|driv(?:e|es|en|ing)|leads?\s+to|results?\s+in|through\s+(?:the\s+)?\w+|"
    r"via|bind(?:s|ing)?|phosphorylat\w*|metaboli\w*|increased\s+(?:\w+\s+){0,2}(?:expression|levels?|activity)|"
    r"decreased\s+(?:\w+\s+){0,2}(?:expression|levels?|activity))\b",
    re.IGNORECASE,
)
# An absence statement scoped to what this run retrieved ("not mentioned in the retrieved abstract") is checkable.
_SCOPED_ABSENCE = re.compile(
    r"\b(?:in|from|within)\s+(?:the|this|these|its)\s+(?:retrieved|available|returned)\b|"
    r"\bin\s+(?:the|its|this)\s+(?:abstract|excerpt|passage)s?\b|\bin\s+this\s+(?:run|search)\b|"
    r"\bretrieved\s+(?:text|passages?|excerpts?|abstracts?)\b",
    re.IGNORECASE,
)
_LITERATURE_REFERENT = re.compile(
    r"\b(?:cited|retrieved|returned)\s+(?:records?|papers?|articles?|passages?|sources?|literature|studies|publications?|reviews?)\b",
    re.IGNORECASE,
)
_FUNCTION_WORDS = {
    "the", "a", "an", "any", "its", "their", "this", "that", "these", "those", "of", "in", "on", "for", "to", "with",
    "and", "or", "by", "at", "from", "as", "is", "are", "was", "were", "be", "been", "it", "they", "specific",
    "explicit", "clear", "clearly", "detailed", "direct", "directly", "further", "additional", "underlying",
    "about", "regarding", "whether", "which", "what", "whatsoever", "evidence", "data",
}

# Clinical endpoints a figure can belong to. Earlier patterns win where spans overlap
# ("progression-free survival" is PFS, not OS).
_ENDPOINT_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("pfs", re.compile(r"(?:radiographic\s+|immune[\s\-]*related\s+)?progression[\s\-]*free(?:\s+survival)?|\b(?:r?pfs|irpfs)\b", re.IGNORECASE)),
    ("efs", re.compile(r"(?:event|disease|relapse|recurrence|metastasis)[\s\-]*free(?:\s+survival)?|\b(?:efs|dfs|rfs|mfs)\b", re.IGNORECASE)),
    ("dor", re.compile(r"duration\s+of\s+(?:the\s+)?response|\bdor\b", re.IGNORECASE)),
    ("dcr", re.compile(r"disease[\s\-]*control(?:\s+rate)?|\bdcr\b|clinical\s+benefit\s+rate|\bcbr\b", re.IGNORECASE)),
    ("orr", re.compile(r"(?:objective|overall|complete|partial|tumou?r)\s+response(?:\s+rate)?|response\s+rates?|\borr\b|"
                       r"responders?|responded|\bresponses?\b", re.IGNORECASE)),
    ("os", re.compile(r"overall\s+survival|\bos\b|\bsurvival\b|\bdied\b|\bdeaths?\b|mortality", re.IGNORECASE)),
    ("safety", re.compile(r"adverse\s+(?:events?|reactions?)|\btraes?\b|\baes?\b|toxicit\w*|discontinu\w*", re.IGNORECASE)),
)
_CLAUSE_BREAK = re.compile(r"[,;]|\b(?:and|while|whereas|but|versus|vs)\b", re.IGNORECASE)
_PCT_TIME_RE = re.compile(
    r"(?<![\w.])([+−\-]?\d+(?:\.\d+)?)\s*(%|percent\b|per\s+cent\b|months?\b|mo\b|weeks?\b|wks?\b|years?\b|yrs?\b)",
    re.IGNORECASE,
)
_HR_RE = re.compile(r"(?:hazard\s+ratio|\bhr\b)\s*(?:\([^)]{0,20}\)\s*)?(?:of|was|=|:|,)?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_CI_RE = re.compile(
    r"\b(?:\d{2}\s*%\s*)?(?:ci|confidence\s+intervals?)\s*(?:of|was|=|:|,)?\s*\[?\(?\s*(\d+(?:\.\d+)?)\s*(?:-|–|—|to|,)\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_P_RE = re.compile(r"\bp\s*(?:-?\s*value)?\s*([<>=≤≥]|<=|>=)\s*(\d*\.?\d+(?:\s*[x×]\s*10\s*[-−]\s*\d+|e-?\d+)?)", re.IGNORECASE)
# "N=102/164" (two arms) gives both counts: group 1 and group 2; a count before a population noun is group 3.
_N_RE = re.compile(
    r"\bn\s*=\s*(\d+)(?:\s*/\s*(\d+))?\b|(?<![\w.])(\d+)\s+(?:patients|participants|subjects|adults|children|individuals|people|women|men)\b",
    re.IGNORECASE,
)
_TIME_UNITS = r"(?:%|percent\b|months?\b|mo\b|weeks?\b|wks?\b|years?\b|yrs?\b)"
# "10.2 (95% CI 8.1-12.0) months" -> "10.2 months (95% CI 8.1-12.0)"; "10.2 vs 8.1 months" -> "10.2 months vs 8.1
# months": the unit is written once for a value its interval or comparator separates from it.
_UNIT_AFTER_PAREN_RE = re.compile(rf"(?<![\w.])(\d+(?:\.\d+)?)(\s*\([^()]{{1,60}}\))\s*({_TIME_UNITS})", re.IGNORECASE)
_UNIT_AFTER_PAIR_RE = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?)(\s*(?:vs\.?|versus|and|or)\s*)(\d+(?:\.\d+)?)\s*({_TIME_UNITS})", re.IGNORECASE
)
# "10.2 (7.6-16.3) and 6.0 months (4.2-11.0)": after the second value got its unit, the first (separated from it by its
# own interval) gets the same one, as an abstract's "... was A (CI) and B (CI) months, respectively" means.
_UNIT_AFTER_PAREN_PAIR_RE = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?)(\s*\([^()]{{1,60}}\)\s*(?:,|vs\.?|versus|and|or)\s*\d+(?:\.\d+)?\s*({_TIME_UNITS}))",
    re.IGNORECASE,
)


def attach_units(text: str) -> str:
    """`text` with a percent or duration unit repeated next to each value it belongs to (see the patterns above)."""
    text = _UNIT_AFTER_PAREN_RE.sub(lambda m: f"{m.group(1)} {m.group(3)}{m.group(2)}", text)
    text = _UNIT_AFTER_PAREN_PAIR_RE.sub(lambda m: f"{m.group(1)} {m.group(3)}{m.group(2)}", text)
    return _UNIT_AFTER_PAIR_RE.sub(lambda m: f"{m.group(1)} {m.group(4)}{m.group(2)}{m.group(3)} {m.group(4)}", text)
_UNIT_FAMILY = {"%": "%", "percent": "%", "per cent": "%", "month": "months", "months": "months", "mo": "months",
                "week": "weeks", "weeks": "weeks", "wk": "weeks", "wks": "weeks", "year": "years", "years": "years",
                "yr": "years", "yrs": "years"}

# Predicates whose polarity can be compared between a claim and a passage. Verb forms only, so nouns such as
# "KRAS inhibitors" are not read as statements.
_PREDICATE_GROUPS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("improve", re.compile(r"\b(?:improv(?:e|es|ed|ing|ement|ements)|prolong(?:s|ed|ing)?|extend(?:s|ed|ing)?|"
                           r"benefit(?:s|ed|ted)?|superior|outperform(?:s|ed)?)\b", re.IGNORECASE)),
    ("increase", re.compile(r"\b(?:increas(?:e|es|ed|ing)|elevat(?:e|es|ed|ing)|upregulat(?:e|es|ed|ing|ion)|enhanc(?:e|es|ed|ing))\b", re.IGNORECASE)),
    ("decrease", re.compile(r"\b(?:reduc(?:e|es|ed|ing|tion)|decreas(?:e|es|ed|ing)|lower(?:s|ed|ing)?|downregulat(?:e|es|ed|ing|ion)|"
                            r"suppress(?:es|ed|ing)?|inhibit(?:s|ed|ing)?)\b", re.IGNORECASE)),
    ("associate", re.compile(r"\b(?:associated|correlated|predicted|predicts|linked)\b", re.IGNORECASE)),
    ("achieve", re.compile(r"\b(?:met|meet|meets|achiev(?:e|es|ed|ing)|reach(?:es|ed)?)\b", re.IGNORECASE)),
    ("significant", re.compile(r"\bsignificant(?:ly)?\b", re.IGNORECASE)),
    ("approve", re.compile(r"\bapprov(?:e|es|ed|al)\b", re.IGNORECASE)),
    ("restore", re.compile(r"\b(?:restor(?:e|es|ed|ing)|overc[oa]m(?:e|es|ing)|re-?sensiti[sz](?:e|es|ed|ing))\b", re.IGNORECASE)),
)
_NEGATOR = re.compile(
    r"\b(?:not|no|never|neither|nor|without|fail(?:s|ed)?\s+to|unable\s+to|lack(?:s|ed)?\s+(?:of\s+)?|absence\s+of)\b|n't\b",
    re.IGNORECASE,
)
_CITED_KEY_RE = re.compile(r"\bPMID:?\s*(\d{1,9})\b|\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)

MAX_CLAIM_RECORDS = 60
MAX_JUDGE_CALLS = 12
# Reason prefix for a figure whose subgroup, arm or denominator the source assigns differently.
RELATION_MISMATCH_PREFIX = "numeric relation mismatch:"


class _Doc(NamedTuple):
    key: str  # 'PMID:<id>', 'DOI:<doi>', 'passage:<id>', 'NCT:<id>', 'tool:<name>'
    text: str  # lowercased
    passage_id: Optional[str]
    section: str
    # The record's own address (final URL after redirects, else canonical/requested), never a link quoted in its text.
    url: Optional[str] = None
    # The population a typed record is about (a CIViC item's disease, a label's indications, a trial's conditions).
    context: Optional[str] = None


def _fold_thousands(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def _leaf_text(value: Any, depth: int = 0) -> str:
    if isinstance(value, dict):
        if depth > 3:
            return ""
        return "; ".join(
            f"{k}: {t}" for k, v in value.items() if k != "plot_base64_png" and (t := _leaf_text(v, depth + 1))
        )
    if isinstance(value, (list, tuple)):
        return "; ".join(t for v in list(value)[:50] if (t := _leaf_text(v, depth + 1)))
    if value is None:
        return ""
    return str(value)[:2000]


# Fields of a typed clinical record that say which population its fact is about.
_CONTEXT_FIELDS = (
    "disease", "diseases", "disease_name", "disease_label", "cancer_type", "tumor_type", "condition", "conditions",
    "indication", "indications", "indications_and_usage", "population",
)


def _record_context(record: Any) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    parts = [_leaf_text(record.get(f)) for f in _CONTEXT_FIELDS if record.get(f)]
    text = "; ".join(p for p in parts if p)
    return text.lower() if text else None


def _record_items(data: Any, max_records: int = 200) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    """(lowercased document, the record it came from) per tool record (each item of a list of dicts), per nested
    section of a record, per nested page with its own URL (a web search's fetched top page), plus one for the scalar
    fields; a claim must be grounded in a single record rather than in terms scattered across a whole output."""
    if not isinstance(data, dict):
        text = _leaf_text(data)
        return [(text.lower(), None)] if text else []
    docs: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    header: Dict[str, Any] = {}
    for k, v in data.items():
        if k == "plot_base64_png":
            continue
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for item in v[:max_records]:
                text = _leaf_text(item)
                if text:
                    docs.append((f"{k}: {text}".lower(), item))
                # A nested section of a record (a drug's Drugs@FDA "approval", a trial's "results") also gets its own
                # focused document: in the flattened record it sits behind disclaimers, past what a judge reads.
                name = next((str(item.get(f)) for f in ("ingredient", "query_name", "pref_name", "nct_id", "name")
                             if isinstance(item.get(f), str) and item.get(f)), "")
                for sub_key, sub in item.items():
                    if isinstance(sub, dict) and sub:
                        sub_text = _leaf_text(sub)
                        if sub_text:
                            # The section keeps its parent's address and, unless it states its own, its population.
                            inherited = {f: item[f] for f in (*URL_FIELDS, "redirects", *_CONTEXT_FIELDS) if f in item}
                            docs.append((f"{k} {name} {sub_key}: {sub_text}".lower(), {**inherited, **sub}))
        elif isinstance(v, dict) and any(isinstance(x, list) for x in v.values()):
            docs.extend((f"{k}: {d}", rec) for d, rec in _record_items(v, max_records))
        elif isinstance(v, dict) and record_url(v) and not v.get("error"):
            # A page fetched in its own right (websurfer's top_page): its own document, judged by its own address.
            text = _leaf_text(v)
            if text:
                docs.append((f"{k}: {text}".lower(), v))
        else:
            header[k] = v
    text = _leaf_text(header)
    if text:
        docs.insert(0, (text.lower(), header))
    return docs


def _record_documents(data: Any, max_records: int = 200) -> List[str]:
    return [text for text, _rec in _record_items(data, max_records)]


# A claim that names its source ("PrimeKG ranks…", "FAERS shows 237 reports", "DGIdb lists…") is checked against
# that source's output first; otherwise the first document sharing two terms (often OpenTargets) is judged, and a
# true statement about another source is marked unsupported.
_SOURCE_NAME_KEYS: Tuple[Tuple[re.Pattern, Tuple[str, ...]], ...] = (
    (re.compile(r"\bprimekg\b", re.I), ("primekg",)),
    (re.compile(r"\b(?:faers|openfda|drugs@fda|nda\s*\d|bla\s*\d|nda/bla|fda\s+label|boxed\s+warning)", re.I), ("openfda", "drug_safety")),
    (re.compile(r"\bdgidb\b", re.I), ("dgidb",)),
    (re.compile(r"\bchembl\b", re.I), ("chembl",)),
    (re.compile(r"\bpharos\b|\btdl\b|\btclin\b|\btchem\b", re.I), ("pharos",)),
    (re.compile(r"\bbindingdb\b", re.I), ("bindingdb",)),
    (re.compile(r"\bsynlethdb\b", re.I), ("synlethdb",)),
    (re.compile(r"\bclinicaltrials\.gov\b|\bregistry\b", re.I), ("clinical_trials", "clinical")),
    (re.compile(r"\bopentargets\b|\bopen\s+targets\b|\bkegg\b", re.I), ("synapta", "opentargets", "disease_targets")),
    (re.compile(r"\bclinvar\b|\bcivic\b|\bgnomad\b|\bcbioportal\b", re.I), ("variant",)),
    (re.compile(r"\balphafold\b|\bpdb\b|\bpldd?t\b", re.I), ("structure",)),
    (re.compile(r"\b(?:vivarix|sbml|biomodels?|simulation|copasi)\b", re.I), ("vivarix", "simulation")),
    (re.compile(r"\b(?:sandbox|code\s+analysis|attached\s+dataset|uploaded\s+data|dataset\s+analysis)\b", re.I), ("analyst", "sandbox")),
)


_ID_TOKEN_RE = re.compile(r"\b(?:nda|bla|anda)\s*(\d{5,6})\b|\b((?:19|20)\d{2}-\d{2}-\d{2})\b", re.I)


def claim_id_tokens(text: str) -> List[str]:
    """Application numbers and ISO dates a claim states ("NDA 214665", "2021-05-28"), as they appear in records."""
    return list(dict.fromkeys((m.group(1) or m.group(2)).lower() for m in _ID_TOKEN_RE.finditer(text or "")))


def judge_window(doc_text: str, claim: str, size: int = 1500) -> str:
    """The part of a long record the semantic judge should read: around the claim's first figure or identifier found
    in it (posted trial results sit far below a registry record's header), else key salient terms, else the head."""
    if len(doc_text) <= size:
        return doc_text
    probes = claim_id_tokens(claim) + [m.group(0) for m in re.finditer(r"\d+\.\d+", claim or "")]
    for p in probes:
        i = doc_text.find(p)
        if i >= 0:
            start = max(0, i - size // 3)
            return doc_text[start:start + size]
    terms = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{3,}", claim.lower()) if w not in _STOPWORDS]
    for t in sorted(terms, key=len, reverse=True):
        i = doc_text.find(t)
        if i >= 0:
            start = max(0, i - size // 3)
            return doc_text[start:start + size]
    return doc_text[:size]


_ID_GENERIC_TERMS = {"approval", "approved", "fda", "fda-approved", "first", "nda", "bla", "date", "drug", "drugs",
                     "inhibitor", "inhibitors", "label", "openfda", "application"}


def _tool_docs_naming(cited: Set[str], docs: List["_Doc"]) -> List["_Doc"]:
    """Tool records that carry a cited identifier themselves (a SynLethDB pair with its PMID, a DGIdb interaction
    with its publications): when the paper's text was not retrieved, the record that cites it is the evidence."""
    ids = [str(c).split(":", 1)[-1].strip().lower() for c in cited if str(c).split(":", 1)[-1].strip()]
    if not ids:
        return []
    return [d for d in docs if d.key.startswith("tool:") and any(re.search(rf"(?<!\d){re.escape(i)}(?!\d)", d.text) for i in ids)]


def prefer_named_sources(text: str, docs: List["_Doc"]) -> List["_Doc"]:
    """docs with those from the sources the claim names moved to the front (stable otherwise)."""
    wanted = [k for pat, keys in _SOURCE_NAME_KEYS if pat.search(text or "") for k in keys]
    if not wanted:
        return docs
    first = [d for d in docs if d.key.startswith("tool:") and any(k in d.key.lower() for k in wanted)]
    return first + [d for d in docs if d not in first] if first else docs


# A named trial in an answer sentence: an acronym with a numeric suffix ("SOLO-1", "PAOLA-1", "KEYNOTE-189",
# "CheckMate-067"). Hyphenated forms only, so gene symbols ("BRCA2", "PARP1") never count as trial names.
_TRIAL_NAME_RE = re.compile(r"(?<![\w-])([A-Z][A-Za-z]{2,}-\d{1,4}[A-Za-z]?)(?![\w-])")
_NOT_TRIAL_NAMES = {"covid", "brca", "her", "erbb", "cdk", "parp", "kras", "pdl", "ctla", "mek", "egfr", "fgfr", "ret"}
_REVIEW_STUDY_TYPES = {"review", "systematic_review", "editorial"}
_PRIMARY_STUDY_TYPES = {"rct", "clinical_trial"}
_CITED_PMID_RE = re.compile(r"\bPMID:?\s*(\d{1,9})\b", re.IGNORECASE)


def _trial_pattern(name: str) -> re.Pattern:
    """'SOLO-1' as it appears in a record: 'SOLO-1', 'SOLO1' or 'SOLO 1' (case-insensitive, whole token)."""
    stem, num = name.rsplit("-", 1)
    return re.compile(rf"(?<![a-z0-9]){re.escape(stem.lower())}[\s\-]?{re.escape(num.lower())}(?![a-z0-9])")


def _literature_records(tool_results: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """PMID -> {"text" (title + every retrieved passage, lowercased), "study_type", "pub_types", "passage"} for the
    retrieved literature, in ranking order."""
    out: Dict[str, Dict[str, Any]] = {}
    for res in (tool_results or {}).values():
        data = getattr(res, "data", None) if getattr(res, "success", False) else None
        if not isinstance(data, dict) or not isinstance(data.get("ranked_passages"), list):
            continue
        for p in data["ranked_passages"]:
            if not isinstance(p, dict) or not p.get("pmid"):
                continue
            rec = out.setdefault(str(p["pmid"]), {"parts": [], "study_type": "", "pub_types": [], "passage": p})
            title = str(p.get("title") or "")
            if title and title not in rec["parts"]:
                rec["parts"].insert(0, title)
            rec["parts"].append(str(p.get("passage_text") or ""))
            grade = p.get("evidence_grade") if isinstance(p.get("evidence_grade"), dict) else {}
            rec["study_type"] = rec["study_type"] or str(grade.get("study_type") or "").lower()
            rec["pub_types"] = rec["pub_types"] or [str(t) for t in p.get("pub_types") or []]
    for rec in out.values():
        rec["text"] = _fold_thousands(" ".join(rec.pop("parts")).lower())
    return out


def _is_review(rec: Dict[str, Any]) -> bool:
    if rec["study_type"] in _REVIEW_STUDY_TYPES:
        return True
    return not rec["study_type"] and any(t.lower() in ("review", "systematic review", "editorial") for t in rec["pub_types"])


def _is_primary_trial_report(rec: Dict[str, Any]) -> bool:
    if rec["study_type"] in _PRIMARY_STUDY_TYPES:
        return True
    return rec["study_type"] in ("", "unclassified") and any(
        "clinical trial" in t.lower() or "randomized controlled trial" in t.lower() for t in rec["pub_types"]
    )


def prefer_primary_trial_reports(text: str, tool_results: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    """The answer with a named trial's primary report cited next to a review that is cited for the trial's results.

    A sentence qualifies when it names a trial ("SOLO-1"), cites a retrieved review (PMID) and not the primary
    report, and states figures; the primary report is a retrieved trial publication (RCT / clinical trial) whose
    title or abstract names the trial and contains every figure of the sentence (exact match). Its PMID is inserted
    before the review's, which stays as the secondary source. Returns (text, [{"trial", "primary", "review"}])."""
    if not text or "PMID" not in text.upper():
        return text, []
    records = _literature_records(tool_results)
    if not records:
        return text, []
    edits: List[Tuple[int, int, str]] = []
    changes: List[Dict[str, str]] = []
    for s, e in sentence_spans(text):
        sentence = text[s:e]
        trials = [n for n in dict.fromkeys(_TRIAL_NAME_RE.findall(sentence)) if n.rsplit("-", 1)[0].lower() not in _NOT_TRIAL_NAMES]
        cited = [(m, m.group(1)) for m in _CITED_PMID_RE.finditer(sentence)]
        if not trials or not cited:
            continue
        cited_ids = {pmid for _, pmid in cited}
        review = next(((m, pmid) for m, pmid in cited if pmid in records and _is_review(records[pmid])), None)
        if review is None:
            continue
        stripped = _CITED_PMID_RE.sub(" ", sentence)
        for name in trials:
            stripped = stripped.replace(name, " ")
        figures = bare_numbers(_fold_thousands(stripped))
        if not figures:
            continue
        for name in trials:
            pat = _trial_pattern(name)
            primary = None
            for pmid, rec in records.items():
                if pmid in cited_ids or not _is_primary_trial_report(rec) or not pat.search(rec["text"]):
                    continue
                doc_numbers = {float(dm.group(1)) for dm in _DOC_NUMBER_RE.finditer(rec["text"]) if not dm.group(2) and not dm.group(3)}
                if all(f in doc_numbers for f in figures):
                    primary = (pmid, rec)
                    break
            if primary is None:
                continue
            m, review_pmid = review
            level = ""
            after = sentence[m.end():]
            close = after.find(")")
            if close >= 0 and re.match(r"\s*;\s*[^;()]*\breview\b", after[:close], re.IGNORECASE):
                try:
                    from ...agents.prompts import evidence_level

                    level = evidence_level(primary[1]["passage"])
                except Exception:  # noqa: BLE001
                    level = ""
                level = "" if level in ("", "unclassified") else level
            insert = f"PMID: {primary[0]}; {level + '; ' if level else ''}"
            edits.append((s + m.start(), s + m.start(), insert))
            changes.append({"trial": name, "primary": f"PMID:{primary[0]}", "review": f"PMID:{review_pmid}"})
            break
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text, changes


def _fold(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(text: str) -> Set[str]:
    """Word tokens with a light plural fold, for word-boundary term matching."""
    return {_fold(w) for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower())}


class _Metric(NamedTuple):
    kind: str  # pct_time | hr | ci | p | n
    value: float
    unit: str  # %, months, weeks, years, or ""
    endpoint: Optional[str]  # orr, os, pfs, ... (pct_time only)
    label: str


_METRIC_KEYWORDS = {
    "hr": re.compile(r"hazard\s+ratio|\bhr\b", re.IGNORECASE),
    "ci": re.compile(r"\bci\b|confidence|hazard|\bhr\b|\bor\b|\brr\b|\([^\)]*[-–—to][^\)]*\)|\[[^\]]*[-–—to][^\]]*\]", re.IGNORECASE),
    "p": re.compile(r"\bp\s*(?:-?\s*value)?\s*(?:=|<|>|≤|≥)", re.IGNORECASE),
    "n": re.compile(r"\bn\s*=|patients|participants|subjects|adults|children|individuals|people|women|men|enrolled", re.IGNORECASE),
}
_STRUCTURED_KEYWORDS = {
    "hr": re.compile(r"hazard|\bhr\b", re.IGNORECASE),
    "ci": re.compile(r"\bci\b|confidence|lower|upper|hazard|\bhr\b|\bor\b|\brr\b|\([^\)]*[-–—to][^\)]*\)|\[[^\]]*[-–—to][^\]]*\]", re.IGNORECASE),
    "p": re.compile(r"\bp(?:[\s_\-]*value)?\b", re.IGNORECASE),
    "n": re.compile(r"\bn\b|patients|participants|subjects|enrol", re.IGNORECASE),
}
_DOC_NUMBER_RE = re.compile(r"(?<![\w.])(\d*\.?\d+)(?:\s*[x×]\s*10\s*[-−]\s*(\d+)|e-?(\d+))?(?![\d])", re.IGNORECASE)


def _p_value(raw: str) -> float:
    m = re.match(r"(\d*\.?\d+)(?:\s*[x×]\s*10\s*[-−]\s*(\d+)|e-?(\d+))?", raw.strip(), re.IGNORECASE)
    if not m:
        return float("nan")
    exp = m.group(2) or m.group(3)
    return float(m.group(1)) * (10 ** -int(exp) if exp else 1)


# ---------------------------------------------------------------------------
# Display text and markdown-aware sentence handling
# ---------------------------------------------------------------------------
def truncate_words(text: Any, limit: int = 240, ellipsis: str = "…") -> str:
    """`text` cut to at most `limit` characters on a word boundary, with an ellipsis when anything was cut.
    Display only: never use the result as a lookup key."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    room = max(1, limit - len(ellipsis))
    cut = s[:room]
    if s[room:room + 1] not in ("", " ") and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:([{-–—") + ellipsis


_ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,15});")


def unescape_entities(text: Any) -> str:
    """HTML entities decoded ("&lt;0.001" -> "<0.001", "&amp;" -> "&"), except a '<' or '>' that would then open
    or close a tag ("&lt;script" stays escaped), so decoding never turns text into markup."""
    s = str(text or "")
    if "&" not in s:
        return s

    def repl(m: "re.Match[str]") -> str:
        decoded = html.unescape(m.group(0))
        nxt = s[m.end():m.end() + 1]
        if decoded == "<" and (nxt.isalpha() or nxt in "/!?"):
            return m.group(0)
        if decoded == ">" and s[max(0, m.start() - 1):m.start()].isalpha():
            return m.group(0)
        return decoded

    return _ENTITY_RE.sub(repl, s)


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|\-]+\|?\s*$")
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s")
_RULE_LINE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_BOLD_LABEL_LINE_RE = re.compile(r"^\s*(?:[-*+•]\s+)?(\*\*|__)[^*_\n]{1,160}\1\s*:?\s*$")
_LINE_MARKER_RE = re.compile(r"^\s*(?:>\s*)*(?:(?:[-*+•]|\d{1,3}[.)])\s+)?")
_EMPHASIS_RE = re.compile(r"(\*\*|__|`)")
_ROW_ID_RE = re.compile(r"\bPMID:?\s*\d{1,9}\b|\bNCT\d{8}\b|\b10\.\d{4,9}/\S+", re.IGNORECASE)
# Calendar dates ("19 Dec 2014", "March 27, 2017", "2014-12-19"): the day of month is not a study figure.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_CALENDAR_DATE_RE = re.compile(
    rf"\b\d{{1,2}}\s+{_MONTH}\s+\d{{4}}\b|\b{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b",
    re.IGNORECASE,
)
_ROW_NUMBER_RE = re.compile(r"(?<![\w/])[<>≤≥=~]?\s*\d+(?:[.,]\d+)?(?![\w/])")
# A passage sentence that itself denies or hedges ("no data on duration", "the rationale remains unclear").
_PASSAGE_NEGATION_RE = re.compile(
    r"\b(?:no|not|none|neither|nor|without|lack(?:s|ed|ing)?|absence\s+of|unclear|uncertain|unknown|"
    r"remains?\s+(?:unknown|unclear|to\s+be)|insufficient|unexplored|undetermined)\b|n't\b",
    re.IGNORECASE,
)
_BARE_DECIMAL_RE = re.compile(r"(?<![\w.])(\d+\.\d+|\d{2,})(?![\w.]|\.\d)")


def _table_cells(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_EMPHASIS_RE.sub("", c).strip() for c in stripped.split("|")]


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line)) and "-" in line and "|" in line


def _row_claim(cells: List[str], headers: List[str], first: str) -> Optional[str]:
    """A claim sentence for a table data row with a figure: "<first cell> — <labels>: <header> <value>; ...",
    keeping identifiers from the row; None when the row has no number."""
    labels: List[str] = []
    figures: List[str] = []
    ids: List[str] = []
    for i, cell in enumerate(cells[1:], 1):
        if not cell or cell in ("-", "—", "–", "NA", "N/A", "n/a"):
            continue
        found_ids = [m.group(0).rstrip(".,;)") for m in _ROW_ID_RE.finditer(cell)]
        rest = re.sub(r"\(\s*\)|\[\s*\]", " ", _ROW_ID_RE.sub(" ", cell)).strip(" ;,")
        for fid in found_ids:
            if fid.lower() not in first.lower() and fid not in ids:
                ids.append(fid)
        if not rest:
            continue
        if _ROW_NUMBER_RE.search(rest):
            header = headers[i] if i < len(headers) else ""
            if header and header.lower() not in rest.lower():
                figures.append(f"{header} {rest}")
            else:
                figures.append(rest)
        else:
            labels.append(rest)
    if not figures:
        return None
    body = "; ".join(figures)
    if labels:
        body = f"{', '.join(labels)}: {body}"
    text = f"{first} — {body}" if first else body
    if ids:
        text += f" ({', '.join(ids)})"
    return text


def markdown_claim_units(text: str) -> List[Tuple[str, str, bool]]:
    """(raw sentence, claim text, from_table) for the checkable sentences of a markdown answer. Headings, rules,
    bold label lines, code blocks, table header and separator rows, and table rows without a figure are skipped;
    list and blockquote markers are stripped; a table data row becomes one sentence that carries its row context
    (the last non-empty first-column value, e.g. the trial, and the column headers). The raw sentence is the
    answer text exactly as sentence_spans cuts it, so claim_support lines up with the citation map."""
    units: List[Tuple[str, str, bool]] = []
    lines = text.split("\n")
    headers: List[str] = []
    first_value = ""
    in_table = False
    in_code = False
    in_limitations = False
    offset = 0
    line_offsets: List[int] = []
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1
    spans = sentence_spans(text)
    span_idx = 0
    for li, line in enumerate(lines):
        lstart, lend = line_offsets[li], line_offsets[li] + len(line)
        line_spans: List[Tuple[int, int]] = []
        while span_idx < len(spans) and spans[span_idx][0] < lend + 1:
            if spans[span_idx][0] >= lstart:
                line_spans.append(spans[span_idx])
            span_idx += 1
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            if not stripped:
                in_table = False
            continue
        if _TABLE_LINE_RE.match(line):
            if _is_table_separator(line):
                continue
            nxt = lines[li + 1] if li + 1 < len(lines) else ""
            if not in_table or _is_table_separator(nxt):
                in_table = True
                first_value = ""
                headers = _table_cells(line)
                if _is_table_separator(nxt):
                    continue  # header row
                headers = []
            cells = _table_cells(line)
            if cells and cells[0]:
                first_value = cells[0]
            claim = _row_claim(cells, headers, first_value)
            if claim:
                units.append((stripped, claim, True))
            continue
        in_table = False
        if _HEADING_LINE_RE.match(line) or _BOLD_LABEL_LINE_RE.match(line):
            # "Evidence limitations" states limits of the run (and quotes removed claims); "What would change this
            # conclusion" and "Suggested next experiments" are proposals and pending work. None of them are findings.
            in_limitations = bool(_LIMITATIONS_HEADING_RE.match(line) or _NON_FINDING_SECTION_RE.match(line))
            continue
        if in_limitations or _RULE_LINE_RE.match(line) or _CONFIDENCE_LINE_RE.search(line):
            continue
        for s, e in line_spans:
            raw = text[s:e].strip()
            claim = raw
            if s == lstart or not text[lstart:s].strip():
                claim = _LINE_MARKER_RE.sub("", claim, count=1)
            claim = _EMPHASIS_RE.sub("", claim).strip()
            if claim and re.search(r"[A-Za-z]", claim):
                units.append((raw, claim, False))
    return units


def bare_numbers(text: str) -> List[float]:
    """Decimal figures (and numbers of two or more digits) in a table-derived claim, minus identifiers and years."""
    cleaned = _CALENDAR_DATE_RE.sub(" ", _ROW_ID_RE.sub(" ", text))
    cleaned = re.sub(
        r"\b\d{2}\s*%\s*(?:ci|confidence\s+intervals?)\b|\b(?:phase|grade|stage|cycle|arm|cohort|part)\s*[0-9IVX]+[ab]?(?:\s*/\s*[0-9IVX]+)?\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    out: List[float] = []
    for m in _BARE_DECIMAL_RE.finditer(cleaned):
        raw = m.group(1)
        if "." not in raw and 1900 <= int(raw) <= 2100:
            continue
        value = float(raw)
        if value not in out:
            out.append(value)
    return out


# Disease families for the population check: canonical name -> patterns. Only clear disease head phrases, so the
# check stays conservative (a doc must name one of these and none of the question's).
_DISEASE_LEXICON: Tuple[Tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (
        ("melanoma", r"\bmelanomas?\b"),
        ("colorectal cancer", r"\bcolorectal\b|\bcolon\s+(?:cancer|carcinoma|adenocarcinoma)|\brectal\s+(?:cancer|carcinoma)|\bm?crc\b"),
        ("lung cancer", r"\blung\s+(?:cancer|carcinoma|adenocarcinoma)|\bn?sclc\b|\bnon[\s\-]small[\s\-]cell\b|\bsmall[\s\-]cell\s+lung\b"),
        ("breast cancer", r"\bbreast\s+(?:cancer|carcinoma)|\btnbc\b"),
        ("prostate cancer", r"\bprostate\s+(?:cancer|carcinoma|adenocarcinoma)|\bm?c[rs]pc\b|\bmhspc\b"),
        ("pancreatic cancer", r"\bpancreatic\s+(?:cancer|carcinoma|adenocarcinoma|ductal)|\bpdac\b"),
        ("ovarian cancer", r"\bovarian\s+(?:cancer|carcinoma)"),
        ("gastric cancer", r"\bgastric\s+(?:cancer|carcinoma|adenocarcinoma)|\bstomach\s+cancer"),
        ("esophageal cancer", r"\b(?:o?esophageal)\s+(?:cancer|carcinoma|adenocarcinoma|squamous)"),
        ("hepatocellular carcinoma", r"\bhepatocellular\b|\bhcc\b|\bliver\s+cancer"),
        ("renal cell carcinoma", r"\brenal\s+cell\b|\brcc\b|\bkidney\s+cancer"),
        ("urothelial cancer", r"\burothelial\b|\bbladder\s+(?:cancer|carcinoma)"),
        ("endometrial cancer", r"\bendometrial\s+(?:cancer|carcinoma)"),
        ("cervical cancer", r"\bcervical\s+(?:cancer|carcinoma)"),
        ("head and neck cancer", r"\bhead\s+and\s+neck\b|\bhnscc\b"),
        ("glioma", r"\bglioblastoma\b|\bgliomas?\b"),
        ("thyroid cancer", r"\bthyroid\s+(?:cancer|carcinoma)"),
        ("mesothelioma", r"\bmesothelioma\b"),
        ("cholangiocarcinoma", r"\bcholangiocarcinoma\b|\bbiliary\s+tract\s+cancer"),
        ("sarcoma", r"\bsarcomas?\b"),
        ("leukemia", r"\bleuka?emias?\b|\b(?:aml|cll|cml)\b"),
        ("lymphoma", r"\blymphomas?\b|\bdlbcl\b"),
        ("multiple myeloma", r"\bmyeloma\b"),
    )
)


DEFECT_LABELS: Dict[str, str] = {
    "CLAIM_CONTRADICTED": "Contradicted by a source",
    "CLINICAL_CLAIM": "Clinical claim not found in the evidence",
    "POPULATION_MISMATCH": "Population mismatch",
    "CLAIM_SUPPORT": "Not supported by the cited source",
    "CITATION_MISMATCH": "Supported by a different source than the one cited",
    "REGIMEN_MISMATCH": "Combination described as monotherapy",
    "NUMERIC_RELATION_MISMATCH": "Figure assigned to the wrong subgroup, arm or denominator",
    "ABSENCE_OVERREACH": "Absence statement goes beyond the evidence",
    "CITATION": "Citation not found in the retrieved sources",
    "RETRACTED_CITATION": "Retracted source cited",
    "IDENTIFIER": "Identifier not returned by any tool",
    "NUMERICAL": "Number does not match the evidence",  # FAERS, variant, potency and simulation checks all use it
    "MODELING_DISTINCTION": "Time-course value described as a steady state",
    "COMPLETION": "Answer incomplete",
    "INTEGRITY": "Integrity issue",
}


def defect_label(category: str) -> str:
    """Human label for a defect category (the category itself stays in the structured data)."""
    return DEFECT_LABELS.get(str(category or ""), str(category or "Issue").replace("_", " ").capitalize())


def defect_display(defect: Any, limit: int = 160) -> str:
    """One user-facing line for a defect: "<label>: <claim>", cut on a word boundary."""
    category = str(getattr(defect, "category", "") or (defect.get("category") if isinstance(defect, dict) else "") or "")
    claim = getattr(defect, "claim", None) if not isinstance(defect, dict) else defect.get("claim")
    description = getattr(defect, "description", "") if not isinstance(defect, dict) else defect.get("description", "")
    body = unescape_entities(claim or description or "")
    body = re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+", "", body)  # a bullet's list marker is not part of the claim
    line = f"{defect_label(category)}: {truncate_words(body, limit)}"
    if category == "POPULATION_MISMATCH" and claim and description:
        line += f" ({truncate_words(str(description).split(' (')[0], 90)})"
    return line


def claim_tally(report: Any) -> str:
    """'N claims checked: S supported, C contradicted, U not checked' (plus unsupported when any)."""
    claims = list(getattr(report, "claims", None) or [])
    counts = {"supported": 0, "contradicted": 0, "unsupported": 0, "not_checked": 0}
    for c in claims:
        status = getattr(c, "status", None) or (c.get("status") if isinstance(c, dict) else None)
        if status in counts:
            counts[status] += 1
    parts = [f"{counts['supported']} supported", f"{counts['contradicted']} contradicted"]
    if counts["unsupported"]:
        parts.append(f"{counts['unsupported']} unsupported")
    parts.append(f"{counts['not_checked']} not checked")
    n = len(claims)
    return f"{n} claim{'' if n == 1 else 's'} checked: " + ", ".join(parts)


REMOVABLE_DEFECTS = ("CLAIM_CONTRADICTED", "POPULATION_MISMATCH")
# Proposals and method steps: "A pipeline would (1) define…", "this step would need to be performed", "Query
# ClinicalTrials.gov for…", "we recommend…". Checked only when they also cite a source or report a figure.
_PROPOSAL_PATTERN = re.compile(
    r"\b(?:would|could|should)\s+(?:\(?\d+[.)]\s*)?(?:need\s+to\s+|have\s+to\s+|also\s+|then\s+|first\s+)?(?:be\s+)?[a-z]+"
    r"|^\s*(?:[-*]\s+|\d+[.)]\s+)?(?:optionally,?\s+|then,?\s+|next,?\s+|finally,?\s+)?(?:perform|query|run|use|screen|test|"
    r"validate|link|rank|prioriti[sz]e|filter|integrate|assess|evaluate|consider|identify|map|compile|cross-reference|"
    r"mine|search|retrieve|pull|extract|check|apply|combine|compute|select|exclude|include|confirm|obtain|collect)\b"
    r"|\b(?:we|i)\s+(?:propose|recommend|suggest)\b"
    # The pipeline or strategy the answer proposes ("A practical pipeline … begins with …").
    r"|^\s*(?:[-*]\s+|\d+[.)]\s+)?(?:a|the|this|our)\s+(?:practical\s+|proposed\s+|step-by-step\s+|suggested\s+)?"
    r"(?:repurposing\s+|screening\s+|validation\s+)?(?:pipeline|workflow|strategy|framework|approach)\b"
    # Hypotheses ("H1: MEK inhibitors will show …"): predictions to be tested, not findings.
    r"|^\s*(?:[-*]\s+)?(?:\*\*)?H\d+(?:\*\*)?\s*[:.)]"
    r"|\bwill\s+(?:show|demonstrate|reduce|increase|decrease|improve|predict|associate|suppress|enhance|restore|overcome)\b",
    re.IGNORECASE,
)
_DISCLOSED_GAP_RE = re.compile(
    r"\bestablished\s+background\b"
    r"|\b(?:not|no(?:t)?\s+(?:been\s+)?|never)\s*(?:present|retrieved|found|available|reported|included|listed|captured)\s+"
    r"(?:in|by|from|within)\s+(?:the\s+)?(?:retrieved|available|current|returned)\s+(?:evidence|data|sources?|results?|records?)"
    r"|\bin\s+the\s+retrieved\s+(?:evidence|data|records?)\b.{0,40}$"
    r"|\b(?:were|was)\s+not\s+retrieved\b",
    re.IGNORECASE,
)
_NON_FINDING_SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)\s*(?:what\s+(?:\w+\s+){0,3}(?:would|could|might)\s+change|suggested\s+next\s+(?:experiments?|steps)|"
    r"next\s+(?:experiments?|steps)|future\s+(?:directions|work|studies))\b",
    re.IGNORECASE,
)
# The answer's own "Confidence: moderate (...)" line rates the evidence; it is not a factual claim to audit.
_CONFIDENCE_LINE_RE = re.compile(
    # The answer's own rating lines ("Confidence: moderate (...)", "*Evidence level:* label") and the FAERS caveat the
    # prompt requires ("they show neither incidence nor causation"): statements about evidence, not findings.
    r"^\s*(?:[-*>]\s*)?(?:\*\*|\*|_)?\s*(?:(?:overall\s+)?confidence|evidence\s+level)\s*(?:\*\*|\*|_)?\s*[:\-–]"
    r"|\b(?:neither\s+incidence\s+nor\s+causation|do(?:es)?\s+not\s+establish\s+(?:causation|causality|incidence))\b",
    re.IGNORECASE,
)
_LIMITATIONS_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)\s*evidence\s+limitations?\b.*$", re.IGNORECASE | re.MULTILINE)
# The level of the answer's own confidence rating ("**Confidence:** high (...)", "Overall confidence: moderate").
_EMPH = r"(?:\*\*|__|\*|_)?"
_CONFIDENCE_LEVEL_RE = re.compile(
    rf"(?<![\w-]){_EMPH}(?:overall\s+)?confidence(?:\s+level)?{_EMPH}\s*[:\-–—]\s*{_EMPH}\s*"
    r"(?P<level>(?:very\s+)?(?:high|moderate|medium|low)(?:\s*(?:[-–/]\s*(?:to\s*[-–]?\s*)?|to\s+)(?:high|moderate|medium|low))?)\b"
    rf"(?P<close>{_EMPH})",
    re.IGNORECASE,
)
_LEVEL_WORDS = {"high": "high", "moderate": "moderate", "medium": "moderate", "low": "low"}
COMPUTED_CONFIDENCE_NOTE = "(computed from claim support)"


def reconcile_confidence_line(text: str, computed_band: Optional[str]) -> str:
    """The answer with its stated confidence level ("Confidence: high (...)") set to the computed band (high,
    medium -> "moderate", low) when they differ, keeping the model's reason and adding "(computed from claim
    support)". Only the first rating (the bottom line's) is reconciled; without a computed band, or when the
    levels agree, the text is unchanged."""
    target = _LEVEL_WORDS.get(str(computed_band or "").strip().lower())
    if not text or target is None:
        return text
    for m in _CONFIDENCE_LEVEL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()]
        # The rating starts its line (after list or quote markers) or follows a finished sentence.
        if prefix.strip(" \t>-*+") and not re.search(r"[.!?)]\s+$", prefix):
            continue
        stated = {_LEVEL_WORDS[w.lower()] for w in re.findall(r"high|moderate|medium|low", m.group("level"), re.IGNORECASE)}
        if stated == {target}:
            return text
        word = target.capitalize() if m.group("level")[:1].isupper() else target
        end = m.end()
        # The note goes after the model's parenthetical reason when one follows the level, else after the level.
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        note_at = end
        rest = text[end:line_end]
        lead = re.match(r"\s*\(", rest)
        if lead:
            depth = 0
            for i in range(lead.end() - 1, len(rest)):
                depth += {"(": 1, ")": -1}.get(rest[i], 0)
                if depth == 0:
                    note_at = end + i + 1
                    break
        note = "" if COMPUTED_CONFIDENCE_NOTE in text[end:line_end] else f" {COMPUTED_CONFIDENCE_NOTE}"
        return text[:m.start("level")] + word + text[m.end("level"):note_at] + note + text[note_at:]
    return text


def _norm_ws(text: str) -> str:
    return " ".join(str(text or "").split())


def removable_defects(report: Any) -> List[Any]:
    """Claim-audit defects whose sentence must not stay in the answer: contradicted claims and population
    mismatches that carry the answer sentence they came from."""
    return [
        d for d in (getattr(report, "defects", None) or [])
        if getattr(d, "category", "") in REMOVABLE_DEFECTS and getattr(d, "sentence", None)
    ]


def _limitation_line(defect: Any) -> str:
    claim = truncate_words(unescape_entities(getattr(defect, "claim", "") or getattr(defect, "sentence", "")), 140)
    if getattr(defect, "category", "") == "POPULATION_MISMATCH":
        why = str(getattr(defect, "description", "") or "").split(" (")[0].rstrip(".")
        return f'- Removed "{claim}": {why[:1].lower() + why[1:] if why else "the source studied a different population"}.'
    return f'- Removed "{claim}" because a retrieved source contradicts it.'


def remove_flagged_sentences(response: str, defects: Iterable[Any]) -> Tuple[str, List[Any]]:
    """The answer without the sentences (or table rows) of the given defects, plus one line per removal under an
    "Evidence limitations" heading (created when missing). Returns (new text, defects actually removed)."""
    text = response
    removed: List[Any] = []
    seen: Set[str] = set()
    for d in defects:
        sentence = str(getattr(d, "sentence", "") or "").strip()
        key = _norm_ws(sentence)
        if not sentence or key in seen:
            continue
        # The verifier quotes sentences from typography-folded text and the answer may still hold the original
        # characters, so the two are compared folded and the match is mapped back to the answer's positions.
        idx, end = find_folded(text, sentence)
        if idx < 0:
            logger.warning("Flagged sentence not found in the answer; it was not removed: %r", sentence[:160])
            continue
        seen.add(key)
        line_start = text.rfind("\n", 0, idx) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        line = text[line_start:line_end]
        rest = (line[: idx - line_start] + line[end - line_start:])
        if _TABLE_LINE_RE.match(line) or not _LINE_MARKER_RE.sub("", rest, count=1).strip():
            # A table row, or a line holding nothing else: drop the whole line.
            cut_end = line_end + 1 if line_end < len(text) else line_end
            text = text[:line_start] + text[cut_end:]
        else:
            after = end
            while after < len(text) and text[after] in " \t":
                after += 1
            text = text[:idx] + text[after:]
        removed.append(d)
    if not removed:
        return response, []
    lines = [_limitation_line(d) for d in removed]
    m = _LIMITATIONS_HEADING_RE.search(text)
    if m:
        insert_at = m.end()
        text = text[:insert_at] + "\n" + "\n".join(lines) + text[insert_at:]
    else:
        text = text.rstrip() + "\n\n### Evidence limitations\n" + "\n".join(lines) + "\n"
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, removed


def disease_families(text: str) -> Set[str]:
    """Canonical disease names (from a small lexicon of cancer types) named in `text`."""
    t = str(text or "")
    return {name for name, pat in _DISEASE_LEXICON if pat.search(t)}


class ClinicalVerifier:
    """
    Biomedical Verifier applying deterministic rules and evidence cross-referencing.
    Requires external evidence or deterministic computational outputs for scientific assertions.
    """

    # Permitted numerical tolerance: 5.0% relative error, or 0.05 absolute difference
    RELATIVE_NUMERICAL_TOLERANCE = 0.05
    ABSOLUTE_NUMERICAL_TOLERANCE = 0.05

    # A clinical sentence is considered grounded in a passage when it shares this many distinctive terms
    MIN_SHARED_CLAIM_TERMS = 2

    @staticmethod
    def _extract_identifiers(text: str) -> Dict[str, List[str]]:
        return {
            "biomodels": sorted(set(re.findall(r"\bBIOMD\d{10}\b", text))),
            "uniprot": sorted(set(re.findall(
                r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b", text
            ))),
            "ensembl": sorted(set(re.findall(r"\bENS(?:[A-Z]{3})?[GTP]\d{11}\b", text))),
            "pmids": sorted(set(re.findall(r"\bPMID:?\s*(\d{1,9})\b", text, re.IGNORECASE))),
            "dois": sorted(set(d.rstrip(".,;)") for d in re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", text))),
        }

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Sentences and lines of `text` (see sentence_spans: abbreviations such as "et al." do not end a sentence
        and a table row stays whole). Not markdown-aware: claim checking uses markdown_claim_units."""
        return [text[s:e].strip() for s, e in sentence_spans(text)]

    # Models often typeset with Unicode hyphens ("Mos‑P", U+2011), minus signs, and narrow/no-break
    # spaces. Fold them to ASCII before matching so "Mos‑P = 23.4" is never read as a claim about "Mos".
    # The table is shared with the orchestrator (phronesis.utils.text), so a flagged sentence can be found again.
    _TYPOGRAPHY_FOLD = TYPOGRAPHY_FOLD

    @classmethod
    def _normalize_typography(cls, text: str) -> str:
        return fold_typography(text)

    @staticmethod
    def _species_pattern(sp_name: str) -> re.Pattern:
        # Hyphen and '+' are treated as part of species names so 'Mos' never matches inside 'Mos-P'; a name closed by
        # ')' and followed by a name character is a complex ("(EGF_EGFR)2-P"), so its '2' is not EGF_EGFR's value.
        return re.compile(rf"(?<![\w\-+]){re.escape(sp_name)}(?![\w\-+]|\)[\w\-+])", re.IGNORECASE)

    @staticmethod
    def _model_molar_scale(quantity_unit: Optional[str]) -> Optional[float]:
        """Return the molar scale of model concentrations (e.g. 'nmol' or 'nM' -> 1e-9), None if not molar."""
        if not quantity_unit:
            return None
        m = re.match(r"^\s*([pnuµμm]?)(?:mol|M)\b", str(quantity_unit))
        return _MOLAR_SCALE[m.group(1)] if m else None

    _NUMBER_RE = re.compile(
        r"(?<![\w.])((?:(?<=[\s(|:=])|^)[-−])?(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
        r"(?:\s*([pnuµμm]?M)\b)?(\s*(?:%|s\b|sec|min|h\b|hours?|seconds?|minutes?|fold|x\b|×))?"
    )
    _BARE_NUMBER_GAP = re.compile(
        r"^[\s*_`|:=≈~()\[\]]*(?:(?:reached|reaches|was|is|of|at|to|final|value|concentration|level)\s*[\s*_`|:=≈~()]*)*$",
        re.IGNORECASE,
    )

    def _iter_numeric_claims(
        self, text: str, sp_name: str, other_species: List[re.Pattern], model_scale: Optional[float]
    ) -> Iterable[Tuple[float, str]]:
        """Yield (value in model units, raw text) for concentration values asserted for a species."""
        for m in self._species_pattern(sp_name).finditer(text):
            line_end = text.find("\n", m.end())
            segment = text[m.end(): line_end if line_end != -1 else len(text)][:120]
            # Stop at the next mention of a different species so values are attributed correctly.
            cut = len(segment)
            for pat in other_species:
                om = pat.search(segment)
                if om:
                    cut = min(cut, om.start())
            segment = segment[:cut]
            other_table = in_non_simulation_table(text, m.start())

            for nm in self._NUMBER_RE.finditer(segment):
                sign, num_str, unit, non_conc_suffix = nm.group(1), nm.group(2), nm.group(3), nm.group(4)
                if non_conc_suffix:
                    continue  # percentages, durations, fold changes are not concentration claims
                if not unit and other_table:
                    continue  # "| KRAS | 2021 |" in an approvals table is a year, not a KRAS concentration
                if not unit and not self._BARE_NUMBER_GAP.match(segment[: nm.start()]):
                    continue  # unitless numbers only count when directly attached ("Mos: 34.1", "| Mos | 34.1 |")
                value = float(num_str)
                if sign:
                    value = -value
                if unit and model_scale:
                    value = value * _MOLAR_SCALE[unit[:-1]] / model_scale
                yield value, nm.group(0).strip()

    def _within_tolerance(self, value: float, expected: float) -> bool:
        abs_err = abs(value - expected)
        if abs_err <= self.ABSOLUTE_NUMERICAL_TOLERANCE:
            return True
        return expected != 0 and abs_err / abs(expected) <= self.RELATIVE_NUMERICAL_TOLERANCE

    @staticmethod
    def _salient_terms(text: str) -> Set[str]:
        return {
            w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower())
            if w not in _STOPWORDS and not re.fullmatch(r"[\d.\-]+", w)
        }

    def verify_response(
        self,
        query: str,
        response_markdown: str,
        tool_results: Dict[str, ToolResult],
        historical_identifiers: Optional[Dict[str, Iterable[str]]] = None,
        context_identifiers: Optional[Dict[str, Iterable[str]]] = None,
        query_context: Any = None,
        entailment_judge: Optional[Callable[[str, List[Dict[str, str]]], Optional[Dict[str, Any]]]] = None,
    ) -> VerificationReport:
        """
        Audit candidate response against observed tool evidence, numerical outputs, and identifiers.
        Missing evidence, unobserved citations, and material numerical errors strictly prevent verification.
        `historical_identifiers` holds identifiers from VERIFIED_EMPIRICAL memory of earlier turns in the thread.
        `context_identifiers` holds identifiers the query context and evidence sources legitimately know about
        (resolved entities, attached datasets, plugin allow-lists), keyed by legacy key or id family name.
        `entailment_judge(claim_text, [{"source_key", "passage"}]) -> {"status", "reason"}` is an optional second
        pass for semantic support; it can only downgrade a deterministic verdict (or confirm an unsupported
        mechanistic claim). Identifier and numeric checks stay deterministic.
        """
        try:
            from ...extensions import load_extensions

            load_extensions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Extensions unavailable for verification: %s", exc)

        response_markdown = self._normalize_typography(response_markdown)
        defects: List[DefectItem] = []
        verified_ids: List[str] = []
        claim_support: List[Dict[str, Any]] = []
        num_passed = 0
        num_failed = 0

        # Collect observed identifiers and artifacts strictly from successful tool executions
        observed_biomodels: Set[str] = set()
        observed_pmids: Set[str] = set()
        observed_dois: Set[str] = set()
        observed_ensembl: Set[str] = set()
        observed_uniprot: Set[str] = set()
        observed_family: Dict[str, Set[str]] = {f.name: set() for f in FAMILIES}
        observed_passages: List[str] = []
        passage_docs: List[Tuple[str, str, Optional[str]]] = []  # (evidence_key, lowercased text, passage_id)
        evidence_documents: List[str] = []  # lowercased passages + serialized tool outputs for claim grounding
        evidence_keys: List[str] = []  # parallel to evidence_documents: 'tool:<name>', 'PMID:<id>', 'DOI:<doi>', ...
        evidence_doc_passage_ids: List[Optional[str]] = []
        # Structured documents for the built-in claim audit: passages with their section, provider documents and
        # one document per tool record, never a whole serialized tool output.
        grounding: List[_Doc] = []
        doc_providers = get_evidence_doc_providers()

        # What tools did in this run, for sentences about the run itself (a failed or empty lookup).
        tool_events: List[Tuple[str, str]] = []
        for t_name, t_res in tool_results.items():
            if t_res is not None and not t_res.success:
                tool_events.append((str(t_name), f"{t_name} {getattr(t_res, 'tool_name', '') or ''}: {t_res.error or 'failed'}".lower()))
            elif t_res is not None and isinstance(t_res.data, dict) and t_res.data.get("error"):
                tool_events.append((str(t_name), f"{t_name}: {t_res.data.get('error')}".lower()))
            if t_res is not None and t_res.success and isinstance(t_res.data, dict):
                # Exact-identifier lookups and their linked corrections are outcomes of this run too: what was
                # retrieved, what was not, and that corrected content is never applied automatically.
                for entry in (t_res.data.get("retrieval") or {}).get("pinned") or []:
                    if not isinstance(entry, dict):
                        continue
                    ident = str(entry.get("normalized") or entry.get("id") or "")
                    tool_events.append((str(t_name), f"{t_name}: exact lookup of {ident}: {entry.get('status')}".lower()))
                    for c in entry.get("corrections") or []:
                        if not isinstance(c, dict):
                            continue
                        cid = str(c.get("id") or "")
                        state = (
                            f"retrieved; {'text available' if c.get('content_available') else 'no abstract or text'}"
                            if c.get("retrieved") else "not retrieved"
                        )
                        event = (
                            f"{t_name}: {c.get('type') or 'correction'} {cid} for {ident}: correction notice {state}; "
                            "corrected content not applied"
                        )
                        tool_events.append((str(t_name), event.lower()))
        for t_name, t_res in tool_results.items():
            if not t_res or not t_res.success:
                continue
            data = apply_serialization_filters(t_name, t_res.data)

            # BioModels executed
            if "biomodel_id" in t_res.canonical_ids:
                observed_biomodels.add(t_res.canonical_ids["biomodel_id"])
            if isinstance(data, dict) and "model_id" in data:
                observed_biomodels.add(str(data["model_id"]))

            # Ensembl & UniProt
            if t_res.canonical_ids.get("ensembl_id"):
                observed_ensembl.add(t_res.canonical_ids["ensembl_id"])
            if t_res.canonical_ids.get("uniprot_id"):
                observed_uniprot.add(t_res.canonical_ids["uniprot_id"])

            # Literature DOIs, PMIDs, and passages
            has_passages = isinstance(data, dict) and "ranked_passages" in data
            tool_passages: List[Tuple[str, str, Optional[str]]] = []
            tool_grounding: List[_Doc] = []
            if has_passages:
                for p in data["ranked_passages"] or []:
                    if isinstance(p, dict):
                        if p.get("pmid"):
                            observed_pmids.add(str(p["pmid"]))
                        if p.get("doi"):
                            observed_dois.add(str(p["doi"]))
                        if p.get("passage_text"):
                            # Folded like the answer ("10·2 months" -> "10.2 months") so figures compare equal.
                            text = fold_typography(str(p["passage_text"])).lower()
                            observed_passages.append(text)
                            if p.get("pmid"):
                                key = f"PMID:{p['pmid']}"
                            elif p.get("doi"):
                                key = f"DOI:{str(p['doi']).lower()}"
                            elif p.get("passage_id"):
                                key = f"passage:{p['passage_id']}"
                            else:
                                key = f"tool:{t_name}"
                            pid = str(p["passage_id"]) if p.get("passage_id") else None
                            tool_passages.append((key, text, pid))
                            title = fold_typography(str(p.get("title") or "")).lower()
                            tool_grounding.append(_Doc(key, f"{title}. {text}" if title else text, pid, str(p.get("section") or "")))
            if isinstance(data, dict):
                # A correction notice the exact lookup retrieved is a retrieved record, cited by its own PMID.
                for entry in (data.get("retrieval") or {}).get("pinned") or []:
                    for c in (entry.get("corrections") or []) if isinstance(entry, dict) else []:
                        if isinstance(c, dict) and c.get("retrieved") and re.fullmatch(r"\d{1,9}", str(c.get("id") or "")):
                            observed_pmids.add(str(c["id"]))
            for pmid_key in ("pmids", "declared_source_pmids"):
                if pmid_key in t_res.canonical_ids:
                    for p in str(t_res.canonical_ids[pmid_key]).split(","):
                        if p.strip():
                            observed_pmids.add(p.strip())
            if "dois" in t_res.canonical_ids:
                for d in str(t_res.canonical_ids["dois"]).split(","):
                    if d.strip():
                        observed_dois.add(d.strip())
            for fam in FAMILIES:
                for ck in fam.canonical_keys:
                    if t_res.canonical_ids.get(ck):
                        observed_family[fam.name] |= normalize_ids(fam.name, t_res.canonical_ids[ck])

            # A registered provider may replace this tool's default evidence documents with finer-grained ones.
            provided: Optional[List[Tuple[str, str]]] = None
            for provider in doc_providers:
                try:
                    out = provider(t_name, t_res)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Evidence doc provider failed for %s: %s", t_name, exc)
                    continue
                if isinstance(out, list):
                    provided = out
                    break

            # Any identifier present anywhere in a successful tool output counts as observed
            serialized = ""
            if data:
                try:
                    serialized = json.dumps(data, default=str)
                except (TypeError, ValueError):
                    serialized = str(data)
                serialized = re.sub(r'"plot_base64_png":\s*"[^"]*"', "", serialized)
                tool_ids = self._extract_identifiers(serialized)
                observed_ensembl.update(tool_ids["ensembl"])
                observed_uniprot.update(tool_ids["uniprot"])
                observed_biomodels.update(tool_ids["biomodels"])
                observed_dois.update(tool_ids["dois"])
                for name, found in extract_family_ids(serialized).items():
                    observed_family[name] |= found

            if provided is not None:
                # Provided documents replace this tool's whole-tool doc or passages for claim grounding;
                # the passages still count for the identifier checks below.
                for key, doc in provided:
                    evidence_documents.append(str(doc).lower())
                    evidence_keys.append(str(key))
                    evidence_doc_passage_ids.append(None)
                    grounding.append(_Doc(str(key), fold_typography(str(doc)).lower(), None, ""))
            else:
                passage_docs.extend(tool_passages)
                grounding.extend(tool_grounding)
                if serialized and not has_passages:
                    # Registered checks still receive the whole serialization; the claim audit uses records.
                    evidence_documents.append(serialized.lower())
                    evidence_keys.append(f"tool:{t_name}")
                    evidence_doc_passage_ids.append(None)
                    grounding.extend(
                        _Doc(f"tool:{t_name}", fold_typography(chunk), None, "", record_url(rec), _record_context(rec))
                        for chunk, rec in _record_items(data)
                    )

        for key, text, pid in passage_docs:
            evidence_documents.append(text)
            evidence_keys.append(key)
            evidence_doc_passage_ids.append(pid)

        # Identifiers verified in earlier turns of this thread (VERIFIED_EMPIRICAL memory only)
        historical = {k: set(v) for k, v in (historical_identifiers or {}).items()}
        observed_biomodels |= historical.get("biomodels", set())
        observed_pmids |= historical.get("pmids", set())
        observed_dois |= historical.get("dois", set())
        observed_ensembl |= historical.get("ensembl", set())
        observed_uniprot |= historical.get("uniprot", set())

        # Identifiers the query context and evidence sources legitimately know about
        context: Dict[str, Set[str]] = {}
        for k, v in (context_identifiers or {}).items():
            context[k] = normalize_ids(k, v) if k in observed_family else {str(x) for x in (v or []) if x}
        observed_biomodels |= context.get("biomodels", set())
        observed_pmids |= context.get("pmids", set())
        observed_dois |= context.get("dois", set())
        observed_ensembl |= context.get("ensembl", set())
        observed_uniprot |= context.get("uniprot", set())

        query_family_ids = extract_family_ids(self._normalize_typography(query or ""))
        for fam in FAMILIES:
            observed_family[fam.name] |= normalize_ids(fam.name, historical.get(fam.name, set()))
            observed_family[fam.name] |= context.get(fam.name, set())
            observed_family[fam.name] |= query_family_ids[fam.name]

        # Registered vetoes (e.g. retracted articles) override every other source of trust.
        vetoed: Dict[str, Dict[str, str]] = {}
        for veto in get_identifier_vetoes():
            try:
                result = veto(tool_results) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Identifier veto failed: %s", exc)
                continue
            for fam_name, ids in result.items():
                bucket = vetoed.setdefault(fam_name, {})
                for vid, reason in (ids or {}).items():
                    norm = str(vid).lower() if fam_name == "dois" else next(iter(normalize_ids(fam_name, [vid])), str(vid))
                    bucket.setdefault(norm, str(reason))
        vetoed_labels = {f.name: f.label for f in FAMILIES}
        vetoed_identifiers = sorted(
            f"{'PMID' if k == 'pmids' else 'DOI' if k == 'dois' else vetoed_labels.get(k, k.upper())}:{vid}"
            for k, ids in vetoed.items()
            for vid in ids
        )

        def retracted_defect(display: str, reason: str) -> DefectItem:
            return DefectItem(
                category="RETRACTED_CITATION",
                severity=DefectSeverity.CRITICAL,
                description=reason,
                suggested_correction=(
                    "Remove the citation and any claim that depends on it; you may state that a retrieved article "
                    "was excluded because it was retracted."
                ),
                claim=display,
            )

        observed_dois_lower = {d.lower() for d in observed_dois}
        extracted_ids = self._extract_identifiers(response_markdown)
        cited_family = {k: sorted(v) for k, v in extract_family_ids(response_markdown).items()}
        resp_lower = response_markdown.lower()

        # -------------------------------------------------------------
        # 1. IDENTIFIER GROUNDING (No Whitelisting: Must be Observed)
        # -------------------------------------------------------------
        # Check BioModels: if cited, MUST have been executed by Vivarix (now or in a verified earlier turn)
        for bm_id in extracted_ids["biomodels"]:
            if bm_id in observed_biomodels:
                verified_ids.append(bm_id)
            else:
                defects.append(
                    DefectItem(
                        category="IDENTIFIER",
                        severity=DefectSeverity.CRITICAL,
                        description=f"BioModel '{bm_id}' was cited in the synthesis but was never executed by Vivarix.",
                        suggested_correction="Only cite BioModels that were actively loaded and simulated in this investigation.",
                        claim=bm_id,
                    )
                )

        # Check DOIs: cited DOIs must be present in retrieved literature passages
        for doi in extracted_ids["dois"]:
            if doi.lower() in vetoed.get("dois", {}):
                defects.append(retracted_defect(f"DOI {doi}", vetoed["dois"][doi.lower()]))
            elif doi.lower() in observed_dois_lower or any(_doi_in_passage(doi, p) for p in observed_passages):
                verified_ids.append(f"DOI:{doi}")
            else:
                defects.append(
                    DefectItem(
                        category="CITATION",
                        severity=DefectSeverity.CRITICAL,
                        description=f"DOI '{doi}' cited in text was not observed in any retrieved literature passage from Lithoscribe.",
                        suggested_correction="Remove unobserved DOI citation or cite only verified DOIs returned by Lithoscribe.",
                        claim=f"DOI {doi}",
                    )
                )

        # Check PMIDs: cited PMIDs must be present in retrieved literature passages
        for pmid in extracted_ids["pmids"]:
            if pmid in vetoed.get("pmids", {}):
                defects.append(retracted_defect(f"PMID {pmid}", vetoed["pmids"][pmid]))
            elif pmid in observed_pmids or any(_pmid_in_passage(pmid, p) for p in observed_passages):
                verified_ids.append(f"PMID:{pmid}")
            else:
                defects.append(
                    DefectItem(
                        category="CITATION",
                        severity=DefectSeverity.CRITICAL,
                        description=f"PMID '{pmid}' cited in text does not correspond to any observed passage retrieved by Lithoscribe.",
                        suggested_correction="Cite only PMIDs present in the empirical literature synthesis.",
                        claim=f"PMID {pmid}",
                    )
                )

        # Check Ensembl gene/transcript/protein accessions
        for ens_id in extracted_ids["ensembl"]:
            if ens_id in observed_ensembl:
                verified_ids.append(f"ENSEMBL:{ens_id}")
            else:
                defects.append(
                    DefectItem(
                        category="IDENTIFIER",
                        severity=DefectSeverity.CRITICAL,
                        description=f"Ensembl identifier '{ens_id}' was cited but was not returned by any executed tool.",
                        suggested_correction="Cite only Ensembl accessions resolved by Synapta/OpenTargets in this investigation.",
                        claim=ens_id,
                    )
                )

        # Check UniProt accessions
        for up_id in extracted_ids["uniprot"]:
            if up_id in observed_uniprot or any(up_id.lower() in p for p in observed_passages):
                verified_ids.append(f"UNIPROT:{up_id}")
            else:
                defects.append(
                    DefectItem(
                        category="IDENTIFIER",
                        severity=DefectSeverity.CRITICAL,
                        description=f"UniProt accession '{up_id}' was cited but was not returned by any executed tool or passage.",
                        suggested_correction="Cite only UniProt accessions observed in tool outputs or retrieved literature.",
                        claim=up_id,
                    )
                )

        # Generic identifier families (NCT, ChEMBL, HGNC, ontology, rsID, ClinVar, CIViC, GEO, dataset ids).
        # Checked whether or not the sentence is hedged: naming an unobserved id is itself the defect.
        for fam in FAMILIES:
            for fid in cited_family.get(fam.name, []):
                if fid in vetoed.get(fam.name, {}):
                    defects.append(retracted_defect(fid, vetoed[fam.name][fid]))
                elif fid in observed_family[fam.name]:
                    verified_ids.append(f"{fam.label}:{fid}")
                else:
                    defects.append(
                        DefectItem(
                            category="IDENTIFIER",
                            severity=DefectSeverity(fam.severity),
                            description=(
                                f"{fam.label} identifier '{fid}' was cited but was not returned by any executed tool, "
                                f"retrieved passage, or your question."
                            ),
                            suggested_correction=fam.correction,
                            claim=fid,
                        )
                    )

        # -------------------------------------------------------------
        # 2. SUBSTANTIVE CLINICAL & SCIENTIFIC CLAIM AUDIT (sentence-level grounding)
        # -------------------------------------------------------------
        claims: List[ClaimRecord] = []
        claim_defects, claim_support = self._audit_claims(
            response_markdown, grounding, claims, entailment_judge, self._question_diseases(query, query_context),
            tool_events=tool_events,
        )
        defects.extend(claim_defects)

        # Disallow forbidden vendor platform leaks
        for forbidden in ["langgraph", "openai", "nvidia"]:
            if re.search(rf"\b{forbidden}\b", resp_lower):
                defects.append(
                    DefectItem(
                        category="INTEGRITY",
                        severity=DefectSeverity.CRITICAL,
                        description=f"Response mentions internal provider or framework name '{forbidden}'.",
                        suggested_correction="Use official Phronesis agent designations (Synapta, Vivarix, Lumicell, Lithoscribe, WebSurfer).",
                    )
                )

        # -------------------------------------------------------------
        # 3. NUMERICAL CONSISTENCY (Strict 5% Relative Tolerance)
        # -------------------------------------------------------------
        vivarix_res = tool_results.get("vivarix") or tool_results.get("vivarix_simulate_time_course")
        steady_res = tool_results.get("vivarix_steady_state") or tool_results.get("vivarix_compute_steady_state")
        if vivarix_res and vivarix_res.success and isinstance(vivarix_res.data, dict):
            v_data = vivarix_res.data
            final_concs: Dict[str, Any] = v_data.get("final_concentrations", {}) or {}
            initial_concs: Dict[str, Any] = v_data.get("initial_concentrations", {}) or {}
            series: Dict[str, Any] = v_data.get("time_series_sample", {}) or {}
            ss_concs: Dict[str, Any] = {}
            if steady_res and steady_res.success and isinstance(steady_res.data, dict):
                ss_concs = steady_res.data.get("steady_state_concentrations", {}) or {}
            model_scale = self._model_molar_scale((v_data.get("units") or {}).get("quantity"))

            species_names = [sp for sp in final_concs.keys() if sp]
            species_patterns = {sp: self._species_pattern(sp) for sp in species_names}
            reported: Set[Tuple[str, float]] = set()

            for sp_name in species_names:
                observed_vals = [
                    float(v)
                    for v in [final_concs.get(sp_name), initial_concs.get(sp_name), ss_concs.get(sp_name), *(series.get(sp_name) or [])]
                    if v is not None
                ]
                if not observed_vals:
                    continue
                expected_final = float(final_concs[sp_name])
                others = [pat for other, pat in species_patterns.items() if other != sp_name]

                for value, raw in self._iter_numeric_claims(response_markdown, sp_name, others, model_scale):
                    if any(self._within_tolerance(value, ov) for ov in observed_vals):
                        num_passed += 1
                        self._add_claim(claims, "computational", f"{sp_name}: {raw}", "supported",
                                        "matches a simulator output value", "tool:vivarix")
                        continue
                    num_failed += 1
                    self._add_claim(claims, "computational", f"{sp_name}: {raw}", "contradicted",
                                    f"simulator final value was {expected_final:.4g}", "tool:vivarix")
                    if (sp_name, round(value, 6)) in reported:
                        continue
                    reported.add((sp_name, round(value, 6)))
                    rel_err = abs(value - expected_final) / abs(expected_final) if expected_final else float("inf")
                    defects.append(
                        DefectItem(
                            category="NUMERICAL",
                            severity=DefectSeverity.CRITICAL,
                            description=(
                                f"Concentration for '{sp_name}' cited as '{raw}' ({value:.4g} in model units), which matches no "
                                f"observed simulator value; final time-course value was {expected_final:.4g} "
                                f"(Relative Error: {rel_err * 100:.1f}%, allowed tolerance: {self.RELATIVE_NUMERICAL_TOLERANCE * 100}%)."
                            ),
                            suggested_correction=f"Align concentration for {sp_name} with the exact simulator output ({expected_final:.4g}) and its unit.",
                            claim=f"{sp_name}: {raw}",
                        )
                    )

        # Check for distinction between transient time-course and steady state
        ss_executed = bool(
            steady_res and steady_res.success
            or any(
                t_res and t_res.success and t_res.tool_name in ("vivarix_compute_steady_state", "compute_steady_state")
                for t_res in tool_results.values()
            )
        )
        if not ss_executed and vivarix_res and vivarix_res.success and isinstance(vivarix_res.data, dict):
            if vivarix_res.data.get("simulation_type") == "TIME_COURSE":
                for sentence in self._split_sentences(response_markdown):
                    if (
                        re.search(r"steady[\s\-]*state", sentence, re.IGNORECASE)
                        and not _HEDGE_PATTERN.search(sentence)
                        and not _SUGGESTION_PATTERN.search(sentence)
                    ):
                        defects.append(
                            DefectItem(
                                category="MODELING_DISTINCTION",
                                severity=DefectSeverity.MAJOR,
                                description="Response describes transient time-course concentrations as validated steady-state values without running steady-state computation.",
                                suggested_correction="Describe values as final time-course simulation points (duration-dependent) rather than verified steady-state equilibria.",
                                claim=truncate_words(sentence, 240),
                            )
                        )
                        break

        # -------------------------------------------------------------
        # 4. TASK COMPLETION & EMPTY EVIDENCE PENALTY
        # -------------------------------------------------------------
        # If all tools failed or were empty, but response claims verified biological findings
        non_empty_tools = [res for res in tool_results.values() if res and res.success and res.data]
        if not non_empty_tools:
            # Response must state that evidence is unavailable
            if "unavailable" not in resp_lower and "insufficient" not in resp_lower and "no data" not in resp_lower:
                defects.append(
                    DefectItem(
                        category="COMPLETION",
                        severity=DefectSeverity.CRITICAL,
                        description="All empirical tool executions yielded no data, but response presents findings without stating that evidence was unavailable.",
                        suggested_correction="Explicitly report that empirical tools returned no data and state the limitation clearly.",
                    )
                )

        query_words = [w.lower() for w in re.split(r"\W+", query) if len(w) > 3]
        matched_q_words = sum(1 for w in query_words if w in resp_lower)
        if query_words and (matched_q_words / len(query_words)) < 0.25:
            defects.append(
                DefectItem(
                    category="COMPLETION",
                    severity=DefectSeverity.MAJOR,
                    description="Response does not appear to address the primary focus of the user inquiry.",
                    suggested_correction=f"Directly address the user query: '{query}'.",
                )
            )

        # -------------------------------------------------------------
        # 4b. REGISTERED FEATURE CHECKS (trial figures, pharmacology, variant frequencies, ...)
        # -------------------------------------------------------------
        checks = get_verification_checks()
        if checks:
            observed_all: Dict[str, Set[str]] = {
                "biomodels": set(observed_biomodels),
                "pmids": set(observed_pmids),
                "dois": set(observed_dois),
                "ensembl": set(observed_ensembl),
                "uniprot": set(observed_uniprot),
                **{k: set(v) for k, v in observed_family.items()},
            }
            vctx = VerificationContext(
                query=query,
                response=response_markdown,
                sentences=self._split_sentences(response_markdown),
                table_rows=self._table_rows(response_markdown),
                tool_results=tool_results,
                evidence_documents=list(evidence_documents),
                evidence_keys=list(evidence_keys),
                observed=observed_all,
                cited={**{k: list(v) for k, v in extracted_ids.items()}, **cited_family},
                historical=historical,
                context_identifiers=context,
                vetoed=vetoed,
                query_context=query_context,
                hedge_pattern=_HEDGE_PATTERN,
            )
            # One claim is flagged once per category: a registered check's more specific defect replaces the
            # generic sentence-grounding one, and two registered checks never both count it.
            flagged: Set[Tuple[str, str]] = set()
            builtin = {(d.category, d.claim): d for d in defects if d.claim}
            for check_key, check in checks:
                try:
                    found = check(vctx) or []
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Verification check %s failed: %s", check_key, exc)
                    continue
                for item in found:
                    defect = self._defect_from_dict(item)
                    if defect is None:
                        continue
                    claim = defect.claim
                    if claim:
                        key = (defect.category, claim)
                        if key in flagged:
                            continue
                        flagged.add(key)
                        generic = builtin.pop(key, None)
                        if generic is not None and generic.severity == defect.severity:
                            defects.remove(generic)
                    defects.append(defect)

        # -------------------------------------------------------------
        # 5. RIGOROUS VERIFICATION SCORE COMPUTATION
        # -------------------------------------------------------------
        base_score = 1.0
        critical_count = 0
        for d in defects:
            if d.severity == DefectSeverity.CRITICAL:
                base_score -= 0.55  # Critical defect immediately pulls score below passing threshold
                critical_count += 1
            elif d.severity == DefectSeverity.MAJOR:
                base_score -= 0.25
            elif d.severity == DefectSeverity.MINOR:
                base_score -= 0.10

        final_score = max(0.0, min(1.0, base_score))
        contradicted = sum(1 for c in claims if c.status == "contradicted")
        unsupported = sum(1 for c in claims if c.status == "unsupported")
        unchecked = sum(1 for c in claims if c.status == "not_checked")
        passed = (final_score >= 0.80) and (critical_count == 0) and contradicted == 0

        lesson = None
        if not passed:
            top_defect = defects[0] if defects else None
            if top_defect:
                lesson = f"Reflexion Defect [{top_defect.category}]: {top_defect.description}"

        return VerificationReport(
            passed=passed,
            verification_score=round(final_score, 2),
            defects=defects,
            verified_identifiers=verified_ids,
            numerical_checks_passed=num_passed,
            numerical_checks_failed=num_failed,
            failure_lesson=lesson,
            claim_support=claim_support,
            vetoed_identifiers=vetoed_identifiers,
            claims=claims[:MAX_CLAIM_RECORDS],
            contradicted_claims=contradicted,
            unsupported_claims=unsupported,
            unchecked_claims=unchecked,
            fully_verified=passed and not (contradicted or unsupported or unchecked),
        )

    @staticmethod
    def _question_diseases(query: str, query_context: Any = None) -> Set[str]:
        """Disease families the question is about: named in the query text or resolved by the query context."""
        found = disease_families(query or "")
        try:
            for d in list(getattr(query_context, "diseases", None) or []):
                found |= disease_families(f"{getattr(d, 'label', '') or ''} {getattr(d, 'mention', '') or ''}")
        except Exception:  # noqa: BLE001 - a context without diseases simply adds nothing
            pass
        return found

    @staticmethod
    def _table_rows(text: str) -> List[str]:
        """Markdown table rows (header and body), excluding |---|---| separator rows."""
        rows = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and not (re.fullmatch(r"[\s|:\-]+", stripped) and "-" in stripped):
                rows.append(stripped)
        return rows

    @staticmethod
    def _defect_from_dict(item: Any) -> Optional[DefectItem]:
        if isinstance(item, DefectItem):
            return item
        if not isinstance(item, dict):
            return None
        try:
            severity = DefectSeverity(str(item.get("severity", "MAJOR")).upper())
        except ValueError:
            severity = DefectSeverity.MAJOR
        return DefectItem(
            category=str(item.get("category") or "CLAIM"),
            severity=severity,
            description=str(item.get("description") or ""),
            suggested_correction=str(item.get("suggested_correction") or ""),
            claim=item.get("claim"),
        )

    def _unsupported_clinical_reason(self, sentence_lower: str, evidence_documents: List[str]) -> Optional[str]:
        """Return why a clinical sentence is unsupported, or None if some evidence document grounds it."""
        return self._find_clinical_support(sentence_lower, evidence_documents)[1]

    def _find_clinical_support(
        self, sentence_lower: str, evidence_documents: List[str]
    ) -> Tuple[Optional[int], Optional[str]]:
        """
        Return (index of the first grounding evidence document, None), or (None, reason it is unsupported).
        A supporting document must contain every cited trial identifier, every quantitative figure bound to the
        same endpoint and unit, share at least MIN_SHARED_CLAIM_TERMS distinctive terms (drug, target, indication)
        with the sentence, and must not state the opposite.
        """
        docs = [_Doc(f"doc:{i}", d, None, "") for i, d in enumerate(evidence_documents)]
        status, reason, doc = self._clinical_verdict(sentence_lower, set(), docs)
        if status == "supported" and doc is not None:
            return int(doc.key[4:]), None
        return None, reason

    # ------------------------------------------------------------------
    # Claim-level audit
    # ------------------------------------------------------------------
    @staticmethod
    def _add_claim(
        claims: List[ClaimRecord],
        claim_type: str,
        text: str,
        status: str,
        reason: str,
        source_key: Optional[str] = None,
        passage: str = "",
        passage_id: Optional[str] = None,
        extra_evidence: Optional[List[ClaimEvidence]] = None,
        cited_source_supported: Optional[bool] = None,
    ) -> ClaimRecord:
        ev: List[ClaimEvidence] = []
        if source_key:
            ev.append(ClaimEvidence(source_key=source_key, passage=truncate_words(passage, 300), passage_id=passage_id))
        if extra_evidence:
            seen_keys = {source_key} if source_key else set()
            for ee in extra_evidence:
                if ee.source_key and ee.source_key not in seen_keys:
                    seen_keys.add(ee.source_key)
                    ev.append(ee)
        record = ClaimRecord(
            claim_id=f"c{len(claims) + 1}",
            claim_type=claim_type,
            claim_text=truncate_words(text, 240),
            status=status,
            reason=reason,
            evidence=ev,
            cited_source_supported=cited_source_supported,
        )
        claims.append(record)
        return record

    @staticmethod
    def _excerpt(doc: Optional[_Doc], pos: Optional[int] = None) -> str:
        if doc is None:
            return ""
        if pos is None or pos < 0:
            return doc.text[:300]
        return doc.text[max(0, pos - 140): pos + 160]

    @classmethod
    def _assertive_text(cls, sentence: str) -> Optional[str]:
        """The sentence minus clauses that only state uncertainty; None when nothing checkable remains."""
        if _DISCLOSED_GAP_RE.search(sentence) and not _CITED_KEY_RE.search(sentence):
            # "… (established background), but the dates were not present in the retrieved evidence": the answer
            # discloses what the evidence lacks or labels background knowledge as the rules require. Not a finding.
            return None
        if _PROPOSAL_PATTERN.search(sentence) and not cls._claim_metrics(sentence) and not _CITED_KEY_RE.search(sentence):
            return None  # a proposed step or recommendation ("the pipeline would…", "Query X for…"), not a finding
        if not _UNCERTAINTY_PATTERN.search(sentence):
            return sentence
        clauses = re.split(
            r";|,\s*(?=(?:although|but|while|whereas|however|though)\b)|\s(?=(?:although|whereas|though)\b)", sentence
        )
        kept = [c.strip() for c in clauses if c.strip() and not _UNCERTAINTY_PATTERN.search(c)]
        if not kept:
            return None
        joined = " ".join(kept)
        low = joined.lower()
        if cls._claim_metrics(joined) or _ABSENCE_PATTERN.search(joined) or any(re.search(p, low) for p in _CLINICAL_CLAIM_PATTERNS):
            return joined
        return None

    @staticmethod
    def _classify_claim(text: str) -> Optional[str]:
        if _ABSENCE_PATTERN.search(text):
            return "absence"
        low = text.lower()
        if any(re.search(p, low) for p in _CLINICAL_CLAIM_PATTERNS) or is_regulatory_claim(text):
            return "clinical"
        if _CITED_KEY_RE.search(text) and len(re.findall(r"[A-Za-z]{3,}", _CITED_KEY_RE.sub(" ", text))) >= 5:
            return "mechanistic"
        return None

    @staticmethod
    def _cited_keys(text: str) -> Set[str]:
        keys: Set[str] = set()
        for pmid, doi in _CITED_KEY_RE.findall(text):
            if pmid:
                keys.add(f"PMID:{pmid}")
            elif doi:
                keys.add(f"DOI:{doi.rstrip('.,;)').lower()}")
        return keys

    @staticmethod
    def _endpoint_spans(text: str) -> List[Tuple[int, int, str]]:
        taken: List[Tuple[int, int, str]] = []
        for name, pat in _ENDPOINT_PATTERNS:
            for m in pat.finditer(text):
                if not any(m.start() < e and s < m.end() for s, e, _ in taken):
                    taken.append((m.start(), m.end(), name))
        return sorted(taken)

    @classmethod
    def _endpoint_near(
        cls, text: str, start: int, end: int, spans: Optional[List[Tuple[int, int, str]]] = None
    ) -> Optional[str]:
        """The endpoint a figure belongs to: the nearest one before it in the same clause, else just after it."""
        spans = cls._endpoint_spans(text) if spans is None else spans
        window_start = max(0, start - 70)
        breaks = [m.end() for m in _CLAUSE_BREAK.finditer(text, window_start, start)]
        clause_start = breaks[-1] if breaks else window_start
        before = [sp for sp in spans if sp[1] <= start and sp[0] >= clause_start]
        if before:
            return before[-1][2]
        brk = _CLAUSE_BREAK.search(text, end, min(len(text), end + 45))
        clause_end = brk.start() if brk else min(len(text), end + 45)
        after = [sp for sp in spans if sp[0] >= end and sp[1] <= clause_end]
        return after[0][2] if after else None

    @classmethod
    def _claim_metrics(cls, text: str) -> List[_Metric]:
        """Quantitative figures in a claim: percentages and durations bound to an endpoint, HR, CI, p and n."""
        t = attach_units(_fold_thousands(text))
        t = re.sub(
            r"\bPMID:?\s*\d+|\b10\.\d{4,9}/\S+|\b(?:phase|grade|stage|cycle)\s*[0-9IVX]+[ab]?\b|\bnct\d{8}\b",
            " ", t, flags=re.IGNORECASE,
        )
        out: List[_Metric] = []
        ci_spans: List[Tuple[int, int]] = []
        for m in _CI_RE.finditer(t):
            ci_spans.append(m.span())
            out.append(_Metric("ci", float(m.group(1)), "", None, m.group(1)))
            out.append(_Metric("ci", float(m.group(2)), "", None, m.group(2)))
        for m in _HR_RE.finditer(t):
            out.append(_Metric("hr", float(m.group(1)), "", "hr", m.group(0).strip()))
        for m in _P_RE.finditer(t):
            op = m.group(1)
            raw_val = m.group(2)
            out.append(_Metric("p", _p_value(raw_val), op, "p", m.group(0).strip()))
        for m in _N_RE.finditer(t):
            out.append(_Metric("n", float(m.group(1) or m.group(3)), "", "n", m.group(0).strip()))
            if m.group(2):
                out.append(_Metric("n", float(m.group(2)), "", "n", m.group(2)))
        spans = cls._endpoint_spans(t)
        for m in _PCT_TIME_RE.finditer(t):
            if any(s <= m.start() < e for s, e in ci_spans):
                continue
            if re.match(r"\s*(?:ci\b|confidence)", t[m.end(): m.end() + 20], re.IGNORECASE):
                continue  # "95% CI" is a confidence level, not a result
            val_str = m.group(1).replace("−", "-").replace("+", "")
            unit = _UNIT_FAMILY.get(re.sub(r"\s+", " ", m.group(2).lower()), m.group(2).lower())
            endpoint = cls._endpoint_near(t, m.start(), m.end(), spans)
            out.append(_Metric("pct_time", float(val_str), unit, endpoint, m.group(0).strip()))
        return out

    @classmethod
    def _matches_number(cls, claim_val: float, claim_label: str, doc_val: float, is_pct: bool = False) -> bool:
        if claim_val == doc_val or abs(claim_val - doc_val) <= 1e-9:
            return True
        if (claim_val < 0) != (doc_val < 0) and not is_pct:
            return False
        val_str = f"{claim_val:g}"
        val_m = re.search(rf"\b{re.escape(val_str)}(?:\.(\d+))?\b", str(claim_label or ""))
        if val_m and val_m.group(1):
            d = len(val_m.group(1))
        elif "." in val_str:
            d = len(val_str.split(".")[1])
        else:
            num_m = re.search(r"\d+\.(\d+)", str(claim_label or ""))
            d = len(num_m.group(1)) if num_m else 0
        if round(doc_val, d) == claim_val or abs(doc_val - claim_val) <= 0.55 * (10 ** -d):
            return True
        doc_str = str(doc_val)
        doc_d = len(doc_str.split(".")[1]) if "." in doc_str else 0
        if round(claim_val, doc_d) == doc_val:
            return True
        if is_pct and 0.0 < abs(doc_val) <= 1.0:
            pct_doc = doc_val * 100.0
            if round(pct_doc, d) == claim_val or abs(pct_doc - claim_val) <= 0.55 * (10 ** -d):
                return True
            if abs(abs(pct_doc) - abs(claim_val)) <= 0.55 * (10 ** -d):
                return True
        if is_pct and abs(abs(doc_val) - abs(claim_val)) <= 0.55 * (10 ** -d):
            return True
        return False

    @classmethod
    def _matches_metric_value(
        cls, metric: _Metric, doc_val: float, dm: Any, doc_text: str
    ) -> bool:
        if metric.kind != "p":
            return cls._matches_number(metric.value, metric.label, doc_val, is_pct=(metric.unit == "%"))
        claim_op = metric.unit or "="
        pre_window = doc_text[max(0, dm.start() - 60): dm.start()]
        op_m = re.search(r"\bp\s*(?:-?\s*value)?\s*([<>=≤≥]|<=|>=)?\s*$", pre_window, re.IGNORECASE)
        doc_op = op_m.group(1) if op_m and op_m.group(1) else "="
        if claim_op in ("<", "<=", "≤"):
            if doc_op in ("<", "<=", "≤", "=", "=="):
                return doc_val <= metric.value or cls._matches_number(metric.value, metric.label, doc_val)
            return False
        elif claim_op in (">", ">=", "≥"):
            if doc_op in (">", ">=", "≥", "=", "=="):
                return doc_val >= metric.value or cls._matches_number(metric.value, metric.label, doc_val)
            return False
        else:
            return cls._matches_number(metric.value, metric.label, doc_val)

    @classmethod
    def _metric_in_doc(
        cls, metric: _Metric, doc_text: str, spans: List[Tuple[int, int, str]], structured: bool = False
    ) -> bool:
        """The figure appears in the document with the same unit and endpoint. Structured records (registry and
        tool outputs) keep value, unit and measure title in separate fields, so they are matched within a record
        window instead of by adjacency."""
        if cls._metric_adjacent(metric, doc_text, spans):
            return True
        if not structured:
            return False
        for dm in _DOC_NUMBER_RE.finditer(doc_text):
            value = _p_value(dm.group(0)) if metric.kind == "p" else float(dm.group(1))
            if not cls._matches_metric_value(metric, value, dm, doc_text):
                continue
            window = doc_text[max(0, dm.start() - 1200): dm.end() + 300]
            if metric.kind == "pct_time":
                is_proportion = (metric.unit == "%" and 0.0 < abs(value) <= 1.0)
                if not is_proportion:
                    unit_word = {"%": r"%|percent", "months": r"month", "weeks": r"week", "years": r"year"}.get(metric.unit, re.escape(metric.unit))
                    if not re.search(unit_word, window):
                        continue
                if metric.endpoint and not any(sp[2] == metric.endpoint for sp in cls._endpoint_spans(window)):
                    continue
                return True
            keyword = _STRUCTURED_KEYWORDS.get(metric.kind)
            if keyword is None or keyword.search(window):
                return True
        return False

    @classmethod
    def _metric_adjacent(cls, metric: _Metric, doc_text: str, spans: List[Tuple[int, int, str]]) -> bool:
        if metric.kind == "pct_time":
            for dm in _PCT_TIME_RE.finditer(doc_text):
                val_str = dm.group(1).replace("−", "-").replace("+", "")
                val = float(val_str)
                if not cls._matches_number(metric.value, metric.label, val, is_pct=(metric.unit == "%")):
                    continue
                if _UNIT_FAMILY.get(re.sub(r"\s+", " ", dm.group(2).lower()), dm.group(2).lower()) != metric.unit:
                    continue
                if metric.endpoint is None:
                    return True
                found = cls._endpoint_near(doc_text, dm.start(), dm.end(), spans)
                if found == metric.endpoint:
                    return True
                if found is None:
                    window = doc_text[max(0, dm.start() - 150): dm.end() + 150]
                    if any(sp[2] == metric.endpoint for sp in cls._endpoint_spans(window)):
                        return True
            if metric.unit == "%":
                for dm in _DOC_NUMBER_RE.finditer(doc_text):
                    val = float(dm.group(1))
                    if 0.0 < abs(val) <= 1.0 and cls._matches_number(metric.value, metric.label, val, is_pct=True):
                        found = cls._endpoint_near(doc_text, dm.start(), dm.end(), spans)
                        if found == metric.endpoint:
                            return True
                        if found is None:
                            window = doc_text[max(0, dm.start() - 150): dm.end() + 150]
                            if any(sp[2] == metric.endpoint for sp in cls._endpoint_spans(window)):
                                return True
            return False
        keyword = _METRIC_KEYWORDS.get(metric.kind)
        for dm in _DOC_NUMBER_RE.finditer(doc_text):
            val = _p_value(dm.group(0)) if metric.kind == "p" else float(dm.group(1))
            if not cls._matches_metric_value(metric, val, dm, doc_text):
                continue
            if keyword is None or keyword.search(doc_text[max(0, dm.start() - 60): dm.end() + 30]):
                return True
        return False

    @staticmethod
    def _polarity(text: str) -> List[Tuple[str, Set[str], bool, int]]:
        """(predicate group, object terms, negated, position) for each comparable predicate in the text."""
        out: List[Tuple[str, Set[str], bool, int]] = []
        for group, pat in _PREDICATE_GROUPS:
            for m in pat.finditer(text):
                before = text[max(0, m.start() - 60): m.start()]
                brk = list(re.finditer(r"[;,.]|\b(?:but|whereas|while|however|although)\b", before, re.IGNORECASE))
                if brk:
                    before = before[brk[-1].end():]
                negated = bool(_NEGATOR.search(" ".join(before.split()[-6:])))
                after = text[m.end(): m.end() + 90]
                stop = re.search(r"[;,.]|\b(?:but|whereas|while|however|although)\b", after, re.IGNORECASE)
                if stop:
                    after = after[: stop.start()]
                words = re.findall(r"[a-z0-9][a-z0-9\-]{2,}", after.lower())[:8]
                objects = {
                    _fold(w) for w in words
                    if w not in _FUNCTION_WORDS and w not in {"compared", "versus", "than", "patient", "patients", "treated", "group", "arm"}
                }
                if objects:
                    out.append((group, objects, negated, m.start()))
        return out

    @classmethod
    def _polarity_conflict(
        cls, claim_polarity: List[Tuple[str, Set[str], bool, int]], doc_text: str
    ) -> Optional[Tuple[str, int]]:
        """(reason, position in doc) when the document states the opposite of a claim predicate and never the same."""
        if not claim_polarity:
            return None
        doc_polarity = cls._polarity(doc_text)
        for group, objects, negated, _ in claim_polarity:
            same = False
            opposite: Optional[int] = None
            for d_group, d_objects, d_negated, d_pos in doc_polarity:
                if d_group != group or not (objects & d_objects):
                    continue
                if d_negated == negated:
                    same = True
                elif opposite is None:
                    opposite = d_pos
            if opposite is not None and not same:
                what = " ".join(sorted(objects)[:4])
                if negated:
                    return f"the source reports a positive '{group}' finding for {what}, which the claim denies", opposite
                return f"the source states that '{group}' did not hold for {what} (opposite negation)", opposite
        return None

    def _clinical_verdict(
        self, text: str, cited: Set[str], docs: List[_Doc], table: bool = False, split: bool = True
    ) -> Tuple[str, str, Optional[_Doc]]:
        if not docs:
            return "unsupported", "no literature passages or tool outputs were retrieved", None
        parts = [p.strip() for p in re.split(r";\s+", text) if p.strip()]
        if table and split and len(parts) > 1:
            # A comparison-table row can join cells from different sources (approval from openFDA, HR from a trial,
            # FAERS counts). The whole row is checked first; only when no single source backs it is each cell with a
            # figure or identifier checked on its own, carrying the row subject.
            whole = self._clinical_verdict(text, cited, docs, table=True, split=False)
            if whole[0] == "supported":
                return whole
            subject = text.split(" — ", 1)[0] if " — " in text else ""
            results = []
            for i, part in enumerate(parts):
                cell = part if i == 0 or not subject or part.startswith(subject) else f"{subject} — {part}"
                if self._claim_metrics(cell) or claim_id_tokens(cell) or bare_numbers(cell):
                    results.append(self._clinical_verdict(cell, cited, docs, table=True, split=False))
            if results and all(r[0] == "supported" for r in results):
                return results[0]
            return whole
        if len(parts) > 1 and sum(1 for p in parts if claim_id_tokens(p)) > 1:
            # One row about several records ("Sotorasib NDA 214665, 2021-05-28; Adagrasib NDA 216340, 2022-12-12"):
            # each part must be backed by its own record. The record-free lead-in ("FDA approval (NDA, date) —")
            # goes with the first part.
            results = [self._clinical_verdict(p, cited, docs, table) for p in parts if claim_id_tokens(p)]
            bad = next((r for r in results if r[0] != "supported"), None)
            return bad if bad is not None else results[0]
        s_lower = _fold_thousands(text.lower())
        nct_ids = re.findall(r"\bnct\d{8}\b", s_lower)
        metrics = self._claim_metrics(text)
        # A table-derived claim's figures are bare numbers ("0.571 (0.421-0.774)"): each must appear in the doc.
        bare = bare_numbers(_fold_thousands(text)) if table else []
        terms = {_fold(t) for t in self._salient_terms(re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s_lower))}
        if not terms and not metrics and not bare and not nct_ids:
            return "not_checked", "the claim has no distinctive terms or figures to check against the evidence", None
        needed = min(self.MIN_SHARED_CLAIM_TERMS, len(terms))
        polarity = self._polarity(s_lower)
        # Documents for an identifier the claim names (PMID/DOI cited, or a trial id in the doc key) come first.
        preferred = set(cited) | {f"NCT:{n.upper()}" for n in nct_ids}
        ordered = (
            [d for d in docs if d.key in preferred or any(n in d.key.lower() for n in nct_ids)]
            + [d for d in docs if not (d.key in preferred or any(n in d.key.lower() for n in nct_ids))]
        ) if preferred else list(docs)
        ordered = prefer_named_sources(text, ordered)
        ordered, ineligible_reason = self._eligible_docs(text, ordered)
        if not ordered:
            return "unsupported", ineligible_reason, None

        # The reason names the closest miss: a document with the trial ids but not the figures beats one without the ids.
        best_reason, best_rank = "no passage shares the claim's drug, target, or indication", 0
        contradiction: Optional[Tuple[_Doc, str]] = None
        id_tokens = claim_id_tokens(text)
        if len(id_tokens) > 1 and not any(all(t in re.sub(r"\s+", "", d.text) for t in id_tokens) for d in ordered):
            # "olaparib (first approved 2014-12-19) and niraparib (2017-03-27)": each date sits in its own drug's
            # record. Supported when every identifier is in a record that also shares a term with the claim (the drug).
            backing: List[_Doc] = []
            for tok in id_tokens:
                # The words just before the identifier name its subject ("niraparib (first approved 2017-03-27)").
                at = s_lower.find(tok)
                near = {_fold(t) for t in self._salient_terms(s_lower[max(0, at - 70):at + 10])} - _ID_GENERIC_TERMS
                if not near:
                    break
                hit = next((d for d in ordered if tok in re.sub(r"\s+", "", d.text) and near & _tokens(d.text)), None)
                if hit is None:
                    break
                backing.append(hit)
            if len(backing) == len(id_tokens) and len({d.text for d in backing}) > 1:
                return "supported", "", backing[0]
        for doc in ordered:
            doc_text = attach_units(_fold_thousands(doc.text))
            if id_tokens and not all(t in re.sub(r"\s+", "", doc_text) for t in id_tokens):
                # "NDA 214665, first approved 2021-05-28" is about one record: never support it from another drug's.
                continue
            if any(n not in doc_text for n in nct_ids):
                if best_rank == 0:
                    best_reason = f"trial identifier(s) {', '.join(n.upper() for n in nct_ids)} not found in evidence"
                continue
            # Each application number or date the claim states and this record contains counts as a shared term:
            # "Sotorasib (NDA 214665, 2021-05-28)" matches the sotorasib approval record on the drug plus those ids.
            # Identifiers only add to a shared term (the drug): another drug's record with the same numbers never counts.
            shared = len(terms & _tokens(doc_text))
            if (id_tokens and terms and shared == 0) or shared + len(id_tokens) < needed:
                continue
            conflict = self._polarity_conflict(polarity, doc_text)
            if conflict is not None:
                if contradiction is None:
                    contradiction = (doc, conflict[0])
                continue
            spans = self._endpoint_spans(doc_text)
            structured = not doc.key.startswith(("PMID:", "DOI:", "passage:"))
            missing = [m for m in metrics if not self._metric_in_doc(m, doc_text, spans, structured)]
            if bare:
                doc_numbers = {float(dm.group(1)) for dm in _DOC_NUMBER_RE.finditer(doc_text) if not dm.group(2) and not dm.group(3)}
                listed = {m.value for m in metrics}
                missing += [
                    _Metric("num", v, "", None, f"{v:g}") for v in bare
                    if not any(self._matches_number(v, f"{v:g}", dn) for dn in doc_numbers)
                    and not any(self._matches_number(v, f"{v:g}", lv) for lv in listed)
                ]
            if missing:
                labels = ", ".join(dict.fromkeys(m.label + (f" ({m.endpoint})" if m.endpoint else "") for m in missing))
                if best_rank < 3:
                    best_reason = (
                        f"reported figure(s) {labels} not found with the same endpoint and unit in a passage "
                        "discussing the same subject"
                    )
                    best_rank = 2
                continue
            relation = denominator_mismatch(text, doc_text)
            if relation:
                # The numbers occur, but the source assigns them to another subgroup or arm.
                best_reason, best_rank = f"{RELATION_MISMATCH_PREFIX} {relation}", 3
                continue
            return "supported", "", doc
        if contradiction is not None:
            return "contradicted", contradiction[1], contradiction[0]
        return "unsupported", best_reason, None

    @staticmethod
    def _eligible_docs(text: str, docs: List[_Doc]) -> Tuple[List[_Doc], str]:
        """(documents that may support this clinical claim, reason when none may). An approval or label claim needs
        a regulatory record; a patient-outcome claim cannot rest on a simulation or a target-association score."""
        if is_regulatory_claim(text):
            return [d for d in docs if is_regulatory_doc(d, text)], "no FDA label or approval record in the evidence"
        if _CLINICAL_OUTCOME_RE.search(text):
            return (
                [d for d in docs if not is_non_clinical_doc(d)],
                "no clinical source (literature, trial registry or label) in the evidence; simulation and "
                "target-association outputs do not describe patients",
            )
        return list(docs), ""

    def _mechanistic_verdict(self, text: str, cited: Set[str], docs: List[_Doc]) -> Tuple[str, str, Optional[_Doc]]:
        cited_docs = [d for d in docs if d.key in cited] or _tool_docs_naming(cited, docs)
        if not cited_docs:
            return "not_checked", "the cited source's text was not retrieved in this run", None
        s_lower = text.lower()
        terms = {_fold(t) for t in self._salient_terms(_CITED_KEY_RE.sub(" ", s_lower))}
        needed = min(self.MIN_SHARED_CLAIM_TERMS, len(terms))
        polarity = self._polarity(s_lower)
        contradiction: Optional[Tuple[_Doc, str]] = None
        for doc in cited_docs:
            conflict = self._polarity_conflict(polarity, doc.text)
            if conflict is not None:
                contradiction = contradiction or (doc, conflict[0])
                continue
            if len(terms & _tokens(doc.text)) >= needed:
                return "supported", "", doc
        if contradiction is not None:
            return "contradicted", contradiction[1], contradiction[0]
        return "unsupported", "the cited passage does not discuss the claim's key terms", cited_docs[0]

    @staticmethod
    def _absence_hit(words: List[str], doc_text: str) -> Optional[Tuple[str, int]]:
        """(sentence, position) of the first doc sentence holding every word that is not itself negated or
        hedged ("the role of X remains unknown" does not show that the source discusses X)."""
        folded = [_fold(w) for w in words]
        for s, e in sentence_spans(doc_text):
            sentence = doc_text[s:e]
            toks = _tokens(sentence)
            if all(w in toks for w in folded) and not _PASSAGE_NEGATION_RE.search(sentence):
                return sentence.strip(), s
        return None

    def _absence_verdict(
        self, text: str, cited: Set[str], lit_docs: List[_Doc], docs: List[_Doc]
    ) -> Tuple[str, str, Optional[_Doc], Optional[int]]:
        m = _ABSENCE_PATTERN.search(text)
        if m is None:
            return "not_checked", "no absence statement found", None, None
        verb = next((g for g in m.groups() if g), "")
        literature_wide = m.group("v7") is not None
        obj_text = text[m.end(): m.end() + 120]
        stop = re.search(r"[.;:()\[,]|\b(?:but|and|although|while|whereas|because|since)\b", obj_text, re.IGNORECASE)
        if stop:
            obj_text = obj_text[: stop.start()]
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", obj_text.lower()) if w not in _FUNCTION_WORDS][:6]
        referent = bool(_LITERATURE_REFERENT.search(text))
        wide_reason = "a literature-wide absence cannot be established from one search; say that it was not retrieved in this run"

        if cited:
            targets = [d for d in docs if d.key in cited]
        elif referent or literature_wide:
            targets = list(lit_docs)
        else:
            targets = lit_docs if len({d.key for d in lit_docs}) == 1 else []
        if not targets:
            if literature_wide:
                return "unsupported", wide_reason, None, None
            if cited:
                return "not_checked", "the cited source's text was not retrieved in this run", None, None
            return "not_checked", "no retrieved passage identifies the source this statement is about", None, None

        informative = [d for d in targets if "method" not in d.section.lower()]
        if _MECHANISM_OBJECT.search(obj_text):
            for d in informative:
                found = list(_MECHANISM_CUES.finditer(d.text))
                cues = list(dict.fromkeys(c.group(0).lower().split()[0] for c in found))
                if len(cues) >= 2:
                    return "contradicted", f"the retrieved passage describes a mechanism ({', '.join(cues[:4])})", d, found[0].start()
        elif words:
            scattered: Optional[_Doc] = None
            for d in targets:
                toks = _tokens(d.text)
                if not all(_fold(w) in toks for w in words):
                    continue
                hit = self._absence_hit(words, d.text)
                if hit is None:
                    # The words occur, but never together in one plain (un-negated, un-hedged) sentence.
                    scattered = scattered or d
                    continue
                sentence, pos = hit
                quote = truncate_words(sentence, 180)
                if _MENTION_VERBS.match(verb or "") or literature_wide:
                    return "contradicted", f'the retrieved passage says: "{quote}"', d, pos
                return "unsupported", f'the retrieved passage discusses it: "{quote}"', d, pos
            if scattered is not None:
                return (
                    "not_checked",
                    "the retrieved passage uses these words, but not together in one plain statement, so the absence "
                    "can be neither confirmed nor refuted",
                    scattered,
                    None,
                )
        if literature_wide:
            return "unsupported", wide_reason, targets[0], None
        if not informative:
            return (
                "unsupported",
                "only a Methods excerpt of the source was retrieved, so an article-wide absence cannot be established",
                targets[0],
                None,
            )
        if referent or _SCOPED_ABSENCE.search(text):
            return "supported", "not present in the retrieved text", targets[0], None
        return (
            "unsupported",
            "absence asserted for the whole source from a retrieved excerpt; state that it was not found in the retrieved text",
            targets[0],
            None,
        )

    def _population_mismatch(self, text: str, doc: Optional[_Doc], question_diseases: Set[str]) -> Optional[str]:
        """The human message when a figure is supported only by a source about another disease than the question's:
        the doc names a disease from the lexicon, none of the question's, and the claim does not name the source's
        population itself. Only claims carrying a figure are checked, so prose mentions of other cancers pass.

        Independently of the question, a claim that names its own disease ("in CRC, EGFR-TKI resistance…") cannot rest
        on a study of a different disease that never mentions the claim's: an NSCLC paper does not support a CRC
        statement even when the question covers both cancers."""
        if doc is None:
            return None
        if not doc.key.startswith(("PMID:", "DOI:", "passage:")):
            return self._record_population_mismatch(text, doc)
        claim_diseases = disease_families(text)
        doc_diseases_all = disease_families(doc.text)
        if claim_diseases and doc_diseases_all and not (claim_diseases & doc_diseases_all):
            studied = " and ".join(sorted(doc_diseases_all))
            return f"This finding comes from a {studied} study, but the claim is about {' and '.join(sorted(claim_diseases))}"
        if not question_diseases:
            return None
        if claim_id_tokens(text) and not self._claim_metrics(text):
            return None  # approval numbers and dates are not population-specific figures
        if not (self._claim_metrics(text) or bare_numbers(text)):
            return None
        doc_diseases = disease_families(doc.text)
        if not doc_diseases or doc_diseases & question_diseases:
            return None
        if disease_families(text) & doc_diseases:
            return None  # the claim already says which population the figure comes from

        def first_pos(name: str) -> int:
            for n, pat in _DISEASE_LEXICON:
                if n == name:
                    m = pat.search(doc.text)
                    return m.start() if m else len(doc.text)
            return len(doc.text)

        other = min(sorted(doc_diseases), key=first_pos)
        asked = " or ".join(sorted(question_diseases))
        return f"This figure comes from a {other} study, not {asked}"

    def _operational_claim(self, claims: List[ClaimRecord], text: str, events: List[Tuple[str, str]]) -> None:
        """Record a sentence about the run (a lookup failed, a record was not retrieved) against the run's tool
        outcomes: supported by the matching failed or empty tool call (evidence key "trace:<tool>"), else not checked.
        It never takes a scientific citation as support and never enters the citation map."""
        low = text.lower()
        ids = {i for pair in _CITED_KEY_RE.findall(text) for i in pair if i} | set(re.findall(r"\bnct\d{8}\b", low))
        terms = {_fold(t) for t in self._salient_terms(_CITED_KEY_RE.sub(" ", low))} - {"search", "retriev", "fail", "run"}
        for name, event in events:
            if (ids and any(i.lower() in event for i in ids)) or (not ids and terms & _tokens(event)):
                self._add_claim(claims, "operational", text, "supported", "operational status: matches this run's tool outcome",
                                f"trace:{name}", event)
                return
        self._add_claim(
            claims, "operational", text, "not_checked",
            "operational status: a statement about this run, not a scientific claim; no matching tool outcome was recorded",
        )

    @staticmethod
    def _record_population_mismatch(text: str, doc: _Doc) -> Optional[str]:
        """A typed clinical record (a CIViC evidence item, a cBioPortal study, a label's indication, a registry
        trial's conditions) states the population its fact is about: a claim naming another disease cannot rest on
        it. Only the record's own population fields count, so a label that warns about leukaemia, or a registry entry
        listing other cancers in free text, is never a mismatch; records without such fields are not judged."""
        if not doc.context:
            return None
        claim_diseases = disease_families(text)
        record_diseases = disease_families(doc.context)
        if not claim_diseases or not record_diseases or claim_diseases & record_diseases:
            return None
        return (
            f"This record is about {' and '.join(sorted(record_diseases))}, but the claim is about "
            f"{' and '.join(sorted(claim_diseases))}"
        )

    def _audit_claims(
        self,
        response: str,
        docs: List[_Doc],
        claims: List[ClaimRecord],
        judge: Optional[Callable[[str, List[Dict[str, str]]], Optional[Dict[str, Any]]]] = None,
        question_diseases: Optional[Set[str]] = None,
        tool_events: Optional[List[Tuple[str, str]]] = None,
    ) -> Tuple[List[DefectItem], List[Dict[str, Any]]]:
        """Classify, check and record each material claim; return (defects, claim_support)."""
        defects: List[DefectItem] = []
        support: List[Dict[str, Any]] = []
        lit_docs = [d for d in docs if d.key.startswith(("PMID:", "DOI:", "passage:"))]
        # Figure checks may read a whole article (its passages joined), not only one passage of it.
        clinical_docs = docs + article_unions(docs)
        judge_calls = 0
        for sentence, unit_text, from_table in markdown_claim_units(response):
            plain = unescape_entities(unit_text)
            if _SOURCE_LOCATION_RE.match(plain):
                # "Source: abstract of PMID X; URL: ..." says where evidence is, it asserts nothing; its identifiers
                # are still checked by the identifier audit.
                continue
            if is_operational_claim(plain):
                # Before the hedge filter: "could not be retrieved" reports the run, it is not a hedge.
                self._operational_claim(claims, plain.strip(), tool_events or [])
                continue
            text = self._assertive_text(plain)
            if text is None:
                continue
            regimen = regimen_mismatch(text)
            if regimen:
                defects.append(
                    DefectItem(
                        category="REGIMEN_MISMATCH",
                        severity=DefectSeverity.MAJOR,
                        description=f"{regimen}.",
                        suggested_correction="Describe the regimen as the combination it is, or name the single agent.",
                        claim=truncate_words(text, 240),
                        sentence=sentence,
                    )
                )
            kind = self._classify_claim(text)
            if kind is None:
                continue
            cited = self._cited_keys(sentence) | self._cited_keys(text)
            pos: Optional[int] = None
            if kind == "absence":
                status, reason, doc, pos = self._absence_verdict(text, cited, lit_docs, docs)
            elif kind == "clinical":
                status, reason, doc = self._clinical_verdict(text, cited, clinical_docs, table=from_table)
            else:
                status, reason, doc = self._mechanistic_verdict(text, cited, docs)

            judgeable = status == "supported" or (kind == "mechanistic" and status == "unsupported")
            if judge is not None and judge_calls < MAX_JUDGE_CALLS and judgeable:
                passages = [doc] if doc is not None else [d for d in docs if d.key in cited][:3]
                if passages:
                    judge_calls += 1
                    try:
                        verdict = judge(text, [{"source_key": d.key, "passage": judge_window(d.text, text)} for d in passages]) or {}
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Entailment judge failed: %s", exc)
                        verdict = {}
                    j_status = str(verdict.get("status") or "")
                    j_reason = str(verdict.get("reason") or "")
                    # Figures and identifiers already matched exactly in the record: an inconclusive judge (it reads a
                    # window of a long record) does not overturn that; a contradiction only lowers it to unsupported.
                    figures_matched = status == "supported" and kind == "clinical" and (
                        bool(self._claim_metrics(text)) or bool(claim_id_tokens(text)) or (from_table and bool(bare_numbers(text)))
                    )
                    relation_error = judge_relation_error(verdict) if j_status in ("unsupported", "contradicted") else None
                    if relation_error and figures_matched:
                        # The numbers occur in the record, but the judge names which group they belong to: matching
                        # numbers do not establish which subgroup, arm or denominator owns them.
                        status, reason = "unsupported", f"{RELATION_MISMATCH_PREFIX} semantic check: {relation_error}"
                    elif j_status == "contradicted" and figures_matched:
                        status, reason = "unsupported", f"semantic check disagrees although the figures match: {j_reason}"
                    elif j_status == "contradicted":
                        status, reason = "contradicted", f"semantic check: {j_reason or 'the passage contradicts the claim'}"
                    elif j_status == "unsupported" and status == "supported" and not figures_matched:
                        status, reason = "unsupported", f"semantic check: {j_reason or 'the passage does not entail the claim'}"
                    elif j_status == "supported" and kind == "mechanistic" and status == "unsupported":
                        status, reason = "supported", f"semantic check: {j_reason}" if j_reason else "semantic check"

            mismatch = (
                self._population_mismatch(text, doc, question_diseases or set())
                if status == "supported" and kind != "absence"
                else None
            )
            if mismatch:
                status, reason = "unsupported", f"population mismatch: {mismatch}"

            extra_docs: List[_Doc] = []
            if from_table and status == "supported":
                parts = [p.strip() for p in re.split(r";\s+", text) if p.strip()]
                if len(parts) > 1:
                    subject = text.split(" — ", 1)[0] if " — " in text else ""
                    for part in parts:
                        cell = part if not subject or part.startswith(subject) else f"{subject} — {part}"
                        if self._claim_metrics(cell) or claim_id_tokens(cell) or bare_numbers(cell):
                            st, _, cd = self._clinical_verdict(cell, cited, clinical_docs, table=True, split=False)
                            if st == "supported" and cd is not None and cd not in extra_docs:
                                if doc is None or cd.key != doc.key:
                                    extra_docs.append(cd)

            # A claim citing a retrieved paper that is backed only by another document: true elsewhere, but the
            # attached citation does not support it. Recorded, never silently re-attributed.
            cited_ok: Optional[bool] = None
            cited_mismatch: Optional[str] = None
            if kind == "clinical" and status == "supported" and doc is not None and any(d.key in cited for d in docs):
                cited_ok = doc.key in cited or any(ed.key in cited for ed in extra_docs)
                if not cited_ok:
                    cited_mismatch = f"supported by {doc.key}, not by the cited {', '.join(sorted(cited))}"
                    reason = "; ".join(r for r in (reason, cited_mismatch) if r)

            excerpt = self._excerpt(doc, pos)
            extra_evidence = [
                ClaimEvidence(source_key=ed.key, passage=truncate_words(self._excerpt(ed), 300), passage_id=ed.passage_id)
                for ed in extra_docs
            ] if extra_docs else None
            self._add_claim(
                claims, kind, text, status, reason, doc.key if doc else None, excerpt,
                doc.passage_id if doc else None, extra_evidence=extra_evidence, cited_source_supported=cited_ok
            )
            claim = truncate_words(text, 240)
            if mismatch:
                defects.append(
                    DefectItem(
                        category="POPULATION_MISMATCH",
                        severity=DefectSeverity.MAJOR,
                        description=f"{mismatch} ({doc.key if doc else 'the source'}).",
                        suggested_correction=(
                            "Name the population the figure comes from (for example 'in a colorectal cancer cohort'), "
                            "or remove the figure and state that no data for the question's population was retrieved."
                        ),
                        claim=claim,
                        sentence=sentence,
                    )
                )
            elif status == "supported":
                if cited_mismatch:
                    defects.append(
                        DefectItem(
                            category="CITATION_MISMATCH",
                            severity=DefectSeverity.MINOR,
                            description=f"The cited source does not support this claim; it is {cited_mismatch}.",
                            suggested_correction=f"Cite {doc.key if doc else 'the supporting source'} for this statement, or remove the citation that does not support it.",
                            claim=claim,
                            sentence=sentence,
                        )
                    )
                # claim_support keeps the raw answer sentence (cut at 240 chars) so the citation map can match it.
                if kind == "clinical":
                    if doc is not None:
                        support.append({"sentence": sentence[:240], "evidence_key": doc.key, "passage_id": doc.passage_id})
                    for ed in extra_docs:
                        support.append({"sentence": sentence[:240], "evidence_key": ed.key, "passage_id": ed.passage_id})
            elif status == "contradicted":
                source = doc.key if doc is not None else "the retrieved evidence"
                defects.append(
                    DefectItem(
                        category="CLAIM_CONTRADICTED",
                        severity=DefectSeverity.CRITICAL,
                        description=f'Claim is contradicted by {source}: {reason}. Source excerpt: "{truncate_words(excerpt, 220)}"',
                        suggested_correction="Correct the statement so it matches what the cited source says, or remove it.",
                        claim=claim,
                        sentence=sentence,
                    )
                )
            elif status == "unsupported" and kind == "clinical" and reason.startswith(RELATION_MISMATCH_PREFIX):
                defects.append(
                    DefectItem(
                        category="NUMERIC_RELATION_MISMATCH",
                        severity=DefectSeverity.MAJOR,
                        description=f"The figures occur in the source but belong to another group ({reason[len(RELATION_MISMATCH_PREFIX):].strip()}).",
                        suggested_correction=(
                            "Give each figure with the subgroup, arm, denominator and endpoint the source assigns it to, "
                            "or mark the subgroup's value as unavailable."
                        ),
                        claim=claim,
                        sentence=sentence,
                    )
                )
            elif status == "unsupported" and kind == "clinical":
                defects.append(
                    DefectItem(
                        category="CLINICAL_CLAIM",
                        severity=DefectSeverity.CRITICAL,
                        description=f"Clinical assertion is not supported by any retrieved passage or tool output ({reason}).",
                        suggested_correction="Remove the claim or restate it with the exact figures, trial identifiers, and citation from a retrieved passage; otherwise state that clinical evidence is unavailable.",
                        claim=claim,
                        sentence=sentence,
                    )
                )
            elif status == "unsupported" and kind == "absence":
                defects.append(
                    DefectItem(
                        category="ABSENCE_OVERREACH",
                        severity=DefectSeverity.MINOR,
                        description=f"Absence statement is not established by the retrieved evidence ({reason}).",
                        suggested_correction="Say that it was not found in the retrieved text of this run instead of asserting that the source or the literature lacks it.",
                        claim=claim,
                        sentence=sentence,
                    )
                )
            elif status == "unsupported":
                defects.append(
                    DefectItem(
                        category="CLAIM_SUPPORT",
                        severity=DefectSeverity.MINOR,
                        description=f"Statement attributed to a cited source is not supported by its retrieved passage ({reason}).",
                        suggested_correction="Restate the finding as the cited passage reports it, or qualify it as not confirmed by the retrieved text.",
                        claim=claim,
                        sentence=sentence,
                    )
                )
        return defects, support
