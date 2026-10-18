"""
What to search the literature and the web for, for any kind of biomedical question.

A SearchPlan holds the question type and up to three concept groups (the central topic first; each group lists
a term as written plus its synonyms). It compiles into EuropePMC queries in two tiers:
- tier 1, run together: a citation-ranked "landmark" search over the last LANDMARK_YEARS (pivotal trials,
  guidelines, consensus definitions) and a relevance-ranked "recent" search over the last RECENT_YEARS; when the
  question is about an intervention's efficacy in a named disease, a citation-ranked "pivotal" search for
  randomized / phase 3 trials of (any of) the drugs in that disease; and, when trial names are known
  ("KEYNOTE-716", "SOLO-1"), a "trial_names" search for them as quoted phrases;
- tier 2, run only when tier 1 finds too little: the same searches relaxed (no question-type facet, title
  anchor widened to title/abstract, no year window).

The deterministic plan is built from the resolved entities and the content phrases of the question. A planner
model may supply a better plan (synonyms, a cleaner web query); its concept groups are kept only when they share
a word with the question, so the model can add synonyms but never a candidate answer ("vancomycin" for
"treatment of MRSA bacteremia").
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

QUESTION_TYPES = (
    "treatment", "diagnosis", "prognosis", "epidemiology", "mechanism", "pharmacology", "safety", "trials",
    "genetics", "target", "methods", "statistics", "comparison", "biomarker", "guideline", "regulatory", "general",
)

LANDMARK_YEARS = 12
RECENT_YEARS = 3
MAX_GROUPS = 3
MAX_TERMS = 4

# Ordered: the first matching type wins, so specific types come before broad ones.
_TYPE_PATTERNS = [
    # Effect measures ("hazard ratios", "p-values") make a statistics question only when asked how to compute or
    # interpret them; "efficacy, including hazard ratios from the pivotal trials" is a trials question.
    ("statistics", r"\b(sample\s+size|power\s+(?:analysis|calculation)|statistic\w*|regression|"
                   r"(?:calculat\w*|comput\w*|interpret\w*|estimat\w*|deriv\w*)\s+(?:the\s+|an?\s+)?(?:p[\s\-]?values?|"
                   r"confidence\s+intervals?|hazard\s+ratios?|odds\s+ratios?)|"
                   r"meta[\s\-]analys\w*\s+method|bayesian|survival\s+analysis|kaplan)\b"),
    ("methods", r"\b(how\s+(?:do|can|should)\s+(?:i|we|you)\s+(?:design|perform|run|measure|set\s+up|do|analy[sz]e|quantify|"
                r"validate|culture|clone|isolate|knock)|protocol|assay|workflow|pipeline|experimental\s+design|best\s+practices?)\b"),
    ("target", r"\b(druggab\w*|therapeutic\s+targets?|target\s+(?:validation|assessment)|tractab\w*)\b"),
    ("guideline", r"\b(guidelines?|consensus\s+statements?|standard\s+of\s+care|practice\s+parameters?)\b"),
    ("regulatory", r"\b(fda|ema|mhra|pmda|approv\w*|labell?(?:ed|ing)|indications?|authori[sz]ed)\b"),
    ("trials", r"\b(trials?|phase\s*(?:[1-4]|i{1,3}v?|iv)\b|nct\d{8}|enrol\w*)"),
    ("safety", r"\b(adverse\s+(?:events?|effects?|reactions?)|toxicit\w*|safety|side[\s\-]effects?|contraindicat\w*|"
               r"black\s+box|cardiotoxic\w*|hepatotoxic\w*|interactions?)\b"),
    ("pharmacology", r"\b(dos(?:e|es|ing|age)|pharmacokinetic\w*|pharmacodynamic\w*|half[\s\-]life|clearance|bioavailab\w*|"
                     r"renal\s+(?:impairment|adjustment)|titrat\w*)\b"),
    ("comparison", r"\b(compare[ds]?|comparison|versus|vs\.?|difference\s+between|better\s+than|head[\s\-]to[\s\-]head)\b"),
    ("biomarker", r"\b(biomarkers?|predict\w*\s+(?:response|outcome|survival)|prognostic\s+markers?|companion\s+diagnostic)\b"),
    ("genetics", r"\b(variants?|mutations?|fusions?|rearrangements?|pathogenic\w*|germline|somatic|hereditary|inherit\w*|polymorphism\w*|genotype\w*|rs\d+)\b"),
    ("diagnosis", r"\b(diagnos\w*|criteria|definitions?|screening|differential|detect\w*|imaging|work[\s\-]?up)\b"),
    ("epidemiology", r"\b(prevalence|incidence|epidemiolog\w*|burden|risk\s+factors?|mortality\s+rates?|how\s+common)\b"),
    ("prognosis", r"\b(prognos\w*|survival|outcomes?|life\s+expectancy|recurrence|relapse\s+rates?)\b"),
    ("treatment", r"\b(treat\w*|therap\w*|management|managed|manage|first[\s\-]line|second[\s\-]line|regimens?|cure|"
                  r"drug\s+of\s+choice|of\s+choice)\b"),
    ("mechanism", r"\b(mechanisms?|how\s+does|how\s+do|pathways?|signal\w*|role\s+of|works?|mode\s+of\s+action|"
                  r"pathophysiolog\w*|pathogenesis|causes?)\b"),
]
_TYPE_RES = [(name, re.compile(pat, re.I)) for name, pat in _TYPE_PATTERNS]

# Title/abstract words that narrow a search to the kind of evidence a question type needs.
_FACETS: Dict[str, List[str]] = {
    "treatment": ["treatment", "therapy", "management"],
    "guideline": ["guideline", "recommendation", "consensus", "management"],
    "diagnosis": ["diagnosis", "diagnostic", "criteria", "definition"],
    "epidemiology": ["prevalence", "incidence", "epidemiology", "burden"],
    "safety": ["adverse", "toxicity", "safety"],
    "pharmacology": ["dose", "dosing", "pharmacokinetics", "clearance"],
    "trials": ["trial"],
    "prognosis": ["prognosis", "survival", "outcome"],
    "biomarker": ["biomarker", "predictive", "predictor"],
    "statistics": ["sample size", "statistical", "power"],
    "regulatory": ["approval", "approved", "FDA"],
    "mechanism": ["mechanism*", "pathway*", "pathogenesis", "mode of action"],
    "methods": ["protocol*", "guide*", "design", "best practice*", "method*"],
    "target": ["therapeutic target", "therapeutic targets", "drug target", "novel therapies", "treatment"],
}
# Question types whose landmark papers name the facet in their title ("...Definitions for Sepsis", "Global
# epidemiology of...", "Mechanisms of insulin resistance"). Treatment and trial titles rarely say "treatment"
# ("Pertuzumab, trastuzumab, and docetaxel in HER2-positive..."), so those match the facet in the abstract.
_TITLE_FACET_TYPES = frozenset(
    {"epidemiology", "diagnosis", "safety", "mechanism", "pharmacology", "biomarker", "statistics", "prognosis", "methods"}
)
_TITLE_FACETS: Dict[str, List[str]] = {
    "safety": ["adverse", "toxicit*", "cardiotoxicit*", "hepatotoxicit*", "safety", "side effect*"],
    "epidemiology": ["prevalence", "incidence", "epidemiolog*", "burden"],
    "diagnosis": ["diagnos*", "criteria", "definition*"],
    "biomarker": ["biomarker*", "predict*", "marker*"],
}

_STOP = frozenset(
    """
    a an the of in on for with and or to by at from is are was were be been being does do did done what which who whom
    whose how why when where can could should would will may might must this that these those it its as vs versus than
    into about between after before during without not no any all my our their there here me i we you your please only
    also via per using use used among against toward towards whether if then so such more most less least very
    tell explain describe give show list find know want need help calculate design perform run measure determine
    recommended recommend best good main major key current currently latest new recent ongoing role effect effects
    impact work works working way ways type types kind kinds thing things much many some other others available
    approved common commonly typical typically usually patient patients people person adult adults human humans
    global worldwide overall general specific particular express expresses expressed expressing compare compared
    choice cause causes caused manage managed affect affects affected associated involved related significance
    significant relevance relevant importance important implication implications meaning mean means below above
    synthesize synthesise summarize summarise review analyze analyse investigate discuss outline overview evaluate
    examine explore characterize characterise across regarding
    identify rank ranked ranking top list distinguish explain cite sources each detail detailed provide
    biological biology supporting support key gap gaps primary secondary overall versus potential possible
    under over less greater higher lower increase increased decrease decreased reduce reduced adjustment
    adjustments adjust adjusted
    paper papers article articles publication publications literature pmid pmids doi dois three two four five six
    seven eight nine ten simulate simulated simulating simulation simulations validate validation hypothetical
    exactly otherwise retrieve retrieved unverified premise invent targeting scan scans sweep sweeps parameter
    parameters override overrides
    improve improves improved improving prolong prolongs prolonged including include includes
    step steps step-by-step incorporating incorporate incorporates
    """.split()
)
# Sentences of a request that only say how to answer ("For each, give its study type", "Otherwise state that ...").
_INSTRUCTION_SENTENCE_RE = re.compile(
    r"^\s*(?:for\s+each\s*,\s*(?:give|report|state|include|provide|list)\b|give|report|state|include|provide|return|show|list|cite|validate|verify|check|run|tell\s+me|"
    r"otherwise|if\b|do\s+not|don'?t|never|please|use|keep|format|answer|respond|explain\s+the\s+limit|distinguish\b)",
    re.I,
)
# Clauses inside any sentence that restrict or forbid an operation ("papers only", "do not run simulations").
_INSTRUCTION_CLAUSE_RE = re.compile(
    r"\b(?:papers?|literature|articles?|publications?|trials?|studies)\s+only\b|\bonly\s+(?:the\s+)?(?:papers?|literature)\b"
    r"|\b(?:do\s+not|don'?t|never)\b[^.;!?]*|\bwithout\s+(?:running|performing|doing|executing|using|any)\b[^.;!?]*",
    re.I,
)
# A literal code-shaped subject ("ZX999", "AMG-510") no resolver confirmed: searched exactly, never generalized.
_LITERAL_SUBJECT_RE = re.compile(r"(?<![\w\-])([A-Z]{1,5}-?\d{2,6})(?![\w\-])")
_ACCESSION_PREFIX_RE = re.compile(r"^(?:BIOMD|MODEL|NCT|PMID|PMC|GSE|GSM|VCV|EID|CHEMBL|EFO|MONDO|HP|GO|RS)\d", re.I)
# Content phrases naming a disease or tissue context, kept as hard anchors before softer topic words.
_DISEASE_PHRASE_RE = re.compile(
    r"(?:cancers?|carcinomas?|tumou?rs?|neoplasms?|leuka?emias?|lymphomas?|myelomas?|sarcomas?|gliomas?|melanomas?|blastomas?|mesotheliomas?|"
    r"diseases?|syndromes?|disorders?|fibrosis|diabetes|infections?|itis|emias?|aemias?|failure|deficiency)$",
    re.I,
)
# Single words too generic to anchor a search on their own.
_GENERIC = frozenset(
    "gene genes protein proteins cell cells disease diseases drug drugs study studies evidence data level levels "
    "factor factors treatment treatments therapy therapies management option options outcome outcomes target targets "
    "role roles mechanism mechanisms".split()
)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
# Words before a disease noun that stage or qualify it rather than name it ("resected stage II cutaneous melanoma"
# is searched as "cutaneous melanoma").
_DISEASE_PHRASE_BREAK = frozenset(
    """
    stage stages resected unresectable advanced metastatic recurrent refractory relapsed early late locally high-risk
    high low grade newly diagnosed adjuvant neoadjuvant maintenance first-line second-line pivotal efficacy
    """.split()
)
_ROMAN_STAGE_RE = re.compile(r"^(?:i{1,3}|iv|v)[a-d]?$|^\d", re.I)
# Qualifiers dropped from a disease name to search its broader form ("cutaneous melanoma" -> "melanoma"; the
# KEYNOTE-716 title says "resected stage IIB or IIC melanoma").
_DISEASE_MODIFIERS = frozenset(
    """
    cutaneous advanced metastatic resected unresectable recurrent refractory relapsed early late locally primary
    high-risk high low grade stage adult adults pediatric paediatric childhood newly diagnosed invasive ductal
    """.split()
)
# Disease-name words too generic to show an article is about this disease ("cancer", "cell", "non-small").
_DISEASE_GENERIC = frozenset(
    """
    cancer cancers carcinoma carcinomas adenocarcinoma adenocarcinomas tumor tumors tumour tumours neoplasm neoplasms
    malignancy malignancies disease diseases syndrome syndromes disorder disorders cell cells small non non-small
    type types squamous solid chronic acute stage positive negative mutant mutated
    """.split()
) | _DISEASE_MODIFIERS
# Phrases in a question that ask for trial evidence of an intervention's effect.
_PIVOTAL_RE = re.compile(
    r"\b(trials?|pivotal|phase\s*(?:2|3|ii|iii)|randomi[sz]ed|end[\s\-]?points?|hazard\s+ratios?|overall\s+survival|"
    r"progression[\s\-]free|(?:recurrence|relapse|event|disease|distant\s+metastasis)[\s\-]free|efficacy|efficacious|"
    r"standard\s+of\s+care|approv\w*|first[\s\-]line|second[\s\-]line|adjuvant|neoadjuvant|maintenance)\b",
    re.I,
)
# Alternatives between two drug mentions ("pembrolizumab or nivolumab"): one concept group, not an AND of two.
_DRUG_ALTERNATIVE_RE = re.compile(r"^\s*(?:,\s*)?(?:or|and/or|vs\.?|versus|/|,)\s*$", re.I)
_TRIAL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[\s\-]?[A-Za-z0-9]+){0,3}$")
PIVOTAL_SLOTS = 3
PIVOTAL_LIMIT = 8
MAX_TRIAL_NAMES = 6
THERAPEUTIC_YEARS = 5
TOPIC_LIMIT = 4

# What a question asks about its subject, beyond the subject and the disease: (name, pattern in the question,
# terms showing a paper covers it). Every aspect ranks the retrieved articles; the ones in _TOPIC_SEARCH also get
# their own search, so "repurposed ... in-silico binding predictions" finds repurposing and docking papers and not
# only the most-cited papers on the subject.
_ASPECTS = (
    ("repurposing", r"\b(?:repurpos\w*|re-?position\w*|drug\s+re-?use)\b", ["repurpos*", "reposition*"]),
    ("therapy", r"\b(?:treat(?:s|ed|ing|ment|ments)?|therap\w*|inhibitors?|drugs?|medications?|regimens?|pharmacotherap\w*)\b",
     ["inhibitor*", "treatment", "treated", "therap*", "drug*", "efficacy"]),
    ("in_silico", r"\b(?:in[\s\-]?silico|docking|virtual\s+screen\w*|binding\s+(?:affinit\w*|predict\w*|pose\w*|energ\w*)|"
                  r"molecular\s+dynamics|structure[\s\-]based\s+(?:drug|design|screen\w*))\b",
     ["docking", "virtual screening", "molecular dynamics", "in silico", "binding affinity"]),
    ("safety", r"\b(?:adverse\s+(?:events?|effects?|reactions?)|safety|toxicit\w*|side[\s\-]effects?|pharmacovigilance)\b",
     ["adverse event*", "adverse effect*", "adverse reaction*", "safety", "toxicit*", "pharmacovigilance"]),
    ("trials", r"\b(?:trials?|phase\s*(?:[1-3]|i{1,3})\b)", ["trial*", "phase"]),
    ("real_world", r"\b(?:real[\s\-]world|electronic\s+(?:health|medical)\s+records?|EHRs?|claims\s+data|registr(?:y|ies))\b",
     ["real-world", "electronic health record*", "electronic medical record*", "retrospective", "registry"]),
)
_ASPECT_RES = [(name, re.compile(pat, re.I), terms) for name, pat, terms in _ASPECTS]
# Aspects searched for on their own (subject AND disease AND aspect), with their search terms and sort.
_TOPIC_SEARCH: Dict[str, tuple] = {
    "therapy": (["inhibitor*", "targeted therapy", "targeted therapies"], "CITED desc"),
    "repurposing": (["repurpos*", "reposition*"], None),
    "in_silico": (["docking", "virtual screening", "molecular dynamics", "in silico"], None),
}
# A question asks for treatments when it says so ("repurposed", "drug discovery"), or names treating and a drug
# kind together, or is a treatment-type question naming a drug or therapy.
_THERAPEUTIC_STRONG_RE = re.compile(r"\b(?:repurpos\w*|re-?position\w*|drug\s+(?:discovery|candidates?|re-?use))\b", re.I)
_TREAT_RE = re.compile(r"\b(?:treat(?:s|ed|ing|ment|ments)?|therap\w*|pharmacotherap\w*)\b", re.I)
_DRUG_KIND_RE = re.compile(r"\b(?:drugs?|inhibitors?|compounds?|agents?|medications?|regimens?|antibod(?:y|ies))\b", re.I)
_THERAPEUTIC_TYPES = frozenset({"treatment", "regulatory", "trials", "comparison", "target", "methods"})
# Titles of diagnostic and technical papers (assays, biopsies, cfDNA detection): for a question about treatment
# they rank below papers about treatment unless the title is about treatment too. A term the question itself
# uses is never demoted ("diagnosis and treatment of ...").
_DIAGNOSTIC_TITLE_TERMS = (
    "diagnos*", "biops*", "assay*", "detection", "detecting", "detect", "cfDNA", "ctDNA", "cell-free DNA",
    "circulating tumor DNA", "circulating tumour DNA", "PCR", "ddPCR", "droplet digital", "fine-needle", "fine needle",
    "FNA", "cytolog*", "genotyping", "mutation testing", "mutation analysis", "specimen*",
)
_ON_TOPIC_TITLE_TERMS = ("inhibit*", "treat*", "therap*", "drug*", "repurpos*", "reposition*", "efficacy", "respon*")
# Unicode hyphens and dashes (U+2010-U+2015, U+2212) as ASCII, one character for one so entity spans still hold.
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−"})
# Questions longer than this are searched on the web by their concepts, not verbatim.
WEB_QUERY_MAX_WORDS = 16
_FDA_APPROVED_RE = re.compile(r"\bFDA[\s\-]approved\b", re.I)
_WEB_ASPECT_WORDS = {
    "repurposing": "drug repurposing", "therapy": "treatment", "in_silico": "docking", "trials": "clinical trials",
    "safety": "safety", "real_world": "real-world",
}


def normalize_dashes(text: str) -> str:
    """Unicode hyphens and dashes as ASCII "-" ("KRAS‑mutant" -> "KRAS-mutant"); the length is unchanged."""
    return text.translate(_DASHES)


def question_aspects(query: str) -> Dict[str, List[str]]:
    """The aspects a question asks about (repurposing, therapy, in-silico, safety, trials, real-world data), each
    with the terms that show an article covers it."""
    text = normalize_dashes(query)
    return {name: list(terms) for name, pat, terms in _ASPECT_RES if pat.search(text)}


def is_therapeutic_question(query: str, question_type: str, names_drug: bool = False) -> bool:
    """Whether the question asks for treatments: it says repurposing or drug discovery, or it is a treatment,
    trial, regulatory, comparison or target question that mentions treating, a drug kind or a named drug.
    Mechanism, diagnosis, safety and other questions are not, however they mention drugs."""
    text = normalize_dashes(query)
    if _THERAPEUTIC_STRONG_RE.search(text):
        return True
    if question_type not in _THERAPEUTIC_TYPES:
        return False
    if question_type == "methods":
        # "Design a pipeline to find drugs for treating X" is; "a protocol to measure ctDNA response to sotorasib
        # treatment" is about the assay.
        return bool(_TREAT_RE.search(text) and _DRUG_KIND_RE.search(text))
    return bool(_TREAT_RE.search(text) or _DRUG_KIND_RE.search(text) or names_drug)


def off_topic_title_terms(query: str) -> List[str]:
    """Diagnostic and technical title terms that mark a paper off topic for a treatment question; the ones the
    question uses itself are left out."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", normalize_dashes(query).lower()) + " "
    out = []
    for term in _DIAGNOSTIC_TITLE_TERMS:
        stem = re.sub(r"[^a-z0-9]+", " ", term.rstrip("*").lower()).strip()
        if f" {stem}" not in text:
            out.append(term)
    return out
