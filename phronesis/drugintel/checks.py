"""
Verifier checks for drug answers, registered as 'drug_pharmacology', 'drug_faers' and 'drug_fda_approval'.

  pharmacology  IC50/Ki/Kd/EC50 values must match a ChEMBL standard_value (within 5% after conversion to nM)
                or a value in a retrieved passage; pChEMBL values must match within 0.05.
  faers         report counts in FAERS sentences must equal a retrieved count exactly; percentages must appear
                in the evidence; causal or incidence language needs a hedge.
  fda approval  a sentence that calls a drug FDA-approved is flagged when Drugs@FDA was queried and lists no
                NDA/BLA approval for it (MAJOR), or when openFDA skipped it as not approved anywhere (MINOR).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

from .render import safety_entries

_TO_NM = {"p": 1e-3, "n": 1.0, "u": 1e3, "µ": 1e3, "μ": 1e3, "m": 1e6}
RELATIVE_TOLERANCE = 0.05
PCHEMBL_TOLERANCE = 0.05

PHARM_SENTENCE_RE = re.compile(r"\b(?:IC50|IC₅₀|EC50|EC₅₀|Ki|Kd|pChEMBL)\b", re.IGNORECASE)
# Thousands may be grouped with commas or with the no-break, narrow no-break and thin spaces models write
# ("272\u202f899"); a plain space is not a separator, since "3 272 reports" may be two numbers.
_GROUP_SEP = "\u00a0\u202f\u2009\u2007\u2002\u2003\u2004\u2005\u2006\u2008\u205f"  # no-break, narrow, thin, figure, en/em...
_NUM = rf"\d{{1,3}}(?:[,{_GROUP_SEP}]\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?"


def _as_int(raw: str) -> int:
    return int(re.sub(rf"[,{_GROUP_SEP}]", "", raw))


# "38 nM", "0.038 µM", and ranges such as "1.0-2.2 nM" (both ends carry the unit).
VALUE_RE = re.compile(rf"(?<![\w.])({_NUM})(?:\s*(?:-|–|—|to)\s*({_NUM}))?\s*([pnuµμm])M\b")
DOC_VALUE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([pnuµμm])m\b")
PCHEMBL_RE = re.compile(r"pChEMBL(?:\s+value)?s?\s*(?:of|=|:|was|is|≈|~)?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

FAERS_SENTENCE_RE = re.compile(r"\bFAERS\b|openFDA|adverse[- ]event reports?|spontaneous reports?", re.IGNORECASE)
_COUNT_WORDS = r"(?:serious|adverse|event|faers|spontaneous|total|individual|safety|reaction|death|fatal)"
COUNT_RE = re.compile(rf"(?<![\w.,])({_NUM})((?:\s+{_COUNT_WORDS})*)\s+(?:reports?|cases?|events?)\b", re.IGNORECASE)
PERCENT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%")
_LABEL_RE = re.compile(r"\blabel(?:ing|led|ed)?\b|set_id|prescribing information|boxed warning|contraindicat", re.IGNORECASE)
_REPORTS_RE = re.compile(r"\bFAERS\b|spontaneous reports?|adverse[- ]event reports?|\breports? (?:mention|list)", re.IGNORECASE)
CAUSAL_RE = re.compile(r"\bcaus\w*|\binduc\w*|\bincidence\b|\bfrequency\b|\brate of\b|\brisk of\b", re.IGNORECASE)
_PASSAGE_WINDOW = 80
_PHARM_WORD_RE = re.compile(r"ic50|ic₅₀|ec50|ec₅₀|\bki\b|\bkd\b|pchembl|potency|inhibit|affinity", re.IGNORECASE)
_PCHEMBL_WORD_RE = re.compile(r"pchembl|\bp(?:ic50|ki|kd|ec50)\b", re.IGNORECASE)
_REPORT_WORD_RE = re.compile(r"reports?|cases?|events?|faers|patients", re.IGNORECASE)
_REPORT_HEADER_RE = re.compile(r"\breports?\b|\bcount\b", re.IGNORECASE)
_RANK_HEADER_RE = re.compile(r"^(?:#|no\.?|rank|n)$", re.IGNORECASE)
_CELL_INT_RE = re.compile(rf"^(?:{_NUM})$")


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _within(value: float, expected: float) -> bool:
    if expected == 0:
        return value == 0
    return abs(value - expected) / abs(expected) <= RELATIVE_TOLERANCE


def _unit_scale(units: Optional[str]) -> Optional[float]:
    if not units:
        return None
    m = re.fullmatch(r"\s*([pnuµμm])M\s*", units)
    return _TO_NM[m.group(1)] if m else None


def _tool_payloads(tool_results: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for res in (tool_results or {}).values():
        if res is not None and getattr(res, "success", False) and isinstance(getattr(res, "data", None), dict):
            yield res.data


def chembl_activity_values(tool_results: Dict[str, Any]) -> Dict[str, Set[float]]:
    """{'nm': standard values in nM, 'pchembl': pChEMBL values} from every successful ChEMBL profile."""
    out: Dict[str, Set[float]] = {"nm": set(), "pchembl": set()}
    for data in _tool_payloads(tool_results):
        for drug in data.get("drugs") or []:
            if not isinstance(drug, dict):
                continue
            for b in drug.get("bioactivities") or []:
                value, scale = b.get("standard_value"), _unit_scale(b.get("standard_units"))
                if value is not None and scale is not None:
                    out["nm"].add(float(value) * scale)
                if b.get("pchembl_value") is not None:
                    out["pchembl"].add(float(b["pchembl_value"]))
        for c in data.get("target_bioactive_compounds") or []:
            if not isinstance(c, dict):
                continue
            value, scale = c.get("standard_value"), _unit_scale(c.get("standard_units"))
            if value is not None and scale is not None:
                out["nm"].add(float(value) * scale)
            if c.get("best_pchembl") is not None:
                out["pchembl"].add(float(c["best_pchembl"]))
    return out


def _doc_values_nm(documents: Iterable[str]) -> Set[float]:
    out: Set[float] = set()
    for doc in documents:
        for m in DOC_VALUE_RE.finditer(doc or ""):
            out.add(float(m.group(1)) * _TO_NM[m.group(2)])
    return out


def _passage_documents(vctx: Any) -> List[str]:
    """Evidence documents other than whole-tool JSON dumps (literature passages, provider documents)."""
    docs = list(getattr(vctx, "evidence_documents", None) or [])
    keys = list(getattr(vctx, "evidence_keys", None) or [])
    return [d for d, k in zip(docs, keys) if not str(k).startswith("tool:")] if len(keys) == len(docs) else docs


def _number_in_passages(raw: str, passages: Iterable[str], context_re: re.Pattern) -> bool:
    """raw appears as a whole number in a passage, with a context word (assay type, 'reports', ...) close by."""
    variants = {raw, raw.replace(",", "")}
    for p in passages:
        for v in variants:
            for m in re.finditer(rf"(?<![\d.,]){re.escape(v)}(?![\d]|[.,]\d)", p or ""):
                window = p[max(0, m.start() - _PASSAGE_WINDOW): m.end() + _PASSAGE_WINDOW]
                if context_re.search(window):
                    return True
    return False


def _defect(category: str, severity: str, description: str, correction: str, claim: str) -> Dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "description": description,
        "suggested_correction": correction,
        "claim": claim[:240],
    }


def _sentences(vctx: Any) -> List[str]:
    """The verifier's sentences (ClinicalVerifier._split_sentences, i.e. utils.text.sentence_spans): no own split."""
    return list(vctx.sentences or [])


