"""
Verifier helpers for ClinicalTrials.gov evidence.

The foundation already checks NCT identifiers generically (hedge-independent). This module adds:
- per-trial evidence documents, so a clinical sentence is grounded in the record of the trial it cites;
- audit_trial_figures: in every sentence or table row that cites an NCT ID present in the trial records,
  each numeric figure must appear verbatim in the union of the cited trials' records. A figure from another
  trial, or a rounded value, is a CRITICAL CLINICAL_CLAIM defect.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..utils.text import fold_typography

NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)

TOOL_KEYS = ("clinical_trials",)
TOOL_NAME = "lithoscribe_clinical_trials_search"

_URL_RE = re.compile(r"https?://\S+|\]\([^)]*\)")
_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}-\d{2}(?:-\d{2})?\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?(?:19|20)\d{2}\b|"
    r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}\b|"
    r"\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(r"\b(?:early\s+)?phase\s*(?:[0-4]|iv|i{1,3})[ab]?(?:\s*[/\-]\s*(?:phase\s*)?(?:[0-4]|iv|i{1,3})[ab]?)?\b", re.IGNORECASE)
_CI_LEVEL_RE = re.compile(r"\b9[059](?:\.\d+)?\s*%\s*(?=c\.?i\b|confidence|ci\b)", re.IGNORECASE)
_FOOTNOTE_RE = re.compile(r"\[\d{1,3}\]")
# Identifiers and loci whose digits are not result figures (PMIDs, DOIs, ontology/database ids, exon numbers).
_PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{1,9})\b", re.IGNORECASE)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s|()\[\]<>\"']+)", re.IGNORECASE)
_ID_RE = re.compile(
    r"\b(?:PMCID|PMC|HGNC|MONDO|EFO|DOID|HP|Orphanet|CHEMBL|GO|OMIM|MIM|ClinVar|VCV|GSE|GSM)\s*[:_]?\s*\d+\b"
    r"|\bexons?\s+\d{1,2}(?:\s*(?:/|,|and|or)\s*\d{1,2})*\b",
    re.IGNORECASE,
)
_THOUSANDS_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+)(?![\d.])")

_NUM = r"(\d+(?:\.\d+)?)"
_PERCENT_RE = re.compile(rf"(?<![\w.]){_NUM}\s*%")
_DURATION_RE = re.compile(rf"(?<![\w.]){_NUM}\s*(?:months?|mos?\.?|weeks?|wks?)\b", re.IGNORECASE)
_RATIO_RE = re.compile(
    # HR/OR/RR are case-sensitive so the English word "or" is not read as an odds ratio.
    r"(?:\b(?:HR|OR|RR)\b|(?i:\b(?:hazard|odds|risk)\s+ratio\b|\brelative\s+risk\b))[^\d\n]{0,24}?"
    r"(\d+\.\d+)(?:[^\d\n]{1,16}(\d+\.\d+)(?:[^\d\n]{1,6}(\d+\.\d+))?)?"
)
_PVALUE_RE = re.compile(r"\bp\s*(?:-?value)?\s*(?:=|<|>|≤|≥|<=|>=)\s*(0?\.\d+|\d+\.\d+(?:e-?\d+)?)", re.IGNORECASE)
_BARE_RE = re.compile(rf"(?<![\w.]){_NUM}(?![\w.]|\.\d)")


def _is_trial_result(tool_key: str, result: Any) -> bool:
    if result is None or not getattr(result, "success", False):
        return False
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get("trials"), list):
        return False
    return tool_key in TOOL_KEYS or getattr(result, "tool_name", "") == TOOL_NAME


def _trial_doc(trial: Dict[str, Any]) -> str:
    return json.dumps(trial, ensure_ascii=False, default=str).lower()


def collect_trial_docs(tool_results: Dict[str, Any]) -> Dict[str, str]:
    """NCT ID -> lowercased serialized record of that trial, over every successful trials result."""
    docs: Dict[str, str] = {}
    for key, res in (tool_results or {}).items():
        if not _is_trial_result(key, res):
            continue
        for t in res.data["trials"]:
            if isinstance(t, dict) and t.get("nct_id"):
                nct = str(t["nct_id"]).upper()
                docs[nct] = (docs.get(nct, "") + " " + _trial_doc(t)).strip()
    return docs


def trial_evidence_docs(tool_key: str, result: Any) -> Optional[List[Tuple[str, str]]]:
    """Evidence doc provider: one doc per trial ('NCT:<id>'), then the whole result as a fallback doc.

    Per-trial docs come first so claim support points at the cited trial; the whole-result doc keeps
    multi-trial comparison sentences groundable (audit_trial_figures still checks per-trial figures)."""
    if not _is_trial_result(tool_key, result):
        return None
    docs: List[Tuple[str, str]] = []
    for t in result.data["trials"]:
        if isinstance(t, dict) and t.get("nct_id"):
            docs.append((f"NCT:{str(t['nct_id']).upper()}", _trial_doc(t)))
    whole = {k: v for k, v in result.data.items() if k != "citation_records"}
    docs.append((f"tool:{tool_key}", json.dumps(whole, ensure_ascii=False, default=str).lower()))
    return docs


def _strip_non_figures(text: str) -> str:
    text = _URL_RE.sub(" ", text)
    text = _DOI_RE.sub(" ", text)
    text = _PMID_RE.sub(" ", text)
    text = _ID_RE.sub(" ", text)
    text = NCT_RE.sub(" ", text)
    text = _FOOTNOTE_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    text = _PHASE_RE.sub(" ", text)
    text = _CI_LEVEL_RE.sub(" ", text)
    return _THOUSANDS_RE.sub(lambda m: m.group(1).replace(",", ""), text)


def extract_figures(text: str, table_row: bool = False) -> List[str]:
    """Numeric figures a reader would take as results: percentages, durations, HR/OR values, p-values;
    in table rows every bare number (phase digits, dates and NCT digits excluded)."""
    cleaned = _strip_non_figures(text)
    found: List[str] = []

    def add(values: Iterable[Optional[str]]) -> None:
        for v in values:
            if v and v not in found:
                found.append(v)

    add(m.group(1) for m in _PERCENT_RE.finditer(cleaned))
    add(m.group(1) for m in _DURATION_RE.finditer(cleaned))
    for m in _RATIO_RE.finditer(cleaned):
        add(m.groups())
    add(m.group(1) for m in _PVALUE_RE.finditer(cleaned))
    if table_row:
        add(m.group(1) for m in _BARE_RE.finditer(cleaned))
    return found


def _figure_in(value: str, doc: str) -> bool:
    # Records write "1,234" and "10·2" where the claim (already folded) has "1234" and "10.2".
    doc = _THOUSANDS_RE.sub(lambda m: m.group(1).replace(",", ""), fold_typography(doc))
    variants = {value}
    if value.startswith("."):
        variants.add("0" + value)
    elif value.startswith("0."):
        variants.add(value[1:])
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?!\d|\.\d)", doc) for v in variants)


def _units(vctx: Any) -> List[Tuple[str, bool]]:
    rows = [r for r in (getattr(vctx, "table_rows", None) or []) if r]
    row_set = {r.strip() for r in rows}
    units: List[Tuple[str, bool]] = [(r, True) for r in rows]
    for s in getattr(vctx, "sentences", None) or []:
        if s and s.strip() not in row_set and not s.strip().startswith("|"):
            units.append((s, False))
    return units


def _literature_docs(vctx: Any) -> Dict[str, str]:
    """Evidence documents keyed 'PMID:<id>' / 'DOI:<doi>', URLs, and arbitrary evidence keys, lowercased."""
    out: Dict[str, str] = {}
    keys = getattr(vctx, "evidence_keys", None) or []
    texts = getattr(vctx, "evidence_documents", None) or []
    for key, doc in zip(keys, texts):
        k = str(key)
        doc_str = str(doc).lower()
        if k.startswith(("PMID:", "DOI:")):
            norm = "PMID:" + k[5:].strip() if k.startswith("PMID:") else "DOI:" + k[4:].strip().lower()
            out[norm] = (out.get(norm, "") + " " + doc_str).strip()
        elif k.startswith(("http://", "https://", "url:")):
            norm_url = k[4:].strip().lower() if k.startswith("url:") else k.strip().lower()
            out[norm_url] = (out.get(norm_url, "") + " " + doc_str).strip()
        else:
            norm_k = k.strip().lower()
            out[norm_k] = (out.get(norm_k, "") + " " + doc_str).strip()
    return out


def _cited_literature(unit: str, lit_docs: Dict[str, str], vctx: Any = None) -> List[str]:
    keys = [f"PMID:{m}" for m in _PMID_RE.findall(unit)]
    keys += [f"DOI:{m.rstrip('.,;:').lower()}" for m in _DOI_RE.findall(unit)]
    out = [lit_docs[k] for k in dict.fromkeys(keys) if k in lit_docs]
    cited_urls = [u.rstrip(".,;:").lower() for u in re.findall(r"https?://[^\s)\]\"'>]+", unit)]
    if cited_urls and vctx is not None:
        ev_docs = getattr(vctx, "evidence_documents", None) or []
        for doc in ev_docs:
            doc_str = str(doc).lower()
            if any(u in doc_str for u in cited_urls):
                out.append(doc_str)
    return out


def _nct_referenced_docs(cited_ncts: List[str], vctx: Any) -> List[str]:
    """Non-registry evidence documents (literature, web) in vctx that explicitly mention any cited NCT ID."""
    ev_docs = getattr(vctx, "evidence_documents", None) or []
    keys = getattr(vctx, "evidence_keys", None) or []
    matched: List[str] = []
    for key, doc in zip(keys, ev_docs):
        k = str(key)
        # Exclude whole-tool or multi-trial registry dumps to prevent cross-trial contamination
        if k.startswith(("tool:clinical_trials", "NCT:")):
            continue
        doc_str = str(doc).lower()
        for nct in cited_ncts:
            if nct.lower() in doc_str:
                matched.append(doc_str)
                break
    return matched


def _trial_result_pmid_docs(cited_ncts: List[str], tool_results: Dict[str, Any], lit_docs: Dict[str, str]) -> List[str]:
    """Literature documents corresponding to result_pmids declared in the trial's registry record."""
    docs: List[str] = []
    for key, res in (tool_results or {}).items():
        if not _is_trial_result(key, res):
            continue
        for t in res.data["trials"]:
            if isinstance(t, dict) and t.get("nct_id"):
                nct = str(t["nct_id"]).upper()
                if nct in cited_ncts:
                    for p in (t.get("result_pmids") or []):
                        pk = f"PMID:{str(p).strip()}"
                        if pk in lit_docs:
                            docs.append(lit_docs[pk])
    return docs


