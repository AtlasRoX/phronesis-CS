"""
Offline gene lexicon and English-word guards for query parsing.

Used when the live resolver (synapta_resolve_entities) is not registered, fails or times out. It holds
symbols and aliases only, never identifiers, so nothing derived from it can whitelist an ID in the verifier.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Tuple

# Symbols named by the historical target regex, plus a few frequently asked partners (SOS1, NRAS, CTLA4...).
GENE_SYMBOLS: FrozenSet[str] = frozenset(
    """
    KRAS NRAS HRAS EGFR ERBB2 ERBB3 ERBB4 BRAF ARAF RAF1 TP53 PIK3CA PIK3CB PIK3CD PIK3CG PTEN MYC BRCA1 BRCA2
    ALK ROS1 RET MET APC CDK4 CDK6 ATM ATR IDH1 IDH2 FGFR1 FGFR2 FGFR3 FGFR4 PDCD1 CD274 CTLA4 AXL VIM
    MAPK1 MAPK3 MAPK8 MAPK14 MAP2K1 MAP2K2 AKT1 AKT2 AKT3 MTOR STAT1 STAT2 STAT3 STAT4 STAT5A STAT5B STAT6
    JAK1 JAK2 JAK3 BCL2 MCL1 MDM2 CDKN2A SMAD2 SMAD3 SMAD4 SRC YES1 FYN LYN BTK FLT3 KIT PDGFRA PDGFRB
    KDR FLT1 FLT4 ESR1 AR INS INSR SOS1 PTPN11 NF1 STK11 KEAP1 SHOC2 TSC1 TSC2
    """.split()
)

# alias -> approved symbol (upper-cased keys).
GENE_ALIASES: Dict[str, str] = {
    "HER2": "ERBB2",
    "NEU": "ERBB2",
    "HER3": "ERBB3",
    "HER4": "ERBB4",
    "ERK1": "MAPK3",
    "ERK2": "MAPK1",
    "ERK": "MAPK1",
    "MEK1": "MAP2K1",
    "MEK2": "MAP2K2",
    "CRAF": "RAF1",
    "C-RAF": "RAF1",
    "SHP2": "PTPN11",
    "SHP-2": "PTPN11",
    "PD-L1": "CD274",
    "PDL1": "CD274",
    "PD-1": "PDCD1",
    "PD1": "PDCD1",
    "VEGFR1": "FLT1",
    "VEGFR2": "KDR",
    "VEGFR3": "FLT4",
    "INSULIN": "INS",
    "P53": "TP53",
    "AKT": "AKT1",
    "MTORC1": "MTOR",
}

# Family names that do not denote one gene; shown as ambiguous and never chosen as the primary target.
GENE_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "PI3K": ("PIK3CA", "PIK3CB", "PIK3CD", "PIK3CG"),
    "MEK": ("MAP2K1", "MAP2K2"),
    "RAF": ("ARAF", "BRAF", "RAF1"),
    "RAS": ("HRAS", "KRAS", "NRAS"),
    "VEGFR": ("FLT1", "KDR", "FLT4"),
    "MAPK": ("MAPK1", "MAPK3"),
}

# Symbols that are also English words: accepted only in upper case, not at the start of a sentence
# in title case, and not right after an article unless a biomedical word sits next to them.
ENGLISH_WORD_SYMBOLS: FrozenSet[str] = frozenset(
    "WAS MET KIT SET CAT MAX ATM IMPACT FIND AR APC RET CAP REST CLOCK SHE HAS MAP NOT ALL CAN TOP SON PIGS ANT INS".split()
)

# Biomarker and genomic-state acronyms (HRD is also an HGNC alias of TBCE, MSS of several genes): never genes.
BIOMARKER_ACRONYMS: FrozenSet[str] = frozenset(
    """
    HRD HRR HRP MSI MSS TMB DMMR PMMR MMR LOH CIN GIS CNA CNAS SCNA SCNAS
    """.split()
)

# Trial endpoint and adverse-event acronyms: neither genes nor diseases ("OS" is not osteosarcoma here).
ENDPOINT_ACRONYMS: FrozenSet[str] = frozenset(
    """
    OS PFS PFS2 ORR DFS RFS EFS DMFS MFS DCR BICR IRC ITT HR CI AE AES SAE SAES IRAE IRAES TRAE TRAES TEAE TEAES
    DLT DLTS MTD RP2D QOL HRQOL
    """.split()
)

# Upper-case tokens that look like genes but are disease, assay or general acronyms.
NON_GENE_ACRONYMS: FrozenSet[str] = frozenset(
    """
    NSCLC SCLC PDAC AML CML ALL CLL CRC HCC TNBC HNSCC RCC GBM DLBCL MM MDS NHL MCL ESCC GIST LUAD LUSC
    CRPC MCRPC HGSOC HGSC EOC
    FDA EMA NCT PMID DOI DNA RNA MRNA CDNA SIRNA SHRNA CRISPR ODE PDE QC UMAP PCA OK USA UK EU WHO NIH NCI
    COPASI SBML BIOMD ADC ADCS ORR PFS OS DFS RFS DCR CR PR SD PD HR CI IC50 EC50 GI50 KD KI AUC TKI TKIS MOA
    PK PD AE AES SAE SAES ICI ICIS IO RT CT MRI PET NGS WES WGS PCR QPCR FISH IHC CNV SNV SNP SNPS INDEL VUS
    AI ML LLM API GPU CPU II III IV VI ECOG RECIST CTCAE NCCN ESMO ASCO
    """.split()
) | BIOMARKER_ACRONYMS | ENDPOINT_ACRONYMS

# Clinical acronyms that HGNC also lists as a symbol, alias or previous symbol (PCI -> SERPINA5, MI -> MITF,
# ALS -> IGFALS, CAR -> NR1I3/CXADR). Read as a gene only next to a biomedical word ("CAD gene", "CAR expression").
CLINICAL_ACRONYMS: FrozenSet[str] = frozenset(
    """
    PCI MI AF CAD COPD GFR ALS ASD UTI CRS CPAP SIDS RDS TMS IVF CRT ACS GCS DBS CAR TCR MS IBD ICD ARB HDL MRSA ROP
    """.split()
)

# Words that, next to an English-word symbol, make it a gene mention ("MET amplification", "ATM loss").
BIOMEDICAL_CONTEXT_WORDS: FrozenSet[str] = frozenset(
    """
    gene genes mutation mutations mutant mutants mutated variant variants amplification amplified deletion
    loss deficiency deficient expression expressed overexpression kinase receptor protein inhibitor inhibitors
    inhibition inhibit inhibits blockade signaling signalling pathway fusion fusions knockout knockdown exon
    phosphorylation activation activity copy alteration alterations isoform ligand antibody tki tkis status
    positive negative wild-type wildtype wt
    """.split()
)

_ARTICLES = frozenset({"the", "a", "an", "this", "that", "my", "our", "your", "their", "its", "at", "to"})


def lookup_symbol(token: str) -> Optional[Tuple[str, str]]:
    """Return (approved symbol, matched_on) for a lexicon token, or None. Families are not resolved here."""
    key = token.upper()
    if key in GENE_SYMBOLS:
        return key, "symbol"
    if key in GENE_ALIASES:
        return GENE_ALIASES[key], "alias_symbol"
    return None


def family_members(token: str) -> Optional[Tuple[str, ...]]:
    return GENE_FAMILIES.get(token.upper())


def _neighbour_words(text: str, start: int, end: int, n: int = 2) -> Tuple[List[str], List[str]]:
    left = re.findall(r"[A-Za-z][\w\-]*", text[:start])[-n:]
    right = re.findall(r"[A-Za-z][\w\-]*", text[end:])[:n]
    return [w.lower() for w in left], [w.lower() for w in right]


def is_gene_mention(text: str, start: int, end: int) -> bool:
    """Case and context guard for a gene-like token at text[start:end].

    Ordinary symbols match in any case ("kras", "Her2"). English-word symbols need upper case (so "met",
    "Met" and "Kit" never match) and, after an article ("at the ATM"), a biomedical neighbour.
    """
    token = text[start:end]
    key = token.upper()
    if key in NON_GENE_ACRONYMS or token == "eGFR":  # eGFR is the estimated filtration rate, not EGFR
        return False
    if key in CLINICAL_ACRONYMS:
        if re.match(r"[\s\-]?(?:T|NK)\b", text[end:]):
            return False  # CAR-T, CAR NK, TCR-T: engineered cell therapies
        left, right = _neighbour_words(text, start, end)
        return any(w in BIOMEDICAL_CONTEXT_WORDS for w in left + right)
    if key not in ENGLISH_WORD_SYMBOLS:
        return True
    if token != key:
        return False
    left, right = _neighbour_words(text, start, end)
    if any(w in BIOMEDICAL_CONTEXT_WORDS for w in left + right):
        return True
    if left and left[-1] in _ARTICLES:
        return False
    return True


def is_english_word_symbol(token: str) -> bool:
    return token.upper() in ENGLISH_WORD_SYMBOLS


# Treatment-related syndromes OpenTargets mapIds does not know (no hit by name or acronym). Names only, no IDs.
OFFLINE_DISEASES: Dict[str, str] = {
    "cytokine release syndrome": "cytokine release syndrome",
    "immune effector cell-associated neurotoxicity syndrome": "immune effector cell-associated neurotoxicity syndrome",
    "ICANS": "immune effector cell-associated neurotoxicity syndrome",
    "differentiation syndrome": "differentiation syndrome",
    "febrile neutropenia": "febrile neutropenia",
    "immune-related adverse events": "immune-related adverse event",
    "long COVID": "post-COVID-19 condition",
}
_OFFLINE_DISEASE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(" + "|".join(re.escape(k) for k in sorted(OFFLINE_DISEASES, key=len, reverse=True)) + r")(?![A-Za-z0-9_\-])",
    re.I,
)


def find_offline_diseases(text: str) -> List[Tuple[str, str, Tuple[int, int]]]:
    """(mention, label, span) for each OFFLINE_DISEASES phrase in text; acronyms must match case."""
    out: List[Tuple[str, str, Tuple[int, int]]] = []
    by_lower = {k.lower(): k for k in OFFLINE_DISEASES}
    for m in _OFFLINE_DISEASE_RE.finditer(text or ""):
        key = by_lower[m.group(1).lower()]
        if key.isupper() and m.group(1) != key:
            continue
        out.append((m.group(1), OFFLINE_DISEASES[key], m.span(1)))
    return out


# ----------------------------------------------------------------------------- acronyms defined in the question
# Words that make a parenthesised long form a gene or protein name ("epidermal growth factor receptor (EGFR)").
_GENE_NAME_WORDS: FrozenSet[str] = frozenset(
    """
    gene genes protein proteins receptor receptors kinase kinases factor factors ligand ligands homolog homologue
    oncogene subunit domain channel transporter enzyme polymerase synthase synthetase phosphatase transferase
    dehydrogenase reductase oxidase ligase protease peptidase helicase nuclease cyclin interleukin antigen
    chemokine integrin member family containing interacting binding regulator regulatory transcription
    """.split()
)
_NOT_ENZYME_ASE = frozenset("disease diseases release increase decrease phase phases database case cases base bases".split())
# Words that make a long form a biomarker, state or endpoint concept ("homologous recombination deficiency (HRD)").
_CONCEPT_WORDS: FrozenSet[str] = frozenset(
    """
    deficient deficiency deficiencies proficient proficiency repair instability burden load syndrome survival
    response responses rate rates ratio status signature score heterozygosity mutational instable stable event
    events toxicity toxicities dose tolerated free progression recurrence remission
    """.split()
)
_DEFINED_ACRONYM_RE = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9\-]{1,9})\s*\)")


def _initials_cover(letters: str, words: List[str]) -> bool:
    """letters (an acronym) is a subsequence of the words' initials and starts with the first word's initial."""
    initials = "".join(w[0] for w in words).upper()
    if not initials or initials[0] != letters[0]:
        return False
    it = iter(initials)
    return all(ch in it for ch in letters)


def _gene_like(words: List[str]) -> bool:
    for w in (x.lower() for x in words):
        if w in _GENE_NAME_WORDS or (len(w) >= 6 and w.endswith(("ase", "ases")) and w not in _NOT_ENZYME_ASE):
            return True
    return False


@lru_cache(maxsize=128)
def _defined_acronyms_cached(text: str) -> Tuple[Tuple[str, str], ...]:
    out: Dict[str, str] = {}
    for m in _DEFINED_ACRONYM_RE.finditer(text):
        acr = m.group(1)
        if sum(ch.isupper() for ch in acr) < 2 or lookup_symbol(acr) or family_members(acr):
            continue
        letters = re.sub(r"[^A-Za-z]", "", acr).upper()
        clause = re.split(r"[.;:!?()\[\],\n]", text[: m.start()])[-1]
        words = re.findall(r"[A-Za-z0-9]+", clause)[-(len(letters) + 4):]
        if not words or not letters:
            continue
        long_form: List[str] = []
        for i in range(len(words) - 1, -1, -1):
            if _initials_cover(letters, words[i:]):
                long_form = words[i:]
                break
        if len(long_form) < 2:
            long_form = []
        tail = long_form or words[-4:]
        if _gene_like(tail):
            continue
        if any(w.lower() in _CONCEPT_WORDS for w in tail):
            kind = "concept"
        elif long_form:
            kind = "name"
        else:
            continue
        for key in {acr.upper(), acr.split("-", 1)[0].upper()}:
            if len(key) >= 2 and lookup_symbol(key) is None:
                out.setdefault(key, kind)
    return tuple(out.items())


def defined_acronyms(text: str) -> Dict[str, str]:
    """Acronyms the question defines as "long form (ACR)" whose long form is not a gene or protein name.

    Maps the upper-cased acronym to "concept" (the long form names a biomarker, state or endpoint: deficient,
    repair, instability, burden, survival...; never a gene) or "name" (the initials match a non-gene long form;
    an approved HGNC symbol may still match, an alias-only hit may not). Lexicon genes are never listed."""
    return dict(_defined_acronyms_cached(text or ""))


# ----------------------------------------------------------------------------- coordinated disease phrases
DISEASE_HEAD_WORDS: FrozenSet[str] = frozenset(
    """
    cancer cancers carcinoma carcinomas adenocarcinoma adenocarcinomas tumor tumors tumour tumours neoplasm neoplasms
    melanoma melanomas leukemia leukemias leukaemia leukaemias lymphoma lymphomas glioblastoma glioblastomas glioma
    gliomas sarcoma sarcomas myeloma myelomas malignancy malignancies disease diseases syndrome syndromes
    """.split()
)
_COORD_STOP = frozenset(
    "a an the of in on for with and or to by at from is are was were be this that these those its their our all both "
    "other such some many most several various solid advanced metastatic early late treatment treatments therapy "
    "therapies patients patient survival risk response prevention diagnosis screening management prognosis "
    "outcome outcomes incidence mortality".split()
)
_MOD = r"[A-Za-z][A-Za-z\-']*"
_COORD_RE = re.compile(
    rf"(?<![A-Za-z0-9_\-])((?:{_MOD}\s*,\s*)*{_MOD}),?\s+(?:and/or|and|or|&)\s+({_MOD})\s+("
    + "|".join(sorted(DISEASE_HEAD_WORDS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_\-])",
    re.I,
)


def _singular_head(head: str) -> str:
    low = head.lower()
    if low.endswith("ies"):
        return head[:-3] + ("Y" if head[-1].isupper() else "y")
    if low.endswith("s") and low[:-1] in DISEASE_HEAD_WORDS:
        return head[:-1]
    return head


def coordinated_disease_terms(text: str) -> List[Tuple[str, str, Tuple[int, int]]]:
    """Expand "ovarian and prostate cancers" / "breast, ovarian, and prostate cancer" into one disease term per
    modifier ("ovarian cancer", "prostate cancer"). Returns (term, mention, span): the mention is literal text of
    the question (the modifier, or "modifier head" for the last one) so the entity can be anchored."""
    out: List[Tuple[str, str, Tuple[int, int]]] = []
    for m in _COORD_RE.finditer(text or ""):
        head = _singular_head(m.group(3))
        mods: List[Tuple[str, Tuple[int, int]]] = []
        for mm in re.finditer(_MOD, m.group(1)):
            mods.append((mm.group(0), (m.start(1) + mm.start(), m.start(1) + mm.end())))
        mods.append((m.group(2), m.span(2)))
        if any(w.lower() in _COORD_STOP for w, _ in mods):
            continue  # "treatment and prostate cancer" is not a list of disease sites
        for i, (word, span) in enumerate(mods):
            term = f"{word} {head}"
            if i == len(mods) - 1:
                span = (span[0], m.end(3))
            out.append((term, text[span[0]:span[1]], span))
    return out


__all__ = [
    "BIOMARKER_ACRONYMS",
    "DISEASE_HEAD_WORDS",
    "ENDPOINT_ACRONYMS",
    "coordinated_disease_terms",
    "defined_acronyms",
    "BIOMEDICAL_CONTEXT_WORDS",
    "CLINICAL_ACRONYMS",
    "ENGLISH_WORD_SYMBOLS",
    "OFFLINE_DISEASES",
    "find_offline_diseases",
    "GENE_ALIASES",
    "GENE_FAMILIES",
    "GENE_SYMBOLS",
    "NON_GENE_ACRONYMS",
    "family_members",
    "is_english_word_symbol",
    "is_gene_mention",
    "lookup_symbol",
]
