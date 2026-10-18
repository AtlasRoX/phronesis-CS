"""
Per-article study-design grading for Lithoscribe literature evidence.

Each EuropePMC record is graded once from metadata the core search already returns, in this order:

1. EuropePMC publication types (``pubTypeList.pubType``), exact matches: confidence ``high``.
2. MeSH design headings (``meshHeadingList``), ignoring "... as Topic" headings, which describe what a
   paper is about rather than what it did: confidence ``medium``. "Humans" never implies a clinical design.
3. A transparent text heuristic on the title, then on the abstract, with the matched cue recorded:
   confidence ``low``.

A tag says how the record was indexed, not what the article reports. When the title/abstract show the reported
analysis is a different design than the tag (a matching-adjusted indirect comparison indexed as "Randomized
Controlled Trial; Clinical Trial, Phase III", a post hoc analysis of a trial, a real-world cohort indexed as a trial)
the grade follows the reported analysis, confidence ``medium``, and keeps the raw tags in indexed_pub_types with the
override explained in design_note.

STUDY_TYPE_POLICY (tier, weight) is a configurable evidence-hierarchy *policy*, not measured data. Tiers
order designs for display (1 = meta-analysis / systematic review ... 6 = editorial / abstract /
unclassified); weights feed BioDiscovery's literature-support component and are never probabilities.
"""

from __future__ import annotations

import html
import re
from enum import Enum
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


class StudyType(str, Enum):
    META_ANALYSIS = "meta_analysis"
    NETWORK_META_ANALYSIS = "network_meta_analysis"  # pools trials through a network; rests on indirect comparisons
    SYSTEMATIC_REVIEW = "systematic_review"
    RCT = "rct"
    CLINICAL_TRIAL = "clinical_trial"  # non-randomized or single-arm, any phase
    # Population-adjusted or anchored comparison across separate studies (MAIC, STC, Bucher): no randomized
    # comparison between the arms it reports, whatever trials it draws on.
    INDIRECT_COMPARISON = "indirect_comparison"
    OBSERVATIONAL = "observational"
    CASE_REPORT = "case_report"
    PRECLINICAL_IN_VIVO = "preclinical_in_vivo"
    IN_VITRO = "in_vitro"
    COMPUTATIONAL = "computational"
    REVIEW = "review"
    EDITORIAL = "editorial"
    CONFERENCE_ABSTRACT = "conference_abstract"
    TRIAL_PROTOCOL = "trial_protocol"  # a registered or published protocol: design only, no results
    UNCLASSIFIED = "unclassified"


class GradeBasis(str, Enum):
    PUBTYPE = "europepmc_pubtype"
    MESH = "mesh_heading"
    HEURISTIC = "text_heuristic"
    NONE = "none"


# Identifies the grading rules and STUDY_TYPE_POLICY below in tool output and run provenance. Bump it whenever a
# rule, tier or weight changes, so a past answer can be traced to the grading it saw.
GRADING_POLICY_VERSION = "lithoscribe-grading/2026-10-18b"

# study type -> (tier, weight). Policy, documented above; adjust here, never per call.
STUDY_TYPE_POLICY: Dict[StudyType, Tuple[int, float]] = {
    StudyType.META_ANALYSIS: (1, 1.0),
    StudyType.SYSTEMATIC_REVIEW: (1, 0.9),
    StudyType.NETWORK_META_ANALYSIS: (2, 0.8),
    StudyType.RCT: (2, 0.9),
    StudyType.CLINICAL_TRIAL: (3, 0.7),
    StudyType.INDIRECT_COMPARISON: (3, 0.5),
    StudyType.OBSERVATIONAL: (3, 0.6),
    StudyType.CASE_REPORT: (4, 0.3),
    StudyType.REVIEW: (4, 0.3),
    StudyType.PRECLINICAL_IN_VIVO: (5, 0.4),
    StudyType.IN_VITRO: (5, 0.25),
    StudyType.COMPUTATIONAL: (5, 0.2),
    StudyType.EDITORIAL: (6, 0.1),
    StudyType.CONFERENCE_ABSTRACT: (6, 0.2),
    StudyType.TRIAL_PROTOCOL: (6, 0.1),
    StudyType.UNCLASSIFIED: (6, 0.25),
}

