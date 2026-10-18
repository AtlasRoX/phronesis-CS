"""
Citation map builder (owned by CITATIONS, feature 4): links identifiers cited in the final answer
to the tool outputs and passages that support them. Pure and offline: no network calls.

An identifier becomes a clickable entry only when the answer cites it AND this turn's evidence
(tool data, passages, plugin citation records, verified identifiers) or verified memory contains it.
Identifiers the verifier rejected are listed in `unverified` and render as plain text.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Literal, NamedTuple, Optional, Set, Tuple
from urllib.parse import quote, unquote

from pydantic import BaseModel, Field

from ..phronesis.agents.verifier import ClinicalVerifier, truncate_words
from ..utils.source_authority import record_url, source_authority
from ..sources.base import CitationRecord
from ..tools.registry import ToolResult
from ..utils.text import sentence_spans

logger = logging.getLogger(__name__)

CiteType = Literal[
    "pmid", "doi", "nct", "chembl", "biomodel", "ensembl", "uniprot", "kegg", "clinvar", "rsid", "civic", "geo", "drugbank",
    "url",
]
PassageKind = Literal["literature", "tool_output", "web_snippet", "mention"]


class CitationPassage(BaseModel):
    passage_id: Optional[str] = None
    text: str  # exact excerpt from the tool output (markup stripped), at most 800 chars
    section: Optional[str] = None
    has_full_text: bool = False
    kind: PassageKind = "literature"
    highlight: Optional[str] = None  # exact substring of text, never offsets


class CitationEntry(BaseModel):
    key: str  # 'PMID:42511798' | 'DOI:10.3390/ijms27146455' | 'NCT:NCT03600883' | ...
    id: str
    type: CiteType
    title: Optional[str] = None
    url: str
    source_agent: str
    source_tool: str
    authors: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[str] = None
    pmcid: Optional[str] = None
    pub_types: List[str] = Field(default_factory=list)
    evidence_level: Optional[str] = None
    # Who published a URL source: regulatory | trial_registry | manufacturer | literature | web, from the record's own
    # final URL (utils.source_authority), never from links inside its text
    source_authority: Optional[str] = None
    retracted: Optional[bool] = None  # None = not checked; True only when a source said so
    retraction_notice: Optional[str] = None
    passages: List[CitationPassage] = Field(default_factory=list)
    verified: bool = False
    note: Optional[str] = None


class ClaimSupport(BaseModel):
    key: str
    occurrence: int  # nth occurrence of key in the answer (code blanked), 0-based
    sentence: str
    passage_id: Optional[str] = None
    highlight: Optional[str] = None
    shared_terms: List[str] = Field(default_factory=list)
    grounded_by_verifier: bool = False


class CitationMap(BaseModel):
    version: int = 1
    entries: Dict[str, CitationEntry] = Field(default_factory=dict)
    aliases: Dict[str, str] = Field(default_factory=dict)
    supports: List[ClaimSupport] = Field(default_factory=list)
    unverified: List[str] = Field(default_factory=list)
    truncated: bool = False


# -------------------------------------------------------------
# Keys, validation and link-outs (URLs are built only from validated ids)
# -------------------------------------------------------------

class _KeySpec(NamedTuple):
    type: str
    pattern: "re.Pattern[str]"
    normalize: Callable[[str], str]
    url: Callable[[str], str]


def _doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(doi, safe="/:;()-._")


_KEY_SPECS: Dict[str, _KeySpec] = {
    "PMID": _KeySpec("pmid", re.compile(r"\d{1,9}"), str, lambda i: f"https://pubmed.ncbi.nlm.nih.gov/{i}/"),
    "DOI": _KeySpec("doi", re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+"), str.lower, _doi_url),
    "NCT": _KeySpec("nct", re.compile(r"NCT\d{8}"), str.upper, lambda i: f"https://clinicaltrials.gov/study/{i}"),
    "CHEMBL": _KeySpec(
        "chembl", re.compile(r"CHEMBL\d{1,9}"), str.upper, lambda i: f"https://www.ebi.ac.uk/chembl/explore/compound/{i}"
    ),
    "BIOMD": _KeySpec("biomodel", re.compile(r"BIOMD\d{10}"), str, lambda i: f"https://www.biomodels.org/{i}"),
    "ENSG": _KeySpec("ensembl", re.compile(r"ENSG\d{11}"), str, lambda i: f"https://platform.opentargets.org/target/{i}"),
    "KEGG": _KeySpec("kegg", re.compile(r"hsa\d{5}"), str, lambda i: f"https://www.kegg.jp/entry/{i}"),
    "VCV": _KeySpec(
        "clinvar",
        re.compile(r"VCV\d{9}"),
        lambda s: s.split(".", 1)[0].upper(),
        lambda i: f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{int(i[3:])}/",
    ),
    "RS": _KeySpec("rsid", re.compile(r"rs\d{3,12}"), str.lower, lambda i: f"https://www.ncbi.nlm.nih.gov/snp/{i}"),
    "EID": _KeySpec("civic", re.compile(r"EID\d{1,6}"), str.upper, lambda i: f"https://civicdb.org/evidence/{i[3:]}"),
    "AID": _KeySpec("civic", re.compile(r"AID\d{1,6}"), str.upper, lambda i: f"https://civicdb.org/assertions/{i[3:]}"),
    "GEO": _KeySpec(
        "geo", re.compile(r"GS[EM]\d{1,9}"), str.upper, lambda i: f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={i}"
    ),
    "DRUGBANK": _KeySpec("drugbank", re.compile(r"DB\d{5}"), str.upper, lambda i: f"https://go.drugbank.com/drugs/{i}"),
}

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_CHEMBL_TARGET_URL_RE = re.compile(r"https://www\.ebi\.ac\.uk/chembl/explore/target/CHEMBL\d{1,9}")
_VALID_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#][^\s<>\"'`]*)?", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """URL identity used for keys: no fragment, no trailing slash (the frontend applies the same rule)."""
    return re.sub(r"/+$", "", str(url).strip().split("#", 1)[0])


def make_key(prefix: str, raw_id: Any) -> Optional[str]:
    """Canonical 'PREFIX:id' key, or None when the id does not validate for that prefix."""
    if raw_id is None:
        return None
    value = str(raw_id).strip()
    if not value:
        return None
    if prefix == "URL":
        return f"URL:{normalize_url(value)}" if _VALID_URL_RE.fullmatch(value) else None
    if prefix == "DOI":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.IGNORECASE).rstrip(".,;")
    spec = _KEY_SPECS.get(prefix)
    if spec is None:
        return None
    value = spec.normalize(value)
    return f"{prefix}:{value}" if spec.pattern.fullmatch(value) else None


def split_key(key: str) -> Tuple[str, str]:
    prefix, _, value = key.partition(":")
    return prefix, value


def url_for_key(key: str) -> str:
    prefix, value = split_key(key)
    if prefix == "URL":
        return value
    return _KEY_SPECS[prefix].url(value)


def type_for_key(key: str) -> str:
    prefix, _ = split_key(key)
    return "url" if prefix == "URL" else _KEY_SPECS[prefix].type


# Same identifier grammar as ClinicalVerifier._extract_identifiers and the id families, ASCII word
# boundaries so web/src/lib/citations.ts (JavaScript \b) finds exactly the same matches. _JS_SPACE is
# JavaScript's \s (it includes the no-break spaces models emit in "PMID: 123"), ASCII \s does not.
_JS_SPACE = "[\t\n\v\f\r    -     　﻿]"
_CITE_RE = re.compile(
    r"\b(?i:PMID):?" + _JS_SPACE + r"*(?P<pmid>\d{1,9})\b"
    r"|(?P<doi>\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b)"
    r"|\b(?P<nct>(?i:NCT)\d{8})\b"
    r"|\b(?P<chembl>(?i:CHEMBL)\d{1,9})\b"
    r"|\b(?P<biomd>BIOMD\d{10})\b"
    r"|\b(?P<ensg>ENSG\d{11})\b"
    r"|\b(?P<kegg>hsa\d{5})\b"
    r"|\b(?P<vcv>VCV\d{9})(?:\.\d+)?\b"
    r"|\b(?P<rs>rs\d{3,12})\b"
    r"|\b(?P<civic>(?:EID|AID)\d{1,6})\b"
    r"|\b(?P<geo>GS[EM]\d{1,9})\b"
    r"|\b(?P<drugbank>DB\d{5})\b",
    re.ASCII,
)
_GROUP_PREFIX = {
    "pmid": "PMID", "doi": "DOI", "nct": "NCT", "chembl": "CHEMBL", "biomd": "BIOMD", "ensg": "ENSG",
    "kegg": "KEGG", "vcv": "VCV", "rs": "RS", "geo": "GEO", "drugbank": "DRUGBANK",
}


def _match_key(m: "re.Match[str]") -> Optional[str]:
    group = m.lastgroup
    if group == "civic":
        value = m.group("civic")
        return make_key(value[:3], value)
    if group == "doi":
        return make_key("DOI", m.group("doi").rstrip(".,;)"))
    return make_key(_GROUP_PREFIX[group], m.group(group)) if group else None


def scan_keys(text: str) -> List[Tuple[int, str]]:
    """(position, key) for every identifier in text, in order."""
    out: List[Tuple[int, str]] = []
    for m in _CITE_RE.finditer(text or ""):
        key = _match_key(m)
        if key:
            out.append((m.start(), key))
    return out


_HREF_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d{1,9})/?(?:[?#].*)?$", re.I), "PMID"),
    (re.compile(r"https?://(?:dx\.)?doi\.org/(10\.[^?#]+)$", re.I), "DOI"),
    (re.compile(r"https?://(?:www\.)?clinicaltrials\.gov/(?:study|ct2/show)/(NCT\d{8})/?(?:[?#].*)?$", re.I), "NCT"),
    (re.compile(r"https?://www\.ebi\.ac\.uk/chembl/(?:explore/)?compound(?:_report_card)?/(CHEMBL\d{1,9})/?(?:[?#].*)?$", re.I), "CHEMBL"),
    (re.compile(r"https?://(?:www\.)?(?:biomodels\.org|ebi\.ac\.uk/biomodels)/(BIOMD\d{10})/?(?:[?#].*)?$", re.I), "BIOMD"),
    (re.compile(r"https?://platform\.opentargets\.org/target/(ENSG\d{11})/?(?:[?#].*)?$", re.I), "ENSG"),
]


def href_to_key(href: str) -> Optional[str]:
    """Key for an answer link: a known database link-out maps to its identifier, anything else to URL:."""
    href = str(href or "").strip()
    for pattern, prefix in _HREF_PATTERNS:
        m = pattern.match(href)
        if m:
            value = unquote(m.group(1)) if prefix == "DOI" else m.group(1)
            return make_key(prefix, value)
    return make_key("URL", href)


# -------------------------------------------------------------
# Answer scanning (code blanked, links counted once by their href)
# -------------------------------------------------------------

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[`~]*[ \t]*$|\Z)", re.M | re.S)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_HTML_SKIP_RE = re.compile(r"<(code|pre|kbd|samp|script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HTML_LINK_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']*)[\"'][^>]*>.*?</a\s*>", re.I | re.S)
_MD_LINK_RE = re.compile(
    r"(!?)\[(?:[^\]\n\\]|\\.)*\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_AUTOLINK_RE = re.compile(r"<(https?://[^\s<>]+)>", re.I)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_WWW_RE = re.compile(r"(?<![\w./@-])www\.[A-Za-z0-9-]+\.[^\s<>\"'`]+", re.I)
_TRAILING_URL_PUNCT = "?!.,:*_~;'\""


def _trim_bare_url(url: str) -> str:
    """Mirror GFM literal autolinks: drop trailing punctuation and unbalanced closing parentheses."""
    while url:
        if url[-1] in _TRAILING_URL_PUNCT:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


def _blank(chars: List[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def scan_answer(response: str) -> List[Tuple[int, str]]:
    """(position, key) of every citable identifier the rendered answer shows outside code, in order.
    A link counts once, by its href; the text inside a link is not scanned (the frontend does the same)."""
    chars = list(response or "")
    found: List[Tuple[int, str]] = []

    def blank_all(pattern: "re.Pattern[str]") -> None:
        for m in pattern.finditer("".join(chars)):
            _blank(chars, m.start(), m.end())

    blank_all(_FENCE_RE)
    blank_all(_INLINE_CODE_RE)
    blank_all(_HTML_SKIP_RE)

    for pattern, href_group in ((_HTML_LINK_RE, 1), (_MD_LINK_RE, 2), (_AUTOLINK_RE, 1)):
        for m in pattern.finditer("".join(chars)):
            if pattern is _MD_LINK_RE and m.group(1) == "!":
                _blank(chars, m.start(), m.end())
                continue
            key = href_to_key(m.group(href_group))
            if key:
                found.append((m.start(), key))
            _blank(chars, m.start(), m.end())
    # Remaining raw tags render as elements, so ids and URLs in their attributes (<img src>, title=) never show.
    blank_all(_HTML_TAG_RE)
    for pattern, scheme in ((_URL_RE, ""), (_WWW_RE, "http://")):
        for m in pattern.finditer("".join(chars)):
            url = _trim_bare_url(m.group(0))
            key = href_to_key(scheme + url)
            if key:
                found.append((m.start(), key))
            _blank(chars, m.start(), m.start() + len(url))

    found.extend(scan_keys("".join(chars)))
    found.sort(key=lambda item: item[0])
    return found


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    """Spans of ClinicalVerifier._split_sentences(text) (the same splitter), so sentences compare with claim_support."""
    return sentence_spans(text)


# -------------------------------------------------------------
# Text helpers
# -------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]{1,200}>")
_AGENT_TERMS = {
    "synapta", "vivarix", "lumicell", "lithoscribe", "websurfer", "biodiscovery", "verifier", "phronesis",
    "retrieved", "retrieve", "retrieves", "pmid", "pmids", "doi", "dois", "europepmc", "passage", "passages",
}
MIN_SHARED_TERMS = ClinicalVerifier.MIN_SHARED_CLAIM_TERMS


def clean_text(value: Any) -> str:
    """Display text of an excerpt: HTML entities decoded, tags such as <h4>/<sup> removed, whitespace collapsed."""
    text = html.unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(value: Any) -> Optional[str]:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", _TAG_RE.sub("", text)).strip()
    return text or None


def _norm_sentence(text: str) -> str:
    folded = ClinicalVerifier._normalize_typography(str(text or "")).replace("­", "")
    return re.sub(r"\s+", " ", folded).strip()


def _claim_terms(sentence: str) -> Set[str]:
    stripped = _URL_RE.sub(" ", sentence)
    stripped = _CITE_RE.sub(" ", stripped)
    stripped = re.sub(r"\bPMID\b|\bDOI\b", " ", stripped, flags=re.IGNORECASE)
    return {t for t in ClinicalVerifier._salient_terms(stripped) if t not in _AGENT_TERMS}


def _best_sentence(terms: Set[str], text: str, min_shared: int) -> Tuple[Optional[str], List[str]]:
    best: Optional[str] = None
    best_shared: List[str] = []
    for sentence in ClinicalVerifier._split_sentences(text):
        shared = sorted(terms & ClinicalVerifier._salient_terms(sentence))
        if len(shared) > len(best_shared):
            best, best_shared = sentence, shared
    if len(best_shared) < max(1, min_shared):
        return None, []
    return best, best_shared


def trim_excerpt(text: str, limit: int, highlight: Optional[str] = None) -> str:
    """Cut text to at most `limit` chars at a word boundary (+ ' [...]'), keeping `highlight` when possible."""
    if len(text) <= limit:
        return text
    suffix = " [...]"
    prefix = ""
    body = text
    if highlight:
        idx = text.find(highlight)
        if idx >= 0 and idx + len(highlight) > limit - len(suffix):
            prefix = "[...] "
            body = text[idx:]
            if len(prefix) + len(body) <= limit:
                return prefix + body
    room = max(0, limit - len(prefix) - len(suffix))
    cut = body[:room]
    if len(body) > room and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return prefix + cut.rstrip() + suffix


def _sentence_with(text: str, needle: str) -> Optional[str]:
    for sentence in ClinicalVerifier._split_sentences(text):
        if needle.lower() in sentence.lower():
            return sentence
    return None


_EVIDENCE_BY_STUDY_TYPE = {
    "meta_analysis": "meta-analysis",
    "systematic_review": "systematic-review",
    "indirect_comparison": "indirect-comparison",
    "network_meta_analysis": "network-meta-analysis",
    "rct": "randomized-trial",
    "clinical_trial": "clinical-trial",
    "observational": "observational",
    "case_report": "case-report",
    "review": "review",
    "preclinical_in_vivo": "preclinical-in-vivo",
    "in_vitro": "preclinical-in-vitro",
    "computational": "computational",
    "editorial": "editorial",
    "conference_abstract": "conference-abstract",
    "trial_protocol": "trial-protocol",
    "unclassified": "research-article",  # no design known: the neutral label, never a design
}

# Human clinical-trial publication types, matched exactly: "Clinical Trial Protocol" reports no results and
# "Clinical Trial, Veterinary" is not a human trial (LIT-010).
_CLINICAL_TRIAL_PUBTYPES = frozenset({
    "clinical trial", "clinical trial, phase i", "clinical trial, phase ii", "clinical trial, phase iii",
    "clinical trial, phase iv", "controlled clinical trial", "pragmatic clinical trial",
})


def evidence_level_from_pub_types(pub_types: Iterable[str]) -> Optional[str]:
    """Evidence level from EuropePMC pubTypeList values (exact matches); None when no publication type is known.
    Used only for records without an evidence grade."""
    types = [re.sub(r"[\s_-]+", " ", str(t).strip().lower()) for t in pub_types or [] if str(t).strip()]
    if not types:
        return None
    if any(t in ("clinical trial protocol", "study protocol") for t in types):
        return "trial-protocol"
    if any(t in ("meta analysis", "metaanalysis") for t in types):
        return "meta-analysis"
    if any(t == "systematic review" for t in types):
        return "systematic-review"
    if any(t in ("randomized controlled trial", "randomised controlled trial") for t in types):
        return "randomized-trial"
    if any(t in _CLINICAL_TRIAL_PUBTYPES for t in types):
        return "clinical-trial"
    if any(t == "observational study" for t in types):
        return "observational"
    if any(t in ("case reports", "case report") for t in types):
        return "case-report"
    if any(t in ("review", "review article", "scoping review") for t in types):
        return "review"
    if any(t in ("editorial", "comment", "letter", "news") for t in types):
        return "editorial"
    if any(t == "preprint" for t in types):
        return "preprint"
    return "research-article"


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [s.strip() for s in value.split(";") if s.strip()]
    return []


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# -------------------------------------------------------------
# Builder
# -------------------------------------------------------------

_DEFAULT_AGENTS = (
    ("lithoscribe", "Lithoscribe"),
    ("synapta", "Synapta"),
    ("vivarix", "Vivarix"),
    ("websurfer", "WebSurfer"),
    ("lumicell", "Lumicell"),
)
_RECORD_PREFIX = {
    "pmid": "PMID", "doi": "DOI", "nct": "NCT", "chembl": "CHEMBL", "biomodel": "BIOMD", "ensembl": "ENSG",
    "kegg": "KEGG", "clinvar": "VCV", "rsid": "RS", "geo": "GEO", "drugbank": "DRUGBANK", "url": "URL", "dailymed": "URL",
}
_REGISTRY_TYPES = {"nct", "chembl", "ensembl", "kegg", "clinvar", "rsid", "civic", "geo", "drugbank"}
_HISTORY_PREFIX = {
    "pmids": "PMID", "dois": "DOI", "biomodels": "BIOMD", "ensembl": "ENSG", "nct": "NCT", "chembl": "CHEMBL",
    "clinvar": "VCV", "rsid": "RS", "geo": "GEO", "drugbank": "DRUGBANK",
}
_UNVERIFIED_CATEGORIES = {"IDENTIFIER", "CITATION", "RETRACTED_CITATION"}
MEMORY_NOTE = "Verified in an earlier turn; passage not stored"
VERIFIER_NOTE = "Found in this turn's tool data and confirmed by the Verifier; no passage stored"


class _Builder:
    def __init__(self, source_agents: Dict[str, str]) -> None:
        self.entries: Dict[str, CitationEntry] = {}
        self.aliases: Dict[str, str] = {}
        self.passage_owner: Dict[str, str] = {}
        # Every entry a passage id was added under. Ids from different tool calls may repeat (legacy "psg_000"
        # numbering, or another tool's own ids); an id claimed by two different entries is ambiguous and never
        # grounds a citation by itself (LIT-001).
        self._passage_owners: Dict[str, Set[str]] = {}
        self.source_agents = source_agents
        self._passage_seq = 0

    def owner_of(self, passage_id: Optional[str]) -> Optional[str]:
        """The single entry key a passage id belongs to, or None when unknown or ambiguous."""
        if not passage_id:
            return None
        owners = {self.resolve(o) for o in self._passage_owners.get(str(passage_id), set())}
        return next(iter(owners)) if len(owners) == 1 else None

    def agent_for(self, tool_key: str, fallback: str = "") -> str:
        if tool_key in self.source_agents:
            return self.source_agents[tool_key]
        for prefix, agent in _DEFAULT_AGENTS:
            if tool_key.startswith(prefix):
                return agent
        return fallback or tool_key

    def resolve(self, key: str) -> str:
        return self.aliases.get(key, key)

    def entry(self, key: str, *, source_agent: str, source_tool: str) -> CitationEntry:
        key = self.resolve(key)
        existing = self.entries.get(key)
        if existing is not None:
            return existing
        _, value = split_key(key)
        created = CitationEntry(
            key=key,
            id=value,
            type=type_for_key(key),
            url=url_for_key(key),
            source_agent=source_agent,
            source_tool=source_tool,
        )
        if key.startswith("URL:"):
            created.source_authority = source_authority(value)
        self.entries[key] = created
        return created

    def alias(self, alias_key: Optional[str], primary_key: str) -> None:
        if not alias_key or alias_key == primary_key:
            return
        if alias_key in self.entries and alias_key not in self.aliases:
            # Merge an entry created earlier under the alias into the primary one.
            moved = self.entries.pop(alias_key)
            target = self.entries.get(primary_key)
            if target is None:
                moved.key, moved.id = primary_key, split_key(primary_key)[1]
                moved.type, moved.url = type_for_key(primary_key), url_for_key(primary_key)
                self.entries[primary_key] = moved
            else:
                _fill(target, moved)
                for p in moved.passages:
                    self.add_passage(primary_key, p)
            for pid, owner in list(self.passage_owner.items()):
                if owner == alias_key:
                    self.passage_owner[pid] = primary_key
        self.aliases[alias_key] = primary_key

    def add_passage(self, key: str, passage: CitationPassage) -> None:
        entry = self.entries[self.resolve(key)]
        if not passage.text or any(p.text == passage.text for p in entry.passages):
            return
        if not passage.passage_id:
            self._passage_seq += 1
            passage.passage_id = f"{entry.source_tool}:{self._passage_seq}"
        entry.passages.append(passage)
        self.passage_owner.setdefault(passage.passage_id, entry.key)
        self._passage_owners.setdefault(passage.passage_id, set()).add(entry.key)

    # --- extractors -------------------------------------------------

    def literature(self, tool_key: str, data: Dict[str, Any]) -> List[Tuple[CitationPassage, str]]:
        """PMID/DOI entries merged per article. Returns (passage, owner label) pairs for mention scanning."""
        agent = self.agent_for(tool_key, "Lithoscribe")
        integrity = _integrity_index(data)
        texts: List[Tuple[CitationPassage, str]] = []
        for p in data.get("ranked_passages") or []:
            if not isinstance(p, dict):
                continue
            pmid_key = make_key("PMID", p.get("pmid"))  # preprint ids such as 'PPR1337147' do not validate
            doi_key = make_key("DOI", p.get("doi"))
            key = pmid_key or doi_key
            text = clean_text(p.get("passage_text"))
            passage = CitationPassage(
                passage_id=str(p["passage_id"]) if p.get("passage_id") else None,
                text=text,
                section=str(p.get("section")) if p.get("section") else None,
                has_full_text=bool(p.get("has_full_text")),
                kind="literature",
            )
            if key is None:
                if text:
                    texts.append((passage, "a retrieved passage"))
                continue
            entry = self.entry(key, source_agent=agent, source_tool=tool_key)
            if pmid_key and doi_key:
                self.alias(doi_key, pmid_key)
                entry = self.entries[pmid_key]
            entry.title = entry.title or clean_title(p.get("title"))
            entry.authors = entry.authors or (str(p.get("authors")).strip() or None if p.get("authors") else None)
            entry.journal = entry.journal or (str(p.get("journal")).strip() or None if p.get("journal") else None)
            year = str(p.get("year") or "").strip()
            entry.year = entry.year or (year if re.fullmatch(r"\d{4}", year) else None)
            entry.pmcid = entry.pmcid or (str(p.get("pmcid")) if re.fullmatch(r"PMC\d+", str(p.get("pmcid") or "")) else None)
            pub_types = _as_list(p.get("pub_types"))
            if pub_types and not entry.pub_types:
                entry.pub_types = pub_types
            level = _literature_level(p, pub_types)
            if level and (entry.evidence_level in (None, "research-article")):
                entry.evidence_level = level
            retracted, notice = _literature_retraction(p, pub_types, integrity)
            if retracted:
                entry.retracted = True
                entry.retraction_notice = entry.retraction_notice or notice
            self.add_passage(entry.key, passage)
            if text:
                label = f"PMID {split_key(pmid_key)[1]}" if pmid_key else f"DOI {split_key(doi_key)[1]}"
                texts.append((passage, label))
        return texts

    def synapta(self, tool_key: str, res: ToolResult) -> None:
        data = res.data or {}
        agent = self.agent_for(tool_key, "Synapta")
        hits = [h for h in data.get("search_hits") or [] if isinstance(h, dict)]
        first = hits[0] if hits else {}
        key = make_key("ENSG", first.get("id")) or make_key("ENSG", (res.canonical_ids or {}).get("ensembl_id"))
        symbol = str((res.canonical_ids or {}).get("target_symbol") or first.get("name") or "").strip()
        if key:
            entry = self.entry(key, source_agent=agent, source_tool=tool_key)
            name = str(first.get("name") or "").strip() if make_key("ENSG", first.get("id")) == key else symbol
            entry.title = entry.title or (name or None)
            entry.evidence_level = entry.evidence_level or "database-record"
            parts = [f"Open Targets target {name or split_key(key)[1]} ({split_key(key)[1]})."]
            assoc = []
            for a in data.get("associations") or []:
                if not isinstance(a, dict) or not a.get("disease_name"):
                    continue
                score = a.get("overall_association_score")
                if isinstance(score, (int, float)):
                    assoc.append(f"{a['disease_name']} (association score {round(float(score), 3)})")
                else:
                    assoc.append(str(a["disease_name"]))
            if assoc:
                parts.append("Top disease associations: " + "; ".join(assoc[:5]) + ".")
            self.add_passage(key, CitationPassage(text=" ".join(parts), section="Open Targets", kind="tool_output"))
        for pw in data.get("pathways") or []:
            if not isinstance(pw, dict):
                continue
            pw_key = make_key("KEGG", pw.get("id"))
            if not pw_key:
                continue
            entry = self.entry(pw_key, source_agent=agent, source_tool=tool_key)
            desc = clean_title(pw.get("description") or pw.get("name"))
            entry.title = entry.title or desc
            entry.evidence_level = entry.evidence_level or "database-record"
            text = f"KEGG pathway {split_key(pw_key)[1]}" + (f": {desc}" if desc else "") + "."
            if symbol:
                text += f" Returned by the KEGG pathway lookup for {symbol}."
            self.add_passage(pw_key, CitationPassage(text=text, section="KEGG", kind="tool_output"))

    def vivarix(self, tool_key: str, data: Dict[str, Any]) -> None:
        key = make_key("BIOMD", data.get("model_id"))
        if not key:
            return
        model_id = split_key(key)[1]
        entry = self.entry(key, source_agent=self.agent_for(tool_key, "Vivarix"), source_tool=tool_key)
        name = clean_title(data.get("model_name"))
        entry.title = entry.title or name
        entry.evidence_level = entry.evidence_level or "simulation"
        sim_type = str(data.get("simulation_type") or "").upper()
        if sim_type == "STEADY_STATE":
            status = data.get("steady_state_status")
            text = f"Vivarix computed the steady state of {name or model_id} ({model_id})"
            text += f"; COPASI status {status}." if status else "."
        else:
            text = f"Vivarix simulated {name or model_id} ({model_id})"
            if sim_type == "TIME_COURSE":
                text += " as a time course"
            duration = data.get("duration")
            if isinstance(duration, (int, float)):
                unit = str((data.get("units") or {}).get("time") or "").strip()
                text += f" over {duration:g}{(' ' + unit) if unit else ''}"
            if data.get("solver_method"):
                text += f" with {data['solver_method']}"
            text += "."
            convergence = (data.get("diagnostics") or {}).get("convergence_status")
            if convergence:
                text += f" Convergence status: {convergence}."
        self.add_passage(key, CitationPassage(text=text, section="Simulation", kind="tool_output"))

    def websurfer(self, tool_key: str, data: Dict[str, Any]) -> None:
        agent = self.agent_for(tool_key, "WebSurfer")
        items = [r for r in data.get("results") or [] if isinstance(r, dict)]
        top = data.get("top_page")
        if isinstance(top, dict) and not top.get("error"):
            # Where the fetch ended after redirects is the page's address and its publisher.
            items.append({"url": record_url(top), "title": top.get("title"), "snippet": top.get("content")})
        for r in items:
            url = record_url(r)
            key = make_key("URL", url)
            if not key:
                continue
            is_new = key not in self.entries
            entry = self.entry(key, source_agent=agent, source_tool=tool_key)
            if is_new:
                entry.url = str(url).strip()
                entry.source_authority = source_authority(entry.url)
            entry.title = entry.title or clean_title(r.get("title"))
            entry.evidence_level = entry.evidence_level or "web"
            snippet = clean_text(r.get("snippet") or r.get("content"))
            if snippet and snippet != "No readable text content extracted.":
                self.add_passage(key, CitationPassage(text=snippet, section="Web", kind="web_snippet"))

    def record(self, tool_key: str, rec: Any) -> None:
        if isinstance(rec, dict):
            try:
                rec = CitationRecord(**rec)
            except Exception:  # noqa: BLE001
                return
        if not isinstance(rec, CitationRecord):
            return
        typ = str(rec.type or "").strip().lower()
        if typ == "civic":
            raw = str(rec.id or "").strip().upper()
            key = make_key(raw[:3], raw) if raw[:3] in ("EID", "AID") else make_key("EID", f"EID{raw}")
        elif typ in ("url", "dailymed"):
            key = make_key("URL", rec.url or rec.id)
        elif typ in _RECORD_PREFIX:
            key = make_key(_RECORD_PREFIX[typ], rec.id)
        else:
            key = None
        if not key:
            return
        source_tool = rec.source_tool or tool_key
        is_new = self.resolve(key) not in self.entries
        entry = self.entry(key, source_agent=rec.source_agent or self.agent_for(tool_key), source_tool=source_tool)
        if is_new and split_key(key)[0] == "URL" and rec.url and _VALID_URL_RE.fullmatch(str(rec.url).strip()):
            entry.url = str(rec.url).strip()
        elif typ == "chembl" and _CHEMBL_TARGET_URL_RE.fullmatch(str(rec.url or "").strip()) and str(rec.url).strip().endswith(entry.id):
            # A ChEMBL id is a compound by default; the tool knows when it is a target.
            entry.url = str(rec.url).strip()
        entry.title = entry.title or clean_title(rec.title)
        entry.authors = entry.authors or (rec.authors or None)
        entry.journal = entry.journal or (rec.journal or None)
        entry.year = entry.year or (rec.year or None)
        default_level = "web" if typ in ("url", "dailymed") else ("database-record" if typ in _REGISTRY_TYPES else None)
        entry.evidence_level = entry.evidence_level or rec.evidence_level or default_level
        if rec.retracted is True:
            entry.retracted = True
        text = clean_text(rec.passage)
        if text:
            kind: PassageKind = "literature" if typ in ("pmid", "doi") else ("web_snippet" if typ == "url" else "tool_output")
            self.add_passage(key, CitationPassage(text=text, section=rec.section or None, kind=kind))

    def mentions(self, tool_key: str, passages: List[Tuple[CitationPassage, str]]) -> None:
        agent = self.agent_for(tool_key, "Lithoscribe")
        for passage, label in passages:
            for _, key in scan_keys(passage.text):
                if split_key(key)[0] not in ("NCT", "CHEMBL"):
                    continue
                is_new = self.resolve(key) not in self.entries
                entry = self.entry(key, source_agent=agent, source_tool=tool_key)
                if is_new:
                    entry.note = f"Mentioned in passage from {label}"
                    entry.evidence_level = "database-record"
                mention = CitationPassage(
                    passage_id=None,
                    text=passage.text,
                    section=passage.section,
                    has_full_text=passage.has_full_text,
                    kind="mention",
                    highlight=_sentence_with(passage.text, split_key(key)[1]),
                )
                if passage.passage_id:
                    mention.passage_id = f"{passage.passage_id}:{split_key(key)[1]}"
                self.add_passage(key, mention)


def _literature_level(p: Dict[str, Any], pub_types: List[str]) -> Optional[str]:
    """The article's evidence grade decides its level; raw publication types only for ungraded records, so the
    chip never contradicts the grade (a protocol is never shown as a clinical trial; LIT-010)."""
    grade = p.get("evidence_grade") if isinstance(p.get("evidence_grade"), dict) else {}
    if grade.get("study_type"):
        level: Optional[str] = _EVIDENCE_BY_STUDY_TYPE.get(str(grade.get("study_type")).lower(), "research-article")
    else:
        level = evidence_level_from_pub_types(pub_types)
    if grade.get("is_preprint") and level in (None, "research-article"):
        level = "preprint"
    if level is None:
        legacy = str(p.get("pub_type") or "").strip().lower()
        level = legacy if legacy in ("clinical-trial", "review", "meta-analysis", "research-article") else None
    return level


def _integrity_index(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Graded/excluded articles (grading feature) indexed by PMID: / DOI: / article_key."""
    index: Dict[str, Dict[str, Any]] = {}
    for art in [*(data.get("graded_articles") or []), *(data.get("excluded_articles") or [])]:
        if not isinstance(art, dict):
            continue
        for k in (make_key("PMID", art.get("pmid")), make_key("DOI", art.get("doi")), art.get("article_key")):
            if k:
                index.setdefault(str(k), art)
    return index


