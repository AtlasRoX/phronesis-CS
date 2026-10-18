"""Extension module owned by GRADING (feature 5, evidence grading + retraction checks). Registers that feature's hooks, sources and tools."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from . import hooks

_STRIP_KEYS = ("integrity", "notices")
_EXCLUDING = ("retracted", "withdrawn")
_PMID_RE = re.compile(r"\bPMID:?\s*(\d{1,9})\b", re.IGNORECASE)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
# Integrity statuses that keep an article citeable only with a caveat in the citing sentence, and the wording that
# counts as that caveat.
_CAVEATS = {
    "expression_of_concern": (re.compile(r"concern", re.IGNORECASE), "an expression of concern"),
    "partially_retracted": (re.compile(r"retract", re.IGNORECASE), "a partial retraction"),
}
_DESIGN_CLAIM_RE = re.compile(
    r"randomi[sz]ed|phase\s*(?:i{1,3}|iv|[1-4])\b|meta-?analys|systematic review", re.IGNORECASE
)
# Designs compatible with each kind of design claim (LIT-019): a randomized / phase claim fits only a trial grade,
# a meta-analysis / systematic-review claim only an evidence-synthesis grade. Every other grade is checked.
_TRIAL_TYPES = frozenset({"rct", "clinical_trial"})
_SYNTHESIS_TYPES = frozenset({"meta_analysis", "network_meta_analysis", "systematic_review"})
# Words that show the sentence already states the article's real design.
_DESIGN_WORDS = {
    # An indirect comparison (MAIC, STC) draws on randomized trials without randomizing the arms it compares: a
    # "randomized" / "phase 3" claim about it fits only when the sentence also names the indirect method.
    "indirect_comparison": re.compile(r"\bindirect\w*\b|\bMAIC\b|\bmatching[- ]adjusted\b|\bsimulated treatment comparison", re.IGNORECASE),
    "in_vitro": re.compile(r"\bin vitro\b|\bcell[- ]?lines?\b|\bcultured cells\b|\borganoid", re.IGNORECASE),
    "preclinical_in_vivo": re.compile(r"\bmice\b|\bmouse\b|\bmurine\b|\bxenograft|\bin vivo\b|\brats?\b|\banimal", re.IGNORECASE),
    "computational": re.compile(r"\bin silico\b|\bcomputational\b|\bdocking\b|\bsimulat", re.IGNORECASE),
    "review": re.compile(r"\breview", re.IGNORECASE),
    "case_report": re.compile(r"\bcase\b", re.IGNORECASE),
    "observational": re.compile(r"\bobservational\b|\bcohort\b|\bretrospective\b|\bregistry\b|\bcase-control\b|\breal-world\b|\bcross-sectional\b", re.IGNORECASE),
    "editorial": re.compile(r"\beditorial\b|\bcommentary\b|\bopinion\b|\bguideline|\bconsensus\b", re.IGNORECASE),
    "conference_abstract": re.compile(r"\babstract\b|\bconference\b|\bmeeting\b|\bposter\b|\bcongress\b", re.IGNORECASE),
    "trial_protocol": re.compile(r"\bprotocol\b|\brationale and design\b|\bdesign of\b", re.IGNORECASE),
}
_NEVER = re.compile(r"(?!x)x")


def _lit_payloads(tool_results: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for res in (tool_results or {}).values():
        data = getattr(res, "data", None)
        if getattr(res, "success", False) and isinstance(data, dict) and ("graded_articles" in data or "excluded_articles" in data):
            yield data


def _strip_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_nested(v) for k, v in value.items() if k not in _STRIP_KEYS}
    if isinstance(value, list):
        return [_strip_nested(v) for v in value]
    return value


def _serialization_filter(tool_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Retracted works and notice identifiers must never become 'observed' (and so citeable)."""
    if "excluded_articles" not in data and "graded_articles" not in data:
        return data
    return _strip_nested({k: v for k, v in data.items() if k != "excluded_articles"})


def _sources_label(notices: List[Dict[str, Any]]) -> str:
    from ..lithoscribe.integrity import source_names

    return ", ".join(source_names(notices)) or "EuropePMC"