_PIVOTAL_DESIGN = (
    '(PUB_TYPE:"Randomized Controlled Trial" OR PUB_TYPE:"Clinical Trial, Phase III" '
    'OR TITLE_ABS:"phase 3" OR TITLE_ABS:"phase III")'
)


@dataclass
class SearchPlan:
    question_type: str = "general"
    concepts: List[List[str]] = field(default_factory=list)
    web_query: str = ""
    source: str = "deterministic"  # deterministic|llm
    # Concepts past the search's group limit ("acquired resistance" after EGFR, osimertinib, NSCLC): not searched
    # for, but they rank the retrieved passages, so the one about the question's actual topic comes first.
    ranking_terms: List[str] = field(default_factory=list)
    # Leading concept groups that are the question's subject (target, mutation, drug, disease, literal code).
    # Relaxed tiers drop facets and softer topic words first and keep every hard anchor.
    hard_groups: int = 0
    # The diseases the question names (with synonyms), even past the group limit: retrieved articles that never
    # mention them are ranked below those that do (synthesis_engine).
    disease_terms: List[str] = field(default_factory=list)
    # The drugs (or, failing that, the intervention phrase) whose trial evidence a "pivotal" search looks for.
    intervention_terms: List[str] = field(default_factory=list)
    pivotal: bool = False  # the question asks about trial efficacy of an intervention in a disease
    # Trial names ("KEYNOTE-716", "CheckMate 76K") searched as quoted phrases; set by the caller when known.
    trial_names: List[str] = field(default_factory=list)
    # What the question asks about the subject (question_aspects): {"repurposing": [terms], "in_silico": [...]}.
    aspects: Dict[str, List[str]] = field(default_factory=dict)
    # The question asks for treatments (drugs to repurpose, a therapy's efficacy): treatment searches are added
    # and diagnostic or technical papers rank below papers about treatment.
    therapeutic: bool = False
    # How many leading concept groups are the question's subject (a gene, target or literal code, not a drug or
    # disease); the per-aspect searches are anchored on the first.
    subject_groups: int = 0
    # For a treatment question: title terms of diagnostic and technical papers the question does not ask about.
    off_topic_terms: List[str] = field(default_factory=list)

    @property
    def facet_type(self) -> str:
        """The question type whose facet narrows the search: a design-a-pipeline question about finding treatments
        searches for treatment papers, not for protocols."""
        return "treatment" if self.question_type == "methods" and self.therapeutic else self.question_type

    def topic(self) -> Dict[str, Any]:
        """What the literature ranking needs beyond the disease: the question's aspects and, for a treatment
        question, the title terms of off-topic (diagnostic, technical) papers."""
        out: Dict[str, Any] = {}
        if self.aspects:
            out["aspects"] = {k: list(v) for k, v in self.aspects.items()}
        if self.therapeutic and self.off_topic_terms:
            out["off_topic"] = list(self.off_topic_terms)
            out["on_topic"] = list(_ON_TOPIC_TITLE_TERMS) + list(self.intervention_terms)
        return out

    def keywords(self) -> List[str]:
        """Lower-case content words of every concept, ranking-only term and question-type facet, for ranking passages."""
        terms = [t for g in self.concepts for t in g] + list(self.ranking_terms) + _FACETS.get(self.facet_type, [])
        words = [w.lower().rstrip("*") for t in terms for w in _WORD_RE.findall(t.replace("*", ""))]
        return list(dict.fromkeys(w for w in words if len(w) > 2 and w not in _STOP))

    def europepmc_queries(self, year: Optional[int] = None) -> List[Dict[str, Any]]:
        """Tiered EuropePMC searches: [{"query", "sort", "tier", "label"}]. Empty when there is nothing to search."""
        if not self.concepts:
            return []
        year = year or _dt.date.today().year
        anchor, others = self.concepts[0], self.concepts[1:MAX_GROUPS]
        facet_type = self.facet_type
        facet = _FACETS.get(facet_type)
        peer = "HAS_ABSTRACT:y AND SRC:MED"

        def clause(*parts: Optional[str]) -> str:
            return " AND ".join(p for p in parts if p)

        rest = [_group("TITLE_ABS", g) for g in others]
        facet_clause = _group("TITLE_ABS", facet) if facet else None
        # Citation ranking favours famous papers, so the landmark search needs the facet where it is most telling.
        landmark_facet = facet_clause
        if facet_type in _TITLE_FACET_TYPES and facet:
            landmark_facet = _group("TITLE", _TITLE_FACETS.get(facet_type, facet))
        landmark = clause(_group("TITLE", anchor), *rest, landmark_facet, peer, f"PUB_YEAR:[{year - LANDMARK_YEARS} TO {year}]")
        recent = clause(_group("TITLE_ABS", anchor), *rest, facet_clause, peer, f"PUB_YEAR:[{year - RECENT_YEARS} TO {year}]")
        queries: List[Dict[str, Any]] = [
            {"query": landmark, "sort": "CITED desc", "tier": 1, "label": "landmark"},
            {"query": recent, "sort": None, "tier": 1, "label": "recent"},
        ]
        kept = rest[: max(1, min(self.hard_groups, MAX_GROUPS) - 1)]
        relaxed_landmark = clause(_group("TITLE_ABS", anchor), *kept, peer, f"PUB_YEAR:[{year - LANDMARK_YEARS} TO {year}]")
        relaxed = clause(_group("TITLE_ABS", anchor), *kept, "HAS_ABSTRACT:y")
        if self.pivotal:
            pivotal = pivotal_trial_query(self.intervention_terms, self.disease_terms)
            if pivotal is not None:
                queries.append(pivotal)
        if self.trial_names:
            named = trial_name_query(self.trial_names, self.disease_terms)
            if named is not None:
                queries.append(named)
        queries.extend(self._topic_queries(year))
        for q, sort, label in ((relaxed_landmark, "CITED desc", "landmark_relaxed"), (relaxed, None, "relaxed")):
            if q not in (landmark, recent):
                queries.append({"query": q, "sort": sort, "tier": 2, "label": label})
        # Last resort: every content word of the first two concepts, unquoted ("CAR-T cytokine release syndrome").
        words = [w for g in self.concepts[:2] for w in _WORD_RE.findall(g[0]) if len(w) > 2 and w.lower() not in _STOP]
        if len(words) > 1:
            queries.append({
                "query": clause(*(f"TITLE_ABS:{_lucene_word(w)}" for w in dict.fromkeys(words)), "HAS_ABSTRACT:y"),
                "sort": None, "tier": 3, "label": "words",
            })
        topic = self.topic()
        for q in queries:
            if self.disease_terms:
                # The ranking step demotes retrieved articles that never mention the question's disease.
                q["disease"] = list(self.disease_terms)
            if topic:
                # ... and ranks articles by the question's aspects (repurposing, docking, safety).
                q["topic"] = topic
        return queries

    def _topic_queries(self, year: int) -> List[Dict[str, Any]]:
        """For a treatment question about a subject (a gene or target, not a drug or disease): the subject in the
        disease AND each searched aspect the question asks about ("KRAS" AND pancreatic cancer AND (inhibitor* OR
        "targeted therapy"); ... AND (repurpos* OR reposition*); ... AND (docking OR "virtual screening")).
        Treatment papers are citation-ranked over the last THERAPEUTIC_YEARS; the others by relevance."""
        if not self.therapeutic:
            return []
        subject = self.concepts[0] if self.subject_groups else None
        diseases = disease_search_terms(self.disease_terms)
        disease_clause = _group("TITLE_ABS", diseases) if diseases else None
        out: List[Dict[str, Any]] = []
        for name, (terms, sort) in _TOPIC_SEARCH.items():
            if name not in self.aspects:
                continue
            if subject is None and not (name == "repurposing" and disease_clause):
                continue  # a treatment or docking search needs a target; repurposing can be for a disease alone
            parts = [
                _group("TITLE" if name == "therapy" else "TITLE_ABS", subject) if subject else None,
                disease_clause, _group("TITLE_ABS", terms), "HAS_ABSTRACT:y AND SRC:MED",
            ]
            if name == "therapy":
                parts.append(f"PUB_YEAR:[{year - THERAPEUTIC_YEARS} TO {year}]")
            out.append({"query": " AND ".join(p for p in parts if p), "sort": sort, "tier": 1, "label": name,
                        "limit": TOPIC_LIMIT})
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"question_type": self.question_type, "concepts": [list(g) for g in self.concepts],
                "ranking_terms": list(self.ranking_terms), "web_query": self.web_query, "source": self.source,
                "hard_groups": self.hard_groups, "disease_terms": list(self.disease_terms),
                "intervention_terms": list(self.intervention_terms), "pivotal": self.pivotal,
                "trial_names": list(self.trial_names), "aspects": {k: list(v) for k, v in self.aspects.items()},
                "therapeutic": self.therapeutic, "subject_groups": self.subject_groups,
                "off_topic_terms": list(self.off_topic_terms)}