def _literature_retraction(
    p: Dict[str, Any], pub_types: List[str], integrity: Dict[str, Dict[str, Any]]
) -> Tuple[bool, Optional[str]]:
    retracted = (
        p.get("retracted") is True
        or any(t.strip().lower() == "retracted publication" for t in pub_types)
        or str(p.get("integrity_status") or "").lower() in ("retracted", "withdrawn")
    )
    notice: Optional[str] = str(p["retraction_notice"]) if p.get("retraction_notice") else None
    art = None
    for k in (p.get("article_key"), make_key("PMID", p.get("pmid")), make_key("DOI", p.get("doi"))):
        if k and str(k) in integrity:
            art = integrity[str(k)]
            break
    status = _get(art, "integrity") if art else None
    if isinstance(status, dict):
        if str(status.get("status") or "").lower() in ("retracted", "withdrawn"):
            retracted = True
        for n in status.get("notices") or []:
            if not isinstance(n, dict) or str(n.get("kind") or "") not in ("retraction", "partial_retraction", "withdrawal", "removal"):
                continue
            if n.get("reference"):
                notice = notice or str(n["reference"])
            elif n.get("notice_pmid"):
                notice = notice or f"Retraction notice PMID {n['notice_pmid']}"
            elif n.get("notice_doi"):
                notice = notice or f"Retraction notice DOI {n['notice_doi']}"
    return retracted, (notice if retracted else None)


