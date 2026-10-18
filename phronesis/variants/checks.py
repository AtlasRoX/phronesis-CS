"""
Verifier checks for variant intelligence answers.

variant_frequencies (CRITICAL NUMERICAL): in non-hedged sentences about prevalence or allele frequency, every
percentage, n/N pair and scientific-notation allele frequency must match a computed cBioPortal or gnomAD
value, or appear verbatim in another evidence document (literature passages, CIViC descriptions). Sums across
studies are flagged as pooled figures.

Count pairs carry their unit (DAT-015): a patient numerator is accepted only over a patient denominator, a sample
numerator only over a sample denominator, gnomAD AC only over AN (alleles). A gnomAD figure from a dataset whose call
FAILED QC filters is the true gnomAD value but not a clean population frequency (DAT-005): quoting it without a QC
caveat is a MAJOR defect (it is not rejected as unsupported, because a caveated quote is correct).

clinvar_classification (MAJOR CLASSIFICATION): a sentence naming ClinVar and a classification term must use a
term that occurs in a returned ClinVar classification, so 'Conflicting classifications' never becomes 'pathogenic'.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SOURCE_KEY = "variant_intel"

_TRIGGER_RE = re.compile(
    r"prevalen|frequen|mutated|mutation rate|allele (?:count|frequency)|carriers|gnomad|cbioportal", re.I
)
# Words that also describe trials ("Global Cohort", "samples"): they trigger the check only next to the variant.
_WEAK_TRIGGER_RE = re.compile(r"samples|cohort|\bpresent in\b|\bharbou?r|\bcarry\b|\bfound in\b", re.I)
_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_PERCENT_RE = re.compile(rf"(?<![\w.])({_NUM})\s*(?:%|percent\b|per\s*cent\b)", re.I)
_PAIR_RE = re.compile(rf"(?<![\w.,/])({_NUM})\s*(?:/|\bof\b|\bout of\b)\s*({_NUM})(?![\w.]*\d)(?!\s*%)", re.I)
_SCI_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:[eE]\s*([-−+]?\d+)|[x×]\s*10\s*\^?\s*\(?\s*([-−⁻]?\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)\)?)"
)
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")

_CLASS_TERMS: Tuple[Tuple[str, str], ...] = (
    ("conflicting classifications of pathogenicity", r"conflicting classifications? of pathogenicity"),
    ("conflicting", r"conflicting"),
    ("uncertain significance", r"uncertain significance|\bvus\b"),
    ("likely pathogenic", r"likely[\s\-]pathogenic"),
    ("pathogenic", r"pathogenic"),
    ("likely benign", r"likely[\s\-]benign"),
    ("benign", r"benign"),
    ("likely oncogenic", r"likely[\s\-]oncogenic"),
    ("oncogenic", r"oncogenic"),
    ("tier ii", r"tier\s+(?:ii|2)"),
    ("tier i", r"tier\s+(?:i|1)"),
)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _variant_data(vctx: Any) -> Optional[Dict[str, Any]]:
    res = (vctx.tool_results or {}).get(SOURCE_KEY)
    if res is None or not getattr(res, "success", False) or not isinstance(getattr(res, "data", None), dict):
        return None
    return res.data


def _all_variants(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The looked-up variants of a result: the top-level one plus data['additional_variants']."""
    return [data, *[d for d in data.get("additional_variants") or [] if isinstance(d, dict)]]


def _other_documents(vctx: Any, data: Dict[str, Any]) -> List[str]:
    """Evidence documents other than this tool's own serialization, plus the CIViC descriptions it returned."""
    # Only literature passages: a number in a trial record or a FAERS table says nothing about prevalence.
    docs = [
        doc for doc, key in zip(vctx.evidence_documents, vctx.evidence_keys)
        if str(key).startswith(("PMID:", "DOI:", "passage:"))
    ]
    for variant in _all_variants(data):
        civic = variant.get("civic") or {}
        docs.extend(str(ev.get("description") or "").lower() for ev in civic.get("evidence") or [])
    return docs


def _verbatim(value: str, docs: Iterable[str]) -> bool:
    pattern = re.compile(rf"(?<![\d.]){re.escape(value)}(?![\d]|\.\d)")
    return any(pattern.search(d) for d in docs)


def _verbatim_percent(value: str, docs: Iterable[str]) -> bool:
    """The value as a percentage ('41%', '41 percent'); '40 days' in a case report is not one."""
    pattern = re.compile(rf"(?<![\d.]){re.escape(value)}\s*(?:%|percent\b|per\s*cent\b)")
    return any(pattern.search(d) for d in docs)