def check_pharmacology(vctx: Any) -> List[Dict[str, Any]]:
    sentences = [s for s in _sentences(vctx) if PHARM_SENTENCE_RE.search(s)]
    if not sentences:
        return []
    chembl = chembl_activity_values(vctx.tool_results)
    doc_values = _doc_values_nm(vctx.evidence_documents or [])
    passages = _passage_documents(vctx)
    defects: List[Dict[str, Any]] = []
    for sentence in sentences:
        for m in VALUE_RE.finditer(sentence):
            scale = _TO_NM[m.group(3)]
            for raw in (m.group(1), m.group(2)):
                if raw is None:
                    continue
                nm = _num(raw) * scale
                if any(_within(nm, v) for v in chembl["nm"] | doc_values):
                    continue
                if _number_in_passages(raw, passages, _PHARM_WORD_RE):
                    continue
                shown = f"{raw} {m.group(3)}M"
                defects.append(
                    _defect(
                        "NUMERICAL",
                        "CRITICAL",
                        f"Potency value {shown} matches no ChEMBL activity record or retrieved passage.",
                        "Quote IC50/Ki/Kd values exactly as listed in the ChEMBL bioactivity records (type, relation, "
                        "value, units, assay ID), or remove the value.",
                        sentence,
                    )
                )
        for m in PCHEMBL_RE.finditer(sentence):
            raw = m.group(1)
            value = float(raw)
            if any(abs(value - p) <= PCHEMBL_TOLERANCE for p in chembl["pchembl"]):
                continue
            if _number_in_passages(raw, passages, _PCHEMBL_WORD_RE):
                continue
            defects.append(
                _defect(
                    "NUMERICAL",
                    "CRITICAL",
                    f"pChEMBL {raw} matches no retrieved ChEMBL activity record.",
                    "Quote pChEMBL values exactly as listed in the ChEMBL bioactivity records, or remove them.",
                    sentence,
                )
            )
    return defects