def _fill(target: CitationEntry, other: CitationEntry) -> None:
    for field in ("title", "authors", "journal", "year", "pmcid", "evidence_level", "retraction_notice", "note"):
        if getattr(target, field) is None and getattr(other, field) is not None:
            setattr(target, field, getattr(other, field))
    if not target.pub_types and other.pub_types:
        target.pub_types = list(other.pub_types)
    if other.retracted is True:
        target.retracted = True


def _report_keys(verification_report: Any) -> Tuple[Set[str], Set[str], List[Dict[str, Any]]]:
    """(verified keys, rejected keys, claim_support) from a VerificationReport or its model_dump()."""
    if verification_report is None:
        return set(), set(), []
    verified = {k for _, k in scan_keys(" ; ".join(str(v) for v in _get(verification_report, "verified_identifiers", []) or []))}
    rejected: Set[str] = set()
    for d in _get(verification_report, "defects", []) or []:
        if str(_get(d, "category", "")).upper() in _UNVERIFIED_CATEGORIES:
            rejected |= {k for _, k in scan_keys(str(_get(d, "claim", "") or ""))}
    rejected |= {k for _, k in scan_keys(" ; ".join(str(v) for v in _get(verification_report, "vetoed_identifiers", []) or []))}
    support = [s for s in _get(verification_report, "claim_support", []) or [] if isinstance(s, dict)]
    return verified, rejected, support


