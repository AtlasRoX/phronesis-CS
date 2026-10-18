"""
Deterministic variant mention parser: gene + protein change ('KRAS G12D', 'KRAS p.Gly12Asp',
'EGFR L858R and T790M'), dbSNP rsIDs, ClinVar VCV accessions and transcript HGVS
('NM_004985.5(KRAS):c.35G>A'), plus a cancer-type keyword extractor for cBioPortal study selection.

Unsupported classes (fusions, exon events, histone marks such as H3K27M) are never guessed.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .models import VariantMention

AA3_TO_1: Dict[str, str] = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C", "Gln": "Q", "Glu": "E", "Gly": "G",
    "His": "H", "Ile": "I", "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F", "Pro": "P", "Ser": "S",
    "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V", "Ter": "*",
}
AA1_TO_3: Dict[str, str] = {v: k for k, v in AA3_TO_1.items()}

GENE_ALIASES: Dict[str, str] = {
    "HER2": "ERBB2", "HER-2": "ERBB2", "NEU": "ERBB2", "HER1": "EGFR", "ERBB1": "EGFR", "HER3": "ERBB3",
    "HER4": "ERBB4", "P53": "TP53", "PD-L1": "CD274", "PDL1": "CD274", "PD-1": "PDCD1", "PD1": "PDCD1",
    "C-MET": "MET", "C-KIT": "KIT", "MEK1": "MAP2K1", "MEK2": "MAP2K2", "ERK1": "MAPK3", "ERK2": "MAPK1",
    "CRAF": "RAF1", "C-RAF": "RAF1", "P16": "CDKN2A", "H3F3A": "H3-3A",
}

# Common cancer genes accepted in lower or mixed case ('kras g12d'); other symbols must be typed in capitals.
KNOWN_GENES = frozenset(
    "KRAS NRAS HRAS BRAF EGFR ERBB2 ERBB3 ERBB4 TP53 PIK3CA PIK3R1 PTEN AKT1 MTOR IDH1 IDH2 ALK ROS1 RET MET KIT "
    "PDGFRA FGFR1 FGFR2 FGFR3 FGFR4 JAK1 JAK2 JAK3 STAT3 FLT3 NPM1 DNMT3A TET2 ASXL1 RUNX1 CEBPA NOTCH1 FBXW7 APC "
    "CTNNB1 SMAD4 CDKN2A CDK4 CDK6 RB1 BRCA1 BRCA2 PALB2 ATM ATR CHEK2 MLH1 MSH2 MSH6 PMS2 POLE ESR1 AR GNAQ GNA11 "
    "GNAS MAP2K1 MAP2K2 MAPK1 MAPK3 RAF1 ARAF NF1 NF2 VHL KEAP1 STK11 SMARCA4 ARID1A SF3B1 U2AF1 SRSF2 EZH2 MYD88 "
    "BTK BCL2 MYC MDM2 CD274 PDCD1 CTLA4 TERT H3-3A HIST1H3B ERCC2 TSC1 TSC2 NTRK1 NTRK2 NTRK3".split()
)

# Uppercase tokens that are never gene symbols here (cell lines, viruses, assay words, accession prefixes).
_GENE_STOPLIST = frozenset(
    "A549 K562 HELA HEK293 HEK293T MCF7 MCF-7 HCT116 SW480 PANC1 PANC-1 U2OS JURKAT COVID COVID-19 SARS SARS-COV-2 "
    "CD4 CD8 H2A H2B H3 H4 H3K27M H3K4 H3K27 BIOMD AND OR NOT THE OF IN FOR WITH VS DNA RNA MRNA PCR NGS WES WGS FDA "
    "NCCN AMP ASCO ESMO TCGA MSK ICGC GDC CPTAC CCLE AACR GENIE IMPACT NSCLC SCLC CRC PDAC AML CML CLL ALL HCC RCC "
    "GBM LGG HNSCC TNBC OS PFS ORR HR CI IC50 EC50 CAR NK ATP GTP GDP ODE SBML "
    # Dosing routes and schedules: '200 mg IV Q3W' and 'PO Q12H' look like a gene plus a protein change.
    "IV PO SC IM IP IT ID QD BID TID QID PRN QW QOD".split()
)

_AA1 = "ACDEFGHIKLMNPQRSTVWY"
_AA3 = "|".join(sorted(AA3_TO_1, key=len, reverse=True))
# 1-letter (G12D, Q61*, E746fs, R248del) or 3-letter (Gly12Asp, Gln61Ter); the 3-letter form is tried first.
_CHANGE = (
    rf"(?:(?P<a3>{_AA3})(?P<p3>\d{{1,5}})(?P<b3>{_AA3}|\*|fs(?:\*|Ter)?\d*|del|dup)"
    rf"|(?P<a1>[{_AA1}])(?P<p1>\d{{1,5}})(?P<b1>[{_AA1}]|\*|fs\*?\d*|del|dup))"
)
_GENE_TOKEN = r"(?P<gene>[A-Za-z][A-Za-z0-9]{1,9}(?:-[A-Za-z0-9]{1,6})?)"
# Case-insensitive; a change not typed in HGVS case ('kras g12d') is accepted only for a well-known gene.
_GENE_CHANGE_RE = re.compile(rf"(?<![\w\-.]){_GENE_TOKEN}(?:\s+|\s*[-:(]\s*)(?:p\.\s?)?\(?{_CHANGE}\)?(?![\w])", re.I)
_CONTINUATION_RE = re.compile(rf"\s*(?:,|/|\+|&|\band\b|\bor\b|\bplus\b|\bvs\.?|\bversus\b)\s*(?:p\.\s?)?\(?{_CHANGE}\)?(?![\w])", re.I)
_HGVS_RE = re.compile(r"\b(?P<tx>NM_\d{4,9}(?:\.\d{1,3})?)\((?P<gene>[A-Za-z0-9\-]{1,15})\):(?P<c>c\.[0-9_+\-*]{1,24}(?:[ACGT]>[ACGT]|del[ACGT]*|dup[ACGT]*|ins[ACGT]+|delins[ACGT]+))")
_GENE_CDNA_RE = re.compile(rf"(?<![\w\-.(]){_GENE_TOKEN}\s*[: ]\s*(?P<c>c\.[0-9_+\-*]{{1,24}}(?:[ACGT]>[ACGT]|del[ACGT]*|dup[ACGT]*|ins[ACGT]+|delins[ACGT]+))")
_RSID_RE = re.compile(r"\b(rs\d{1,12})\b", re.I)
_VCV_RE = re.compile(r"\b(VCV\d{9})(?:\.\d+)?\b", re.I)
_GENE_WORD_RE = re.compile(r"(?<![\w\-.])([A-Za-z][A-Za-z0-9]{1,9}(?:-[A-Za-z0-9]{1,6})?)(?![\w])")


def normalize_gene(token: str) -> Optional[str]:
    """Return the HGNC-style symbol for a typed gene token, or None when it is not plausibly a gene."""
    up = token.upper()
    if up in _GENE_STOPLIST or up.startswith("BIOMD"):
        return None
    up = GENE_ALIASES.get(up, up)
    if token != token.upper() and up not in KNOWN_GENES:
        return None  # lower/mixed-case words only count when they are well-known cancer genes
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{0,14}", up) or not re.search(r"[A-Z]", up):
        return None
    if re.fullmatch(rf"[{_AA1}]\d+[{_AA1}]?", up):
        return None  # a protein change ('T790M') is not a gene
    return up


def _is_suffix(b: str) -> bool:
    low = b.lower()
    return low.startswith("fs") or low in ("del", "dup")


def _canonical_case(m: re.Match) -> bool:
    """True when the change is typed as HGVS writes it ('G12D', 'Gly12Asp', 'E746fs'), not 'g12d'."""
    if m.group("a3"):
        b = m.group("b3")
        ok_b = b in AA3_TO_1 or b == "*" or (_is_suffix(b) and b.replace("Ter", "ter") == b.lower())
        return m.group("a3") in AA3_TO_1 and ok_b
    b = m.group("b1")
    return m.group("a1").isupper() and (b.isupper() or b == "*" or (_is_suffix(b) and b == b.lower()))


def _change_from_match(m: re.Match) -> Tuple[str, Optional[str]]:
    """(1-letter change, 3-letter change or None) from a _CHANGE match, case-normalized."""
    if m.group("a3"):
        a, pos, b = AA3_TO_1[m.group("a3").capitalize()], m.group("p3"), m.group("b3")
        if _is_suffix(b):
            b1 = b.lower().replace("ter", "*")
        else:
            b1 = AA3_TO_1.get(b.capitalize(), b)
    else:
        a, pos, b = m.group("a1").upper(), m.group("p1"), m.group("b1")
        b1 = b.lower() if _is_suffix(b) else b.upper()
    a3, b3 = AA1_TO_3.get(a), AA1_TO_3.get(b1)
    three = f"{a3}{pos}{b3}" if a3 and b3 else None
    return f"{a}{pos}{b1}", three


def protein_change_3letter(change: str) -> Optional[str]:
    """'G12D' -> 'Gly12Asp'; None for frameshift, deletion and duplication notations."""
    m = re.fullmatch(rf"([{_AA1}])(\d{{1,5}})([{_AA1}*])", change or "")
    if not m:
        return None
    return f"{AA1_TO_3[m.group(1)]}{m.group(2)}{AA1_TO_3[m.group(3)]}"


def _first_gene_in(text: str) -> Optional[str]:
    for m in _GENE_WORD_RE.finditer(text):
        sym = normalize_gene(m.group(1))
        if sym and (sym in KNOWN_GENES or m.group(1).upper() in GENE_ALIASES):
            return sym
    return None


def parse_variant_mentions(query: str) -> List[VariantMention]:
    """All variant mentions in a question, in order of appearance, deduplicated."""
    text = query or ""
    found: List[Tuple[int, VariantMention]] = []
    consumed: List[Tuple[int, int]] = []

    for m in _HGVS_RE.finditer(text):
        gene = normalize_gene(m.group("gene")) or m.group("gene").upper()
        found.append((m.start(), VariantMention(raw=m.group(0), gene=gene, cdna_change=m.group("c"), hgvs=m.group(0))))
        consumed.append(m.span())

    for m in _GENE_CHANGE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        gene = normalize_gene(m.group("gene"))
        if not gene or (not _canonical_case(m) and gene not in KNOWN_GENES):
            continue
        change, three = _change_from_match(m)
        found.append((m.start(), VariantMention(raw=m.group(0).strip(), gene=gene, protein_change=change, protein_change_3=three)))
        pos = m.end()
        while True:
            cont = _CONTINUATION_RE.match(text, pos)
            if not cont:
                break
            change, three = _change_from_match(cont)
            found.append((cont.start(), VariantMention(raw=f"{gene} {change}", gene=gene, protein_change=change, protein_change_3=three)))
            pos = cont.end()
        consumed.append((m.start(), pos))

    for m in _GENE_CDNA_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        gene = normalize_gene(m.group("gene"))
        if gene:
            found.append((m.start(), VariantMention(raw=m.group(0), gene=gene, cdna_change=m.group("c"))))

    context_gene = next((v.gene for _, v in found if v.gene), None) or _first_gene_in(text)
    for m in _RSID_RE.finditer(text):
        found.append((m.start(), VariantMention(raw=m.group(1), gene=context_gene, rsid=m.group(1).lower())))
    for m in _VCV_RE.finditer(text):
        found.append((m.start(), VariantMention(raw=m.group(0), gene=context_gene, clinvar_vcv=m.group(1).upper())))

    out: List[VariantMention] = []
    seen = set()
    for _, v in sorted(found, key=lambda t: t[0]):
        key = (v.gene, v.protein_change, v.cdna_change, v.rsid, v.clinvar_vcv)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# canonical keyword -> (query pattern, study/cancer-type name terms matched at a word start, cBioPortal cancerTypeIds)
CANCER_KEYWORDS: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "pancreatic": (r"pancrea\w*|\bpdac\b|\bpaad\b", ("pancrea",), ("paad", "pancreas")),
    "nsclc": (r"\bnsclc\b|non[\s\-]?small[\s\-]?cell", ("non-small cell lung",), ("nsclc", "luad", "lusc")),
    "lung": (r"\blung\b|\bluad\b|\blusc\b|\bsclc\b", ("lung",), ("luad", "lusc", "nsclc", "sclc")),
    "colorectal": (r"colorectal|\bcolon\b|\brectal\b|\bcrc\b|\bcoadread\b", ("colorectal", "colon", "rectal"), ("coadread", "coad", "read")),
    "melanoma": (r"melanoma|\bskcm\b", ("melanoma",), ("skcm", "mel")),
    "breast": (r"\bbreast\b", ("breast",), ("brca", "breast")),
    "ovarian": (r"ovar\w*", ("ovar",), ("ov", "hgsoc")),
    "prostate": (r"prostat\w*", ("prostat",), ("prad", "prostate")),
    "glioma": (r"\bgliom\w*|glioblastoma|\bgbm\b", ("glioma", "glioblastoma"), ("gbm", "lgg", "difg")),
    "thyroid": (r"\bthyroid", ("thyroid",), ("thca", "thpa", "thyroid")),
    "bladder": (r"\bbladder|urothelial", ("bladder", "urothelial"), ("blca", "bladder")),
    "gastric": (r"gastric|stomach", ("gastric", "stomach"), ("stad", "egc")),
    "esophageal": (r"o?esophag\w*", ("esophag",), ("esca",)),
    "liver": (r"hepatocellular|\bliver\b|\bhcc\b", ("hepatocellular", "liver"), ("lihc", "hcc")),
    "endometrial": (r"endometri\w*|uterine", ("endometri", "uterine"), ("ucec", "ucs")),
    "kidney": (r"\brenal\b|kidney", ("renal", "kidney"), ("kirc", "kirp", "kich", "ccrcc")),
    "head and neck": (r"head\s+and\s+neck|\bhnscc\b", ("head and neck",), ("hnsc",)),
    "cholangiocarcinoma": (r"cholangio\w*|biliary|gall\s?bladder", ("cholangio", "biliary", "gallbladder"), ("chol", "gbc")),
    "leukemia": (r"leuka?emia|\baml\b", ("leukemia",), ("aml", "laml")),
    "sarcoma": (r"sarcoma", ("sarcoma",), ("sarc",)),  # not cancerTypeId soft_tissue, which includes PCPG
}


# Extra CIViC disease-name patterns per keyword (CIViC names diseases 'Lung Non-small Cell Carcinoma').
_CIVIC_DISEASE_EXTRA: Dict[str, str] = {
    "nsclc": r"lung\s+(?:adeno|squamous|large\s+cell)",
    "liver": r"hepat\w*",
    "kidney": r"\bclear\s+cell\s+renal|\brcc\b",
}


def disease_matches_keyword(disease: Optional[str], keyword: Optional[str]) -> bool:
    """
    Whether a CIViC disease name belongs to the question's cancer type: 'pancreatic' matches 'Pancreatic Cancer',
    'Pancreatic Ductal Carcinoma' and 'Malignant Exocrine Pancreas Neoplasm'. Unknown keywords fall back to
    case-insensitive token overlap on the first 6 letters of each word of 4+ letters.
    """
    if not disease or not keyword:
        return False
    key = keyword.strip().lower()
    entry = CANCER_KEYWORDS.get(key)
    if entry:
        if re.search(entry[0], disease, re.I):
            return True
        extra = _CIVIC_DISEASE_EXTRA.get(key)
        return bool(extra and re.search(extra, disease, re.I))
    stems = {w[:6] for w in re.findall(r"[a-z]{4,}", key) if w not in {"cancer", "cancers", "tumor", "tumour", "carcinoma"}}
    words = re.findall(r"[a-z]{4,}", disease.lower())
    return any(w.startswith(s) or s.startswith(w[:6]) for s in stems for w in words)


def extract_cancer_keyword(query: str) -> Optional[str]:
    """The first cancer type named in the question as a canonical keyword ('pancreatic'), or None."""
    text = query or ""
    best: Optional[Tuple[int, str]] = None
    for key, (pattern, _terms, _ids) in CANCER_KEYWORDS.items():
        m = re.search(pattern, text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), key)
    return best[1] if best else None
