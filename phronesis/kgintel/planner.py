"""
Choose which PrimeKG analyses answer a question, from the resolved entities, the question text and the
search-plan question type. Pure: takes plain strings, returns a list of (operation, kwargs, reason).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analytics import indication_proxy
from .graph import PrimeKG, normalize_name

REPURPOSE_RE = re.compile(
    r"\b(?:repurpos\w*|reposition\w*|new (?:indications?|uses?|therap\w*)|could (?:also )?treat|might treat|"
    r"candidate (?:drugs?|therap\w*|compounds?)|alternative (?:drugs?|therap\w*|treatments?)|"
    r"which (?:existing |approved )?drugs?|what (?:existing |approved )?drugs?|drugs? (?:that|which) (?:could|might|may)|"
    r"off[- ]label|novel (?:therap\w*|treatments?))\b",
    re.IGNORECASE,
)
DDX_RE = re.compile(
    r"\b(?:differential|diagnos\w*|which (?:disease|condition|syndrome|disorder)s?|what (?:disease|condition|syndrome|disorder)s?|"
    r"present(?:s|ing)? with|presentation|symptoms?|phenotypes?|signs? (?:of|and)|could (?:this|it) be|rare disease)\b",
    re.IGNORECASE,
)
POLY_RE = re.compile(
    r"\b(?:interact\w*|combination|combined|together|co-?administ\w*|polypharm\w*|regimen|concomitant|"
    r"drug[- ]drug|ddi|taking .+ (?:and|with) )",
    re.IGNORECASE,
)
PATH_RE = re.compile(
    r"\b(?:mechanis\w*|how (?:does|do|might|could|is|are)|why|link\w*|connect\w*|relationship|relate[sd]?|"
    r"explain\w*|paths?|via|between|underl\w+|rationale)\b",
    re.IGNORECASE,
)
ENRICH_RE = re.compile(
    r"\b(?:enrich\w*|over-?represent\w*|gene (?:set|list|signature|module)s?|these genes|this gene set|"
    r"common pathways?|shared pathways?|pathways? (?:of|for|shared by|common to))\b",
    re.IGNORECASE,
)
SIMILAR_RE = re.compile(r"\b(?:similar|resembl\w*|analog\w*|comparable|related (?:diseases?|drugs?|genes?))\b", re.IGNORECASE)
KG_RE = re.compile(r"\b(?:prime ?kg|knowledge graph|biomedical graph)\b", re.IGNORECASE)
NEGATION_RE = re.compile(r"\b(?:no|without|absence of|absent|denies|negative for|not)\s+((?:[\w'-]+\s*){1,5})", re.IGNORECASE)

# Common words that are also PrimeKG phenotype / disease names and would otherwise match every question.
PHENOTYPE_STOP = {
    "all", "death", "pain", "obesity", "cancer", "disease", "drug", "inflammation", "infection", "tumor", "fever",
    "mutation", "abnormality", "phenotypic abnormality", "mode of inheritance", "clinical modifier", "frequency",
    "onset", "chronic", "acute", "progressive", "severe", "mild", "variable", "recurrent", "episodic", "drugs",
}
DISEASE_STOP = {"disease", "syndrome", "cancer", "tumor", "infection", "disorder", "pain", "obesity", "inflammation",
                "deficiency", "carcinoma", "anemia", "allergy", "lesion", "malformation"}

MAX_ANALYSES = 5


@dataclass
class Plan:
    analyses: List[Tuple[str, Dict[str, Any], str]] = field(default_factory=list)
    entities: Dict[str, List[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def add(self, op: str, kwargs: Dict[str, Any], reason: str) -> None:
        if len(self.analyses) < MAX_ANALYSES and (op, kwargs) not in [(o, k) for o, k, _ in self.analyses]:
            self.analyses.append((op, kwargs, reason))


def has_kg_intent(text: str) -> bool:
    return any(r.search(text) for r in (REPURPOSE_RE, DDX_RE, POLY_RE, ENRICH_RE, KG_RE))


def _symbols_in_text(kg: PrimeKG, text: str) -> List[str]:
    out = []
    for tok in re.findall(r"\b[A-Z][A-Z0-9-]{1,11}\b", text):
        if len(tok) >= 3 and kg._by_id.get(f"SYMBOL_{tok}") and tok not in out:
            out.append(tok)
    return out


def extract_phenotypes(kg: PrimeKG, text: str) -> Tuple[List[str], List[str]]:
    """(present, absent) HPO phenotype names found verbatim in text; 'no X' / 'without X' marks X absent."""
    negated_spans = [normalize_name(m.group(1)) for m in NEGATION_RE.finditer(text)]
    hits = kg.scan_text(text, ["effect/phenotype"], max_ngram=6, min_chars=4, stop=PHENOTYPE_STOP)
    present, absent = [], []
    for idx, phrase in hits:
        name = kg.name(idx)
        if any(phrase and (span.startswith(phrase) or f" {phrase}" in f" {span}") for span in negated_spans):
            absent.append(name)
        else:
            present.append(name)
    return list(dict.fromkeys(present)), list(dict.fromkeys(absent))


_ID_RE = re.compile(r"^(?:MONDO|DOID|HP|EFO|ORPHANET|OMIM|UMLS|MESH)[_:]\w+$", re.IGNORECASE)


def _disease_labels(kg: PrimeKG, diseases: Sequence[str], names: Sequence[str]) -> Dict[str, str]:
    """{query: readable name}. Diseases are queried by ontology id when one is known; the reader sees the name
    the question used (or PrimeKG's), never 'MONDO_0005184'."""
    out: Dict[str, str] = {}
    for k, d in enumerate(diseases):
        name = str(names[k]).strip() if k < len(names) and names[k] else ""
        if not name or _ID_RE.match(name):
            name = ""
            if _ID_RE.match(str(d)):
                hits = kg.resolve(str(d), node_types=["disease"], limit=1)
                name = kg.name(hits[0].index) if hits else ""
        out[d] = name or str(d)
    return out


# "KRAS-mutant", "KRAS mutated", "KRAS G12C", "EGFR-driven": the gene whose pathway the question is about.
_MUTANT_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{1,9})(?:[\s\-]+[A-Z]\d{1,4}[A-Z*]?)?[\s\-]+(?:mutant|mutated|mutations?|driven|altered)\b"
    r"|\b([A-Z][A-Z0-9]{1,9})[\s\-]+[A-Z]\d{1,4}[A-Z]\b"
)