def audit_trial_figures(vctx: Any) -> List[Dict[str, Any]]:
    """Verification check: figures next to an NCT ID must come from that trial's posted record, or from a
    retrieved publication (PMID/DOI passage or web link) cited in the same sentence or row, or from an
    evidence document referencing the cited trial; the prompt allows both."""
    docs = collect_trial_docs(getattr(vctx, "tool_results", None) or {})
    if not docs:
        return []
    lit_docs = _literature_docs(vctx)
    nct_ref_docs = _nct_referenced_docs(list(docs.keys()), vctx)
    defects: List[Dict[str, Any]] = []
    seen = set()
    for unit, is_row in _units(vctx):
        cited = []
        for m in NCT_RE.findall(unit):
            nct = m.upper()
            if nct in docs and nct not in cited:
                cited.append(nct)
        if not cited:
            continue
        cited_nct_refs = [doc for doc in nct_ref_docs if any(n.lower() in doc for n in cited)]
        cited_pmid_docs = [
            doc for n in cited for doc in _trial_result_pmid_docs([n], getattr(vctx, "tool_results", None) or {}, lit_docs)
        ]
        union = " ".join([docs[n] for n in cited] + _cited_literature(unit, lit_docs, vctx) + cited_nct_refs + cited_pmid_docs)
        for fig in extract_figures(unit, table_row=is_row):
            if _figure_in(fig, union):
                continue
            key = (unit.strip(), fig)
            if key in seen:
                continue
            seen.add(key)
            defects.append(
                {
                    "category": "CLINICAL_CLAIM",
                    "severity": "CRITICAL",
                    "description": f"figure {fig} not found in the posted record of {', '.join(cited)}",
                    "suggested_correction": (
                        "Quote figures exactly as posted on ClinicalTrials.gov for the cited trial (value, unit and arm), "
                        "or cite the publication (PMID/DOI) if reporting published results not posted to ClinicalTrials.gov."
                    ),
                    "claim": unit.strip()[:240],
                }
            )
    return defects
