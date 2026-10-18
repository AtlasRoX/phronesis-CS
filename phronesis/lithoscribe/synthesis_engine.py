"""
Scientific Literature Synthesis Engine for Lithoscribe (retrieval and evidence preparation; the answer model
synthesises). Retrieval-augmented design in the spirit of PaperQA2 / OpenScholar:
- Planned EuropePMC searches (from the orchestrator's search plan), or a query decomposition around the question's
  subject terms (a mechanism facet only for mechanistic questions)
- One shared time budget for every search tier, the Crossref check and the full-text fetches; a search still
  running at the deadline is cancelled and reported, and partial results are kept
- Full-text retrieval for open-access publications via PMCID XML parsing (Results/Discussion; Introduction and
  Background, which summarise other work, are never evidence passages)
- Passage reranking with quantitative-readout and results-section prioritisation
- Passages carrying provenance (PMID/DOI, section, study-design grade, integrity status) under stable passage ids
  derived from the article's identity and the passage's position, unique across literature calls
- Per-article study-design grading and retraction / partial-retraction / expression-of-concern checks
  (EuropePMC + Crossref); retracted and withdrawn works and integrity notices are excluded before chunking
- Version-aware de-duplication: a preprint and its journal version are one work, and merged copies pool their
  integrity metadata (the worst status wins)
- Subject anchoring: records that do not mention the requested subject are excluded from the evidence (an
  empty subject-specific result is reported as such, never padded with unrelated literature)
- Limitations (failed searches, Crossref coverage, integrity coverage, excerpted full text) and a provenance
  record (retrieval time, APIs, hit counts, grades, integrity statuses, grading-policy version) for audit
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from pydantic import BaseModel, Field

from ..agents.search_plan import clean_trial_names, disease_relevance_terms, trial_name_query
from ..utils.biocontext import BioContextMCPClient
from ..tools.registry import ErrorType, ToolResult
from ..utils.text import sentence_spans
from .evidence_grading import (
    GRADING_POLICY_VERSION,
    UNSPECIFIED_SETTING,
    EvidenceGrade,
    grade_article,
    is_integrity_notice,
    is_preprint,
    legacy_pub_type,
    legacy_setting,
    pub_types,
    setting_from_text,
)
from .integrity import (
    CROSSREF_WORKS_URL,
    EXCLUDING_STATUSES,
    CrossrefBatchResult,
    CrossrefIntegrityClient,
    GradedArticle,
    IntegrityStatus,
    europepmc_integrity,
    merge_integrity,
    source_names,
)

EUROPEPMC_SEARCH_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class _DeadlineExceeded(Exception):
    """A search still running when the literature time budget ran out (cancelled, reported, never fatal)."""

    def __str__(self) -> str:
        return "deadline exceeded"

logger = logging.getLogger(__name__)

# Instruction and filler words: never searched for and never ranked on ("papers only; do not simulate").
_PROCEDURAL_WORDS = frozenset(
    """
    simulate simulates simulated simulation simulations simulating run runs running calculate calculating compute
    computing computation please seconds second sec secs ms milliseconds minutes minute min hours hour hrs points
    point over for the in of and or a an at to with into from by what which who how why when where show give me
    find list provide return report state tell explain describe summarize summarise papers paper articles article
    publications publication studies only do does did not no don't without any each their its it is are was were be
    been being this that these those there here than then also about using use used per one two three four five six
    seven eight nine ten main primary key study type types finding findings limitation limitations pmid pmids doi
    dois single-cell single cell analysis analyses analyze analyse literature search searches retrieve
    supporting source sources evidence otherwise only if unless can could should would will must may might
    """.split()
)
_STOPWORDS = frozenset(
    """
    the and for are but not you all any can had her was one our out has have his how its may new now old see
    two way who did get let put say she too use that with this from they will would there their what about which
    when your than then them these into some such only also more most other been being does were what's between
    after before during under over within without against among across via per each both very just than
    """.split()
)
# Literal subject codes the user typed (drug codes, hypothetical proteins: ZX999, AZD9291, MRTX1133). Database
# accessions are identifiers, not subjects.
_LITERAL_CODE_RE = re.compile(r"\b[A-Z]{2,}-?\d{2,}[A-Z0-9]*\b")
_ACCESSION_RE = re.compile(r"^(?:BIOMD|MODEL|NCT|PMC|PMID|CHEMBL|GSE|GSM|ENSG|ENST|SRR|SRP|PRJ|EFO|MONDO|HP|DOID)", re.I)
_GROUP_TERM_RE = re.compile(r'TITLE(?:_ABS)?:(?:"([^"]+)"|([A-Za-z0-9]+\*))')
# Quantitative readouts lift a passage's rank; whole words only ("nm" is not inside "environment").
_QUANT_RE = re.compile(r"%|\b(?:ic50|ec50|gi50|kd|ki|nm|µm|um|orr|dcr|pfs|os|hr)\b|\bp\s*[<=]", re.I)
_MECHANISTIC_RE = re.compile(
    r"\b(?:mechanis\w*|pathways?|signal\w*|how does|why does|mediat\w*|regulat\w*|resistance|interact\w*|metabol\w*)\b",
    re.I,
)
# Section kinds. Methods, funding and back-matter are not findings and never reach the evidence.
_SKIP_SECTION_RE = re.compile(
    r"\b(?:methods?|materials|methodology|experimental procedures|statistical analys[ie]s|funding|financial support|"
    r"acknowledge?ments?|author(?:s'?)? contributions?|contributions|competing interests?|conflicts? of interest|"
    r"declarations?|references|abbreviations|data availability|availability of data|code availability|"
    r"supplementary|supporting information|ethics|ethical|consent for publication|disclosures?)\b",
    re.I,
)
_SKIP_SEC_TYPES = frozenset({"methods", "materials|methods", "materials", "supplementary-material", "funding",
                             "acknowledgments", "data-availability", "ethics-statement", "COI-statement"})
_RESULTS_SECTION_RE = re.compile(r"\b(?:results?|findings|conclusions?)\b", re.I)
_DISCUSSION_SECTION_RE = re.compile(r"\bdiscussion\b", re.I)
# Introduction / Background summarise *other* studies; their sentences would be cited under this article's PMID
# and design grade, so they never become evidence passages (LIT-009).
_BACKGROUND_SECTION_RE = re.compile(r"\b(?:introduction|background|literature review|state of the art)\b", re.I)
_BACKGROUND_SEC_TYPES = frozenset({"intro", "introduction", "background"})
_INTRO_SECTION_RE = _BACKGROUND_SECTION_RE


# Inline markup EuropePMC titles and abstracts carry, as tags or as entity-escaped tags ("&lt;sup&gt;"). Only
# known tag names are removed, so a comparison ("p<0.001", "HR > 1") is never mistaken for a tag.
_MARKUP_TAGS = r"(?:i|b|u|em|strong|sup|sub|inf|span|br|p|a|div|font|small|big|italic|bold|sc|underline|h[1-6]|ul|ol|li|mml:\w+|math)"
_MARKUP_RE = re.compile(
    rf"</?{_MARKUP_TAGS}(?:\s[^<>]*)?/?>|&lt;/?{_MARKUP_TAGS}(?:\s(?:(?!&gt;)[^<>])*)?/?&gt;", re.I
)


def _section_sentences(text: str) -> List[str]:
    """Sentences of a full-text section longer than 30 characters: what is chunked into passages and what the
    truncation count measures (the same split, so the two agree)."""
    return [text[s:e].strip() for s, e in sentence_spans(text or "") if len(text[s:e].strip()) > 30]


def _plain_text(text: Any) -> str:
    """Tag-free, entity-decoded, whitespace-normalized text (EuropePMC titles and abstracts carry inline HTML and
    entities). Tags become a space ("KRAS<sup>G12D</sup>" -> "KRAS G12D"); entities are decoded after the tags
    are gone ("&lt;0.001" -> "<0.001"), and a decoded "<" is never stripped again because only markup tag names
    match."""
    t = _MARKUP_RE.sub(" ", str(text or ""))
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


# Conference bundles ("BNOS 2016 Abstracts: Poster Presentations"): many unrelated abstracts under one record.
_BUNDLE_PUBTYPE_RE = re.compile(r"abstract|congress|poster|meeting|proceedings", re.I)
_BUNDLE_TITLE_RE = re.compile(
    r"\bAbstracts?\b.*\b(?:Posters?|Presentations?|Proceedings)\b|\bMeeting Abstracts?\b|\bProceedings of\b", re.I
)


def is_conference_bundle(art: Dict[str, Any]) -> bool:
    """A collection of meeting abstracts rather than one study: no PMID and an abstract/meeting/congress/poster
    publication type, or a title that names an abstract collection."""
    title = _plain_text(art.get("title"))
    if _BUNDLE_TITLE_RE.search(title):
        return True
    if art.get("pmid"):
        return False
    return any(_BUNDLE_PUBTYPE_RE.search(t) for t in pub_types(art)) or bool(re.search(r"\bCongress\b", title))


# Named cancer types, for telling an article about another cancer from one about the question's disease.
_CANCER_FAMILIES: Dict[str, re.Pattern] = {
    name: re.compile(rf"\b(?:{pat})\b", re.I)
    for name, pat in {
        "melanoma": r"melanomas?",
        "lung": r"lung|nsclc|sclc|non-small[\s-]cell",
        "colorectal": r"colorectal|colon|rectal|crc",
        "breast": r"breast",
        "prostate": r"prostate|prostatic|crpc|mcrpc",
        "ovarian": r"ovarian|ovary|fallopian",
        "pancreatic": r"pancreatic|pancreas|pdac",
        "head_neck": r"head\s+and\s+neck|hnscc|oropharyn\w*|nasopharyn\w*|laryn\w*|oral\s+(?:cavity|squamous|cancer)",
        "gastric": r"gastric|stomach|gastro-?o?esophageal",
        "esophageal": r"o?esophageal|o?esophagus",
        "liver": r"hepatocellular|hcc|liver\s+cancer",
        "renal": r"renal\s+cell|kidney\s+cancer|rcc",
        "bladder": r"bladder|urothelial",
        "glioma": r"gliomas?|glioblastomas?|gbm|astrocytomas?",
        "leukemia": r"leuka?emias?|aml|cll",
        "lymphoma": r"lymphomas?|dlbcl",
        "myeloma": r"myelomas?",
        "cervical": r"cervical\s+(?:cancer|carcinoma|neoplasia)|cervix",
        "endometrial": r"endometrial|uterine",
        "thyroid": r"thyroid",
        "sarcoma": r"sarcomas?",
        "mesothelioma": r"mesotheliomas?",
        "biliary": r"cholangiocarcinomas?|biliary\s+tract",
    }.items()
}


def cancer_families(text: str) -> Set[str]:
    """The named cancer types a text mentions ("NSCLC" -> {"lung"})."""
    t = _plain_text(text)
    return {name for name, pat in _CANCER_FAMILIES.items() if pat.search(t)}


def _norm_for_match(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", _plain_text(text).lower()).strip() + " "


def _term_matches(term: str, haystack: str) -> bool:
    """Whole-word, case-insensitive match; 'toxicit*' is a prefix search. haystack is _norm_for_match output."""
    term = term.strip()
    if not term:
        return False
    if term.endswith("*"):
        stem = re.sub(r"[^a-z0-9]+", " ", term[:-1].lower()).strip()
        return bool(stem) and f" {stem}" in haystack
    needle = _norm_for_match(term)
    return needle.strip() != "" and needle in haystack


def normalize_doi(doi: Any) -> str:
    d = str(doi or "").strip().lower()
    d = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", d)
    return d


_MIN_TITLE_KEY_WORDS = 6  # shorter titles ("Editorial", "Reply") are too generic to identify a work


def title_identity_key(art: Dict[str, Any]) -> Optional[str]:
    """'title:<normalised title>|<first author surname>': how a preprint and its journal version (different DOIs,
    no shared id) are recognised as one work. None for titles too short to identify a work."""
    words = re.sub(r"[^a-z0-9]+", " ", _plain_text(art.get("title")).lower()).split()
    if len(words) < _MIN_TITLE_KEY_WORDS:
        return None
    author = re.split(r"[\s,;.]+", str(art.get("authorString") or "").strip().lower())[0] if art.get("authorString") else ""
    return f"title:{' '.join(words)}|{author}"


def article_identity_keys(art: Dict[str, Any]) -> List[str]:
    """Every identifier one work may be listed under across MED, PMC and PPR records, plus its title key."""
    keys = []
    if normalize_doi(art.get("doi")):
        keys.append("doi:" + normalize_doi(art.get("doi")))
    if art.get("pmid"):
        keys.append("pmid:" + str(art["pmid"]).strip())
    if art.get("pmcid"):
        keys.append("pmcid:" + str(art["pmcid"]).strip().upper())
    if not keys:
        keys.append(f"{art.get('source') or ''}:{art.get('id') or art.get('title') or ''}".lower())
    title_key = title_identity_key(art)
    if title_key:
        keys.append(title_key)
    return keys


def _record_rank(art: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Which copy of one work to keep: the MEDLINE record, then a published (non-preprint) one, then one with an
    abstract and indexed publication types."""
    return (
        int(str(art.get("source") or "").upper() == "MED"),
        int(not is_preprint(art)),
        int(bool(art.get("abstractText"))),
        int(bool(pub_types(art))),
    )