def faers_counts(tool_results: Dict[str, Any]) -> Set[int]:
    out: Set[int] = set()
    for data in _tool_payloads(tool_results):
        for entry in safety_entries(data):
            for key in ("total_reports", "serious_reports", "deaths_reported"):
                if entry.get(key) is not None:
                    out.add(int(entry[key]))
            for r in [*(entry.get("top_reactions") or []), *(entry.get("excluded_reactions") or [])]:
                if r.get("count") is not None:
                    out.add(int(r["count"]))
    return out


def faers_terms(tool_results: Dict[str, Any]) -> Set[str]:
    """Upper-case MedDRA reaction terms in the retrieved FAERS counts."""
    return {
        str(r["term"]).upper()
        for data in _tool_payloads(tool_results)
        for entry in safety_entries(data)
        for r in [*(entry.get("top_reactions") or []), *(entry.get("excluded_reactions") or [])]
        if r.get("term")
    }


def _without_terms(sentence: str, terms: Set[str]) -> str:
    """The sentence with retrieved reaction terms blanked, so 'DRUG-INDUCED LIVER INJURY' is not read as a causal claim."""
    out = sentence
    for term in sorted(terms, key=len, reverse=True):
        out = re.sub(re.escape(term), " ", out, flags=re.IGNORECASE)
    return out


def _count_defect(raw: str, allowed: Set[int], claim: str) -> Dict[str, Any]:
    return _defect(
        "NUMERICAL",
        "CRITICAL",
        f"FAERS count {raw} does not equal any retrieved openFDA count"
        + ("." if allowed else " (no openFDA counts were retrieved)."),
        "Quote FAERS report counts exactly as retrieved (reaction counts, total or serious reports), or remove them.",
        claim,
    )


def _percent_defect(token: str, claim: str) -> Dict[str, Any]:
    return _defect(
        "NUMERICAL",
        "MAJOR",
        f"Percentage {token} in a FAERS statement is not in the evidence; FAERS counts cannot be turned into rates.",
        "Remove computed percentages and report the raw report counts with the spontaneous-report caveat.",
        claim,
    )


def _percent_in_docs(value: str, docs: List[str]) -> bool:
    return any(f"{value}%" in d or f"{value} %" in d for d in docs)