def _sci_value(m: re.Match) -> Optional[float]:
    mant = float(m.group(1))
    exp = m.group(2) or m.group(3)
    if exp is None:
        return None
    exp = exp.translate(_SUPERSCRIPT).replace("−", "-").replace(" ", "")
    try:
        return mant * (10 ** int(exp))
    except ValueError:
        return None


# The unit a claim gives a count pair: the noun right after "n/N" ("49/179 patients", "4 of 1,460,562 alleles").
_PAIR_UNIT_RE = re.compile(
    r"\s*(?:\(?[\d.,]+\s*%\)?\s*)?(?:[\w\-]+\s+){0,2}?(patients?|individuals?|people|persons?|subjects?|"
    r"samples?|tumou?rs?|specimens?|alleles?|chromosomes?)\b",
    re.I,
)
_UNIT_OF = {"patient": "patients", "individual": "patients", "people": "patients", "person": "patients",
            "subject": "patients", "sample": "samples", "tumor": "samples", "tumour": "samples",
            "specimen": "samples", "allele": "alleles", "chromosome": "alleles"}
# Words that state the QC caveat for a gnomAD figure from a call that failed QC filters.
_QC_CAVEAT_RE = re.compile(r"\bqc\b|quality|filter|fail|unreliable|caution|caveat|flagged|low[- ]confidence", re.I)


def _pair_unit(text: str, end: int) -> Optional[str]:
    """'patients', 'samples' or 'alleles' for the noun right after a count pair, None when no unit noun follows."""
    m = _PAIR_UNIT_RE.match(text[end:end + 60])
    if not m:
        return None
    word = m.group(1).lower()
    for stem, unit in _UNIT_OF.items():
        if word.startswith(stem):
            return unit
    return None