# study type -> (label, short summary noun singular, plural)
STUDY_TYPE_LABELS: Dict[StudyType, Tuple[str, str, str]] = {
    StudyType.META_ANALYSIS: ("Meta-analysis", "meta-analysis", "meta-analyses"),
    StudyType.SYSTEMATIC_REVIEW: ("Systematic review", "systematic review", "systematic reviews"),
    StudyType.NETWORK_META_ANALYSIS: ("Network meta-analysis", "network meta-analysis", "network meta-analyses"),
    StudyType.RCT: ("Randomized controlled trial", "RCT", "RCTs"),
    StudyType.CLINICAL_TRIAL: ("Clinical trial", "clinical trial", "clinical trials"),
    StudyType.INDIRECT_COMPARISON: ("Indirect comparison", "indirect comparison", "indirect comparisons"),
    StudyType.OBSERVATIONAL: ("Observational study", "observational study", "observational studies"),
    StudyType.CASE_REPORT: ("Case report", "case report", "case reports"),
    StudyType.PRECLINICAL_IN_VIVO: ("Preclinical in vivo", "preclinical in vivo", "preclinical in vivo"),
    StudyType.IN_VITRO: ("In vitro", "in vitro", "in vitro"),
    StudyType.COMPUTATIONAL: ("Computational", "computational study", "computational studies"),
    StudyType.REVIEW: ("Review", "review", "reviews"),
    StudyType.EDITORIAL: ("Editorial / opinion", "editorial", "editorials"),
    StudyType.CONFERENCE_ABSTRACT: ("Conference abstract", "conference abstract", "conference abstracts"),
    StudyType.TRIAL_PROTOCOL: ("Trial protocol", "trial protocol", "trial protocols"),
    StudyType.UNCLASSIFIED: ("Unclassified", "unclassified", "unclassified"),
}

# Long nouns for rationale text ("1 randomized controlled trial, 2 preclinical in vivo").
_LONG_NOUNS: Dict[StudyType, Tuple[str, str]] = {
    StudyType.RCT: ("randomized controlled trial", "randomized controlled trials"),
}


class EvidenceGrade(BaseModel):
    study_type: StudyType
    label: str
    tier: int
    weight: float
    basis: GradeBasis
    cues: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    is_preprint: bool = False
    # Indexed publication-type tags as the database lists them, kept apart from the design read from the
    # article's own title/abstract: a tag says how the record was indexed, not necessarily what the study did.
    indexed_pub_types: List[str] = Field(default_factory=list)
    inferred_design: Optional[str] = None  # StudyType value from the text heuristic, when it found one
    inferred_cue: Optional[str] = None
    mixed_design: bool = False
    design_note: str = ""  # why this grade, and any disagreement between the tag and the text
    # The design the indexed tags alone gave (StudyType value) when the reported analysis overrode it, else None.
    indexed_design: Optional[str] = None


def make_grade(
    study_type: StudyType, basis: GradeBasis, cues: Iterable[str], confidence: str, is_preprint: bool = False
) -> EvidenceGrade:
    tier, weight = STUDY_TYPE_POLICY[study_type]
    return EvidenceGrade(
        study_type=study_type,
        label=STUDY_TYPE_LABELS[study_type][0],
        tier=tier,
        weight=weight,
        basis=basis,
        cues=list(cues),
        confidence=confidence,  # type: ignore[arg-type]
        is_preprint=is_preprint,
    )


# -----------------------------------------------------------------------------
# Metadata accessors (EuropePMC core records, or the client's flattened article dicts)
# -----------------------------------------------------------------------------

def pub_types(article: Dict[str, Any]) -> List[str]:
    ptl = article.get("pubTypeList")
    if isinstance(ptl, dict):
        values = ptl.get("pubType") or []
        if isinstance(values, str):
            values = [values]
        return [str(v) for v in values if v]
    legacy = article.get("pubType")
    if isinstance(legacy, str) and legacy.strip():
        return [v.strip() for v in legacy.split(";") if v.strip()]
    return []


def mesh_descriptors(article: Dict[str, Any]) -> List[str]:
    mhl = article.get("meshHeadingList")
    headings = (mhl or {}).get("meshHeading") if isinstance(mhl, dict) else None
    return [str(h.get("descriptorName")) for h in (headings or []) if isinstance(h, dict) and h.get("descriptorName")]


# Exact EuropePMC pubType values (lowercased), in precedence order.
_PUBTYPE_RULES: List[Tuple[StudyType, Tuple[str, ...]]] = [
    # A protocol reports no results; it must not reach the trial tiers through its title wording.
    (StudyType.TRIAL_PROTOCOL, ("clinical trial protocol", "study-protocol", "study protocol")),
    (StudyType.META_ANALYSIS, ("meta-analysis",)),
    (StudyType.SYSTEMATIC_REVIEW, ("systematic review", "systematic-review")),
    (StudyType.RCT, ("randomized controlled trial",)),
    (StudyType.CLINICAL_TRIAL, (
        "clinical trial", "clinical trial, phase i", "clinical trial, phase ii", "clinical trial, phase iii",
        "clinical trial, phase iv", "controlled clinical trial", "pragmatic clinical trial",
    )),
    (StudyType.OBSERVATIONAL, ("observational study",)),
    (StudyType.CASE_REPORT, ("case reports", "case-report")),
    (StudyType.REVIEW, ("review", "review-article", "scoping review")),
    (StudyType.EDITORIAL, ("editorial", "comment", "letter", "practice guideline", "guideline", "consensus development conference")),
    (StudyType.CONFERENCE_ABSTRACT, ("abstract", "meeting abstract", "congress", "conference abstract")),
]