def _faers_tables(response: str, terms: Set[str]) -> List[List[List[str]]]:
    """Markdown tables (rows of cells, header first) that present FAERS data: the header or the line just above
    names FAERS/openFDA, or a report-count header sits over rows that name a retrieved reaction term."""
    tables: List[List[List[str]]] = []
    lines = (response or "").splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        start = i
        rows: List[List[str]] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            row = lines[i].strip()
            if not (re.fullmatch(r"[\s|:\-]+", row) and "-" in row):
                rows.append([c.strip() for c in row.strip("|").split("|")])
            i += 1
        lead = next((lines[j] for j in range(start - 1, max(-1, start - 3), -1) if lines[j].strip()), "")
        if len(rows) < 2:
            continue
        header = " ".join(rows[0])
        names_term = any(c.strip("*_ `").upper() in terms for r in rows[1:] for c in r)
        if FAERS_SENTENCE_RE.search(header + " " + lead) or (names_term and _REPORT_HEADER_RE.search(header)):
            tables.append(rows)
    return tables


_NON_COUNT_HEADER_RE = re.compile(
    r"\b(?:approv\w*|year|date|nda|bla|anda|application|phase|pmid|nct|chembl|score|dose|mg|ic50|ki|kd|ec50|"
    r"hr|hazard|ratio|ci|median|months?|weeks?|orr|pfs|os|response|trial|target|pchembl|enrol+ment|n\s*=)\b",
    re.IGNORECASE,
)


def _check_faers_tables(vctx: Any, allowed: Set[int], docs: List[str], passages: List[str]) -> List[Dict[str, Any]]:
    """FAERS tables carry counts without the word 'reports' on each row, so their cells are checked directly."""
    defects: List[Dict[str, Any]] = []
    for rows in _faers_tables(getattr(vctx, "response", "") or "", faers_terms(vctx.tool_results)):
        header, body = rows[0], rows[1:]
        # Rank columns, and columns of something other than report counts (an approval year, an NDA number, a phase,
        # a potency) in the same table: "2014" under "FDA approval" is not a FAERS count.
        skip_cols = {i for i, h in enumerate(header)
                     if _RANK_HEADER_RE.match(h.strip("*_ `")) or _NON_COUNT_HEADER_RE.search(h)}
        for row in body:
            claim = "| " + " | ".join(row) + " |"
            for col, cell in enumerate(row):
                if col in skip_cols:
                    continue
                text = cell.strip("*_ `")
                for m in PERCENT_RE.finditer(text):
                    if not _percent_in_docs(m.group(1), docs):
                        defects.append(_percent_defect(f"{m.group(1)}%", claim))
                if _CELL_INT_RE.match(text) and "." not in text:
                    if _as_int(text) in allowed or _number_in_passages(text, passages, _REPORT_WORD_RE):
                        continue
                    defects.append(_count_defect(text, allowed, claim))
    return defects


def check_faers(vctx: Any) -> List[Dict[str, Any]]:
    sentences = [s for s in _sentences(vctx) if FAERS_SENTENCE_RE.search(s)]
    allowed = faers_counts(vctx.tool_results)
    docs = list(vctx.evidence_documents or [])
    passages = _passage_documents(vctx)
    defects: List[Dict[str, Any]] = _check_faers_tables(vctx, allowed, docs, passages)
    if not sentences:
        return defects
    terms = faers_terms(vctx.tool_results)
    hedge = getattr(vctx, "hedge_pattern", None)
    for sentence in sentences:
        for m in COUNT_RE.finditer(sentence):
            if re.search(r"\btop\s*$", sentence[: m.start()], re.IGNORECASE):
                continue  # "the top 10 reported events" is a list length, not a count
            raw = m.group(1)
            if "." in raw:
                continue
            if _as_int(raw) in allowed or _number_in_passages(raw, passages, _REPORT_WORD_RE):
                continue
            # "1 454 total FAERS reports": a plain space can group thousands too. It is not a separator in general
            # ("3 272 reports" may be two numbers), so accept the grouped reading only when it equals a retrieved count.
            lead = re.search(r"(?<![\w.,])(\d{1,3}(?: \d{3})*) $", sentence[: m.start()])
            if lead and "," not in raw and int(lead.group(1).replace(" ", "") + raw) in allowed:
                continue
            defects.append(_count_defect(raw, allowed, sentence))
        for m in PERCENT_RE.finditer(sentence):
            if _percent_in_docs(m.group(1), docs):
                continue
            defects.append(_percent_defect(f"{m.group(1)}%", sentence))
        # Label content (contraindications, warnings) may state causes and risks; the rule is about report counts.
        about_label = _LABEL_RE.search(sentence) and not _REPORTS_RE.search(sentence)
        if (
            CAUSAL_RE.search(_without_terms(sentence, terms))
            and not (hedge is not None and hedge.search(sentence))
            and not about_label
        ):
            defects.append(
                _defect(
                    "CLINICAL_CLAIM",
                    "MAJOR",
                    "FAERS reports do not establish causation or incidence, but the sentence states or implies them.",
                    "Describe FAERS figures as spontaneous reports that mention the drug, and state that they do not "
                    "establish causation or incidence.",
                    sentence,
                )
            )
    unique = {(d["description"], d["claim"]): d for d in defects}  # a table row can also be a FAERS sentence
    return list(unique.values())