def _clean_term(term: str) -> str:
    term = re.sub(r'["\\]', " ", term)
    return re.sub(r"\s+", " ", term).strip(" .,;:?!()[]{}")


def _variants(term: str) -> List[str]:
    """A term plus its form without a possessive ("Alzheimer's disease" -> "Alzheimer disease")."""
    out = [term]
    plain = re.sub(r"(\w)['’]s\b", r"\1", term)
    if plain != term:
        out.append(plain)
    return out


def _lucene_word(word: str) -> str:
    """One word for an unquoted query clause, quoted when it holds a character Lucene would parse ("CAR-T")."""
    return word if re.fullmatch(r"[A-Za-z0-9]+\*?", word) else f'"{word}"'


def _group(field_name: str, terms: Sequence[str]) -> str:
    # A single word ending in * is a prefix search and must stay unquoted ("toxicit*").
    parts = [f"{field_name}:{t}" if re.fullmatch(r"[A-Za-z]+\*", t) else f'{field_name}:"{t}"' for t in terms if t]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def disease_search_terms(terms: Sequence[str], limit: int = 6) -> List[str]:
    """Disease names plus their broader forms for searching: qualifiers dropped ("cutaneous melanoma" ->
    "melanoma") and a carcinoma named as the organ's cancer ("pancreatic ductal adenocarcinoma" ->
    "pancreatic adenocarcinoma", "pancreatic cancer")."""
    out: List[str] = []

    def push(t: str) -> None:
        t = _clean_term(t)
        if len(t) >= 3 and t.lower() not in (o.lower() for o in out):
            out.append(t)

    for term in terms:
        if not isinstance(term, str):
            continue
        push(term)
        words = term.split()
        core = [w for w in words if w.lower() not in _DISEASE_MODIFIERS and not _ROMAN_STAGE_RE.match(w)]
        if core and len(core) < len(words):
            push(" ".join(core))
        if len(core) >= 2 and core[-1].lower().rstrip("s") in ("carcinoma", "adenocarcinoma") and core[0].lower() not in _DISEASE_GENERIC:
            push(f"{core[0]} cancer")
    return out[:limit]