def _strip_failed_gnomad(data: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the variant data without the gnomAD figures of datasets whose call failed QC filters."""
    g = data.get("gnomad")
    if not isinstance(g, dict):
        return data
    from .render import gnomad_qc

    qc = gnomad_qc(g)
    if not qc["exome"] and not qc["genome"]:
        return data
    clean = dict(g)
    for ds in ("exome", "genome"):
        if qc[ds]:
            for k in (f"{ds}_ac", f"{ds}_an", f"{ds}_af"):
                clean[k] = None
    if qc["exome"]:  # FAF95 and the population breakdown are computed from the exome call
        clean["faf95_popmax"] = None
        clean["populations"] = []
    return {**data, "gnomad": clean}


def _allowed(data: Dict[str, Any]) -> Tuple[List[Tuple[int, int, str]], List[float], Set[int], Set[int]]:
    """(returned count pairs with their unit, allele frequencies, pooled numerators, pooled denominators).
    Units: 'samples', 'patients' (only when the patient denominator is known) and 'alleles' (gnomAD AC/AN)."""
    pairs: List[Tuple[int, int, str]] = []
    afs: List[float] = []
    studies = data.get("cbioportal") or []
    for st in studies:
        n_prof = int(st.get("profiled_samples") or 0)
        for k in ("mutated_samples", "gene_mutated_samples"):
            if st.get(k) is not None:
                pairs.append((int(st[k]), n_prof, "samples"))
        # DAT-015: mutated_patients counts patients, so it pairs only with the profiled-patient count; it is never
        # certified over profiled_samples (multi-sample cohorts such as MSK-IMPACT have more samples than patients).
        if st.get("mutated_patients") is not None and st.get("profiled_patients"):
            pairs.append((int(st["mutated_patients"]), int(st["profiled_patients"]), "patients"))
    g = data.get("gnomad") or {}
    for ac_k, an_k, af_k in (("exome_ac", "exome_an", "exome_af"), ("genome_ac", "genome_an", "genome_af")):
        if g.get(ac_k) is not None and g.get(an_k):
            pairs.append((int(g[ac_k]), int(g[an_k]), "alleles"))
        if g.get(af_k) is not None:
            afs.append(float(g[af_k]))
    if g.get("faf95_popmax") is not None:
        afs.append(float(g["faf95_popmax"]))
    for p in g.get("populations") or []:
        if p.get("an"):
            pairs.append((int(p.get("ac") or 0), int(p["an"]), "alleles"))
            if p.get("af") is not None:
                afs.append(float(p["af"]))
    pooled_num: Set[int] = set()
    pooled_den: Set[int] = set()
    for r in range(2, len(studies) + 1):
        for combo in itertools.combinations(studies, r):
            pooled_num.add(sum(int(s.get("mutated_samples") or 0) for s in combo))
            pooled_den.add(sum(int(s.get("profiled_samples") or 0) for s in combo))
    return pairs, afs, pooled_num, pooled_den


def _percent_ok(value: float, pairs: List[Tuple[int, int, str]], afs: List[float]) -> bool:
    for n, d, _unit in pairs:
        if not d:
            continue
        exact = 100.0 * n / d
        if any(abs(value - round(exact, places)) <= 0.05 + 1e-9 for places in (0, 1, 2)):
            return True
    return any(af and abs(value - 100.0 * af) / (100.0 * af) <= 0.05 for af in afs)


def _defect(sentence: str, description: str, correction: str) -> Dict[str, Any]:
    return {
        "category": "NUMERICAL",
        "severity": "CRITICAL",
        "description": description,
        "suggested_correction": correction,
        "claim": sentence[:240],
    }


def _variant_mention_re(data: Dict[str, Any]) -> re.Pattern:
    names = [str(v.get(k)) for v in _all_variants(data) for k in ("protein_change", "hgvs_p", "hgvs_c", "rsid") if v.get(k)]
    if not names:
        return re.compile(r"(?!x)x")
    return re.compile("|".join(rf"(?<![\w.]){re.escape(n)}(?!\w)" for n in names), re.I)


def check_variant_frequencies(vctx: Any) -> List[Dict[str, Any]]:
    data = _variant_data(vctx)
    if data is None:
        return []
    # Each variant's figures are allowed; pooled sums are computed per variant (its own studies).
    pairs, afs, pooled_num, pooled_den = [], [], set(), set()
    clean_pairs: List[Tuple[int, int, str]] = []
    clean_afs: List[float] = []
    for variant in _all_variants(data):
        v_pairs, v_afs, v_num, v_den = _allowed(variant)
        pairs += v_pairs
        afs += v_afs
        pooled_num |= v_num
        pooled_den |= v_den
        c_pairs, c_afs, _n, _d = _allowed(_strip_failed_gnomad(variant))
        clean_pairs += c_pairs
        clean_afs += c_afs
    pair_set = {(n, d) for n, d, _u in pairs}
    unit_pairs = {(n, d, u) for n, d, u in pairs}
    clean_pair_set = {(n, d) for n, d, _u in clean_pairs}
    qc_defects: List[Dict[str, Any]] = []
    docs = _other_documents(vctx, data)
    hedge = vctx.hedge_pattern
    defects: List[Dict[str, Any]] = []
    correction = (
        "Report cBioPortal prevalence per study exactly as returned ('n/N profiled samples (x%)' with the study name) "
        "and gnomAD allele counts/frequencies exactly as returned; never pool studies or estimate."
    )
    variant_re = _variant_mention_re(data)
    for sentence in vctx.sentences:
        if hedge.search(sentence):
            continue
        if not (_TRIGGER_RE.search(sentence) or (_WEAK_TRIGGER_RE.search(sentence) and variant_re.search(sentence))):
            continue
        text = sentence.replace("−", "-")
        flagged: List[str] = []
        failed_pairs: List[str] = []
        pair_spans: List[Tuple[int, int]] = []
        for m in _PAIR_RE.finditer(text):
            n_raw, d_raw = m.group(1), m.group(2)
            n, d = _num(n_raw), _num(d_raw)
            if n != int(n) or d != int(d) or d == 0 or n > d:
                continue
            pair_spans.append(m.span())
            unit = _pair_unit(text, m.end())
            if (int(n), int(d)) in pair_set:
                if unit is not None and (int(n), int(d), unit) not in unit_pairs:
                    # DAT-015: the counts exist, but not in the stated unit (e.g. patients over a sample denominator).
                    units = sorted({u for a, b, u in pairs if (a, b) == (int(n), int(d))})
                    flagged.append(f"'{m.group(0)} {unit}' pairs counts returned for {'/'.join(units)}, not {unit}")
                    continue
                if (int(n), int(d)) not in clean_pair_set and not _QC_CAVEAT_RE.search(sentence):
                    failed_pairs.append(m.group(0))
                continue
            # Both counts must come from the same document; numbers scattered across unrelated tool outputs do not count.
            if _verbatim(f"{n_raw}/{d_raw}", docs) or any(_verbatim(n_raw, [d]) and _verbatim(d_raw, [d]) for d in docs):
                continue
            if int(n) in pooled_num or int(d) in pooled_den:
                flagged.append(f"'{m.group(0)}' looks pooled across cBioPortal studies")
            else:
                flagged.append(f"'{m.group(0)}' matches no returned per-study count or gnomAD allele count")
        for m in _PERCENT_RE.finditer(text):
            raw = m.group(1)
            value = _num(raw)
            if _percent_ok(value, pairs, afs) or _verbatim_percent(raw, docs):
                if not _percent_ok(value, clean_pairs, clean_afs) and _percent_ok(value, pairs, afs) \
                        and not _verbatim_percent(raw, docs) and not _QC_CAVEAT_RE.search(sentence):
                    failed_pairs.append(m.group(0))
                continue
            pooled = any(
                d and abs(value - round(100.0 * n / d, 1)) <= 0.05 for n in pooled_num for d in pooled_den
            )
            flagged.append(f"'{m.group(0)}' " + ("looks like a pooled prevalence" if pooled else "matches no per-study prevalence returned by cBioPortal"))
        for m in _SCI_RE.finditer(text):
            if any(s <= m.start() < e for s, e in pair_spans):
                continue
            value = _sci_value(m)
            if value is None or value >= 1:
                continue
            if any(af and abs(value - af) / af <= 0.05 for af in afs) or _verbatim(m.group(0).lower(), docs):
                if not any(af and abs(value - af) / af <= 0.05 for af in clean_afs) \
                        and not _verbatim(m.group(0).lower(), docs) and not _QC_CAVEAT_RE.search(sentence):
                    failed_pairs.append(m.group(0))
                continue
            flagged.append(f"allele frequency '{m.group(0)}' matches no gnomAD value (5% tolerance)")
        if flagged:
            defects.append(_defect(
                sentence,
                "Variant frequency figure not supported by the variant lookup: " + "; ".join(flagged) + ".",
                correction,
            ))
        if failed_pairs:
            qc_defects.append({
                "category": "NUMERICAL",
                "severity": "MAJOR",
                "description": (
                    "gnomAD figure " + ", ".join(f"'{x}'" for x in dict.fromkeys(failed_pairs)) + " comes from a gnomAD "
                    "call that failed QC filters, but the sentence presents it without the QC caveat."
                ),
                "suggested_correction": (
                    "State that gnomAD marks this call as failing QC (name the filter) and that the frequency is "
                    "unreliable, or omit the figure."
                ),
                "claim": sentence[:240],
            })
    return defects + qc_defects


def _sentence_terms(sentence_lower: str) -> List[str]:
    """Classification terms in a sentence, longest first, without double-counting nested terms."""
    found: List[str] = []
    taken: List[Tuple[int, int]] = []
    for name, pattern in _CLASS_TERMS:
        for m in re.finditer(rf"(?<![\w/]){pattern}(?![\w])", sentence_lower):
            if any(s <= m.start() < e or s < m.end() <= e for s, e in taken):
                continue
            taken.append(m.span())
            found.append(name)
    return found


def check_clinvar_classification(vctx: Any) -> List[Dict[str, Any]]:
    data = _variant_data(vctx)
    if data is None:
        return []
    records = [rec for variant in _all_variants(data) for rec in variant.get("clinvar") or []]
    if not records:
        return []
    descriptions = [
        str(rec.get(k) or "").lower()
        for rec in records
        for k in ("germline_classification", "oncogenicity_classification", "somatic_clinical_impact")
        if rec.get(k)
    ]
    defects: List[Dict[str, Any]] = []
    for sentence in vctx.sentences:
        low = sentence.lower()
        if "clinvar" not in low or vctx.hedge_pattern.search(sentence):
            continue
        for term in _sentence_terms(low):
            pattern = dict(_CLASS_TERMS)[term]
            if any(re.search(rf"(?<![\w]){pattern}(?![\w])", d) for d in descriptions):
                continue
            returned = "; ".join(sorted({d for d in descriptions})) or "none"
            defects.append({
                "category": "CLASSIFICATION",
                "severity": "MAJOR",
                "description": f"ClinVar classification '{term}' does not match any returned ClinVar classification ({returned}).",
                "suggested_correction": (
                    "State the ClinVar germline, oncogenicity and somatic clinical-impact classifications exactly as "
                    "returned, each with its review status; do not upgrade 'Conflicting classifications' to 'pathogenic'."
                ),
                "claim": sentence[:240],
            })
            break
    return defects