# -----------------------------------------------------------------------------
# FDA approval claims (Drugs@FDA)
# -----------------------------------------------------------------------------

FDA_APPROVED_RE = re.compile(
    r"\bFDA[\s-]+approv\w*|\bapproved\s+by\s+the\s+(?:U\.?\s?S\.?\s+)?(?:FDA|Food\s+and\s+Drug\s+Administration)\b|"
    r"\b(?:received|granted|gained|won|has|holds?|obtained)\s+(?:an?\s+)?(?:accelerated\s+|full\s+|regular\s+)?"
    r"(?:US\s+)?FDA\s+approvals?\b|\bFDA\s+approvals?\s+(?:of|for)\b",
    re.IGNORECASE,
)
# Hedged, conditional, negated or non-US statements about approval are not claims that the drug is FDA-approved.
_APPROVAL_HEDGE_RE = re.compile(
    r"\b(?:not|no|never|none|nor|without|lacks?|lacking|yet|pending|awaiting|await\w*|may|might|could|would|if|"
    r"whether|unless|expected|anticipated|potential(?:ly)?|possible|possibly|seek\w*|sought|submitted|submission|"
    r"under\s+review|investigational|experimental|preclinical|candidate|unapproved|off[- ]label|hope\w*|plan\w*|"
    r"aim\w*|toward|towards|path\s+to|EMA|European|Japan\w*|PMDA|China|NMPA|outside\s+the\s+US|ex-US)\b|n't\b",
    re.IGNORECASE,
)
# Clause boundaries: a contrast or a new clause usually changes the subject ("A is FDA-approved, whereas B ...").
_CLAUSE_SPLIT_RE = re.compile(r";|,?\s+(?:while|whereas|but|although|though)\b", re.IGNORECASE)
# CBER-regulated product types are absent from Drugs@FDA, so "not found" there says nothing about their licence.
_CBER_TYPES = {"cell", "gene", "unknown", "oligosaccharide", "protein-based vaccine"}


def _drug_names(*values: Any) -> List[str]:
    out: List[str] = []
    for v in values:
        name = re.sub(r"\s+", " ", str(v or "")).strip()
        if len(name) >= 3 and name.lower() not in (n.lower() for n in out):
            out.append(name)
    return out