# Integrity notices themselves: not evidence, never graded as a study.
NOTICE_PUBTYPES = frozenset({
    "retraction of publication", "retraction notice", "retraction", "expression of concern",
    "expression-of-concern", "published erratum", "erratum", "correction",
})

_MESH_OBSERVATIONAL = frozenset({
    "Retrospective Studies", "Prospective Studies", "Follow-Up Studies", "Cohort Studies",
    "Case-Control Studies", "Cross-Sectional Studies", "Longitudinal Studies",
})
_MESH_IN_VIVO = frozenset({"Mice", "Rats", "Xenograft Model Antitumor Assays", "Disease Models, Animal"})
_MESH_IN_VITRO = frozenset({"Cell Line, Tumor", "Cell Line", "Cells, Cultured", "In Vitro Techniques"})
_MESH_COMPUTATIONAL = frozenset({
    "Molecular Docking Simulation", "Molecular Dynamics Simulation", "Computer Simulation", "Computational Biology",
})

# A cue that describes *other* work (a named trial, earlier trials) rather than the article's own design. Applied to
# the sentence holding a trial cue; the named-trial part is case-sensitive ("In the KEYNOTE-189 trial", "In the
# FLAURA trial"), so "In the present trial" still counts as the article's own.
_OTHER_WORK_RE = re.compile(
    r"\b[Ii]n the (?:[A-Z][A-Za-z]*[- ]?\d+[\w-]*|[A-Z]{3,}[\w-]*) (?:trial|study)\b"
    r"|(?i:\b(?:previous|prior|earlier|published|landmark|pivotal|several|recent|other|large) (?:[\w-]+ ){0,3}trials?\b)"
    r"|(?i:\btrials\b)|(?i:\bhave been randomi[sz]ed\b)"
)
# An observational emulation or a non-randomized trial is not an RCT; nor is a trial the sentence names as other work.
_NOT_RCT_RE = re.compile(
    r"(?i:\bemulat\w*\b|\bnon-?randomi[sz]ed\b|\bquasi-?randomi[sz]ed\b)|" + _OTHER_WORK_RE.pattern
)
# A "case study" of thousands of patients in a registry is a population study, not a case report.
_NOT_CASE_REPORT_RE = re.compile(
    r"\b\d[\d,.]*\s+(?:patients|participants|subjects|individuals|cases|people|women|men|children|adults)\b"
    r"|\bcohorts?\b|\bregistry\b|\bpopulation-based\b|\bnationwide\b",
    re.I,
)

# Indirect comparisons. The named methods are trusted anywhere; the generic phrase only as the article's own analysis
# ("an anchored indirect comparison", "we indirectly compared"), so a limitations sentence of an RCT does not count.
_INDIRECT_METHOD = (
    r"\bmatching[- ]adjusted indirect (?:treatment )?comparisons?\b|\bMAICs?\b"
    r"|\bsimulated treatment comparisons?\b|\bpopulation[- ]adjusted indirect comparisons?\b"
)
_INDIRECT_TITLE_RE = re.compile(_INDIRECT_METHOD + r"|\bindirect (?:treatment )?comparisons?\b", re.I)
_INDIRECT_ABSTRACT_RE = re.compile(
    _INDIRECT_METHOD
    + r"|\b(?:an?|this|the present) (?:(?:anchored|unanchored|adjusted|bucher)\s+)?indirect (?:treatment )?comparisons?\b"
    r"|\b(?:we|were) (?:then )?indirectly compared\b",
    re.I,
)
_NMA_RE = re.compile(r"\bnetwork meta-?analys[ie]s\b|\bmixed[- ]treatment comparisons?\b", re.I)