def exclusion_reason(article: Dict[str, Any], display: str) -> str:
    """'PMID 9500320 is retracted (Retraction in PMID 20137807, 2010; sources: EuropePMC, Crossref/Retraction Watch)'."""
    from ..lithoscribe.integrity import notice_year

    integrity = article.get("integrity") or {}
    status = str(integrity.get("status") or "retracted")
    notices = [n for n in integrity.get("notices") or [] if isinstance(n, dict)]
    wanted = ("retraction",) if status == "retracted" else ("withdrawal", "removal")
    relevant = [n for n in notices if n.get("kind") in wanted] or notices
    # The most recent notice is the operative one (Wakefield 1998: a 2004 partial retraction, then the 2010 retraction).
    by_recency = sorted(relevant, key=lambda n: notice_year(n) or "", reverse=True)
    lead = next((n for n in by_recency if n.get("notice_pmid")), None) or next((n for n in by_recency if n.get("notice_doi")), None)
    label = "Retraction" if status == "retracted" else "Withdrawal"
    detail = label
    if lead:
        ref = f"PMID {lead['notice_pmid']}" if lead.get("notice_pmid") else f"DOI {lead['notice_doi']}"
        year = notice_year(lead) or next((notice_year(n) for n in relevant if notice_year(n)), None)
        detail = f"{label} in {ref}{', ' + year if year else ''}"
    return f"{display} is {status} ({detail}; sources: {_sources_label(relevant)})"