def _list_of(container: Any, key: str) -> List[Any]:
    values = (container or {}).get(key) if isinstance(container, dict) else None
    if isinstance(values, (str, dict)):
        values = [values]
    return list(values or [])


def merge_records(kept: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    """One record for two copies of a work (LIT-013). The better-ranked copy is the base; identifiers and the
    abstract are filled from the other; integrity metadata (comment/correction links, 'Retracted Publication') is
    pooled from both, so the worst integrity status of either copy survives the merge. A journal version's
    publication types replace a preprint's ('Preprint' is dropped once a published version is known)."""
    base, extra = (dict(other), kept) if _record_rank(other) > _record_rank(kept) else (dict(kept), other)
    for fld in ("pmid", "doi", "pmcid", "abstractText", "journalTitle", "authorString", "pubYear"):
        if not base.get(fld) and extra.get(fld):
            base[fld] = extra[fld]
    seen_cc: Set[Tuple[str, str, str]] = set()
    pooled_cc: List[Any] = []
    for cc in _list_of(base.get("commentCorrectionList"), "commentCorrection") + _list_of(extra.get("commentCorrectionList"), "commentCorrection"):
        if not isinstance(cc, dict):
            continue
        sig = (str(cc.get("type") or "").lower(), str(cc.get("id") or ""), str(cc.get("reference") or ""))
        if sig not in seen_cc:
            seen_cc.add(sig)
            pooled_cc.append(cc)
    if pooled_cc:
        base["commentCorrectionList"] = {"commentCorrection": pooled_cc}
    types = list(dict.fromkeys(pub_types(base)))
    for t in pub_types(extra):
        if t.lower() == "retracted publication" or (not types and t.lower() != "preprint"):
            if t not in types:
                types.append(t)
    if not is_preprint(base):
        types = [t for t in types if t.lower() != "preprint"] or types
    if types != pub_types(base):
        base["pubTypeList"] = {"pubType": types}
        base["pubType"] = "; ".join(types)
    if not _list_of(base.get("meshHeadingList"), "meshHeading") and _list_of(extra.get("meshHeadingList"), "meshHeading"):
        base["meshHeadingList"] = extra["meshHeadingList"]
    versions = [*(kept.get("_versions") or [_version_of(kept)]), *(other.get("_versions") or [_version_of(other)])]
    base["_versions"] = list({(v["source"], v["id"]): v for v in versions}.values())
    return base


def _version_of(art: Dict[str, Any]) -> Dict[str, str]:
    return {"source": str(art.get("source") or ""), "id": str(art.get("id") or ""), "doi": str(art.get("doi") or "")}


def literal_subject_codes(query: str) -> List[str]:
    return list(dict.fromkeys(
        m.group(0) for m in _LITERAL_CODE_RE.finditer(query or "") if not _ACCESSION_RE.match(m.group(0))
    ))


def content_keywords(text: str) -> List[str]:
    """Lower-case content words for ranking: no stopwords, no instruction words, no bare numbers."""
    words = [w.lower() for w in re.split(r"[^\w-]+", text or "") if w]
    return list(dict.fromkeys(
        w for w in words
        if len(w) > 2 and w not in _STOPWORDS and w not in _PROCEDURAL_WORDS and not re.fullmatch(r"[\d.]+", w)
    ))


class PassageEvidence(BaseModel):
    """Granular passage with full provenance and experimental context."""
    passage_id: str
    pmid: Optional[str] = None
    doi: Optional[str] = None
    pmcid: Optional[str] = None
    title: str
    authors: str
    journal: str
    year: str
    pub_type: str = "research-article"  # clinical-trial | review | meta-analysis | research-article
    # human-clinical | in-vivo | in-vitro | computational | unspecified (no design known: never a default in-vitro)
    experimental_setting: str = UNSPECIFIED_SETTING
    section: str = "Abstract"
    passage_text: str
    relevance_score: float = 0.0
    has_full_text: bool = False
    # Per-article grading and integrity, shared by every passage of the same article
    article_key: str = ""
    pub_types: List[str] = Field(default_factory=list)
    retracted: Optional[bool] = None  # None when no integrity source answered
    citation: str = ""  # citation_label(), serialized for the evidence block and citations
    evidence_grade: Optional[Dict[str, Any]] = None
    grade_label: Optional[str] = None
    integrity_status: str = "unchecked"

    def citation_label(self) -> str:
        lead_author = self.authors.split(",")[0].strip() if self.authors else "Unknown"
        if " " in lead_author:
            lead_author = lead_author.split()[0]
        year_str = self.year or "n.d."
        id_str = f"PMID: {self.pmid}" if self.pmid else (f"DOI: {self.doi}" if self.doi else "EuropePMC")
        return f"[{lead_author} et al., {year_str} | {id_str}]"


class LiteratureSynthesisReport(BaseModel):
    """Structured literature evidence: ranked passages with provenance, grades, integrity and limitations.

    (The former grounded_claims / contradictory_evidence_notes outputs had no consumer and the contradiction
    heuristic could name one passage on both sides; they were removed, LIT-018.)"""
    query: str
    decomposed_subqueries: List[str]
    total_articles_screened: int
    total_passages_extracted: int
    ranked_passages: List[PassageEvidence]
    limitations: List[str] = Field(default_factory=list)
    evidence_sufficiency: str = "SUFFICIENT"  # SUFFICIENT | PARTIAL | INSUFFICIENT
    # Declared after the fields above: memory keeps only the first 250 serialized characters.
    graded_articles: List[GradedArticle] = Field(default_factory=list)
    excluded_articles: List[GradedArticle] = Field(default_factory=list)
    integrity_check: Dict[str, str] = Field(default_factory=dict)
    grade_summary: Dict[str, int] = Field(default_factory=dict)  # study_type -> included articles
    # Records that never mentioned the requested subject: context only, never evidence (titles, no identifiers).
    off_topic_articles: List[Dict[str, str]] = Field(default_factory=list)
    # Audit trail: subject anchors, queries with hit counts, pinned ids, de-duplication, exclusions, and per
    # article the full-text sections available vs supplied to synthesis.
    retrieval: Dict[str, Any] = Field(default_factory=dict)
    # What a later reviewer needs to reconstruct the evidence state this answer saw (LIT-020): retrieval time,
    # APIs, per-query hit counts, Crossref status, per-article grade and integrity, grading-policy version.
    provenance: Dict[str, Any] = Field(default_factory=dict)


class LiteratureSynthesisEngine:
    """
    Engine executing query decomposition, passage ranking, and PaperQA2 synthesis.
    """

    # One time budget for the whole call (LIT-014), below the registry's 40 s tool timeout so the engine always
    # returns what it gathered instead of being cancelled with nothing: every search tier, the Crossref check and
    # the full-text fetches share it. A further search tier starts only with MIN_TIER_S left; RESERVE_S is kept
    # for grading, Crossref and chunking.
    DEADLINE_S = 34.0
    MIN_TIER_S = 6.0
    RESERVE_S = 4.0
    # Full texts are fetched in parallel, each bounded end to end (multi-MB JATS files can stream for 20 s+);
    # an article that misses the bound (or the deadline) uses its abstract.
    FULLTEXT_CONCURRENCY = 8
    FULLTEXT_TIMEOUT_S = 10.0
    MAX_PASSAGES_PER_ARTICLE = 3
    MAX_TOP_PASSAGES = 8
    # Every article in the top passages also contributes its full abstract, so at most one abstract per top passage
    # is added: a paper is never represented only by two-sentence chunks that miss the figures its abstract reports.
    MAX_PASSAGES_WITH_ABSTRACTS = MAX_TOP_PASSAGES * 2
    MAX_SECTION_PARAGRAPHS = 3
    MAX_SECTION_SENTENCES = 12
    MAX_PINNED_IDS = 5
    # Each exact lookup (and its one alternative) is bounded on its own, so a slow upstream cannot hold the call.
    PINNED_TIMEOUT_S = 10.0
    # Linked corrections fetched per pinned record ("Erratum in", "Correction in", ...).
    MAX_LINKED_CORRECTIONS = 2

    def __init__(
        self,
        biocontext_client: Optional[BioContextMCPClient] = None,
        crossref_client: Optional[CrossrefIntegrityClient] = None,
        pubmed_fetcher: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.client = biocontext_client or BioContextMCPClient()
        self.crossref = crossref_client or CrossrefIntegrityClient()
        # The one alternative strategy for a PMID Europe PMC could not return (NCBI efetch). It is the default only
        # with the real Europe PMC client: an injected stand-in client gets no live alternative unless one is given.
        if pubmed_fetcher is None and isinstance(self.client, BioContextMCPClient):
            from .pubmed_fallback import fetch_pubmed_record

            pubmed_fetcher = fetch_pubmed_record
        self.pubmed_fetcher = pubmed_fetcher

    # Words naming what is asked about a subject (outcome, setting, facet), not the subject itself.
    _ASPECT_WORDS = frozenset(
        """
        overall survival benefit benefits efficacy effective effectiveness effect effects outcome outcomes safety
        toxicity toxicities adverse events event risk risks incidence prevalence mortality morbidity response
        responses rate rates treatment treatments therapy therapies trial trials evidence role impact association
        associated prognosis prognostic diagnosis diagnostic management dose dosing compared versus vs comparison
        progression-free disease-free free long-term term first-line second-line line patients patient population
        adults children women men people humans human clinical current recent new latest best standard care
        mechanism mechanisms pathway pathways signaling signalling mediated mediates regulation interaction
        interactions resistance metabolism
        """.split()
    )
    # Records with an abstract only: a title-only record cannot be chunked or graded beyond its title.
    _DECOMPOSED_FILTER = " AND HAS_ABSTRACT:y"

    @classmethod
    def decompose_query(cls, query: str) -> List[str]:
        """
        Decompose a research inquiry into focused EuropePMC searches built around the question's subject terms
        (used when no search plan is supplied). Instruction words (simulate, calculate, run), numbers and units
        are never search terms. Every search keeps the subject terms; a mechanism facet is added only when the
        question asks about mechanism (LIT-015), and every search requires an abstract. Preprints are allowed and
        handled downstream (graded as preprints; merged with their journal version when both are found).
        """
        clean_q = re.sub(r'["\']', '', query).strip()
        subqueries: List[str] = []

        # A model's own publication is fetched by its validated PMID/DOI (pinned_ids), not guessed from free
        # text; the accession itself only finds papers that cite the model.
        bm_match = re.search(r"\b(BIOMD\d{10})\b", clean_q, re.IGNORECASE)
        if bm_match:
            subqueries.append(bm_match.group(1).upper())

        # Instruction words, numbers and units are never search anchors ("papers only", "120 seconds").
        tokens = [w.strip("-") for w in re.split(r"[^\w-]", clean_q) if w]
        bio_terms = list(dict.fromkeys(
            w for w in tokens
            if len(w) > 1 and w.lower() not in _PROCEDURAL_WORDS and w.lower() not in _STOPWORDS
            and not re.fullmatch(r"[\d.]+(?:ms|s|min|h)?", w, re.I) and not re.fullmatch(r"BIOMD\d{10}", w, re.I)
        ))
        subject = [w for w in bio_terms if w.lower() not in cls._ASPECT_WORDS and not _MECHANISTIC_RE.fullmatch(w)]

        if bio_terms:
            subqueries.append(" ".join(bio_terms[:6]) + cls._DECOMPOSED_FILTER)
            if subject:
                # The subject alone, for recall when the aspect words are phrased differently in the literature.
                subqueries.append(" ".join(subject[:4]) + cls._DECOMPOSED_FILTER)
                if _MECHANISTIC_RE.search(clean_q):
                    subqueries.append(
                        f"{' '.join(subject[:3])} AND (mechanism OR pathway OR signaling){cls._DECOMPOSED_FILTER}"
                    )

        # Fallback to cleaned query if nothing extracted
        if not subqueries:
            subqueries.append(clean_q)

        return list(dict.fromkeys(subqueries))[:3]

    @staticmethod
    def _detect_experimental_setting(text: str) -> str:
        return setting_from_text(text)

    @staticmethod
    def _add_article(
        found: Dict[str, Dict[str, Any]], index: Dict[str, str], art: Dict[str, Any], log: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Add one record unless the same work is already present: a shared DOI, PMID or PMCID, or the same
        normalised title and first author (a preprint and its journal version). The two copies are merged
        (merge_records): the better copy is kept, identifiers fill in (a PMC copy contributes its PMCID for full
        text) and integrity metadata is pooled, so a retraction listed on either copy is never lost (LIT-013)."""
        art_id = art.get("id") or art.get("doi") or art.get("title")
        if not art_id or art_id in found:
            return False
        keys = article_identity_keys(art)
        existing = next((index[k] for k in keys if k in index), None)
        if existing is not None:
            kept = found[existing]
            merged = merge_records(kept, art)
            found[existing] = merged
            for k in article_identity_keys(merged) + keys:
                index.setdefault(k, existing)
            if log is not None:
                log["duplicates_merged"] = log.get("duplicates_merged", 0) + 1
                if is_preprint(kept) != is_preprint(art):
                    log.setdefault("versions_linked", []).append(
                        {"kept": _version_of(merged), "merged": [v for v in merged.get("_versions", []) if v["id"] != str(merged.get("id") or "")]}
                    )
            return False
        found[art_id] = art
        for k in keys:
            index[k] = art_id
        return True

    async def _search_all(
        self, calls: List[Tuple[str, int, Any, bool]], deadline: Optional[float]
    ) -> List[Any]:
        """Run EuropePMC searches ((query, limit, sort, pass_sort) each) concurrently, bounded by the deadline.
        Results come back in call order; a search still running at the deadline is cancelled and returned as
        _DeadlineExceeded, a failed one as its exception, so partial results are never lost (LIT-014)."""
        if not calls:
            return []

        def one(query: str, limit: int, sort: Any, pass_sort: bool) -> Any:
            if pass_sort:
                return self.client.async_query_europepmc(query, limit=limit, sort=sort)
            return self.client.async_query_europepmc(query, limit=limit)

        tasks = [asyncio.ensure_future(one(*c)) for c in calls]
        timeout = None if deadline is None else max(0.0, deadline - time.perf_counter())
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        out: List[Any] = []
        for t in tasks:
            if t in pending or t.cancelled():
                out.append(_DeadlineExceeded())
            elif t.exception() is not None:
                out.append(t.exception())
            else:
                out.append(t.result())
        return out

    @staticmethod
    def _log_search(entry: Dict[str, Any], res: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Fill a retrieval-log entry from one search result: (articles, error text or None)."""
        if isinstance(res, BaseException):
            entry["error"] = "deadline exceeded" if isinstance(res, _DeadlineExceeded) else type(res).__name__
            return [], str(res) or type(res).__name__
        if not isinstance(res, dict):
            entry["error"] = "invalid response"
            return [], "invalid response"
        arts = list(res.get("articles") or [])
        entry["hits"] = len(arts)
        if res.get("hit_count") is not None:
            entry["hit_count"] = res.get("hit_count")
        if res.get("api_version"):
            entry["api_version"] = res.get("api_version")
        if res.get("error"):
            entry["error"] = str(res["error"])[:200]
            return arts, str(res["error"])
        return arts, None

    async def _planned_search(
        self, search_queries: List[Dict[str, Any]], max_articles: int, log: Optional[Dict[str, Any]] = None,
        found: Optional[Dict[str, Dict[str, Any]]] = None, index: Optional[Dict[str, str]] = None,
        found_by: Optional[Dict[str, List[str]]] = None, useful: Optional[Callable[[Dict[str, Any]], bool]] = None,
        deadline: Optional[float] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        """Run tier-1 searches together and interleave their results; widen to tier 2 only when too few useful
        records (useful(), e.g. on the question's disease and not a conference bundle) were found. found_by
        records which query labels retrieved each kept record. A query may set its own "limit". With a deadline
        (perf_counter time), a tier still running at it is cut off and a further tier starts only with
        MIN_TIER_S left; skipped tiers are logged."""
        found = {} if found is None else found
        index = {} if index is None else index
        last_error: Optional[str] = None
        per_query = max(3, max_articles // 2 + 1)
        for tier in sorted({int(q.get("tier", 1)) for q in search_queries}):
            batch = [q for q in search_queries if int(q.get("tier", 1)) == tier and q.get("query")]
            if deadline is not None and tier > 1 and deadline - time.perf_counter() < self.MIN_TIER_S:
                for q in batch:
                    if log is not None:
                        log.setdefault("queries", []).append({
                            "query": q["query"], "sort": q.get("sort"), "tier": tier, "label": q.get("label"),
                            "hits": 0, "error": "not run: literature time budget exhausted",
                        })
                last_error = last_error or "literature time budget exhausted"
                continue
            results = await self._search_all(
                [(q["query"], int(q.get("limit") or per_query), q.get("sort"), True) for q in batch], deadline
            )
            lists: List[Tuple[str, List[Dict[str, Any]]]] = []
            for q, res in zip(batch, results):
                entry = {"query": q["query"], "sort": q.get("sort"), "tier": tier, "label": q.get("label"), "hits": 0}
                arts, err = self._log_search(entry, res)
                if err:
                    last_error = err
                    logger.warning("Planned search '%s' failed: %s", q.get("label"), err)
                if not isinstance(res, BaseException):
                    lists.append((str(q.get("label") or ""), arts))
                if log is not None:
                    log.setdefault("queries", []).append(entry)
            # Interleave (landmark 1, recent 1, landmark 2, ...) so both kinds of evidence reach the top.
            for i in range(max((len(x) for _, x in lists), default=0)):
                for label, arts in lists:
                    if i < len(arts):
                        self._add_article(found, index, arts[i], log)
                        if found_by is not None:
                            kept = next((index[k] for k in article_identity_keys(arts[i]) if k in index), None)
                            if kept is not None and label and label not in found_by.setdefault(kept, []):
                                found_by[kept].append(label)
            enough = sum(1 for art in found.values() if useful is None or useful(art))
            if enough >= min(4, max_articles):
                break
        return found, last_error

    async def _fetch_pinned(
        self, pinned_ids: List[str], found: Dict[str, Dict[str, Any]], index: Dict[str, str], log: Dict[str, Any]
    ) -> Tuple[List[str], Optional[str]]:
        """Fetch named publications (a model's own paper, or a paper the question names) by exact identifier before
        any topical search. Europe PMC is asked first; a PMID it cannot return (no hit, error, timeout) gets one
        alternative lookup (NCBI efetch). Each entry of log["pinned"] records the normalized request, the upstream
        status and the outcome: found (with "via"), invalid_identifier or not_retrieved, which is a failed lookup,
        not proof that the paper is absent. Linked corrections ("Erratum in", ...) are fetched and reported."""
        from .identifiers import europepmc_exact_query, normalize_literature_id

        last_error: Optional[str] = None
        lookups: List[Tuple[Dict[str, Any], str]] = []
        for raw in list(dict.fromkeys(i.strip() for i in pinned_ids if i and i.strip()))[: self.MAX_PINNED_IDS]:
            ident, kind = normalize_literature_id(raw)
            query = europepmc_exact_query(ident, kind) if ident else None
            if query is None:
                log.setdefault("pinned", []).append({
                    "id": raw, "status": "invalid_identifier", "note": "not a PMID, PMCID or DOI; not looked up",
                })
                continue
            entry = {"id": raw, "normalized": ident, "kind": kind, "query": query}
            log.setdefault("pinned", []).append(entry)
            lookups.append((entry, query))

        async def bounded(coro: Any) -> Any:
            return await asyncio.wait_for(coro, self.PINNED_TIMEOUT_S)

        results = await asyncio.gather(
            *(bounded(self.client.async_query_europepmc(q, limit=1)) for _, q in lookups), return_exceptions=True
        )
        pinned_keys: List[str] = []
        for (entry, _q), res in zip(lookups, results):
            arts: List[Dict[str, Any]] = []
            if isinstance(res, BaseException):
                entry["upstream"] = "timeout" if isinstance(res, asyncio.TimeoutError) else f"error: {type(res).__name__}"
            elif res.get("error"):
                entry["upstream"] = f"error: {str(res['error'])[:120]}"
            else:
                arts = list(res.get("articles") or [])
                entry["upstream"] = "ok" if arts else "no_hit"
            via = "europepmc"
            if not arts and entry["kind"] == "pmid" and self.pubmed_fetcher is not None:
                alt: Dict[str, Any] = {"source": "ncbi_efetch"}
                try:
                    record = await bounded(self.pubmed_fetcher(entry["normalized"]))
                except Exception as exc:  # noqa: BLE001 - recorded, never raised
                    alt["status"] = "timeout" if isinstance(exc, asyncio.TimeoutError) else "error"
                    alt["error"] = type(exc).__name__
                else:
                    alt["status"] = "found" if record else "no_hit"
                    if record:
                        arts, via = [record], "ncbi_efetch"
                entry["alternative"] = alt
            if not arts:
                entry["status"] = "not_retrieved"
                if entry["upstream"] != "no_hit" or (entry.get("alternative") or {}).get("status") not in (None, "no_hit"):
                    last_error = f"{entry['normalized']}: {entry['upstream']}"
                continue
            entry["status"], entry["via"] = "found", via
            art = arts[0]
            self._add_article(found, index, art, log)
            key = art.get("id") or art.get("doi") or art.get("title")
            if key in found:
                pinned_keys.append(key)
            else:  # merged into an equivalent record already present
                pinned_keys.extend(index[k] for k in article_identity_keys(art) if k in index)
            entry["title"] = _plain_text(art.get("title"))[:200]
            entry["corrections"] = await self._linked_corrections(art, bounded)
        return list(dict.fromkeys(pinned_keys)), last_error

    _CORRECTION_TYPES = ("erratum in", "correction in", "corrigendum in", "corrected and republished in")

    async def _linked_corrections(self, art: Dict[str, Any], bounded: Callable[[Any], Any]) -> List[Dict[str, Any]]:
        """The corrections a record links to, each looked up by its PMID: whether the notice was retrieved and whether
        it carries text. Corrected content is never merged automatically ("applied" is always False)."""
        links = [
            cc for cc in _list_of(art.get("commentCorrectionList"), "commentCorrection")
            if isinstance(cc, dict) and str(cc.get("type") or "").strip().lower() in self._CORRECTION_TYPES
        ][: self.MAX_LINKED_CORRECTIONS]
        out: List[Dict[str, Any]] = []
        for cc in links:
            cid = str(cc.get("id") or "").strip()
            item: Dict[str, Any] = {
                "id": cid, "type": str(cc.get("type") or ""), "reference": str(cc.get("reference") or ""),
                "retrieved": False, "title": "", "content_available": False, "applied": False,
            }
            if re.fullmatch(r"\d{1,9}", cid) and str(cc.get("source") or "MED").upper() == "MED":
                try:
                    res = await bounded(self.client.async_query_europepmc(f"EXT_ID:{cid} AND SRC:MED", limit=1))
                except Exception:  # noqa: BLE001 - an unreachable notice is reported as not retrieved
                    res = None
                notice = ((res or {}).get("articles") or [None])[0] if isinstance(res, dict) else None
                if notice:
                    item["retrieved"] = True
                    item["title"] = _plain_text(notice.get("title"))[:200]
                    item["content_available"] = bool(str(notice.get("abstractText") or "").strip())
            out.append(item)
        return out

    @staticmethod
    def _pinned_limitations(retrieval: Dict[str, Any]) -> List[str]:
        """Plain disclosures of each exact lookup, for the evidence block: what was not retrieved, and the state of
        every linked correction."""
        out: List[str] = []
        for e in retrieval.get("pinned") or []:
            ident = e.get("normalized") or e.get("id")
            label = f"PMID {ident}" if e.get("kind") == "pmid" else str(ident)
            if e.get("status") == "invalid_identifier":
                out.append(f"'{e.get('id')}' is not a valid PMID, PMCID or DOI, so it was not looked up.")
            elif e.get("status") == "not_retrieved":
                alt = e.get("alternative") or {}
                tried = f"Europe PMC ({e.get('upstream')})" + (f" and NCBI PubMed ({alt.get('status')})" if alt else "")
                out.append(
                    f"{label} was not retrieved by exact lookup ({tried}); this is a failed lookup, not proof that the "
                    "paper is absent."
                )
            for c in e.get("corrections") or []:
                ref = f" ({c['reference']})" if c.get("reference") else ""
                if not c.get("retrieved"):
                    state = "the correction notice could not be retrieved, so corrected content was not applied"
                elif not c.get("content_available"):
                    state = "the correction notice was retrieved but carries no abstract or text, so corrected content was not applied"
                else:
                    state = "the correction notice was retrieved; corrected content was not applied automatically, check it before using the figures"
                out.append(f"{label} has a linked {c.get('type', 'correction').lower()} (PMID {c.get('id')}){ref}; {state}.")
        return out

    @staticmethod
    def subject_anchors(
        query: str, search_queries: Optional[List[Dict[str, Any]]] = None, anchors: Optional[List[List[str]]] = None
    ) -> List[List[str]]:
        """Hard subject anchors: each group lists synonyms and every group must appear in a record's title or
        abstract. Taken from the caller, else from the search plan's lead concept, plus literal codes the user
        typed (ZX999). Facets (resistance, safety, pathway) are never anchors, so they relax first."""
        groups: List[List[str]] = [[t for t in g if t.strip()] for g in (anchors or []) if g]
        groups = [g for g in groups if g]
        if not groups and search_queries:
            ordered = sorted((q for q in search_queries if q.get("query")), key=lambda q: int(q.get("tier", 1)))
            lead = next((q for q in ordered if q.get("label") != "words"), None)
            if lead is not None:
                first_clause = str(lead["query"]).split(" AND ")[0]
                terms = [a or b for a, b in _GROUP_TERM_RE.findall(first_clause)]
                if terms:
                    groups.append(list(dict.fromkeys(terms)))
        for code in literal_subject_codes(query):
            if not any(_term_matches(code, _norm_for_match(" ".join(g))) for g in groups):
                groups.append([code])
        return groups

    @staticmethod
    def _matches_anchors(art: Dict[str, Any], groups: List[List[str]]) -> bool:
        haystack = _norm_for_match(f"{art.get('title') or ''} {art.get('abstractText') or ''}")
        return all(any(_term_matches(t, haystack) for t in g) for g in groups)

    @staticmethod
    def _mentions_any(art: Dict[str, Any], terms: List[str], title_only: bool = False) -> bool:
        text = str(art.get("title") or "") if title_only else f"{art.get('title') or ''} {art.get('abstractText') or ''}"
        haystack = _norm_for_match(text)
        return any(_term_matches(t, haystack) for t in terms)

    @staticmethod
    def disease_terms_from(search_queries: Optional[List[Dict[str, Any]]]) -> List[str]:
        """The question's disease names the search plan attached to its queries ("disease")."""
        for q in search_queries or []:
            terms = q.get("disease") if isinstance(q, dict) else None
            if isinstance(terms, list) and terms:
                return [str(t) for t in terms if isinstance(t, str) and t.strip()]
        return []

    @staticmethod
    def topic_from(search_queries: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """The question's aspects and off-topic title terms the search plan attached to its queries ("topic")."""
        for q in search_queries or []:
            topic = q.get("topic") if isinstance(q, dict) else None
            if isinstance(topic, dict) and topic:
                return topic
        return {}

    @classmethod
    def disease_relevance(
        cls, art: Dict[str, Any], relevance_terms: List[str], families: Set[str]
    ) -> Tuple[bool, Optional[str]]:
        """(on_disease, other_cancer). on_disease: the title or abstract names the question's disease (a name,
        a broader form, its distinctive word or organ stem). other_cancer: the title names a different cancer
        type and not the question's, so the article is about another disease (only when the question's disease
        is itself a named cancer type)."""
        on = cls._mentions_any(art, relevance_terms) or bool(
            families and cancer_families(f"{art.get('title') or ''} {art.get('abstractText') or ''}") & families
        )
        if not families:
            return on, None
        title = str(art.get("title") or "")
        named = cancer_families(title)
        if named and not (named & families) and not cls._mentions_any(art, relevance_terms, title_only=True):
            return on, ", ".join(sorted(named))
        return on, None

    async def execute_literature_synthesis(
        self,
        query: str,
        max_articles: int = 8,
        check_integrity: bool = True,
        search_queries: Optional[List[Dict[str, Any]]] = None,
        keywords: Optional[List[str]] = None,
        pinned_ids: Optional[List[str]] = None,
        anchors: Optional[List[List[str]]] = None,
        disease_terms: Optional[List[str]] = None,
        trial_names: Optional[List[str]] = None,
    ) -> ToolResult:
        """
        Execute full PaperQA2/OpenScholar literature synthesis pipeline.
        Fetches actual full-text XML for open-access publications when available.
        check_integrity=False skips the Crossref retraction check (EuropePMC integrity metadata is always used).
        search_queries (from the orchestrator's search plan) replace the query decomposition; keywords rank passages.
        pinned_ids (PMIDs, PMCIDs or DOIs, e.g. a BioModel's own publication) are fetched first by exact identifier
        and always kept. anchors ([[synonym, ...], ...]) are the subject terms every other record must mention;
        when omitted they come from the search plan's lead concept and literal codes in the query.
        disease_terms (default: the "disease" the search plan attached to its queries) are the question's
        diseases: articles that never mention them rank below every article that does, and articles whose title
        is about a different named cancer are excluded. trial_names ("KEYNOTE-716", "CheckMate 76K", "SOLO-1")
        are searched as quoted phrases; articles naming them are guaranteed a place among the ranked articles.
        Conference abstract bundles are excluded, and articles are ranked by relevance (not retrieval order),
        with a minimum number of question keywords, before the max_articles cut. The "topic" the search plan
        attached to its queries (the question's aspects; for a treatment question, the title terms of diagnostic
        and technical papers) lifts articles covering what was asked and ranks off-topic ones lower.
        """
        t0 = time.perf_counter()
        clean_q = re.sub(r'["\']', '', query).strip()
        all_articles: Dict[str, Dict[str, Any]] = {}
        index: Dict[str, str] = {}
        found_by: Dict[str, List[str]] = {}
        last_error = None
        retrieval: Dict[str, Any] = {"queries": []}
        diseases = [t for t in (disease_terms or self.disease_terms_from(search_queries)) if isinstance(t, str) and t.strip()]
        relevance_terms = disease_relevance_terms(diseases) if diseases else []
        our_families = cancer_families(" ".join(diseases)) if diseases else set()
        names = clean_trial_names(trial_names)
        if diseases:
            retrieval["disease_terms"] = diseases
        if names:
            retrieval["trial_names"] = names

        def useful(art: Dict[str, Any]) -> bool:
            if is_conference_bundle(art):
                return False
            return not relevance_terms or self.disease_relevance(art, relevance_terms, our_families)[0]

        # 0. Named publications first, by exact identifier: the caller's pinned_ids and every identifier the query
        #    itself names (a free-text "PMID:39369609" search finds nothing, Europe PMC has no PMID field). The
        #    identifiers are not topical search terms.
        from .identifiers import extract_literature_ids, strip_literature_ids

        question_ids = extract_literature_ids(clean_q)
        pinned_ids = list(dict.fromkeys([*(pinned_ids or []), *question_ids]))
        topical_q = strip_literature_ids(clean_q) if question_ids else clean_q
        if question_ids:
            retrieval["question_identifiers"] = question_ids
        pinned_keys: List[str] = []
        if pinned_ids:
            pinned_keys, pinned_error = await self._fetch_pinned(pinned_ids, all_articles, index, retrieval)
            last_error = pinned_error or last_error

        # 1. Planned searches, or iterative search across decomposed subqueries. Named trials are one more
        #    tier-1 search (never widened away).
        planned = [dict(q) for q in (search_queries or []) if isinstance(q, dict)]
        if names and not any(q.get("label") == "trial_names" for q in planned):
            named_query = trial_name_query(names, diseases)
            if named_query is not None:
                planned.append(named_query)
        if planned:
            _, planned_error = await self._planned_search(
                planned, max_articles, retrieval, all_articles, index, found_by=found_by, useful=useful,
            )
            last_error = planned_error or last_error
        # A query that is only an identifier lookup ("PMID:39369609", "Retrieve PMID ... by exact identifier") has no
        # topic to search for.
        topical = bool(content_keywords(topical_q)) if question_ids else True
        subqueries = [] if search_queries or not topical else self.decompose_query(topical_q)
        for sq in subqueries:
            entry: Dict[str, Any] = {"query": sq, "sort": None, "tier": 1, "label": "decomposed", "hits": 0}
            retrieval["queries"].append(entry)
            try:
                res = await self.client.async_query_europepmc(sq, limit=max(2, max_articles // len(subqueries) + 1))
                if res.get("error"):
                    last_error = res["error"]
                    entry["error"] = str(res["error"])[:200]
                entry["hits"] = len(res.get("articles", []))
                for art in res.get("articles", []):
                    self._add_article(all_articles, index, art, retrieval)
            except Exception as e:
                last_error = str(e)
                entry["error"] = type(e).__name__
                logger.warning("Subquery search failed for '%s': %s", sq, e)

        if len(all_articles) == len(pinned_keys) and topical and not (question_ids and pinned_keys):
            # Broader search on the question's content words only; subject anchors still apply below. Not when
            # the question named its papers and they were retrieved: other papers would not answer it.
            terms = content_keywords(topical_q)
            fallback_q = " ".join(terms[:4])
            if fallback_q:
                entry = {"query": fallback_q, "sort": None, "tier": 9, "label": "fallback", "hits": 0}
                retrieval["queries"].append(entry)
                try:
                    broad_res = await self.client.async_query_europepmc(fallback_q, limit=5)
                    if broad_res.get("error"):
                        last_error = broad_res["error"]
                        entry["error"] = str(broad_res["error"])[:200]
                    entry["hits"] = len(broad_res.get("articles", []))
                    for art in broad_res.get("articles", []):
                        self._add_article(all_articles, index, art, retrieval)
                except Exception as e:
                    last_error = str(e)
                    entry["error"] = type(e).__name__

        # 1b. Subject anchoring: a record that never mentions the requested subject is not evidence for it.
        #     Records found by the pivotal-trial or trial-name searches matched drug, disease and design (or the
        #     trial's name) already, so a lead concept naming only one of several alternative drugs does not apply.
        anchor_groups = self.subject_anchors(topical_q, search_queries, anchors)
        retrieval["anchors"] = anchor_groups
        trial_found = {a for a, labels in found_by.items() if {"pivotal", "trial_names"} & set(labels)}
        off_topic: List[Dict[str, str]] = []

        def exclude(art_id: str, reason: str) -> None:
            art = all_articles.pop(art_id)
            off_topic.append({"title": _plain_text(art.get("title"))[:160], "reason": reason})

        if anchor_groups:
            codes = {c.lower() for c in literal_subject_codes(clean_q)}
            broader = [g for g in anchor_groups if not (len(g) == 1 and g[0].lower() in codes)]
            keep = lambda aid, groups: (  # noqa: E731
                aid in pinned_keys or aid in trial_found or self._matches_anchors(all_articles[aid], groups)
            )
            if (
                broader != anchor_groups
                and all_articles
                and not any(keep(a, anchor_groups) for a in all_articles)
                and any(keep(a, broader) for a in all_articles)
            ):
                # A typed code given as an example ("KRAS G12D inhibitors such as MRTX1133") named in no retrieved
                # record: keep articles on the broader subject rather than returning nothing, and say so.
                retrieval["anchor_relaxed"] = [g[0] for g in anchor_groups if g not in broader]
                anchor_groups = broader
                retrieval["anchors"] = anchor_groups
            for art_id in list(all_articles):
                if keep(art_id, anchor_groups):
                    continue
                exclude(art_id, "does not mention the requested subject")
        # 1c. Conference abstract bundles are not a study; an article about a different named cancer is not
        #     evidence for this one. Other articles that never mention the disease are only ranked lower (step 2).
        off_disease: Set[str] = set()
        for art_id in list(all_articles):
            if art_id in pinned_keys:
                continue
            art = all_articles[art_id]
            if is_conference_bundle(art):
                exclude(art_id, "conference abstract collection, not a single study")
                continue
            if relevance_terms:
                on, other = self.disease_relevance(art, relevance_terms, our_families)
                if other:
                    exclude(art_id, f"about a different cancer type ({other})")
                elif not on:
                    off_disease.add(art_id)
        retrieval["off_topic_excluded"] = len(off_topic)
        retrieval["off_disease_demoted"] = len(off_disease)
        retrieval.setdefault("duplicates_merged", 0)

        # Distinguish upstream outage vs truly empty results. No canned narrative replaces missing evidence.
        if not all_articles:
            elapsed = time.perf_counter() - t0
            subject = " / ".join(g[0] for g in anchor_groups) if anchor_groups else ""
            missed = [e for e in retrieval.get("pinned") or [] if e.get("status") in ("not_retrieved", "invalid_identifier")]
            if missed and not off_topic:
                # A named paper that could not be fetched: say so per identifier, never "no publications exist".
                notes = self._pinned_limitations(retrieval)
                return ToolResult(
                    success=False,
                    tool_name="lithoscribe_literature_synthesis",
                    error=" ".join(notes)[:600] or "The named publications were not retrieved.",
                    error_type=ErrorType.UPSTREAM_API_ERROR if last_error else ErrorType.NOT_FOUND,
                    execution_time_seconds=elapsed,
                    data={"retrieval": retrieval, "ranked_passages": [], "limitations": notes},
                )
            if last_error and not off_topic:
                return ToolResult(
                    success=False,
                    tool_name="lithoscribe_literature_synthesis",
                    error=f"Upstream EuropePMC query failure: {last_error}",
                    error_type=ErrorType.UPSTREAM_API_ERROR,
                    execution_time_seconds=elapsed,
                    data={"retrieval": retrieval, "ranked_passages": []},
                )
            if off_topic:
                message = (
                    f"No retrieved publication mentions the requested subject ({subject}); "
                    f"{len(off_topic)} unrelated record{'s' if len(off_topic) != 1 else ''} excluded. "
                    "This is the result of this search, not proof that no such literature exists."
                )
            else:
                message = f"No peer-reviewed publications found in this search for: '{clean_q}'"
            return ToolResult(
                success=False,
                tool_name="lithoscribe_literature_synthesis",
                error=message,
                error_type=ErrorType.NOT_FOUND,
                execution_time_seconds=elapsed,
                data={"retrieval": retrieval, "ranked_passages": [], "off_topic_articles": off_topic},
            )

        # 2. Grade every article once and check its integrity. Notices and retracted/withdrawn works are
        #    excluded here, so they are never chunked, cited, or remembered.
        assessed: Dict[str, Tuple[Dict[str, Any], EvidenceGrade, IntegrityStatus, bool]] = {
            art_id: (art, grade_article(art), europepmc_integrity(art), is_integrity_notice(art))
            for art_id, art in all_articles.items()
        }
        if keywords:
            keywords = list(dict.fromkeys(k.lower() for k in keywords if k))
        else:
            keywords = content_keywords(query)
        eligible = [
            art_id for art_id in assessed
            if not assessed[art_id][3] and assessed[art_id][2].status not in EXCLUDING_STATUSES
        ]
        topic = self.topic_from(search_queries)
        ranked = self._rank_articles(
            eligible, all_articles, keywords, max_articles, pinned_keys, found_by, off_disease, names, planned, topic
        )
        considered = ranked["considered"]
        for art_id in ranked["below"]:
            off_topic.append({
                "title": _plain_text(all_articles[art_id].get("title"))[:160],
                "reason": "mentions too few of the question's terms (below the minimum relevance)",
            })
        retrieval["ranking"] = {
            "order": "relevance",
            "min_keyword_hits": ranked["min_hits"],
            "below_min_relevance": len(ranked["below"]),
            "reserved_trial_slots": len(ranked["reserved"]),
            "trial_named": len(ranked["named"]),
        }
        if topic:
            retrieval["ranking"]["aspects"] = sorted((topic.get("aspects") or {}).keys())
            retrieval["ranking"]["off_topic_demoted"] = len(ranked["off_topic"])

        async def crossref_check() -> CrossrefBatchResult:
            if not check_integrity:
                return CrossrefBatchResult(status="disabled")
            dois = [art.get("doi") for art, _, _, notice in assessed.values() if not notice]
            try:
                return await self.crossref.async_check_dois(dois)
            except Exception as exc:  # noqa: BLE001
                return CrossrefBatchResult(status="unavailable", error=type(exc).__name__)

        # Crossref runs concurrently with the full-text fetches.
        crossref, sections_by_id = await asyncio.gather(
            crossref_check(), self._collect_sections({a: all_articles[a] for a in considered})
        )

        graded_by_id: Dict[str, GradedArticle] = {}
        excluded: List[GradedArticle] = []
        for art_id, (art, grade, epmc, notice) in assessed.items():
            status = merge_integrity(epmc, crossref, art.get("doi"))
            reason = "integrity_notice" if notice else (status.status if status.status in EXCLUDING_STATUSES else None)
            ga = GradedArticle(
                article_key=self._article_key(art),
                source=str(art.get("source") or ""),
                pmid=art.get("pmid") or None,
                doi=art.get("doi") or None,
                pmcid=art.get("pmcid") or None,
                title=_plain_text(art.get("title")),
                journal=_plain_text(art.get("journalTitle")),
                year=str(art.get("pubYear") or ""),
                grade=grade,
                integrity=status,
                is_integrity_notice=notice,
                excluded_reason=reason,
            )
            graded_by_id[art_id] = ga
            if reason:
                excluded.append(ga)

        # 3. Extract and chunk passages: the complete abstract is always one base passage per article, and
        #    full-text sections (Results/Discussion first for mechanistic questions) add excerpts.
        passages: List[PassageEvidence] = []
        abstracts: Dict[str, PassageEvidence] = {}
        limitations: List[str] = self._pinned_limitations(retrieval) + self._integrity_limitations(crossref, excluded)
        mechanistic = bool(_MECHANISTIC_RE.search(query))
        sections_log: List[Dict[str, Any]] = []
        any_truncated = False

        for art_id in considered:
            ga = graded_by_id[art_id]
            if ga.excluded_reason:
                continue
            art, grade = assessed[art_id][0], assessed[art_id][1]
            pmcid = art.get("pmcid")
            sections_to_process, has_full_text, inventory = sections_by_id.get(art_id, ([], False, {}))
            abstract = _plain_text(art.get("abstractText", ""))
            if inventory:
                any_truncated = any_truncated or bool(inventory.get("truncated"))
            sections_log.append({
                "article_key": ga.article_key,
                "abstract": bool(abstract),
                "available": inventory.get("available", []),
                "supplied": (["Abstract"] if abstract else []) + [name for name, _ in sections_to_process],
                "skipped": inventory.get("skipped", []),
                "truncated": inventory.get("truncated", []),
            })

            if not sections_to_process and not abstract:
                ref = f"PMID: {art['pmid']}" if art.get("pmid") else (f"DOI: {art['doi']}" if art.get("doi") else ga.article_key)
                limitations.append(f"Full text and abstract unavailable for '{(_plain_text(art.get('title')) or 'Unknown')[:50]}' ({ref})")
                continue

            # Design fields are per article, so every passage of a paper carries the same grade.
            pub_type = legacy_pub_type(grade)
            exp_setting = legacy_setting(grade, f"{art.get('title', '')} {art.get('abstractText', '')}")
            grade_dict = grade.model_dump(mode="json")
            integrity_name = ga.integrity.status

            def make_passage(sec_name: str, text: str, score: float) -> PassageEvidence:
                passage = PassageEvidence(
                    passage_id=f"psg_{len(passages) + len(abstracts):03d}",
                    pmid=art.get("pmid") or None,
                    doi=art.get("doi") or None,
                    pmcid=pmcid or None,
                    title=_plain_text(art.get("title")) or "",
                    authors=art.get("authorString") or "",
                    journal=art.get("journalTitle") or "",
                    year=str(art.get("pubYear") or ""),
                    pub_type=pub_type,
                    experimental_setting=exp_setting,
                    section=sec_name,
                    passage_text=text,
                    relevance_score=score,
                    has_full_text=has_full_text,
                    article_key=ga.article_key,
                    pub_types=pub_types(art),
                    retracted=None if integrity_name == "unchecked" else False,
                    evidence_grade=grade_dict,
                    grade_label=grade.label,
                    integrity_status=integrity_name,
                )
                passage.citation = passage.citation_label()
                return passage

            # A named trial's own paper, or a pivotal-trial hit, ranks above incidental mentions.
            art_bonus = ranked["bonus"].get(art_id, 0.0)
            if abstract:
                abstracts[art_id] = make_passage("Abstract", abstract, self._score(abstract, keywords) + 0.5 + art_bonus)
                if art_id in pinned_keys:
                    abstracts[art_id].relevance_score += 2.0

            # Chunk full-text sections into evidence passages
            for sec_name, sec_body in sections_to_process:
                sentences = _section_sentences(sec_body)
                bonus = 0.0
                if mechanistic and _RESULTS_SECTION_RE.search(sec_name):
                    bonus = 1.0
                elif _INTRO_SECTION_RE.search(sec_name):
                    bonus = -0.5  # background describes other work, not this paper's findings
                for i in range(0, min(len(sentences), self.MAX_SECTION_SENTENCES), 2):
                    chunk_text = " ".join(sentences[i : i + 2])
                    passages.append(make_passage(sec_name, chunk_text, self._score(chunk_text, keywords) + bonus + art_bonus))

        if any_truncated:
            limitations.append(
                f"Full-text sections were excerpted (first {self.MAX_SECTION_PARAGRAPHS} paragraphs, at most "
                f"{self.MAX_SECTION_SENTENCES} sentences per section); what an article does not report cannot be "
                "concluded from these excerpts."
            )

        # 4. Rerank passages; an article may contribute at most MAX_PASSAGES_PER_ARTICLE excerpts to the top
        #    MAX_TOP_PASSAGES, and every article that is cited also contributes its complete abstract.
        #    Passages of articles that never mention the question's disease rank below every on-disease passage.
        #    Then passages of diagnostic or technical papers, for a question about treatment.
        demoted_keys = {graded_by_id[a].article_key for a in off_disease if a in graded_by_id}
        off_topic_keys = {graded_by_id[a].article_key for a in ranked["off_topic"] if a in graded_by_id}

        def rank_key(p: PassageEvidence) -> Tuple[bool, bool, float]:
            return (p.article_key in demoted_keys, p.article_key in off_topic_keys, -p.relevance_score)

        candidates = sorted([*passages, *abstracts.values()], key=rank_key)
        per_article: Dict[str, int] = {}
        top_passages: List[PassageEvidence] = []
        for p in candidates:
            if per_article.get(p.article_key, 0) >= self.MAX_PASSAGES_PER_ARTICLE:
                continue
            per_article[p.article_key] = per_article.get(p.article_key, 0) + 1
            top_passages.append(p)
            if len(top_passages) == self.MAX_TOP_PASSAGES:
                break
        abstract_by_key = {p.article_key: p for p in abstracts.values()}
        for key in list(dict.fromkeys(p.article_key for p in top_passages)):
            base = abstract_by_key.get(key)
            if base is None or base in top_passages or len(top_passages) >= self.MAX_PASSAGES_WITH_ABSTRACTS:
                continue
            top_passages.append(base)
        # Articles holding a reserved trial slot (a named trial, a pivotal-trial hit) are represented by their abstract.
        for art_id in ranked["reserved"]:
            base = abstracts.get(art_id)
            if base is not None and base not in top_passages:
                top_passages.append(base)
        top_passages.sort(key=rank_key)
        # Pinned publications (the model's own paper) are always represented.
        for art_id in pinned_keys:
            base = abstracts.get(art_id)
            if base is not None and base not in top_passages:
                top_passages.insert(0, base)

        # 5. Contradiction detection: name the passages that point different ways, never a canned narrative.
        contradictions = []
        positive = [p for p in top_passages if re.search(r"significantly improved|synergistic", p.passage_text, re.I)]
        negative = [p for p in top_passages if re.search(r"\b(resistance|relapse|ineffective)\b", p.passage_text, re.I)]
        if positive and negative:
            contradictions.append(
                f"Retrieved passages point in different directions: {positive[0].citation_label()} reports benefit, "
                f"while {negative[0].citation_label()} reports resistance, relapse or lack of effect."
            )

        sufficiency = "SUFFICIENT" if len(top_passages) >= 3 else ("PARTIAL" if top_passages else "INSUFFICIENT")
        if sufficiency == "INSUFFICIENT":
            limitations.append("Insufficient peer-reviewed passages retrieved to firmly validate all clinical claims without qualification.")

        # 6. Build grounded claims
        grounded_claims = []
        for p in top_passages[:4]:
            grounded_claims.append({
                "assertion": p.passage_text,
                "citation": p.citation_label(),
                "doi": p.doi,
                "pmid": p.pmid,
                "section": p.section,
                "has_full_text": p.has_full_text,
                "setting": p.experimental_setting,
            })

        # Only articles whose passages reach the evidence are listed as graded evidence.
        included = {ga.article_key: ga for ga in graded_by_id.values() if not ga.excluded_reason}
        graded_articles = [included[k] for k in dict.fromkeys(p.article_key for p in top_passages) if k in included]
        grade_summary: Dict[str, int] = {}
        for ga in graded_articles:
            grade_summary[ga.grade.study_type.value] = grade_summary.get(ga.grade.study_type.value, 0) + 1

        report = LiteratureSynthesisReport(
            query=query,
            decomposed_subqueries=subqueries,
            total_articles_screened=len(all_articles),
            total_passages_extracted=len(passages),
            ranked_passages=top_passages,
            grounded_claims=grounded_claims,
            contradictory_evidence_notes=contradictions,
            limitations=limitations,
            evidence_sufficiency=sufficiency,
            graded_articles=graded_articles,
            excluded_articles=excluded,
            integrity_check={"europepmc": "checked", "crossref": crossref.check_label()},
            grade_summary=grade_summary,
            off_topic_articles=off_topic,
            retrieval={**retrieval, "sections": sections_log, "articles_considered": len(considered)},
        )

        elapsed = time.perf_counter() - t0
        pmid_list = list(dict.fromkeys(p.pmid for p in top_passages if p.pmid))
        doi_list = list(dict.fromkeys(p.doi for p in top_passages if p.doi))

        return ToolResult(
            success=True,
            tool_name="lithoscribe_literature_synthesis",
            data=report.model_dump(mode="json"),
            canonical_ids={
                "pmids": ", ".join(pmid_list),
                "dois": ", ".join(doi_list),
            },
            execution_time_seconds=elapsed,
            metadata={"adapter": "DIRECT_REST", "framework": "PaperQA2/OpenScholar"},
        )

    @staticmethod
    def _article_key(art: Dict[str, Any]) -> str:
        source, art_id = str(art.get("source") or ""), str(art.get("id") or "")
        if source and art_id:
            return f"{source}:{art_id}"
        return art_id or str(art.get("doi") or "") or str(art.get("title") or "")[:80]

    # Article ranking before the max_articles cut.
    MIN_KEYWORD_HITS = 2  # question keywords an article must mention (1 when the question has fewer than 4)
    TRIAL_NAME_BONUS = 3.0
    PIVOTAL_BONUS = 1.0
    ASPECT_BONUS = 1.0  # per aspect of the question (repurposing, docking, safety, ...) an article covers
    TOPIC_SEARCH_BONUS = 0.5  # found by a search for one of the question's aspects
    TOPIC_SEARCH_LABELS = frozenset({"therapy", "repurposing", "in_silico"})

    def _rank_articles(
        self,
        eligible: List[str],
        articles: Dict[str, Dict[str, Any]],
        keywords: List[str],
        max_articles: int,
        pinned_keys: List[str],
        found_by: Dict[str, List[str]],
        off_disease: Set[str],
        trial_names: List[str],
        queries: List[Dict[str, Any]],
        topic: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The articles to read, by relevance rather than retrieval order: pinned publications first, then the
        reserved trial slots (articles naming a requested trial, then the best pivotal-trial hits on the
        question's disease), then every other article by (on the disease first, on topic, score, retrieval order).
        Articles mentioning fewer than the minimum number of question keywords are dropped (pinned and trial
        hits excepted).

        topic (the search plan's): "aspects" ({name: terms}) add ASPECT_BONUS for each aspect of the question an
        article covers; "off_topic" title terms (a treatment question's diagnostic and technical words: assay,
        biopsy, cfDNA) mark an article off topic unless its title also has an "on_topic" term (inhibitor,
        therapy, the question's drug); off-topic articles rank after on-topic ones."""
        topic = topic or {}
        aspects = [list(v) for v in (topic.get("aspects") or {}).values() if isinstance(v, list) and v]
        off_terms = [t for t in topic.get("off_topic") or [] if isinstance(t, str) and t.strip()]
        on_terms = [t for t in topic.get("on_topic") or [] if isinstance(t, str) and t.strip()]
        off_topic: Set[str] = set()
        min_hits = (self.MIN_KEYWORD_HITS if len(keywords) >= 4 else 1) if keywords else 0
        order = {a: i for i, a in enumerate(eligible)}
        scores: Dict[str, float] = {}
        hits: Dict[str, int] = {}
        bonus: Dict[str, float] = {}
        named: Set[str] = set()
        for a in eligible:
            art = articles[a]
            text = f"{art.get('title') or ''} {art.get('abstractText') or ''}"
            haystack = _norm_for_match(text)
            hits[a] = sum(1 for kw in keywords if _term_matches(kw, haystack))
            extra = 0.0
            if trial_names and any(_term_matches(n, haystack) for n in trial_names):
                named.add(a)
                extra += self.TRIAL_NAME_BONUS
            if "pivotal" in found_by.get(a, []):
                extra += self.PIVOTAL_BONUS
            if aspects:
                extra += self.ASPECT_BONUS * sum(1 for terms in aspects if any(_term_matches(t, haystack) for t in terms))
            if self.TOPIC_SEARCH_LABELS & set(found_by.get(a, [])):
                extra += self.TOPIC_SEARCH_BONUS
            if off_terms:
                title = _norm_for_match(str(art.get("title") or ""))
                if any(_term_matches(t, title) for t in off_terms) and not any(_term_matches(t, title) for t in on_terms):
                    off_topic.add(a)
            bonus[a] = extra
            scores[a] = self._score(text, keywords) + extra
        pinned = [a for a in dict.fromkeys(pinned_keys) if a in scores]
        trial_hits = [
            a for a in eligible
            if a not in pinned and (a in named or {"pivotal", "trial_names"} & set(found_by.get(a, [])))
        ]
        below = [a for a in eligible if a not in pinned and a not in trial_hits and hits[a] < min_hits]
        if len(below) == len(eligible):
            # Nothing reaches the minimum (the subject anchors already passed): rank all rather than return none.
            below = []
        slots = sum(int(q.get("slots") or 0) for q in queries if q.get("label") in ("pivotal", "trial_names"))
        slots = min(slots, max(1, max_articles // 2), max(0, max_articles - len(pinned)))
        reserved = sorted(
            (a for a in trial_hits if a not in off_disease), key=lambda a: (a not in named, -scores[a], order[a])
        )[:slots]
        rest = sorted(
            (a for a in eligible if a not in pinned and a not in reserved and a not in below),
            key=lambda a: (a in off_disease, a in off_topic, -scores[a], order[a]),
        )
        return {
            "considered": (pinned + reserved + rest)[:max_articles],
            "reserved": reserved,
            "below": below,
            "named": named,
            "bonus": bonus,
            "min_hits": min_hits,
            "off_topic": off_topic,
        }

    @staticmethod
    def _score(text: str, keywords: List[str]) -> float:
        """Whole-word keyword hits, plus a lift for quantitative readouts."""
        haystack = _norm_for_match(text)
        score = sum(1.0 for kw in keywords if _term_matches(kw, haystack))
        if _QUANT_RE.search(text):
            score += 1.5
        return score

    async def _collect_sections(
        self, articles: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Tuple[List[Tuple[str, str]], bool, Dict[str, List[str]]]]:
        """Full-text sections per article id, fetched concurrently for open-access records with a PMCID:
        (supplied sections, has_full_text, inventory of available / skipped / truncated section titles)."""
        sem = asyncio.Semaphore(self.FULLTEXT_CONCURRENCY)

        async def one(art: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], bool, Dict[str, List[str]]]:
            pmcid = art.get("pmcid")
            sections: List[Tuple[str, str]] = []
            inventory: Dict[str, List[str]] = {}
            if not pmcid:
                return sections, False, inventory
            try:
                async with sem:
                    xml_text = await asyncio.wait_for(
                        self.client.async_fetch_europepmc_fulltext(pmcid), timeout=self.FULLTEXT_TIMEOUT_S
                    )
                if xml_text and "<body" in xml_text:
                    # Parsing a multi-MB document takes seconds; keep it off the event loop.
                    sections, inventory = await asyncio.to_thread(self._parse_sections_detail, xml_text)
            except Exception as ft_err:  # noqa: BLE001
                logger.debug("Failed full text parse for %s: %s", pmcid, ft_err)
            return sections, bool(sections), inventory

        ids = list(articles)
        results = await asyncio.gather(*(one(articles[a]) for a in ids))
        return dict(zip(ids, results))

    @staticmethod
    def _parse_sections(xml_text: str) -> List[Tuple[str, str]]:
        """Substantive JATS body sections as (title, first paragraphs)."""
        return LiteratureSynthesisEngine._parse_sections_detail(xml_text)[0]

    @classmethod
    def _parse_sections_detail(cls, xml_text: str) -> Tuple[List[Tuple[str, str]], Dict[str, List[str]]]:
        """Body sections as ("Results > Subsection", text). Each section contributes only its own paragraphs, so
        nested sections are never repeated; Methods, funding and other back matter are skipped; words split by
        inline markup stay separated ("in <italic>vitro</italic>" -> "in vitro")."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)  # JATS XML through the stdlib parser
            soup = BeautifulSoup(xml_text, "html.parser")
        body = soup.find("body") or soup
        sections: List[Tuple[str, str]] = []
        inventory: Dict[str, List[str]] = {"available": [], "skipped": [], "truncated": []}

        def text_of(tag: Any) -> str:
            t = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
            t = re.sub(r"\s+([,.;:!?)\]])", r"\1", t)
            return re.sub(r"([(\[])\s+", r"\1", t).strip()

        def own_title(sec: Any) -> str:
            title_tag = sec.find("title", recursive=False)
            return text_of(title_tag) if title_tag else ""

        for sec in body.find_all("sec"):
            chain = [sec, *sec.find_parents("sec")][::-1]  # outermost first
            titles = [own_title(s) for s in chain]
            name = " > ".join(t for t in titles if t) or "Body"
            inventory["available"].append(name)
            skip = any(
                _SKIP_SECTION_RE.search(t or "") or str(s.get("sec-type") or "").strip() in _SKIP_SEC_TYPES
                for s, t in zip(chain, titles)
            )
            if skip:
                inventory["skipped"].append(name)
                continue
            paragraphs = [text_of(p) for p in sec.find_all("p", recursive=False)]
            paragraphs = [p for p in paragraphs if len(p) > 40]
            if not paragraphs:
                continue
            used = paragraphs[: cls.MAX_SECTION_PARAGRAPHS]
            text = " ".join(used)
            n_sentences = len(_section_sentences(text))
            if len(paragraphs) > len(used) or n_sentences > cls.MAX_SECTION_SENTENCES:
                inventory["truncated"].append(name)
            sections.append((name, text))
        return sections, inventory

    @staticmethod
    def _integrity_limitations(crossref: CrossrefBatchResult, excluded: List[GradedArticle]) -> List[str]:
        notes: List[str] = []
        if crossref.status == "unavailable":
            notes.append(f"Crossref retraction check unavailable ({crossref.error}); integrity based on EuropePMC only.")
        for status in ("retracted", "withdrawn"):
            hits = [ga for ga in excluded if ga.excluded_reason == status]
            if hits:
                sources = source_names(n.model_dump() for ga in hits for n in ga.integrity.notices)
                notes.append(
                    f"{len(hits)} retrieved article{'s' if len(hits) != 1 else ''} excluded: {status}"
                    + (f" ({'; '.join(sources)})." if sources else ".")
                )
        notices = [ga for ga in excluded if ga.excluded_reason == "integrity_notice"]
        if notices:
            notes.append(
                f"{len(notices)} retrieved integrity notice record{'s' if len(notices) != 1 else ''} excluded (a notice is not evidence)."
            )
        return notes