def mutant_genes_in_text(kg: PrimeKG, text: str) -> List[str]:
    """Gene symbols the question names as mutated ('KRAS-mutant', 'KRAS G12C'), PrimeKG genes only."""
    out: List[str] = []
    for m in _MUTANT_RE.finditer(text or ""):
        sym = (m.group(1) or m.group(2) or "").upper()
        if sym and kg._by_id.get(f"SYMBOL_{sym}") and sym not in out:
            out.append(sym)
    return out[:3]


def _disease_index(kg: PrimeKG, query: str) -> Optional[int]:
    hits = kg.resolve(str(query), node_types=["disease"], limit=1)
    return hits[0].index if hits and hits[0].score >= 0.75 else None


def plan_investigation(
    kg: PrimeKG,
    query: str,
    drugs: Sequence[str] = (),
    diseases: Sequence[str] = (),
    genes: Sequence[str] = (),
    question_type: Optional[str] = None,
    disease_names: Sequence[str] = (),
    disease_mentions: Sequence[str] = (),
) -> Plan:
    plan = Plan()
    drugs, diseases, genes = list(drugs), list(diseases), list(genes)
    given_names = list(disease_names) if diseases else []
    qtype = question_type or "general"
    repurpose = bool(REPURPOSE_RE.search(query))
    ddx = bool(DDX_RE.search(query)) or qtype == "diagnosis"
    poly = bool(POLY_RE.search(query))
    mech = bool(PATH_RE.search(query)) or qtype in ("mechanism", "pharmacology")
    enrich = bool(ENRICH_RE.search(query))
    sim = bool(SIMILAR_RE.search(query))

    # Entities the upstream resolver missed: exact PrimeKG names in the text (drugs, diseases) and gene symbols.
    # In a presentation ("seizures, microcephaly ...") phenotype words are phenotypes, not diseases to profile.
    if not drugs:
        drugs = [kg.name(i) for i, _ in kg.scan_text(query, ["drug"], min_chars=4)][:4]
    present: List[str] = []
    absent: List[str] = []
    if ddx and not drugs:
        present, absent = extract_phenotypes(kg, query)
    if not diseases:
        pheno_words = {normalize_name(p) for p in present + absent}
        diseases = [kg.name(i) for i, phrase in kg.scan_text(query, ["disease"], min_chars=5, stop=DISEASE_STOP)
                    if phrase not in pheno_words and normalize_name(kg.name(i)) not in pheno_words][:2]
        if present and len(present) >= 2:
            diseases = []  # a phenotype list asks which disease; profiling a co-named one is noise
    if not genes:
        genes = _symbols_in_text(kg, query)[:12]
    labels = _disease_labels(kg, diseases, given_names)
    dname = lambda d: labels.get(d, d)  # noqa: E731
    plan.entities = {"drugs": drugs, "diseases": diseases, "genes": genes}
    if any(labels.get(d, d) != d for d in diseases):
        plan.entities["disease_names"] = [dname(d) for d in diseases]

    if present:
        if len(present) >= 2 or (present and not diseases):
            plan.add("phenotype_diagnosis", {"present": present[:15], "absent": absent[:10]},
                     f"phenotypes named: {', '.join(present[:6])}")

    if len(drugs) >= 2 and (poly or qtype == "safety" or len(drugs) >= 3):
        plan.add("polypharmacy", {"drugs": drugs[:6], "diseases": diseases[:2]},
                 f"regimen of {len(drugs[:6])} drugs")

    if drugs and diseases:
        plan.add("drug_disease", {"drug": drugs[0], "disease": diseases[0]},
                 f"how {drugs[0]} relates to {dname(diseases[0])}")

    # A disease node with no indication edges (the resolver's "exocrine pancreatic carcinoma") borrows those of a
    # close relative that has them ("pancreatic adenocarcinoma"), for calibration and the profile; labelled.
    proxies: Dict[str, Dict[str, Any]] = {}
    mentions = [str(m) for m in disease_mentions if m]
    for k, d in enumerate(diseases[:2]):
        idx = _disease_index(kg, d)
        if idx is None:
            continue
        own_mentions = [mentions[k]] if k < len(mentions) else []
        proxy = indication_proxy(kg, idx, own_mentions + [dname(d)])
        if proxy is not None and proxy.get("curie"):
            proxies[d] = proxy

    def via(d: str) -> str:
        p = proxies.get(d)
        return f", using indications of '{p['name']}' ({p['reason']})" if p else ""

    if diseases and (repurpose or (qtype == "treatment" and not drugs)):
        kwargs_r: Dict[str, Any] = {"disease": diseases[0], "limit": 15}
        reason = f"drug candidates for {dname(diseases[0])}{via(diseases[0])}"
        if diseases[0] in proxies:
            kwargs_r["indications_from"] = [proxies[diseases[0]]["curie"]]
        mutant = mutant_genes_in_text(kg, query)
        if mutant:
            kwargs_r["mutant_genes"] = mutant
            reason += f"; {'/'.join(mutant)}-pathway targets ranked up"
        plan.add("repurposing", kwargs_r, reason)

    if len(genes) >= 3 and (enrich or len(genes) >= 5):
        plan.add("enrichment", {"genes": genes[:200], "collections": ["pathway", "biological_process", "disease", "drug"]},
                 f"over-representation across {len(genes)} genes")
        genes_for_profiles: List[str] = []
    else:
        genes_for_profiles = genes[:2]

    for d in drugs[:2]:
        plan.add("drug_profile", {"drug": d, "limit": 15}, f"PrimeKG profile of {d}")
    for d in diseases[:2]:
        kwargs_d: Dict[str, Any] = {"disease": d, "limit": 15}
        if d in proxies:
            kwargs_d["indications_from"] = [proxies[d]["curie"]]
        plan.add("disease_profile", kwargs_d, f"PrimeKG profile of {dname(d)}{via(d)}")
    for g in genes_for_profiles:
        kwargs: Dict[str, Any] = {"gene": g, "limit": 15}
        reason = f"PrimeKG profile of {g}"
        if diseases:
            kwargs["context_diseases"] = diseases[:2]
            reason += f", ranked for {' and '.join(dname(d) for d in diseases[:2])}"
        plan.add("gene_profile", kwargs, reason)

    if mech:
        if genes and diseases:
            plan.add("paths", {"source": genes[0], "target": diseases[0], "source_type": "gene/protein",
                               "target_type": "disease", "limit": 6}, f"paths {genes[0]} → {dname(diseases[0])}")
        elif drugs and genes:
            plan.add("paths", {"source": drugs[0], "target": genes[0], "source_type": "drug",
                               "target_type": "gene/protein", "limit": 6}, f"paths {drugs[0]} → {genes[0]}")

    if sim:
        subject = (diseases[:1] and ("disease", diseases[0])) or (drugs[:1] and ("drug", drugs[0])) or \
                  (genes[:1] and ("gene/protein", genes[0])) or None
        if subject:
            label = dname(subject[1]) if subject[0] == "disease" else subject[1]
            plan.add("similar", {"entity": subject[1], "node_type": subject[0], "limit": 10},
                     f"entities similar to {label}")
    if not plan.analyses:
        plan.notes.append("no drug, disease, gene or phenotype set in the question matched PrimeKG")
    return plan