def _identifier_veto(tool_results: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {"pmids": {}, "dois": {}}
    for data in _lit_payloads(tool_results):
        for art in list(data.get("excluded_articles") or []) + list(data.get("graded_articles") or []):
            if not isinstance(art, dict) or str((art.get("integrity") or {}).get("status")) not in _EXCLUDING:
                continue
            if art.get("pmid"):
                out["pmids"].setdefault(str(art["pmid"]), exclusion_reason(art, f"PMID {art['pmid']}"))
            if art.get("doi"):
                out["dois"].setdefault(str(art["doi"]).lower(), exclusion_reason(art, f"DOI {art['doi']}"))
    return {k: v for k, v in out.items() if v}


def _included_articles(tool_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [a for data in _lit_payloads(tool_results) for a in data.get("graded_articles") or [] if isinstance(a, dict)]


def _citations_in(text: str, articles: List[Dict[str, Any]]) -> List[Tuple[int, int, Dict[str, Any], str]]:
    """(start, end, article, display) for every PMID / DOI of a known article cited in text, in order."""
    by_pmid = {str(a["pmid"]): a for a in articles if a.get("pmid")}
    by_doi = {str(a["doi"]).lower(): a for a in articles if a.get("doi")}
    hits = []
    for m in _PMID_RE.finditer(text):
        if m.group(1) in by_pmid:
            hits.append((m.start(), m.end(), by_pmid[m.group(1)], f"PMID {m.group(1)}"))
    for m in _DOI_RE.finditer(text):
        doi = m.group(0).rstrip(".,;)").lower()
        if doi in by_doi:
            hits.append((m.start(), m.end(), by_doi[doi], f"DOI {doi}"))
    return sorted(hits, key=lambda h: h[0])


def _claim_texts(vctx: Any) -> List[str]:
    return list(vctx.sentences or []) + list(vctx.table_rows or [])


def _integrity_flag_check(vctx: Any) -> List[Dict[str, Any]]:
    """An article under an expression of concern or partially retracted (LIT-007) must carry that caveat in every
    sentence that cites it."""
    articles = _included_articles(vctx.tool_results)
    defects, seen = [], set()
    for status, (caveat_re, phrase) in _CAVEATS.items():
        flagged = [a for a in articles if (a.get("integrity") or {}).get("status") == status]
        if not flagged:
            continue
        for sentence in _claim_texts(vctx):
            if caveat_re.search(sentence):
                continue
            for _, _, art, display in _citations_in(sentence, flagged):
                if display in seen:
                    continue
                seen.add(display)
                defects.append({
                    "category": "INTEGRITY_FLAG",
                    "severity": "MAJOR",
                    "claim": display,
                    "description": f"{display} is subject to {phrase} ({_sources_label((art.get('integrity') or {}).get('notices') or [])}) but is cited without that caveat.",
                    "suggested_correction": f"State in the same sentence that {display} is subject to {phrase}, or remove the claim.",
                })
    return defects


# A trial named as another entity's development stage, or by its own registry id, is not the cited paper's design.
_DEVELOPMENT_STATUS_RE = re.compile(
    r"\b(?:advanc\w*|entered|enters|entering|moved into|moving into|progressed (?:to|into)|undergoing|ongoing|planned|"
    r"registered|underway|being (?:tested|evaluated))\b[^.;]{0,40}\bphase\b|\bNCT\d{8}\b|\bCTR\d{6,}\b",
    re.IGNORECASE,
)


def _described_as(match: str) -> str:
    low = match.lower()
    if low.startswith("randomi"):
        return "a randomized trial"
    if low.startswith("phase"):
        return "a phased clinical trial"
    if low.startswith("meta"):
        return "a meta-analysis"
    return "a systematic review"


def _claim_fits(claim: str, study_type: str) -> bool:
    low = claim.lower()
    if low.startswith(("randomi", "phase")):
        return study_type in _TRIAL_TYPES
    return study_type in _SYNTHESIS_TYPES


def _evidence_grade_check(vctx: Any) -> List[Dict[str, Any]]:
    """Every included article is checked (LIT-019): a randomized / phase claim about a non-trial, or a
    meta-analysis / systematic-review claim about anything but an evidence synthesis, is a MAJOR defect when the
    grade comes from indexed metadata (pubType or MeSH), and a MINOR "design not verifiable" defect when the grade
    is unclassified or inferred from text alone (low confidence)."""
    articles = [a for a in _included_articles(vctx.tool_results) if (a.get("grade") or {}).get("study_type")]
    if not articles:
        return []
    defects, seen = [], set()
    for sentence in _claim_texts(vctx):
        prev_end = 0
        for start, end, art, display in _citations_in(sentence, articles):
            # The clause that leads up to a citation is the claim made about that citation.
            window = sentence[prev_end:end]
            prev_end = end
            grade = art.get("grade") or {}
            study_type = str(grade.get("study_type"))
            claim = _DESIGN_CLAIM_RE.search(window)
            if not claim or display in seen or _claim_fits(claim.group(0), study_type):
                continue
            if _DESIGN_WORDS.get(study_type, _NEVER).search(window) or vctx.hedge_pattern.search(window):
                continue
            if _DEVELOPMENT_STATUS_RE.search(window):
                continue  # "X has advanced to phase 3 (CTR...)" reports a drug's status, not the cited study's design
            seen.add(display)
            described = _described_as(claim.group(0))
            if study_type == "unclassified" or grade.get("confidence") == "low":
                inferred = (
                    "no design is indexed for it" if study_type == "unclassified"
                    else f"its design ({str(grade.get('label') or study_type).lower()}) was only inferred from its wording"
                )
                defects.append({
                    "category": "EVIDENCE_GRADE",
                    "severity": "MINOR",
                    "claim": display,
                    "description": f"{display} is described as {described}, but {inferred}; the design is not verifiable.",
                    "suggested_correction": f"Do not state a design for {display} that its record does not confirm, or say that the design is unverified.",
                })
                continue
            basis = {
                "europepmc_pubtype": "EuropePMC publication type",
                "mesh_heading": "MeSH",
                "text_heuristic": "reported analysis, indexed differently" if grade.get("indexed_design") else "title/abstract wording",
            }.get(str(grade.get("basis")), "metadata")
            cues = ", ".join(str(c) for c in grade.get("cues") or [])
            label = str(grade.get("label") or study_type).lower()
            article_word = "article" if study_type in ("review", "case_report", "editorial", "conference_abstract") else "study"
            defects.append({
                "category": "EVIDENCE_GRADE",
                "severity": "MAJOR",
                "claim": display,
                "description": f"{display} is {'an' if label[:1] in 'aeiou' else 'a'} {label} {article_word} ({basis}: {cues}) but is described as {described}.",
                "suggested_correction": f"Describe {display} by its actual design ({label}) and do not present it as {described}.",
            })
    return defects


def _document_attrs(passage: Dict[str, Any]) -> Dict[str, str]:
    grade = passage.get("evidence_grade")
    if not isinstance(grade, dict):
        return {}
    return {
        "study_type": str(grade.get("study_type") or "unclassified"),
        "grade_label": str(passage.get("grade_label") or grade.get("label") or ""),
        "grade_basis": str(grade.get("basis") or "none"),
        "grade_confidence": str(grade.get("confidence") or "low"),
        "preprint": "true" if grade.get("is_preprint") else "false",
        "integrity": str(passage.get("integrity_status") or "unchecked"),
    }


def _literature_tail(lit: Dict[str, Any]) -> str:
    """Excluded works by title only: the model may report the exclusion but has no identifier to cite."""
    excluded = [a for a in (lit or {}).get("excluded_articles") or [] if isinstance(a, dict)]
    if not excluded:
        return ""
    rows = [
        f"<article reason={quoteattr(str(a.get('excluded_reason') or 'excluded'))} year={quoteattr(str(a.get('year') or ''))}>"
        f"{escape(_DOI_RE.sub('', str(a.get('title') or '')))}</article>"
        for a in excluded
    ]
    return f'<excluded_literature count="{len(excluded)}">' + "".join(rows) + "</excluded_literature>"


def step_parts(data: Dict[str, Any]) -> List[str]:
    from ..lithoscribe.evidence_grading import summary_phrases

    parts: List[str] = []
    summary = summary_phrases(data.get("grade_summary") or {})
    if summary:
        parts.append(" · ".join(summary))
    excluded = [a for a in data.get("excluded_articles") or [] if isinstance(a, dict)]
    for reason in _EXCLUDING:
        n = sum(1 for a in excluded if a.get("excluded_reason") == reason)
        if n:
            parts.append(f"{n} {reason} article{'s' if n != 1 else ''} excluded")
    crossref = str((data.get("integrity_check") or {}).get("crossref") or "")
    if crossref.startswith("unavailable for"):
        parts.append("Crossref check partly unavailable")
    elif crossref.startswith("unavailable"):
        parts.append("Crossref check unavailable")
    return parts


def _step_augmenter(res: Any, _title_ctx: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    data = getattr(res, "data", None)
    if not getattr(res, "success", False) or not isinstance(data, dict):
        return None, []
    return None, step_parts(data)


def _artifact_builder(tool_results: Dict[str, Any], _ctx: Any) -> Dict[str, Any]:
    res = (tool_results or {}).get("lithoscribe") or (tool_results or {}).get("lithoscribe_literature_synthesis")
    data = getattr(res, "data", None)
    if not getattr(res, "success", False) or not isinstance(data, dict) or "graded_articles" not in data:
        return {}
    # Limitations of every literature call this turn (the main search and any follow-up searches), so the export
    # and the report state what the integrity check and the excerpts could not cover (LIT-005).
    limitations: List[str] = []
    for payload in [data, *_lit_payloads(tool_results)]:
        for lim in payload.get("limitations") or []:
            if isinstance(lim, str) and lim.strip() and lim not in limitations:
                limitations.append(lim)
    literature: Dict[str, Any] = {
        "graded_articles": data.get("graded_articles") or [],
        "excluded_articles": data.get("excluded_articles") or [],
        "integrity_check": data.get("integrity_check") or {},
        "grade_summary": data.get("grade_summary") or {},
        "limitations": limitations,
        "evidence_sufficiency": data.get("evidence_sufficiency"),
    }
    if isinstance(data.get("provenance"), dict):
        literature["provenance"] = data["provenance"]
    return {"literature": literature}


PROMPT_RULE = (
    "Weigh literature by study_type: meta-analyses and randomized trials outweigh observational studies, which "
    "outweigh case reports, reviews and preclinical work. State a cited document's design from its evidence_level "
    "(for example 'in a randomized phase III trial (PMID: ...)'); an unclassified or unverified design is not a "
    "trial. Never extrapolate in vitro, animal or computational results to clinical efficacy. Flag a document whose "
    "integrity is expression_of_concern or partially_retracted in the sentence citing it. Cite a document for its "
    "own results, not for studies its Discussion describes. Never cite a work in <excluded_literature>; you may only "
    "report how many were excluded and why."
)

hooks.register_serialization_filter("grading", _serialization_filter)
hooks.register_identifier_veto("grading", _identifier_veto)
hooks.register_verification_check("integrity_flag", _integrity_flag_check)
hooks.register_verification_check("evidence_grade", _evidence_grade_check)
hooks.register_evidence_decorator("literature_document_attrs", "grading", _document_attrs)
hooks.register_evidence_decorator("literature_tail", "grading", _literature_tail)
hooks.register_step_augmenter("lithoscribe", "grading", _step_augmenter)
hooks.register_artifact_builder("literature", _artifact_builder)


def register_prompt_hooks() -> None:
    hooks.register_prompt_rule("grading", PROMPT_RULE)


def register_tools(registry: Any) -> None:
    """Lithoscribe is a built-in tool; the prompt rule joins it when the real registry is built."""
    register_prompt_hooks()