# (study type, title pattern, abstract pattern or None when the cue is only trusted in a title, reject pattern or
# None). Rules are tried in order and the first accepted cue wins; a cue is rejected when the sentence holding it
# matches the reject pattern (it then describes other work), and the next rule is tried.
_HEURISTICS: List[Tuple[StudyType, re.Pattern, Optional[re.Pattern], Optional[re.Pattern]]] = [
    # "...: study protocol for a randomised controlled trial" reports no results; it must not read as an RCT.
    (StudyType.TRIAL_PROTOCOL,
     re.compile(r"\btrial protocol\b|\bprotocol (?:for|of) (?:a|an|the) [^.;:]{0,80}?\btrial\b|\bstudy protocol\b[^.;:]{0,80}?\btrial\b", re.I),
     None, None),
    # Before META_ANALYSIS: "network meta-analysis" contains "meta-analysis".
    (StudyType.INDIRECT_COMPARISON, _INDIRECT_TITLE_RE, _INDIRECT_ABSTRACT_RE, None),
    (StudyType.NETWORK_META_ANALYSIS, _NMA_RE, _NMA_RE, None),
    (StudyType.META_ANALYSIS, re.compile(r"\bmeta-?analys[ie]s\b", re.I), re.compile(r"\bmeta-?analys[ie]s\b", re.I), None),
    (StudyType.SYSTEMATIC_REVIEW, re.compile(r"\bsystematic (?:literature )?review\b", re.I), re.compile(r"\bsystematic (?:literature )?review\b", re.I), None),
    # Only standalone article-type words: "guideline-directed therapy" or a "safety checklist" trial is no editorial.
    (StudyType.EDITORIAL,
     re.compile(r"\b(?:editorial|commentary|viewpoint|consensus statement|position (?:statement|paper))\b"
                r"|^(?:comment|perspective|opinion)\s*[:\-]"
                r"|\b(?:clinical )?practice guidelines?\b|\bguidelines? (?:for|on) (?:the )?(?:management|diagnosis|treatment|prevention|use)\b"
                r"|\bconsensus (?:guidelines?|recommendations?)\b", re.I),
     None, None),
    # A review describes other studies' designs (cohorts, trials); its own review wording must win over them.
    (StudyType.REVIEW,
     re.compile(r"\b(?:narrative|literature|scoping|structured|comprehensive|critical|mini-?|state-of-the-art) review\b|\ba review\b|\breview of (?:the )?(?:literature|current|recent|evidence)\b|\bperspective on\b", re.I),
     re.compile(r"\b(?:narrative|literature|scoping|structured|comprehensive|critical|mini-?) review\b|\bwe review\b|\bthis (?:review|perspective)\b"
                r"|\bwe summari[sz]e (?:the )?(?:current|recent|existing|available) (?:evidence|literature|knowledge|data)\b"
                r"|\b(?:searched|search(?:es)? of|search in) (?:the )?(?:pubmed|medline|embase|scopus|web of science|google scholar|literature)\b", re.I),
     None),
    # An economic evaluation (often of a trial) is not graded as the trial: no design tier is inferred for it.
    (StudyType.UNCLASSIFIED,
     re.compile(r"\bcost[- ](?:effectiveness|utility|benefit)\b|\beconomic evaluation\b|\bbudget impact\b", re.I),
     re.compile(r"\b(?:this|we performed an?|we conducted an?) (?:[\w-]+ ){0,2}(?:cost[- ](?:effectiveness|utility)|economic) (?:analysis|evaluation)\b", re.I),
     None),
    # A post hoc / secondary analysis re-uses trial data outside the primary randomized comparison: trial tier 3,
    # never the RCT tier.
    (StudyType.CLINICAL_TRIAL,
     re.compile(r"\b(?:post[- ]?hoc|secondary|exploratory|ancillary|subgroup) analys[ie]s\b[^.;:]{0,80}?\b(?:trials?|RCTs?)\b", re.I),
     re.compile(r"\b(?:this|we performed an?|we conducted an?|in this) (?:post[- ]?hoc|secondary|exploratory|ancillary|subgroup) analys[ie]s\b", re.I),
     None),
    (StudyType.RCT, re.compile(r"\brandomi[sz]ed\b[^.;:]{0,60}?\btrial\b", re.I),
     re.compile(r"\b(?:we|this) (?:conducted|performed|report|did|designed|undertook) an? [^.]{0,40}?randomi[sz]ed\b[^.]{0,40}?\btrial\b"
                r"|\bin this (?:[\w-]+ ){0,4}randomi[sz]ed\b|\brandomly (?:assigned|allocated)\b", re.I),
     _NOT_RCT_RE),
    (StudyType.CLINICAL_TRIAL,
     re.compile(r"\bphase\s*(?:[1-4]|i{1,3}|iv)[ab]?\b[^.;:]{0,40}?\b(?:trial|study)\b|\bsingle-arm\b|\bnon-?randomi[sz]ed\b[^.;:]{0,40}?\btrial\b", re.I),
     re.compile(r"\bin this (?:open-label |single-arm |multicenter )*phase\s*(?:[1-4]|i{1,3}|iv)[ab]?\b|\bsingle-arm\b", re.I),
     _OTHER_WORK_RE),
    (StudyType.CASE_REPORT, re.compile(r"\bcase (?:report|study)\b", re.I),
     re.compile(r"\b(?:this|a) case (?:report|study)\b|\bwe (?:report|describe|present) (?:a|an|the) (?:case|patient)\b", re.I),
     _NOT_CASE_REPORT_RE),
    (StudyType.OBSERVATIONAL, re.compile(r"\breal-world\b|\bretrospective\b|\bcohort\b|\bcase-control\b|\bcross-sectional\b|\bregistry\b|\bpopulation-based\b", re.I),
     re.compile(r"\breal-world\b|\bretrospective(?:ly)?\b|\bprospective cohort\b|\bcohort study\b|\bcase-control\b|\bcross-sectional\b|\bpopulation-based\b", re.I),
     None),
    (StudyType.REVIEW, re.compile(r"\breview\b", re.I), re.compile(r"\b(?:this|our|in this) (?:narrative |comprehensive |critical )?review\b|\bwe review\b", re.I), None),
    (StudyType.PRECLINICAL_IN_VIVO, re.compile(r"\bxenografts?\b|\bmice\b|\bmouse models?\b|\bmurine\b|\bin vivo\b|\brats?\b", re.I),
     re.compile(r"\bxenografts?\b|\bmice\b|\bmouse models?\b|\bmurine\b|\bin vivo\b", re.I), None),
    (StudyType.IN_VITRO, re.compile(r"\bcell lines?\b|\bin vitro\b|\borganoids?\b|\bcultured cells\b", re.I),
     re.compile(r"\bcell lines?\b|\bin vitro\b|\borganoids?\b|\bcultured cells\b", re.I), None),
    (StudyType.COMPUTATIONAL, re.compile(r"\bin silico\b|\bmolecular (?:docking|dynamics)\b|\bbioinformatic\w*\b|\bmachine learning\b|\bdeep learning\b|\bkinetic model\b|\bordinary differential equations?\b", re.I),
     re.compile(r"\bin silico\b|\bmolecular (?:docking|dynamics)\b|\bbioinformatic\w*\b|\bmachine learning\b|\bdeep learning\b|\bkinetic model\b|\bordinary differential equations?\b|\bODE models?\b", re.I),
     None),
]