def disease_relevance_terms(terms: Sequence[str]) -> List[str]:
    """Terms any one of which shows an article is about the disease: the names and broader forms, their
    distinctive words ("melanoma", "lung"), and organ adjectives as prefixes ("pancrea*" for "pancreatic",
    which also matches "pancreas"). A trailing * marks a prefix match."""
    out: List[str] = []
    for t in disease_search_terms(terms, limit=12):
        out.append(t)
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", t):
            low = w.lower()
            if low in _DISEASE_GENERIC or len(low) < 4:
                if re.fullmatch(r"[A-Z0-9]{3,}", w):  # an acronym alias ("PDAC", "GBM", "NSCLC")
                    out.append(w)
                continue
            stem = ""
            for suffix in ("tic", "ian", "ic", "al", "ar", "ine"):
                if low.endswith(suffix) and len(low) - len(suffix) >= 5:
                    stem = low[: len(low) - len(suffix)]
                    break
            out.append(stem + "*" if stem else low)
    return list(dict.fromkeys(out))


def _intervention_terms(terms: Sequence[str]) -> List[str]:
    """Intervention names plus the singular of a drug-class plural ("PARP inhibitors" -> "PARP inhibitor")."""
    out: List[str] = []
    for t in terms:
        if not isinstance(t, str):
            continue
        t = _clean_term(t)
        singular = re.sub(r"\b(inhibitor|agonist|antagonist|blocker)s\b", r"\1", t, flags=re.I)
        singular = re.sub(r"\bantibodies\b", "antibody", singular, flags=re.I)
        for v in (t, singular):
            if len(v) >= 3 and v.lower() not in (o.lower() for o in out):
                out.append(v)
    return out[:6]