def fda_unapproved_drugs(tool_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[{names, severity, reason}] for drugs the evidence shows are not FDA-approved: Drugs@FDA was queried and lists
    no NDA/BLA approval (MAJOR), or openFDA skipped them because ChEMBL lists no approval anywhere (MINOR). A failed
    lookup, a label with an NDA/BLA number, or a CBER-type product (cell, gene) never counts."""
    out: List[Dict[str, Any]] = []
    for data in _tool_payloads(tool_results):
        for entry in safety_entries(data):
            approval = entry.get("approval") or {}
            if approval.get("status") != "not_found":
                continue
            label_app = str(((entry.get("label") or {}).get("application_number")) or "").upper()
            if label_app.startswith(("NDA", "BLA")):
                continue  # conflicting evidence: an FDA label exists, so stay silent
            if str(entry.get("molecule_type") or "").strip().lower() in _CBER_TYPES:
                continue
            out.append({
                "names": _drug_names(entry.get("ingredient"), entry.get("us_name"), approval.get("ingredient")),
                "severity": "MAJOR",
                "reason": "Drugs@FDA lists no approved NDA or BLA for it",
            })
        for s in (data.get("skipped") or []) if isinstance(data, dict) else []:
            try:
                phase = float(s.get("max_phase")) if isinstance(s, dict) and s.get("max_phase") is not None else None
            except (TypeError, ValueError):
                phase = None
            if phase is None or phase >= 4:
                continue
            out.append({
                "names": _drug_names(s.get("drug")),
                "severity": "MINOR",
                "reason": f"ChEMBL lists it at max phase {phase:g} (not approved by any regulator); Drugs@FDA was not queried",
            })
    return [d for d in out if d["names"]]


# A drug named right after these words is compared with, not described as, the FDA-approved one.
_CONTRAST_LEAD_RE = re.compile(
    r"(?:\bunlike|\bthan|\bversus|\bvs\.?|\bcompared\s+(?:with|to)|\bin\s+contrast\s+to|\binstead\s+of|"
    r"\bother\s+than|\bbesides|\bbeyond|\bexcept(?:\s+for)?)\s+(?:\S+\s+){0,2}$",
    re.IGNORECASE,
)
_APPROVAL_DISTANCE = 60  # characters between the drug name and the approval phrase


def _subject_mention(name: str, clause: str) -> bool:
    """The drug is named in the clause close to the approval phrase, and not only as a point of comparison."""
    phrases = [m.span() for m in FDA_APPROVED_RE.finditer(clause)]
    for m in re.finditer(rf"(?<![\w-]){re.escape(name)}(?![\w-])", clause, re.IGNORECASE):
        if _CONTRAST_LEAD_RE.search(clause[: m.start()]):
            continue
        if any(min(abs(m.start() - e), abs(s - m.end())) <= _APPROVAL_DISTANCE for s, e in phrases):
            return True
    return False


def _approval_clauses(sentence: str) -> List[str]:
    """The clauses of a sentence that contain an FDA-approval phrase."""
    return [c for c in _CLAUSE_SPLIT_RE.split(sentence) if c and FDA_APPROVED_RE.search(c)]


def check_fda_approval(vctx: Any) -> List[Dict[str, Any]]:
    """Flag a sentence that calls a drug FDA-approved when the evidence shows it is not (see fda_unapproved_drugs).
    Conservative: the drug and the approval phrase must share a clause, and hedged, negated, conditional or non-US
    statements are skipped."""
    sentences = [s for s in _sentences(vctx) if FDA_APPROVED_RE.search(s)]
    if not sentences:
        return []
    drugs = fda_unapproved_drugs(vctx.tool_results)
    if not drugs:
        return []
    hedge = getattr(vctx, "hedge_pattern", None)
    defects: List[Dict[str, Any]] = []
    for sentence in sentences:
        for clause in _approval_clauses(sentence):
            if _APPROVAL_HEDGE_RE.search(clause) or (hedge is not None and hedge.search(clause)):
                continue
            for d in drugs:
                hit = next((n for n in d["names"] if _subject_mention(n, clause)), None)
                if not hit:
                    continue
                major = d["severity"] == "MAJOR"
                defects.append(_defect(
                    "CLAIM_CONTRADICTED" if major else "CLINICAL_CLAIM",
                    d["severity"],
                    f"The answer calls {hit} FDA-approved, but {d['reason']}.",
                    f"Do not call {hit} FDA-approved; describe its status as the evidence gives it (e.g. investigational, "
                    "or approved outside the US), or cite a retrieved source that shows the FDA approval.",
                    sentence,
                ))
                break
    unique = {(d["description"], d["claim"]): d for d in defects}
    return list(unique.values())