# Known markup tags only, so a comparison ("p<0.001") is never taken for a tag.
_MARKUP = r"(?:i|b|u|em|strong|sup|sub|inf|span|br|p|a|div|font|small|big|italic|bold|sc|underline|h[1-6]|ul|ol|li|mml:\w+|math)"
_TAG_RE = re.compile(rf"</?{_MARKUP}(?:\s[^<>]*)?/?>|&lt;/?{_MARKUP}(?:\s(?:(?!&gt;)[^<>])*)?/?&gt;", re.I)
# Text showing the work itself was done in animals ("patient-derived xenograft models", "tumor-bearing mice").
# A statement that in vivo work is still needed is not one.
_IN_VIVO_CUE_RE = re.compile(
    r"\bxenografts?\b|\bPDX\b|\bmice\b|\bmouse models?\b|\bmurine models?\b|\btumou?r-bearing\b|"
    r"\bin vivo\b(?![^.;]{0,40}\b(?:needed|warranted|required|remain|future)\b)",
    re.I,
)


def _plain(text: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", str(text or "")))).strip()


def is_preprint(article: Dict[str, Any]) -> bool:
    return str(article.get("source") or "").upper() == "PPR" or any(p.lower() == "preprint" for p in pub_types(article))


def is_integrity_notice(article: Dict[str, Any]) -> bool:
    """True for a retraction / expression-of-concern / erratum notice record (the notice, not the paper)."""
    return any(p.lower() in NOTICE_PUBTYPES for p in pub_types(article))


def _grade_from_pubtypes(types: List[str]) -> Optional[Tuple[StudyType, List[str]]]:
    lowered = [(t, t.lower()) for t in types]
    for study_type, values in _PUBTYPE_RULES:
        matched = [orig for orig, low in lowered if low in values]
        if matched:
            return study_type, matched
    return None


def grade_from_mesh(descriptors: List[str]) -> Optional[Tuple[StudyType, List[str]]]:
    """Design grade from MeSH descriptors; '... as Topic' headings are ignored."""
    design = [d for d in descriptors if not d.endswith(" as Topic")]
    in_vivo = [d for d in design if d in _MESH_IN_VIVO or d.startswith(("Mice, ", "Rats, "))]
    for study_type, matched in (
        (StudyType.OBSERVATIONAL, [d for d in design if d in _MESH_OBSERVATIONAL]),
        (StudyType.PRECLINICAL_IN_VIVO, in_vivo),
        (StudyType.IN_VITRO, [d for d in design if d in _MESH_IN_VITRO]),
        (StudyType.COMPUTATIONAL, [d for d in design if d in _MESH_COMPUTATIONAL]),
    ):
        if matched:
            return study_type, matched
    return None


def _sentence_at(text: str, start: int, end: int) -> str:
    """The sentence of text holding [start, end) (the whole text for a title)."""
    left = max(text.rfind(". ", 0, start), text.rfind("; ", 0, start))
    right_candidates = [i for i in (text.find(". ", end), text.find("; ", end)) if i >= 0]
    return text[left + 2 if left >= 0 else 0: min(right_candidates) if right_candidates else len(text)]


def _grade_from_text(title: str, abstract: str) -> Optional[Tuple[StudyType, str]]:
    for field_name, text in (("title", title), ("abstract", abstract)):
        if not text:
            continue
        for study_type, title_re, abstract_re, reject_re in _HEURISTICS:
            pattern = title_re if field_name == "title" else abstract_re
            if pattern is None:
                continue
            for m in pattern.finditer(text):
                if reject_re is not None and reject_re.search(_sentence_at(text, m.start(), m.end())):
                    continue  # the cue describes other work (a named trial, earlier trials, a registry cohort)
                return study_type, f'{field_name}: "{m.group(0)}"'
    return None


# Designs that describe what the work itself did; a tag and the text disagreeing between two of these is a
# mixed or mis-indexed design worth explaining.
_DESIGN_FAMILIES: Dict[StudyType, str] = {
    StudyType.META_ANALYSIS: "evidence synthesis",
    StudyType.NETWORK_META_ANALYSIS: "evidence synthesis",
    StudyType.SYSTEMATIC_REVIEW: "evidence synthesis",
    StudyType.INDIRECT_COMPARISON: "indirect comparison",
    StudyType.REVIEW: "review",
    StudyType.RCT: "trial",
    StudyType.CLINICAL_TRIAL: "trial",
    StudyType.OBSERVATIONAL: "observational",
    StudyType.CASE_REPORT: "case report",
}


# A real-world / retrospective cohort indexed as a trial: trusted in the title, and in the abstract only as the
# article's own design ("this retrospective", "we retrospectively", "real-world data/cohort/study").
_REAL_WORLD_TITLE_RE = re.compile(r"\breal-world\b|\bretrospective\b|\bobservational\b", re.I)
_REAL_WORLD_ABSTRACT_RE = re.compile(
    r"\b(?:this|a|our) (?:multicenter |multicentre |single-cent(?:er|re) )?(?:retrospective|real-world|observational)\b"
    r"|\bwe retrospectively\b|\bretrospectively (?:reviewed|analy[sz]ed|identified|collected)\b"
    # "real-world data are needed" in an RCT's conclusion is not its design: the data must be the article's own.
    r"|\breal-world (?:data|cohort)s? (?:from|of|were|was)\b",
    re.I,
)
# Post hoc / secondary / pooled analyses re-use trial data outside the primary randomized comparison.
_SECONDARY_TITLE_RE = re.compile(r"\b(?:post[- ]?hoc|secondary|exploratory|ancillary|pooled|subgroup) analys[ie]s\b", re.I)
_SECONDARY_ABSTRACT_RE = re.compile(
    r"\b(?:this|we performed an?|we conducted an?|in this|a) (?:post[- ]?hoc|secondary|exploratory|ancillary|pooled) analys[ie]s\b",
    re.I,
)
_TRIAL_TAGGED = (StudyType.RCT, StudyType.CLINICAL_TRIAL)
_SYNTHESIS_TAGGED = (StudyType.META_ANALYSIS, StudyType.SYSTEMATIC_REVIEW)
# (reported design, tagged designs it overrides, title pattern, abstract pattern, phrase for design_note), in order.
_ANALYSIS_OVERRIDES: List[Tuple[StudyType, Tuple[StudyType, ...], re.Pattern, re.Pattern, str]] = [
    (StudyType.NETWORK_META_ANALYSIS, _TRIAL_TAGGED + _SYNTHESIS_TAGGED, _NMA_RE, _NMA_RE,
     "a network meta-analysis (its comparisons rest partly on indirect evidence)"),
    (StudyType.INDIRECT_COMPARISON, _TRIAL_TAGGED + _SYNTHESIS_TAGGED + (StudyType.OBSERVATIONAL,),
     _INDIRECT_TITLE_RE, _INDIRECT_ABSTRACT_RE,
     "an indirect comparison across separate studies (no randomized comparison between the arms it reports)"),
    (StudyType.OBSERVATIONAL, _TRIAL_TAGGED, _REAL_WORLD_TITLE_RE, _REAL_WORLD_ABSTRACT_RE,
     "a real-world / retrospective observational analysis"),
    (StudyType.CLINICAL_TRIAL, (StudyType.RCT,), _SECONDARY_TITLE_RE, _SECONDARY_ABSTRACT_RE,
     "a post hoc / secondary analysis of trial data, outside the primary randomized comparison"),
]


def _reported_analysis(title: str, abstract: str, tagged: StudyType) -> Optional[Tuple[StudyType, str, str]]:
    """(design, cue, note phrase) when the title/abstract show the reported analysis differs from the tagged design."""
    for design, overrides, title_re, abstract_re, phrase in _ANALYSIS_OVERRIDES:
        if tagged not in overrides or design == tagged:
            continue
        for field_name, text, pattern in (("title", title, title_re), ("abstract", abstract, abstract_re)):
            m = pattern.search(text) if text else None
            if m:
                return design, f'{field_name}: "{m.group(0)}"', phrase
    return None


def grade_article(article: Dict[str, Any]) -> EvidenceGrade:
    """Grade one EuropePMC record by its study design (see the module docstring for precedence).

    The indexed tags and the design read from the title/abstract are recorded separately; when they point to
    different designs (an RCT-tagged paper whose abstract reports retrospective cohorts) the grade follows the
    higher-precedence basis and design_note explains the disagreement instead of hiding it."""
    preprint = is_preprint(article)
    indexed = pub_types(article)
    title, abstract = _plain(article.get("title")), _plain(article.get("abstractText"))
    text_hit = _grade_from_text(title, abstract)
    hit = _grade_from_pubtypes(indexed)
    override = _reported_analysis(title, abstract, hit[0]) if hit else None
    if hit and override:
        # The tag describes how the record was indexed; the grade follows the analysis the article reports.
        grade = make_grade(override[0], GradeBasis.HEURISTIC, [override[1]], "medium", preprint)
        grade.indexed_design = hit[0].value
        tags = [t for t in indexed if t.lower() not in ("journal article", "research support")] or hit[1]
        note = (
            f"Indexed as {', '.join(tags)}, but the {override[1].split(':')[0]} reports {override[2]} "
            f"({override[1]}); graded as {STUDY_TYPE_LABELS[override[0]][1]}, not as the indexed "
            f"{STUDY_TYPE_LABELS[hit[0]][1]}."
        )
    elif hit:
        grade = make_grade(hit[0], GradeBasis.PUBTYPE, hit[1], "high", preprint)
        note = f"Indexed publication type: {', '.join(hit[1])}."
    else:
        mesh_hit = grade_from_mesh(mesh_descriptors(article))
        if mesh_hit:
            grade = make_grade(mesh_hit[0], GradeBasis.MESH, mesh_hit[1], "medium", preprint)
            note = f"MeSH design headings: {', '.join(mesh_hit[1][:3])}."
        elif text_hit:
            grade = make_grade(text_hit[0], GradeBasis.HEURISTIC, [text_hit[1]], "low", preprint)
            if text_hit[0] == StudyType.UNCLASSIFIED:
                note = f"No design tag indexed; {text_hit[1]} names an economic evaluation, which is not graded as a study design."
            else:
                note = f"No design tag indexed; inferred from {text_hit[1]} (low confidence)."
        else:
            grade = make_grade(StudyType.UNCLASSIFIED, GradeBasis.NONE, [], "low", preprint)
            note = "No design tag indexed and no design wording found; unclassified."
    if grade.study_type == StudyType.IN_VITRO and grade.basis in (GradeBasis.MESH, GradeBasis.HEURISTIC):
        # MeSH "Cell Line, Tumor" (or an in-vitro title cue) does not outweigh animal work the text reports.
        for field_name in ("title", "abstractText"):
            m = _IN_VIVO_CUE_RE.search(_plain(article.get(field_name)))
            if m:
                cue = f'{"title" if field_name == "title" else "abstract"}: "{m.group(0)}"'
                prior = ", ".join(grade.cues[:3])
                grade = make_grade(StudyType.PRECLINICAL_IN_VIVO, GradeBasis.HEURISTIC, [cue], "medium", preprint)
                note = (f"The {cue.split(':')[0]} reports in vivo work ({m.group(0)}), which outranks the in vitro "
                        f"design read from {prior or 'the metadata'}.")
                break
    grade.indexed_pub_types = indexed
    if grade.indexed_design:
        grade.mixed_design = True
    elif text_hit:
        grade.inferred_design, grade.inferred_cue = text_hit[0].value, text_hit[1]
        tagged, inferred = _DESIGN_FAMILIES.get(grade.study_type), _DESIGN_FAMILIES.get(text_hit[0])
        if grade.basis != GradeBasis.HEURISTIC and tagged and inferred and tagged != inferred:
            grade.mixed_design = True
            note += (
                f" The title/abstract also suggest the design '{STUDY_TYPE_LABELS[text_hit[0]][1]}' ({text_hit[1]}); the work may"
                " combine designs or be indexed differently from what it reports."
            )
    grade.design_note = note
    return grade


# -----------------------------------------------------------------------------
# Summaries and legacy fields
# -----------------------------------------------------------------------------

def _as_type(value: Any) -> StudyType:
    try:
        return value if isinstance(value, StudyType) else StudyType(str(value))
    except ValueError:
        return StudyType.UNCLASSIFIED


def ordered_counts(counts: Dict[str, int]) -> List[Tuple[StudyType, int]]:
    items = [(_as_type(k), int(v)) for k, v in counts.items() if int(v or 0) > 0]
    return sorted(items, key=lambda kv: (STUDY_TYPE_POLICY[kv[0]][0], list(StudyType).index(kv[0])))


def summary_phrases(counts: Dict[str, int], style: Literal["short", "long"] = "short") -> List[str]:
    """['1 RCT', '2 preclinical in vivo'] (short) or ['1 randomized controlled trial', ...] (long), by tier."""
    out = []
    for st, n in ordered_counts(counts):
        if style == "long" and st in _LONG_NOUNS:
            singular, plural = _LONG_NOUNS[st]
        else:
            _, singular, plural = STUDY_TYPE_LABELS[st]
        out.append(f"{n} {singular if n == 1 else plural}")
    return out


def basis_phrase(grade: Dict[str, Any]) -> str:
    return {
        GradeBasis.PUBTYPE.value: "EuropePMC pubType",
        GradeBasis.MESH.value: "MeSH",
        GradeBasis.HEURISTIC.value: "text heuristic",
    }.get(str(grade.get("basis")), "unclassified")


def legacy_pub_type(grade: EvidenceGrade) -> str:
    """The pre-grading pub_type enum: clinical-trial | review | meta-analysis | research-article."""
    st = grade.study_type
    if st in (StudyType.RCT, StudyType.CLINICAL_TRIAL):
        return "clinical-trial"
    if st in (StudyType.META_ANALYSIS, StudyType.NETWORK_META_ANALYSIS):
        return "meta-analysis"
    if st in (StudyType.REVIEW, StudyType.SYSTEMATIC_REVIEW):
        return "review"
    return "research-article"


_SETTING_CLINICAL = re.compile(r"\bphase\s*(?:i{1,3}|[123])\b|\bpatients\b|\bcohort\b|\boverall response rate\b|\bclinical trial\b", re.I)
_SETTING_IN_VIVO = re.compile(r"\bxenografts?\b|\bmice\b|\bmouse\b|\bin vivo\b|\bmurine\b", re.I)
_SETTING_COMPUTATIONAL = re.compile(r"\bdocking\b|\bmolecular dynamics\b|\bsimulations?\b|\bbioinformatic\w*\b|\bODEs?\b|\bin silico\b", re.I)
UNSPECIFIED_SETTING = "unspecified"


def setting_from_text(text: str) -> str:
    """Word-bounded setting cues ('ode' inside 'mode' or 'code' no longer reads as computational). No cue gives
    'unspecified' (never a default 'in-vitro')."""
    if _SETTING_CLINICAL.search(text):
        return "human-clinical"
    if _SETTING_IN_VIVO.search(text):
        return "in-vivo"
    if _SETTING_COMPUTATIONAL.search(text):
        return "computational"
    return UNSPECIFIED_SETTING


# Evidence syntheses describe the setting of the studies they pool or review; for those the text cue is kept.
_SYNTHESIS_TYPES = (StudyType.META_ANALYSIS, StudyType.NETWORK_META_ANALYSIS, StudyType.SYSTEMATIC_REVIEW, StudyType.REVIEW)


def legacy_setting(grade: EvidenceGrade, text: str = "") -> str:
    """The pre-grading experimental_setting enum: human-clinical | in-vivo | in-vitro | computational | unspecified.

    The setting follows the grade. An unclassified article, an editorial, a conference abstract or a protocol is
    'unspecified': a cue in its text ('patients', 'phase 3') usually describes other work, and turning it into a
    design label would upgrade the evidence (LIT-002)."""
    st = grade.study_type
    if st in (StudyType.RCT, StudyType.CLINICAL_TRIAL, StudyType.OBSERVATIONAL, StudyType.CASE_REPORT,
              StudyType.INDIRECT_COMPARISON):
        return "human-clinical"
    if st == StudyType.PRECLINICAL_IN_VIVO:
        return "in-vivo"
    if st == StudyType.IN_VITRO:
        return "in-vitro"
    if st == StudyType.COMPUTATIONAL:
        return "computational"
    if st in _SYNTHESIS_TYPES:
        return setting_from_text(_plain(text))
    return UNSPECIFIED_SETTING


# Alias for compatibility with external harnesses
grade_publication = grade_article