def _history_keys(historical_identifiers: Optional[Dict[str, Iterable[str]]]) -> Set[str]:
    keys: Set[str] = set()
    for name, values in (historical_identifiers or {}).items():
        for value in values or []:
            value = str(value).strip()
            if name == "civic":
                key = make_key(value[:3].upper(), value)
            elif name in _HISTORY_PREFIX:
                key = make_key(_HISTORY_PREFIX[name], value)
            else:
                key = None
            if key:
                keys.add(key)
    return keys


_PMID_FIELDS = {"pmid", "pmids", "pubmed_id", "pubmed_ids"}
_DOI_FIELDS = {"doi", "dois"}
_SKIP_FIELD_RE = re.compile(r"base64|_png$", re.IGNORECASE)


def _observed_keys(tool_results: Dict[str, ToolResult]) -> Set[str]:
    """Keys this turn's successful tool data states as identifiers: prefixed ids in its text, plus PMIDs/DOIs
    held in pmid/doi fields. The verifier also accepts a PMID whose digits merely occur in a passage
    ('1234 patients') and a DOI that is a prefix of a longer one, so its verdict alone never makes a chip.
    canonical_ids are skipped: their joined pmid/doi lists are truncated and can end in a partial id."""
    from ..extensions.hooks import apply_serialization_filters

    keys: Set[str] = set()

    def walk(value: Any, field: str, depth: int) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if not _SKIP_FIELD_RE.search(str(k)):
                    walk(v, str(k).lower(), depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, field, depth + 1)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value)
            if field in _PMID_FIELDS or field in _DOI_FIELDS:
                prefix = "PMID" if field in _PMID_FIELDS else "DOI"
                keys.update(k for k in (make_key(prefix, part) for part in re.split(r"[,;\s]+", text)) if k)
            if isinstance(value, str):
                keys.update(k for _, k in scan_keys(text))

    for tool_key, res in (tool_results or {}).items():
        if res and getattr(res, "success", False) and isinstance(res.data, dict):
            walk(apply_serialization_filters(tool_key, res.data), "", 0)
    return keys