def pivotal_trial_query(interventions: Sequence[str], disease_terms: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Citation-ranked randomized / phase 3 trials of (any of) the interventions in the disease:
    (drug1 OR drug2) AND disease AND (RCT OR phase 3). Its hits are guaranteed up to "slots" ranked articles."""
    drugs, diseases = _intervention_terms(interventions), disease_search_terms(disease_terms)
    if not drugs or not diseases:
        return None
    query = " AND ".join([_group("TITLE_ABS", drugs), _group("TITLE_ABS", diseases), _PIVOTAL_DESIGN, "SRC:MED"])
    return {"query": query, "sort": "CITED desc", "tier": 1, "label": "pivotal", "limit": PIVOTAL_LIMIT,
            "slots": PIVOTAL_SLOTS}


def clean_trial_names(names: Optional[Sequence[Any]]) -> List[str]:
    """Trial names safe to search as quoted phrases ("KEYNOTE-716", "CheckMate 76K", "SOLO-1")."""
    out: List[str] = []
    for raw in names or []:
        if not isinstance(raw, str):
            continue
        name = " ".join(raw.split()).strip(" .,;:()[]")
        if 3 <= len(name) <= 40 and _TRIAL_NAME_RE.match(name) and name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out[:MAX_TRIAL_NAMES]


def trial_name_query(names: Optional[Sequence[Any]], disease_terms: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """Named trials as quoted title/abstract phrases. A name with a number ("KEYNOTE-716") is specific on its own;
    a bare word ("PRIMA", "PROfound") must appear together with the disease when one is known."""
    cleaned = clean_trial_names(names)
    if not cleaned:
        return None
    numbered = [n for n in cleaned if re.search(r"\d", n)]
    plain = [n for n in cleaned if n not in numbered]
    diseases = disease_search_terms(disease_terms)
    parts = []
    if numbered:
        parts.append(_group("TITLE_ABS", numbered))
    if plain:
        named = _group("TITLE_ABS", plain)
        parts.append(f"({named} AND {_group('TITLE_ABS', diseases)})" if diseases else named)
    clause = parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
    return {"query": f"{clause} AND SRC:MED", "sort": "CITED desc", "tier": 1, "label": "trial_names",
            "limit": min(10, 2 * len(cleaned) + 2), "slots": min(len(cleaned), 4), "trial_names": cleaned}


def _normalize_group(terms: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for raw in terms:
        if not isinstance(raw, str):
            continue
        t = _clean_term(raw)
        if len(t) < 3 or len(t) > 80 or len(t.split()) > 6:
            continue
        for v in _variants(t):
            if v.lower() not in (o.lower() for o in out):
                out.append(v)
    return out[:MAX_TERMS]


def classify_question(query: str) -> str:
    for name, pat in _TYPE_RES:
        if pat.search(query):
            return name
    return "general"


def _content_phrases(query: str, covered: Sequence[tuple]) -> List[str]:
    """Runs of content words outside entity spans and question-type words, at most four each ("skeletal muscle")."""
    covered = [*covered, *(m.span() for _, pat in _TYPE_RES for m in pat.finditer(query))]
    phrases: List[str] = []
    run: List[str] = []
    facet_words = {w for ws in _FACETS.values() for f in ws for w in f.split()}

    def emit(words: List[str]) -> None:
        if words and not (len(words) == 1 and (words[0].lower() in _GENERIC or len(words[0]) < 3)):
            phrases.append(" ".join(words[:4]))

    def flush() -> None:
        words = [w for w in run if w.lower() not in facet_words] or []
        heads = [i for i, w in enumerate(words) if _DISEASE_PHRASE_RE.search(w)]
        if heads and (len(words) > 4 or heads[-1] != len(words) - 1):
            # "resected stage II cutaneous melanoma": the disease is its own phrase, its qualifiers another.
            i = heads[-1]
            start = i
            while start > 0 and i - start < 2:
                prev = words[start - 1]
                if prev.lower() in _DISEASE_PHRASE_BREAK or _ROMAN_STAGE_RE.match(prev):
                    break
                start -= 1
            emit(words[:start])
            emit(words[start:i + 1])
            emit(words[i + 1:])
        else:
            emit(words)
        run.clear()

    for m in _WORD_RE.finditer(query):
        word = m.group(0).strip("'-")
        inside = any(s <= m.start() < e for s, e in covered)
        if inside or not word or word.lower() in _STOP or word.isdigit():
            flush()
            continue
        run.append(word)
    flush()
    return list(dict.fromkeys(phrases))


def build_search_plan(ctx: Any, query: str) -> SearchPlan:
    """The deterministic plan: entities first (primary gene, drugs, diseases), then content phrases."""
    query = normalize_dashes(query)  # one character for one, so entity spans still hold
    groups: List[List[str]] = []
    covered: List[tuple] = []

    def add(terms: Sequence[str], span: Optional[tuple] = None, exact_only: bool = False) -> None:
        g = _normalize_group(terms)
        if span and span != (0, 0):
            covered.append(span)
        if not g:
            return
        lowered = {t.lower() for t in g}
        for existing in groups:
            ex = {t.lower() for t in existing}
            if lowered & ex:
                return
            if not exact_only and (any(a in b for a in lowered for b in ex) or any(b in a for a in lowered for b in ex)):
                return
        groups.append(g)

    entities = list(getattr(ctx, "entities", []) or [])

    def inside_longer(e: Any) -> bool:
        """'insulin' inside 'insulin resistance': the longer mention is the concept."""
        s, t = e.span
        return (s, t) != (0, 0) and any(
            o is not e and o.type != "variant" and o.span[0] <= s and t <= o.span[1] and (t - s) < (o.span[1] - o.span[0])
            for o in entities
        )

    # Drugs named as alternatives ("pembrolizumab or nivolumab") are one concept: either may answer.
    drugs = sorted((e for e in entities if e.type == "drug" and e.span != (0, 0) and not inside_longer(e)), key=lambda e: e.span[0])
    alternatives: Dict[int, List[Any]] = {}
    lead_of: Dict[int, Any] = {}
    for prev, cur in zip(drugs, drugs[1:]):
        if _DRUG_ALTERNATIVE_RE.match(query[prev.span[1]:cur.span[0]]):
            lead = lead_of.get(id(prev), prev)
            lead_of[id(cur)] = lead
            alternatives.setdefault(id(lead), [lead]).append(cur)
    disease_terms: List[str] = []
    drug_terms: List[str] = []

    # Leading groups naming the question's subject (a gene or target): counted while no drug or disease came first.
    subject_groups = 0

    def add_subject(terms: Sequence[str], span: Optional[tuple]) -> None:
        nonlocal subject_groups
        before = len(groups)
        add(terms, span)
        if len(groups) > before and subject_groups == before:
            subject_groups += 1

    primary = getattr(ctx, "primary_gene", None)
    if primary is not None and not inside_longer(primary):
        add_subject([primary.mention, primary.label, *getattr(primary, "aliases", [])[:2]], primary.span)
    for e in sorted(entities, key=lambda e: (e.span == (0, 0), e.span[0])):
        if e is primary or inside_longer(e):
            continue
        if e.type == "gene" and not e.ambiguous_with:
            add_subject([e.mention, e.label], e.span)
        elif e.type == "drug":
            drug_terms.extend(t for t in (e.mention, e.label) if t)
            if id(e) in lead_of:
                covered.append(e.span)  # searched inside the group of the first alternative
                continue
            members = alternatives.get(id(e), [e])
            if len(members) > 1:
                add([m.mention for m in members] + [m.label for m in members if m.label], e.span)
            else:
                add([e.mention, e.label], e.span)
        elif e.type == "disease":
            label = e.label if len((e.label or "").split()) <= 4 else ""
            disease_terms.extend(t for t in (e.mention, label, *e.aliases[:2]) if t)
            add([e.mention, *disease_search_terms([e.mention])[1:2], label, *e.aliases[:2]], e.span)
            for alias in e.aliases:  # "(IPF)" defined in the question is part of this concept, not a new one
                covered.extend(m.span() for m in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", query))
        elif e.type == "variant":
            # Searched as the change itself ("G12C", "c.68_69delAG", "EML4-ALK"), next to the gene's own group.
            change = e.protein_change or e.mention or e.label
            if e.gene and change.upper().startswith(e.gene.upper()) and e.variant_kind != "fusion":
                change = change[len(e.gene):].strip(" :-")
            add([change], e.span, exact_only=True)
    entity_spans = [e.span for e in entities if e.span != (0, 0)]
    instructions = _instruction_spans(query, entity_spans)
    covered.extend(instructions)
    covered.extend(_argument_spans(query))
    # Literal, unresolved subjects ("ZX999") are searched exactly; they are never replaced by generic terms.
    for m in _LITERAL_SUBJECT_RE.finditer(query):
        code = m.group(1)
        if _ACCESSION_PREFIX_RE.match(code) or any(s <= m.start() < e for s, e in covered):
            continue
        before = len(groups)
        add([code], m.span(), exact_only=True)
        if len(groups) > before and subject_groups == before:
            subject_groups += 1
    n_entity_groups = len(groups)
    phrases = _content_phrases(query, covered)
    # A disease or tissue context ("pancreatic cancer") is a hard anchor and outranks softer topic words.
    disease_phrases = [p for p in phrases if _DISEASE_PHRASE_RE.search(p)]
    phrases = disease_phrases + [p for p in phrases if p not in disease_phrases]
    disease_terms.extend(disease_phrases)
    # Diseases listed together ("in ovarian cancer and prostate cancer") are one concept: a paper about either
    # is on topic, and an AND of the two would find almost nothing.
    disease_alts: Dict[str, List[str]] = {}
    absorbed: set = set()
    for a, b in zip(disease_phrases, disease_phrases[1:]):
        if re.search(re.escape(a) + r"\s*,?\s*(?:and|or|and/or|,)\s+" + re.escape(b), query, re.I):
            lead = next((k for k, v in disease_alts.items() if a in v), a)
            disease_alts.setdefault(lead, [lead]).append(b)
            absorbed.add(b)

    def add_phrase(phrase: str) -> None:
        if phrase not in absorbed:
            add(disease_search_terms(disease_alts.get(phrase, [phrase]), limit=MAX_TERMS))

    asked = _without_spans(query, instructions)
    qtype = classify_question(asked)
    therapeutic = is_therapeutic_question(asked, qtype, names_drug=bool(drug_terms))
    aspects = question_aspects(asked)
    checklist: List[str] = []
    if therapeutic and n_entity_groups:
        # "repurposed", "in-silico binding predictions", "real-world EHR safety data" say what the answer must
        # cover about the subject: they rank the papers (and some get their own searches), but they are not
        # concepts every paper must mention.
        aspect_phrases = [p for p in phrases if p not in disease_phrases and any(pat.search(p) for _, pat, _ in _ASPECT_RES)]
        checklist.extend(aspect_phrases)
        phrases = [p for p in phrases if p not in aspect_phrases]
    n_hard = n_entity_groups
    is_brief = (len(phrases) > 3) or bool(re.search(r"\b(?:for\s+each\s+\w+,\s*explain|rank\s+(?:the\s+)?top\b|for\s+each,\s*give)\b", query, re.I))
    if n_entity_groups and is_brief:
        # A long brief ("explain its role, inhibitors, safety risks, evidence gaps") lists what the answer must cover,
        # not what the papers are about: search on the named entities, rank passages by the checklist.
        for phrase in disease_phrases:
            before = len(groups)
            add_phrase(phrase)
            n_hard += len(groups) - before
        checklist.extend(p for p in phrases if p not in disease_phrases)
    else:
        for phrase in phrases:
            before = len(groups)
            add_phrase(phrase)
            if phrase in disease_phrases:
                n_hard += len(groups) - before

    if qtype in ("methods", "statistics") and not (qtype == "methods" and therapeutic):
        # "How do I design a CRISPR knockout screen for drug resistance?" is about the method, not the disease.
        # "Design a pipeline to identify drugs to repurpose for KRAS-mutant pancreatic cancer" is about KRAS in
        # pancreatic cancer: its subject and disease stay the anchors.
        groups = groups[n_entity_groups:] + groups[:n_entity_groups]
        n_hard = 0
        subject_groups = 0
    web =_web_query(query, groups, aspects)
    diseases = _normalize_terms(disease_terms, 8)
    interventions = _normalize_terms(drug_terms, 6)
    asks_trials = bool(_PIVOTAL_RE.search(_without_spans(query, instructions)))
    if not interventions and asks_trials:
        # A drug class named after a target ("PARP inhibitors", "PD-1 antibodies") is the intervention.
        interventions = _target_drug_classes(query, entities)
    if not interventions and asks_trials:
        # No resolved drug: the intervention is the leading non-disease, non-gene concept ("PARP inhibitors"),
        # never a word about the trial itself ("pivotal", "efficacy").
        disease_l = {d.lower() for d in diseases}
        lead = next((
            g for g in groups[n_entity_groups:]
            if not ({t.lower() for t in g} & disease_l) and not _DISEASE_PHRASE_RE.search(g[0])
            and not _PIVOTAL_RE.fullmatch(g[0].strip())
        ), None)
        interventions = list(lead or [])
    return SearchPlan(
        question_type=qtype, concepts=groups[:MAX_GROUPS], web_query=web, source="deterministic",
        ranking_terms=[g[0] for g in groups[MAX_GROUPS:]] + checklist,
        hard_groups=min(n_hard, MAX_GROUPS, len(groups)),
        disease_terms=diseases, intervention_terms=interventions,
        pivotal=bool(diseases and interventions and (asks_trials or drug_terms)) and qtype not in ("methods", "statistics"),
        aspects=aspects, therapeutic=therapeutic, subject_groups=min(subject_groups, MAX_GROUPS),
        off_topic_terms=off_topic_title_terms(asked) if therapeutic else [],
    )


_DRUG_CLASS_SUFFIX = r"[\s\-]+(inhibitors?|antagonists?|agonists?|blockers?|antibod(?:y|ies)|degraders?)\b"


def _target_drug_classes(query: str, entities: Sequence[Any]) -> List[str]:
    """Drug classes the question names after a gene or target ("PARP inhibitors", "KRAS G12C inhibitors")."""
    out: List[str] = []
    for e in entities:
        if getattr(e, "type", None) != "gene" or not getattr(e, "mention", None):
            continue
        m = re.search(rf"(?<![\w\-]){re.escape(e.mention)}(?:[\s\-]+[A-Z]\d+[A-Z])?{_DRUG_CLASS_SUFFIX}", query)
        if m and m.group(0) not in out:
            out.append(" ".join(m.group(0).split()))
    return out


def _web_query(query: str, groups: Sequence[Sequence[str]], aspects: Dict[str, List[str]]) -> str:
    """Web search engines read natural language well, so a short question is its own web query. A long one
    (a brief listing what to incorporate) is searched by its concepts plus the aspects it asks about
    ("KRAS pancreatic cancer FDA-approved drug repurposing docking clinical trials"): search engines match
    every word, and a 40-word brief finds nothing."""
    text = " ".join(query.split())
    words = text.split()
    if len(words) <= WEB_QUERY_MAX_WORDS or not groups:
        return _cap_words(text, 200)
    parts = [g[0] for g in groups[:MAX_GROUPS] if g]
    if _FDA_APPROVED_RE.search(text):
        parts.append("FDA-approved")
    picked = [_WEB_ASPECT_WORDS[a] for a in ("repurposing", "therapy", "in_silico", "trials", "safety", "real_world") if a in aspects]
    if "repurposing" in aspects:
        picked = [w for w in picked if w != "treatment"]
    parts.extend(picked[:3])
    return _cap_words(" ".join(dict.fromkeys(parts)), 200)


def _cap_words(text: str, limit: int) -> str:
    """text cut to at most limit characters at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def _normalize_terms(terms: Sequence[Any], limit: int) -> List[str]:
    out: List[str] = []
    for raw in terms:
        if not isinstance(raw, str):
            continue
        t = _clean_term(raw)
        if 3 <= len(t) <= 80 and t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out[:limit]


def _instruction_spans(query: str, entity_spans: Sequence[tuple]) -> List[tuple]:
    """Spans that only say how to answer: instruction clauses anywhere, and instruction sentences after the first
    one that name no entity ("For each, give its study type ...", "Otherwise state that ...")."""
    spans = [m.span() for m in _INSTRUCTION_CLAUSE_RE.finditer(query)]
    for i, m in enumerate(re.finditer(r"[^.;!?]+[.;!?]*", query)):
        if i == 0 or not _INSTRUCTION_SENTENCE_RE.match(m.group(0)):
            continue
        if any(m.start() <= s < m.end() for s, _ in entity_spans):
            continue
        spans.append(m.span())
    return spans


_MODEL_ACCESSION_RE = re.compile(r"(?<![A-Za-z0-9])(?:BIOMD|MODEL)\d{10}(?![0-9])", re.I)


def _argument_spans(query: str) -> List[tuple]:
    """Simulation arguments (durations, override mappings, scan ranges, model accessions): never search terms."""
    from .query_planner import procedural_spans  # the planner imports this module; resolve at call time

    return [*procedural_spans(query), *(m.span() for m in _MODEL_ACCESSION_RE.finditer(query))]


def _without_spans(query: str, spans: Sequence[tuple]) -> str:
    out = query
    for s, e in sorted(spans, reverse=True):
        out = out[:s] + " " * (e - s) + out[e:]
    return out


def _stems(text: str) -> set:
    return {w.lower()[:5] for w in _WORD_RE.findall(text) if len(w) >= 3 and w.lower() not in _STOP}


def merge_llm_plan(base: SearchPlan, raw: Optional[Dict[str, Any]], query: str) -> SearchPlan:
    """Use the planner model's concept groups, type and web query where they are valid; keep the rest of base."""
    if not isinstance(raw, dict):
        return base
    q_stems = _stems(query)
    groups: List[List[str]] = []
    for g in raw.get("concepts") or []:
        if not isinstance(g, list):
            continue
        terms = _normalize_group(g)
        # A group must share a word with the question: synonyms are welcome, new topics are not.
        if terms and any(_stems(t) & q_stems for t in terms):
            groups.append(terms)
        if len(groups) == MAX_GROUPS:
            break
    qtype = raw.get("question_type")
    web = raw.get("web_query")
    web = _clean_term(normalize_dashes(web)) if isinstance(web, str) and 3 <= len(web) <= 200 else ""
    if not groups and not web:
        return base

    def stems_of(g: Sequence[str]) -> set:
        return set().union(*(_stems(t) for t in g)) if g else set()

    # The question's own subject (target, drug, disease, a literal code such as "ZX999") is never dropped or
    # replaced by the model's concepts: a hard anchor the model left out leads the merged plan.
    base_hard = [list(g) for g in base.concepts[: base.hard_groups]]
    missing = [g for g in base_hard if not any(stems_of(g) & stems_of(kept) for kept in groups)]
    groups = (missing + groups)[:MAX_GROUPS]
    # The model's groups lead (they carry synonyms); a deterministic concept the model left out is kept after them.
    for g in base.concepts:
        if len(groups) == MAX_GROUPS:
            break
        stems = set().union(*(_stems(t) for t in g))
        if not any(stems & set().union(*(_stems(t) for t in kept)) for kept in groups):
            groups.append(list(g))
    hard = 0
    for g in groups:
        if not any(stems_of(g) & stems_of(b) for b in base_hard):
            break
        hard += 1
    return SearchPlan(
        question_type=qtype if qtype in QUESTION_TYPES else base.question_type,
        concepts=groups or base.concepts,
        web_query=web or base.web_query,
        source="llm",
        ranking_terms=list(base.ranking_terms),
        hard_groups=hard if groups else base.hard_groups,
        disease_terms=list(base.disease_terms),
        intervention_terms=list(base.intervention_terms),
        pivotal=base.pivotal,
        trial_names=list(base.trial_names),
        aspects={k: list(v) for k, v in base.aspects.items()},
        therapeutic=base.therapeutic,
        subject_groups=_kept_subject_groups(base, groups) if groups else base.subject_groups,
        off_topic_terms=list(base.off_topic_terms),
    )


def _kept_subject_groups(base: SearchPlan, groups: Sequence[Sequence[str]]) -> int:
    """How many leading merged groups are the base plan's subject groups (by shared word stems)."""
    subjects = [set().union(*(_stems(t) for t in g)) for g in base.concepts[: base.subject_groups]]
    n = 0
    for g in groups:
        stems = set().union(*(_stems(t) for t in g)) if g else set()
        if not any(stems & s for s in subjects):
            break
        n += 1
    return n


__all__ = [
    "QUESTION_TYPES", "SearchPlan", "build_search_plan", "classify_question", "clean_trial_names",
    "disease_relevance_terms", "disease_search_terms", "is_therapeutic_question", "merge_llm_plan", "normalize_dashes",
    "off_topic_title_terms", "pivotal_trial_query", "question_aspects", "trial_name_query",
]