def _source_agents() -> Dict[str, str]:
    try:
        from ..sources import get_sources

        return {s.key: s.agent for s in get_sources() if getattr(s, "key", "") and getattr(s, "agent", "")}
    except Exception:  # noqa: BLE001
        logger.debug("Source registry unavailable for citation agents", exc_info=True)
        return {}


def build_citation_map(
    response: str,
    tool_results: Dict[str, ToolResult],
    verification_report: Any,
    historical_identifiers: Optional[Dict[str, Iterable[str]]] = None,
    query_context: Any = None,
    source_citations: Optional[Dict[str, List[CitationRecord]]] = None,
) -> Dict[str, Any]:
    """CitationMap.model_dump() for the answer as shown (call it before any banner is prepended)."""
    response = response or ""
    builder = _Builder(_source_agents())

    mention_passages: List[Tuple[str, List[Tuple[CitationPassage, str]]]] = []
    record_batches: List[Tuple[str, List[Any]]] = []
    for tool_key, res in (tool_results or {}).items():
        if not res or not getattr(res, "success", False) or not isinstance(res.data, dict):
            continue
        data = res.data
        try:
            if "ranked_passages" in data:
                mention_passages.append((tool_key, builder.literature(tool_key, data)))
            if "search_hits" in data or tool_key == "synapta":
                builder.synapta(tool_key, res)
            if tool_key.startswith("vivarix") or "model_id" in data:
                builder.vivarix(tool_key, data)
            if tool_key == "websurfer" or ("results" in data and "top_page" in data):
                builder.websurfer(tool_key, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Citation extraction failed for %s: %s", tool_key, exc)
        if isinstance(data.get("citation_records"), list):
            record_batches.append((tool_key, data["citation_records"]))
    for src_key, records in (source_citations or {}).items():
        record_batches.append((src_key, list(records or [])))
    for tool_key, records in record_batches:
        for rec in records:
            try:
                builder.record(tool_key, rec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Citation record from %s skipped: %s", tool_key, exc)
    for tool_key, passages in mention_passages:
        builder.mentions(tool_key, passages)

    verified, rejected, claim_support = _report_keys(verification_report)
    history = _history_keys(historical_identifiers)

    cited = [(pos, builder.resolve(key)) for pos, key in scan_answer(response)]
    rejected = {builder.resolve(k) for k in rejected}
    verified = {builder.resolve(k) for k in verified}

    cmap = CitationMap(aliases=dict(builder.aliases))
    unverified: List[str] = []
    observed: Optional[Set[str]] = None
    for _, key in cited:
        if key in rejected:
            if key not in unverified:
                unverified.append(key)
            continue
        if key in cmap.entries:
            continue
        entry = builder.entries.get(key)
        if entry is None and key in history:
            entry = CitationEntry(
                key=key, id=split_key(key)[1], type=type_for_key(key), url=url_for_key(key),
                source_agent="Memory", source_tool="memory", note=MEMORY_NOTE,
            )
        elif entry is None and key in verified:
            if observed is None:
                observed = {builder.resolve(k) for k in _observed_keys(tool_results)}
            if key not in observed:
                continue
            entry = CitationEntry(
                key=key, id=split_key(key)[1], type=type_for_key(key), url=url_for_key(key),
                source_agent="Verifier", source_tool="verifier", note=VERIFIER_NOTE,
            )
        if entry is None:
            continue
        entry.verified = key in verified
        cmap.entries[key] = entry
    cmap.unverified = unverified
    cmap.aliases = {a: k for a, k in builder.aliases.items() if k in cmap.entries or k in unverified}

    _attach_supports(cmap, response, cited, claim_support, builder)
    return enforce_budget(cmap).model_dump()


def _attach_supports(
    cmap: CitationMap,
    response: str,
    cited: List[Tuple[int, str]],
    claim_support: List[Dict[str, Any]],
    builder: _Builder,
) -> None:
    spans = _sentence_spans(response)
    # The verifier cuts each sentence to 240 raw chars before any whitespace folding, so a cut sentence
    # (a long, padded table row) is matched as a prefix of the folded answer sentence.
    grounded: List[Tuple[str, bool, str, Optional[str]]] = []
    for s in claim_support:
        raw = str(s.get("sentence") or "")
        norm = _norm_sentence(raw)
        if norm:
            grounded.append((norm, len(raw) >= 240, str(s.get("evidence_key") or ""), s.get("passage_id")))

    def grounded_for(norm: str) -> List[Tuple[str, Optional[str]]]:
        return [(ek, pid) for g, cut, ek, pid in grounded if g == norm or (cut and norm.startswith(g))]

    def support_matches(entry: CitationEntry, evidence_key: str, passage_id: Optional[str]) -> bool:
        # The document the verifier grounded the sentence on is authoritative: when it names an identifier, that
        # identifier must resolve to the cited entry. A passage id alone is not enough, since ids from different
        # literature calls can repeat and point at another paper (LIT-001).
        prefix, value = split_key(evidence_key) if ":" in evidence_key else ("", "")
        key = make_key(prefix, value) if prefix in _KEY_SPECS else None
        if key:
            return builder.resolve(key) == entry.key
        if evidence_key.startswith("passage:"):
            return builder.owner_of(evidence_key[8:]) == entry.key
        if passage_id and builder.owner_of(str(passage_id)) == entry.key:
            return True
        if evidence_key.startswith("tool:"):
            return evidence_key[5:] == entry.source_tool
        return False

    counts: Dict[str, int] = {}
    used: Dict[str, List[str]] = {}
    for pos, key in cited:
        occurrence = counts.get(key, 0)
        counts[key] = occurrence + 1
        entry = cmap.entries.get(key)
        if entry is None:
            continue
        span = next(((s, e) for s, e in spans if s <= pos < e), (pos, pos))
        sentence = response[span[0]:span[1]].strip()
        norm = _norm_sentence(sentence)
        is_grounded = False
        verifier_pid: Optional[str] = None
        for evidence_key, pid in grounded_for(norm):
            if support_matches(entry, evidence_key, pid):
                is_grounded = True
                verifier_pid = str(pid) if pid else None
                break

        terms = _claim_terms(sentence)
        best: Tuple[int, Optional[CitationPassage], Optional[str], List[str]] = (0, None, None, [])
        candidates = entry.passages
        if verifier_pid and any(p.passage_id == verifier_pid for p in entry.passages):
            candidates = [p for p in entry.passages if p.passage_id == verifier_pid]
        for passage in candidates:
            hl, shared = _best_sentence(terms, passage.text, 1 if is_grounded else MIN_SHARED_TERMS)
            if hl and len(shared) > best[0]:
                best = (len(shared), passage, hl, shared)
        chosen = best[1] or (candidates[0] if verifier_pid and candidates else None)
        if chosen is not None:
            used.setdefault(key, [])
            if chosen.passage_id not in used[key]:
                used[key].append(chosen.passage_id or "")
            if best[2] and not chosen.highlight:
                chosen.highlight = best[2]
        cmap.supports.append(
            ClaimSupport(
                key=key,
                occurrence=occurrence,
                sentence=truncate_words(sentence, 240),
                passage_id=chosen.passage_id if chosen is not None else None,
                highlight=best[2],
                shared_terms=best[3][:8],
                grounded_by_verifier=is_grounded,
            )
        )

    # Keep at most two passages per entry: the ones claims point to first, then retrieval order.
    for key, entry in cmap.entries.items():
        order = used.get(key, [])
        ranked = sorted(
            entry.passages,
            key=lambda p: (order.index(p.passage_id or "") if (p.passage_id or "") in order else len(order)),
        )
        entry.passages = ranked[:2]


def _passage_fit(passage: CitationPassage, limit: int) -> bool:
    """Trim a passage to `limit` chars; returns True when text was cut."""
    if len(passage.text) <= limit:
        return False
    passage.text = trim_excerpt(passage.text, limit, passage.highlight)
    if passage.highlight and passage.highlight not in passage.text:
        passage.highlight = None
    return True


def _sync_support_highlights(cmap: CitationMap) -> None:
    by_id: Dict[Tuple[str, str], CitationPassage] = {}
    for key, entry in cmap.entries.items():
        for p in entry.passages:
            by_id[(key, p.passage_id or "")] = p
    for s in cmap.supports:
        p = by_id.get((s.key, s.passage_id or ""))
        if p is None:
            s.passage_id = None
            s.highlight = None
        elif s.highlight and s.highlight not in p.text:
            s.highlight = None


def _size(cmap: CitationMap) -> int:
    return len(json.dumps(cmap.model_dump()))


def enforce_budget(
    cmap: CitationMap, max_bytes: int = 24000, max_entries: int = 40, passage_chars: int = 800
) -> CitationMap:
    """Cap the map for transport and localStorage. Sets `truncated` when anything beyond the
    per-passage length cap had to be dropped or shortened."""
    for entry in cmap.entries.values():
        for p in entry.passages:
            _passage_fit(p, passage_chars)

    def drop_entries(keep: int) -> None:
        kept = dict(list(cmap.entries.items())[:keep])
        cmap.entries = kept
        cmap.supports = [s for s in cmap.supports if s.key in kept]
        cmap.aliases = {a: k for a, k in cmap.aliases.items() if k in kept or k in cmap.unverified}

    if len(cmap.entries) > max_entries:
        drop_entries(max_entries)
        cmap.truncated = True
    _sync_support_highlights(cmap)

    def one_passage() -> None:
        for e in cmap.entries.values():
            e.passages = e.passages[:1]

    def shrink(limit: int) -> Callable[[], None]:
        def run() -> None:
            for e in cmap.entries.values():
                for p in e.passages:
                    _passage_fit(p, limit)
        return run

    def no_passages() -> None:
        for e in cmap.entries.values():
            e.passages = []

    def lean_supports() -> None:
        for s in cmap.supports:
            s.shared_terms = []
            s.sentence = s.sentence[:120]

    def no_supports() -> None:
        cmap.supports = []

    for step in (one_passage, shrink(400), shrink(200), no_passages, lean_supports, no_supports):
        if _size(cmap) <= max_bytes:
            break
        step()
        _sync_support_highlights(cmap)
        cmap.truncated = True
    while cmap.entries and _size(cmap) > max_bytes:
        drop_entries(len(cmap.entries) - 1)
        cmap.truncated = True
    return cmap
