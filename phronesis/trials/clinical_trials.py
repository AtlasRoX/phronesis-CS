"""
ClinicalTrials.gov API v2 client for Lithoscribe: registry search plus posted results.

One tool call makes at most MAX_HTTP_CALLS requests:
  1. the relevance search and an aggFilters=results:with companion search, in parallel, for the first
     query strategy (the companion lifts trials that have posted results, which relevance often buries);
  2. a fallback strategy (relevance only) when the previous one matched nothing;
  3. one batched filter.ids request for the posted results of trials with hasResults.
A search that matched nothing under phase/status filters is retried once without them (labelled as such).

Ranked mode (the production client, fetch_size > max_trials): the relevance search fetches a larger page,
a phase 3 companion search (when no phase was requested) and the results-only companion run with the same
condition and setting filter, and the pooled trials are re-ranked locally (phase, design, setting-term match,
enrollment, posted results) before the top max_trials are shown. Setting terms ('adjuvant', 'resected') go in
query.term; a setting search that matches nothing falls back to the same search without them. Up to three
conditions are searched concurrently (one search each) and merged round-robin, deduplicated by NCT ID.
The whole search has a deadline below the 25 s registry tool timeout, so a slow upstream yields the partial
result already fetched instead of being cancelled. HTTP 429 is retried once, honouring Retry-After.
Responses are cached for 10 minutes by exact params. Every result value stays the string the API returned
("NA", "<0.0001"): the verifier matches figures exactly, so nothing is parsed into floats or rounded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

import httpx
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from ..tools.registry import ErrorType, ToolResult
from ..utils.http import retry_after_seconds
from ..utils.http_limits import TTLCache, get_limiter

logger = logging.getLogger(__name__)

BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"
USER_AGENT = "Phronesis-ClinicalTrials/1.0"
TIMEOUT = httpx.Timeout(12.0, connect=8.0)
SOURCE_NAME = "ClinicalTrials.gov API v2"

SEARCH_FIELDS = ",".join(
    [
        "NCTId", "BriefTitle", "OfficialTitle", "Acronym", "OverallStatus", "WhyStopped", "Phase", "StudyType", "EnrollmentInfo",
        "LeadSponsorName", "LeadSponsorClass", "Condition", "InterventionType", "InterventionName",
        "PrimaryOutcomeMeasure", "PrimaryOutcomeTimeFrame", "StartDate", "PrimaryCompletionDate",
        "LastUpdatePostDate", "HasResults", "ResultsFirstPostDate", "DesignAllocation", "BriefSummary", "Keyword",
    ]
)
RESULT_FIELDS = "NCTId,HasResults,OutcomeMeasuresModule,ReferencePMID,ReferenceType"

MAX_HTTP_CALLS = 4
RANKED_EXTRA_CALLS = 4  # companions, setting fallback and late companions in ranked mode, per condition
FETCH_SIZE = 50  # trials fetched per search in ranked mode before local re-ranking
RANKED_COMPANION_SIZE = 20  # results-only companion page in ranked mode
MAX_CONDITIONS = 3
MAX_TRIAL_NAMES = 6
NAMED_TRIAL_PAGE = 10  # trials fetched by the named-trial title/acronym lookup
MAX_STRATEGIES = 3
MAX_MEASURES = 6
MAX_MEASUREMENTS = 12
MAX_ANALYSES = 3
CACHE_TTL_S = 600.0
SEARCH_DEADLINE_S = 20.0  # under the 25 s registry tool timeout
MAX_RETRY_AFTER_S = 5.0

PHASES = ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA")
STATUSES = (
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING", "RECRUITING",
    "SUSPENDED", "TERMINATED", "WITHDRAWN", "AVAILABLE", "NO_LONGER_AVAILABLE", "TEMPORARILY_NOT_AVAILABLE",
    "APPROVED_FOR_MARKETING", "WITHHELD", "UNKNOWN",
)
PhaseName = Literal["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
NctId = Annotated[str, StringConstraints(pattern=r"^NCT\d{8}$")]

_CACHE = TTLCache(maxsize=256, ttl_s=CACHE_TTL_S)

# Data version of the registry (DAT-010): /api/v2/version returns {"apiVersion", "dataTimestamp"}. It is probed
# best effort next to the search, cached for VERSION_TTL_S, and never fails or noticeably delays a search.
VERSION_URL = "https://clinicaltrials.gov/api/v2/version"
VERSION_TTL_S = 6 * 3600.0
VERSION_TIMEOUT_S = 4.0
VERSION_WAIT_S = 1.5  # extra wait after the search finished, at most
_VERSION_CACHE: Dict[str, Tuple[float, str]] = {}


def clear_cache() -> None:
    _CACHE.clear()
    _VERSION_CACHE.clear()


def version_label(body: Any) -> Optional[str]:
    """'ClinicalTrials.gov data 2026-10-15T09:00:05 (API 2.0.5)' from a /version body, or None."""
    if not isinstance(body, dict):
        return None
    stamp = str(body.get("dataTimestamp") or "").strip()
    if not stamp:
        return None
    api = str(body.get("apiVersion") or "").strip()
    return f"ClinicalTrials.gov data {stamp}" + (f" (API {api})" if api else "")


# -----------------------------------------------------------------------------
# Models (result values are kept as the API's strings)
# -----------------------------------------------------------------------------

class TrialArmValue(BaseModel):
    group_id: str
    group_title: Optional[str] = None
    n: Optional[str] = None
    value: Optional[str] = None
    lower: Optional[str] = None
    upper: Optional[str] = None
    spread: Optional[str] = None
    class_title: Optional[str] = None
    category_title: Optional[str] = None
    comment: Optional[str] = None


class TrialAnalysis(BaseModel):
    group_ids: List[str] = Field(default_factory=list)
    # Titles of group_ids taken from the measure's group list (every group, not only the measurements kept under
    # MAX_MEASUREMENTS), in registry order. The order does not encode the direction of the estimate (DAT-002).
    group_titles: List[Optional[str]] = Field(default_factory=list)
    param_type: Optional[str] = None
    param_value: Optional[str] = None
    ci_pct: Optional[str] = None
    ci_num_sides: Optional[str] = None  # ONE_SIDED | TWO_SIDED
    ci_lower: Optional[str] = None
    ci_upper: Optional[str] = None
    p_value: Optional[str] = None
    p_value_comment: Optional[str] = None
    method: Optional[str] = None
    # SUPERIORITY | NON_INFERIORITY | EQUIVALENCE | OTHER (not a hypothesis test of those kinds) | legacy combinations
    non_inferiority_type: Optional[str] = None
    non_inferiority_comment: Optional[str] = None
    # The registry's statement of the estimate's direction ("A hazard ratio < 1 favours osimertinib").
    estimate_comment: Optional[str] = None
    group_description: Optional[str] = None
    statistical_comment: Optional[str] = None


class TrialOutcomeResult(BaseModel):
    type: str
    title: str
    param_type: Optional[str] = None
    dispersion_type: Optional[str] = None
    unit: Optional[str] = None
    time_frame: Optional[str] = None
    measurements: List[TrialArmValue] = Field(default_factory=list)
    analyses: List[TrialAnalysis] = Field(default_factory=list)
    # Pre-cap counts (DAT-011): values and analyses beyond MAX_MEASUREMENTS / MAX_ANALYSES are not kept, and the
    # totals say so. None on records built before these fields existed.
    measurements_total: Optional[int] = None
    analyses_total: Optional[int] = None


class TrialRecord(BaseModel):
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    url: str
    brief_title: Optional[str] = None
    official_title: Optional[str] = None
    acronym: Optional[str] = None
    overall_status: Optional[str] = None
    why_stopped: Optional[str] = None
    phases: List[str] = Field(default_factory=list)
    study_type: Optional[str] = None
    enrollment_count: Optional[int] = None
    enrollment_type: Optional[str] = None
    lead_sponsor: Optional[str] = None
    lead_sponsor_class: Optional[str] = None
    conditions: List[str] = Field(default_factory=list)
    interventions: List[Dict[str, Optional[str]]] = Field(default_factory=list)
    primary_outcomes: List[Dict[str, Optional[str]]] = Field(default_factory=list)
    start_date: Optional[str] = None
    primary_completion_date: Optional[str] = None
    last_update_posted: Optional[str] = None
    has_results: bool = False
    results_first_posted: Optional[str] = None
    posted_results: List[TrialOutcomeResult] = Field(default_factory=list)
    # Posted primary/efficacy-like measures before the MAX_MEASURES cap (DAT-011); None when results were not fetched.
    posted_results_total: Optional[int] = None
    result_pmids: List[str] = Field(default_factory=list)


class TrialSearchQuery(BaseModel):
    term: Optional[str] = None
    intervention: Optional[str] = None
    condition: Optional[str] = None
    conditions: List[str] = Field(default_factory=list)  # every condition searched (one search each) when > 1
    setting_terms: List[str] = Field(default_factory=list)
    nct_ids: List[str] = Field(default_factory=list)
    phases: List[str] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)
    trial_names: List[str] = Field(default_factory=list)  # trials the question names ("SOLO-1"): listed and fetched first
    strategy: str
    api_params: Dict[str, str] = Field(default_factory=dict)


class ClinicalTrialsReport(BaseModel):
    source: Literal["ClinicalTrials.gov API v2"] = SOURCE_NAME
    query: TrialSearchQuery
    total_count: int
    trials: List[TrialRecord]
    results_fetched_for: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    # ClinicalTrials.gov data version (/api/v2/version dataTimestamp), best effort; None when not retrieved (DAT-010).
    source_release: Optional[str] = None

    def to_markdown_table(self, max_rows: int = 10) -> str:
        """Landscape table plus a posted-results table, values copied verbatim from the registry."""
        if not self.trials:
            return f"No registered trials matched ({describe_strategy(self.query)}; {self.total_count} on ClinicalTrials.gov)."
        lines = ["| NCT ID | Phase | Status | Enrollment | Lead sponsor |", "|---|---|---|---|---|"]
        for t in self.trials[:max_rows]:
            lines.append(
                f"| {t.nct_id} | {_cell(phase_label(t.phases))} | {_cell(status_label(t.overall_status))} | "
                f"{_cell(enrollment_label(t.enrollment_count, t.enrollment_type))} | {_cell(t.lead_sponsor or '—')} |"
            )
        result_rows: List[str] = []
        for t in self.trials:
            for r in t.posted_results:
                for m in r.measurements:
                    arm = " · ".join(x for x in (m.group_title or m.group_id, m.class_title, m.category_title) if x)
                    result_rows.append(f"| {t.nct_id} | {_cell(r.title)} | {_cell(arm)} | {_cell(value_label(m, r.unit))} |")
        if result_rows:
            lines += ["", "| NCT ID | Endpoint | Arm | Value |", "|---|---|---|---|", *result_rows[:24]]
        return "\n".join(lines)


class ClinicalTrialsSearchInput(BaseModel):
    """ClinicalTrials.gov search. intervention and term are alternative anchors: the search tries
    intervention (+condition), then term (+condition), then term alone, stopping at the first match.
    Pass extracted entities (a drug, a target such as 'KRAS G12C', a condition), never the raw question:
    query.term ANDs every word. conditions adds up to two more conditions, each searched separately and merged;
    setting_terms ('adjuvant', 'resected', 'stage IIB') narrow the search to the clinical setting and steer ranking."""

    term: Optional[str] = Field(default=None, max_length=200)
    intervention: Optional[str] = Field(default=None, max_length=200)
    condition: Optional[str] = Field(default=None, max_length=200)
    conditions: List[Annotated[str, StringConstraints(max_length=200)]] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    setting_terms: List[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=12)
    nct_ids: List[NctId] = Field(default_factory=list, max_length=20)
    phases: List[PhaseName] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)
    max_trials: int = Field(default=10, ge=1, le=25)
    include_results: bool = True
    max_results_trials: int = Field(default=5, ge=0, le=10)
    trial_names: List[Annotated[str, StringConstraints(max_length=40)]] = Field(
        default_factory=list, max_length=MAX_TRIAL_NAMES,
        description="trial names the question mentions (acronyms such as 'SOLO-1', 'KEYNOTE-716'): those trials are "
        "listed first and their posted results fetched first",
    )

    @field_validator("term", "intervention", "condition", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any, info: Any) -> Any:
        if isinstance(v, (list, tuple)):
            # Agents pass ["osimertinib", "lazertinib"]: alternatives for an intervention, words for a term,
            # and the first one for a single condition (more conditions belong in `conditions`).
            items = [" ".join(str(x).split()) for x in v if str(x).strip()]
            if not items:
                return None
            v = " OR ".join(items) if info.field_name == "intervention" else items[0] if info.field_name == "condition" else " ".join(items)
        if isinstance(v, str):
            v = " ".join(v.split())
            return v or None
        return v

    @field_validator("trial_names", mode="before")
    @classmethod
    def _clean_trial_names(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = re.split(r"[;,]", v)
        if isinstance(v, list):
            out: List[str] = []
            for x in v:
                x = " ".join(re.sub(r"[\"()\[\]]", " ", str(x)).split())[:40]
                if len(x) >= 3 and re.search(r"[A-Za-z]", x) and x.lower() not in {o.lower() for o in out}:
                    out.append(x)
            return out[:MAX_TRIAL_NAMES]
        return v

    @field_validator("conditions", "setting_terms", mode="before")
    @classmethod
    def _clean_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [x for x in v.split(";")]
        if isinstance(v, list):
            out: List[Any] = []
            for x in v:
                if isinstance(x, str):
                    # Quotes and brackets would change the registry's search expression.
                    x = " ".join(re.sub(r"[\"()\[\]]", " ", x).split())
                    if not x or x.lower() in {str(o).lower() for o in out}:
                        continue
                out.append(x)
            return out
        return v

    @field_validator("nct_ids", mode="before")
    @classmethod
    def _normalize_ncts(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x]
        if isinstance(v, list):
            out: List[Any] = []
            for x in v:
                x = x.strip().upper() if isinstance(x, str) else x
                if x not in out:
                    out.append(x)
            return out
        return v

    @field_validator("phases", mode="before")
    @classmethod
    def _normalize_phases(cls, v: Any) -> Any:
        """'Phase 3', 'phase III', '3', 'Phase 2/3', 'N/A', 'early phase 1' -> registry phase codes."""
        if isinstance(v, str):
            v = [v]
        out: List[str] = []
        roman = {"IV": "4", "III": "3", "II": "2", "I": "1"}
        for raw in v or []:
            text = str(raw).strip().upper()
            if text in ("NA", "N/A", "NOT APPLICABLE"):
                codes = ["NA"]
            elif "EARLY" in text:
                codes = ["EARLY_PHASE1"]
            elif re.fullmatch(r"PHASE[1-4]", text):
                codes = [text]
            else:
                text = re.sub(r"\b(IV|III|II|I)\b", lambda m: roman[m.group(1)], text)
                codes = [f"PHASE{d}" for d in re.findall(r"[1-4]", text)]
            for c in codes:
                if c not in out:
                    out.append(c)
        return out

    @field_validator("statuses", mode="before")
    @classmethod
    def _validate_statuses(cls, v: Any) -> Any:
        """Registry codes or their display forms ('Active, not recruiting'); unknown values are dropped."""
        if isinstance(v, str):
            whole = re.sub(r"[^A-Z]+", "_", v.strip().upper()).strip("_")
            v = [v] if whole in STATUSES else [x for x in v.split(",") if x.strip()]
        out, unknown = [], []
        for s in v or []:
            s_norm = re.sub(r"[^A-Z]+", "_", str(s).strip().upper()).strip("_")
            if s_norm not in STATUSES:
                unknown.append(str(s))
            elif s_norm not in out:
                out.append(s_norm)
        if unknown and not out:
            # Nothing usable: tell the caller (an agent fixes its arguments from this message).
            raise ValueError(f"unknown overall status '{unknown[0]}'; expected one of {', '.join(STATUSES)}")
        return out

    @model_validator(mode="after")
    def _require_search_field(self) -> "ClinicalTrialsSearchInput":
        if not (self.term or self.intervention or self.condition or self.conditions or self.nct_ids):
            raise ValueError("one of term, intervention, condition or nct_ids is required")
        return self

    def search_conditions(self) -> List[Optional[str]]:
        """The conditions to search, one search each (at most MAX_CONDITIONS); [None] when none was given."""
        out: List[str] = []
        for c in [self.condition, *self.conditions]:
            if c and c.lower() not in {o.lower() for o in out}:
                out.append(c)
        return list(out[:MAX_CONDITIONS]) or [None]


# -----------------------------------------------------------------------------
# Display helpers shared by the report, the renderer and the step text
# -----------------------------------------------------------------------------

def _cell(text: Any) -> str:
    return " ".join(str(text).replace("|", "/").split())


def phase_label(phases: List[str]) -> str:
    """['PHASE2', 'PHASE3'] -> 'Phase 2/3'; ['EARLY_PHASE1'] -> 'Early Phase 1'; [] or ['NA'] -> 'N/A'."""
    nums = [p.replace("PHASE", "") for p in phases if re.fullmatch(r"PHASE\d", p or "")]
    if "EARLY_PHASE1" in phases:
        return "Early Phase 1" + (f"/{'/'.join(nums)}" if nums else "")
    if nums:
        return "Phase " + "/".join(nums)
    return "N/A"


_STATUS_LABELS = {"ACTIVE_NOT_RECRUITING": "Active, not recruiting", "UNKNOWN": "Unknown status"}


def status_label(status: Optional[str]) -> str:
    if not status:
        return "—"
    return _STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


def enrollment_label(count: Optional[int], kind: Optional[str]) -> str:
    if count is None:
        return "—"
    suffix = {"ACTUAL": " actual", "ESTIMATED": " est."}.get(kind or "", "")
    return f"{count}{suffix}"


def value_label(m: TrialArmValue, unit: Optional[str]) -> str:
    """'76.7 Percentage of participants (71.29 to 81.53)', every number exactly as posted."""
    value = m.value if m.value is not None else "—"
    text = f"{value} {unit}".strip() if unit and value not in ("NA", "—") else value
    if m.lower is not None or m.upper is not None:
        text += f" ({m.lower or 'NA'} to {m.upper or 'NA'})"
    elif m.spread is not None:
        text += f" (± {m.spread})"
    return text


def describe_strategy(query: TrialSearchQuery) -> str:
    if query.strategy == "nct_ids":
        return "NCT IDs: " + ", ".join(query.nct_ids)
    parts = []
    params = query.api_params
    multi = len(query.conditions) > 1
    for key, label in (("query.intr", "intervention"), ("query.term", "term"), ("query.cond", "condition")):
        if key == "query.cond" and multi:
            parts.append("conditions: " + "; ".join(query.conditions) + " (one search each)")
        elif params.get(key):
            parts.append(f"{label}: {params[key]}")
    if query.phases:
        parts.append(f"phase: {phase_label(query.phases)}")
    if query.statuses:
        parts.append("status: " + ", ".join(status_label(s) for s in query.statuses))
    return " · ".join(parts) or query.strategy


# -----------------------------------------------------------------------------
# Parsers
# -----------------------------------------------------------------------------

def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _date(module: Dict[str, Any], key: str) -> Optional[str]:
    return _s((module.get(key) or {}).get("date"))


def _study_nct(study: Any) -> str:
    if not isinstance(study, dict):
        return ""
    ident = (study.get("protocolSection") or {}).get("identificationModule") or {}
    return str(ident.get("nctId") or "").upper()


def parse_studies(payload: Dict[str, Any]) -> List[TrialRecord]:
    """Trial records of a search response; a study without a valid NCT ID is skipped, not fatal."""
    return [parse_trial(s) for s in payload.get("studies") or [] if re.fullmatch(r"NCT\d{8}", _study_nct(s))]


def parse_trial(study: Dict[str, Any]) -> TrialRecord:
    ps = study.get("protocolSection") or {}
    ident = ps.get("identificationModule") or {}
    status = ps.get("statusModule") or {}
    sponsor = (ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
    design = ps.get("designModule") or {}
    enrollment = design.get("enrollmentInfo") or {}
    nct_id = str(ident.get("nctId") or "").upper()
    count = enrollment.get("count")
    interventions = [
        {"type": _s(iv.get("type")), "name": _s(iv.get("name"))}
        for iv in (ps.get("armsInterventionsModule") or {}).get("interventions") or []
        if isinstance(iv, dict) and iv.get("name")
    ]
    primary = [
        {"measure": _s(po.get("measure")), "time_frame": _s(po.get("timeFrame"))}
        for po in (ps.get("outcomesModule") or {}).get("primaryOutcomes") or []
        if isinstance(po, dict) and po.get("measure")
    ]
    return TrialRecord(
        nct_id=nct_id,
        url=STUDY_URL.format(nct_id=nct_id),
        brief_title=_s(ident.get("briefTitle")),
        official_title=_s(ident.get("officialTitle")),
        acronym=_s(ident.get("acronym")),
        overall_status=_s(status.get("overallStatus")),
        why_stopped=_s(status.get("whyStopped")),
        phases=[str(p) for p in design.get("phases") or []],
        study_type=_s(design.get("studyType")),
        enrollment_count=count if isinstance(count, int) else None,
        enrollment_type=_s(enrollment.get("type")),
        lead_sponsor=_s(sponsor.get("name")),
        lead_sponsor_class=_s(sponsor.get("class")),
        conditions=[str(c) for c in (ps.get("conditionsModule") or {}).get("conditions") or []][:10],
        interventions=interventions[:12],
        primary_outcomes=primary[:6],
        start_date=_date(status, "startDateStruct"),
        primary_completion_date=_date(status, "primaryCompletionDateStruct"),
        last_update_posted=_date(status, "lastUpdatePostDateStruct"),
        has_results=bool(study.get("hasResults")),
        results_first_posted=_date(status, "resultsFirstPostDateStruct"),
    )


_CORE_EFFICACY = re.compile(
    r"\b(?:overall\s+survival|(?:progression|disease|event|recurrence|relapse|metastasis)[\s\-]*free|"
    r"objective\s+response|overall\s+response|complete\s+(?:response|remission)|pathologic(?:al)?\s+complete|"
    r"response\s+rate)",
    re.IGNORECASE,
)
_CORE_ABBREV = re.compile(r"\b(?:ORR|PFS|OS|DFS|EFS|RFS|pCR|CR)\b")
_OTHER_EFFICACY = re.compile(
    r"\b(?:survival|response|remission|disease\s+control|clinical\s+benefit|progression|relapse|recurrence|"
    r"time\s+to\s+(?:progression|treatment\s+failure|death|recurrence|relapse|metastas\w*)|tumou?r\s+shrinkage)",
    re.IGNORECASE,
)


def _measure_tier(measure: Dict[str, Any]) -> Optional[int]:
    """0 primary, 1 core efficacy (survival, response rate), 2 other efficacy-like; None is not kept."""
    if str(measure.get("type") or "").upper() == "PRIMARY":
        return 0
    title = str(measure.get("title") or "")
    if _CORE_EFFICACY.search(title) or _CORE_ABBREV.search(title):
        return 1
    if _OTHER_EFFICACY.search(title):
        return 2
    return None


def _counts(denoms: Any) -> Dict[str, str]:
    first = (denoms or [{}])[0] if isinstance(denoms, list) and denoms else {}
    return {str(c.get("groupId")): str(c.get("value")) for c in first.get("counts") or [] if c.get("groupId")}


_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~])")


def _md(v: Any) -> Optional[str]:
    """A registry free-text comment with its markdown backslash escapes removed ('HR \\< 1' -> 'HR < 1')."""
    if v is None:
        return None
    text = _MD_ESCAPE_RE.sub(r"\1", str(v)).strip()
    return text or None


def _measurement_count(measure: Dict[str, Any]) -> int:
    return sum(
        len(cat.get("measurements") or [])
        for cls in measure.get("classes") or []
        for cat in cls.get("categories") or []
    )


def parse_measure(measure: Dict[str, Any]) -> TrialOutcomeResult:
    titles = {str(g.get("id")): _s(g.get("title")) for g in measure.get("groups") or [] if g.get("id")}
    measure_n = _counts(measure.get("denoms"))
    values: List[TrialArmValue] = []
    for cls in measure.get("classes") or []:
        class_n = _counts(cls.get("denoms")) or measure_n
        for cat in cls.get("categories") or []:
            for m in cat.get("measurements") or []:
                if len(values) >= MAX_MEASUREMENTS:
                    break
                gid = str(m.get("groupId") or "")
                values.append(
                    TrialArmValue(
                        group_id=gid,
                        group_title=titles.get(gid),
                        n=class_n.get(gid),
                        value=_s(m.get("value")),
                        lower=_s(m.get("lowerLimit")),
                        upper=_s(m.get("upperLimit")),
                        spread=_s(m.get("spread")),
                        class_title=_s(cls.get("title")),
                        category_title=_s(cat.get("title")),
                        comment=_s(m.get("comment")),
                    )
                )
    all_analyses = [a for a in measure.get("analyses") or [] if isinstance(a, dict)]
    analyses: List[TrialAnalysis] = []
    for a in all_analyses[:MAX_ANALYSES]:
        gids = [str(g) for g in a.get("groupIds") or []]
        analyses.append(
            TrialAnalysis(
                group_ids=gids,
                group_titles=[titles.get(g) for g in gids],
                param_type=_s(a.get("paramType")),
                param_value=_s(a.get("paramValue")),
                ci_pct=_s(a.get("ciPctValue")),
                ci_num_sides=_s(a.get("ciNumSides")),
                ci_lower=_s(a.get("ciLowerLimit")),
                ci_upper=_s(a.get("ciUpperLimit")),
                p_value=_s(a.get("pValue")),
                p_value_comment=_md(a.get("pValueComment")),
                method=_s(a.get("statisticalMethod")),
                non_inferiority_type=_s(a.get("nonInferiorityType")),
                non_inferiority_comment=_md(a.get("nonInferiorityComment")),
                estimate_comment=_md(a.get("estimateComment")),
                group_description=_md(a.get("groupDescription")),
                statistical_comment=_md(a.get("statisticalComment")),
            )
        )
    return TrialOutcomeResult(
        type=str(measure.get("type") or "OTHER"),
        title=str(measure.get("title") or ""),
        param_type=_s(measure.get("paramType")),
        dispersion_type=_s(measure.get("dispersionType")),
        unit=_s(measure.get("unitOfMeasure")),
        time_frame=_s(measure.get("timeFrame")),
        measurements=values,
        analyses=analyses,
        measurements_total=_measurement_count(measure),
        analyses_total=len(all_analyses),
    )


def _ranked_posted_measures(study: Dict[str, Any]) -> List[Dict[str, Any]]:
    measures = (((study.get("resultsSection") or {}).get("outcomeMeasuresModule") or {}).get("outcomeMeasures")) or []
    ranked: List[Tuple[int, int, Dict[str, Any]]] = []
    for idx, m in enumerate(measures):
        if not isinstance(m, dict) or m.get("reportingStatus") != "POSTED":
            continue
        tier = _measure_tier(m)
        if tier is not None:
            ranked.append((tier, idx, m))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [m for _, _, m in ranked]


def parse_posted_results(study: Dict[str, Any]) -> List[TrialOutcomeResult]:
    """POSTED primary and efficacy-like outcome measures, primary first, at most MAX_MEASURES."""
    return [parse_measure(m) for m in _ranked_posted_measures(study)[:MAX_MEASURES]]


def count_posted_results(study: Dict[str, Any]) -> int:
    """POSTED primary and efficacy-like outcome measures before the MAX_MEASURES cap (DAT-011)."""
    return len(_ranked_posted_measures(study))


def parse_result_pmids(study: Dict[str, Any]) -> List[str]:
    """PMIDs of references the registry marks as RESULT publications (BACKGROUND/DERIVED are not results)."""
    refs = ((study.get("protocolSection") or {}).get("referencesModule") or {}).get("references") or []
    out: List[str] = []
    for r in refs:
        pmid = str(r.get("pmid") or "").strip()
        if r.get("type") == "RESULT" and pmid.isdigit() and pmid not in out:
            out.append(pmid)
    return out


# -----------------------------------------------------------------------------
# Query strategies
# -----------------------------------------------------------------------------

def build_strategies(
    term: Optional[str] = None,
    intervention: Optional[str] = None,
    condition: Optional[str] = None,
    nct_ids: Optional[List[str]] = None,
) -> List[Tuple[str, Dict[str, str]]]:
    """Ordered (name, query params) strategies, most specific first, at most MAX_STRATEGIES."""
    if nct_ids:
        return [("nct_ids", {"filter.ids": ",".join(nct_ids)})]
    out: List[Tuple[str, Dict[str, str]]] = []
    cond = {"query.cond": condition} if condition else {}
    if intervention:
        out.append(("intervention+condition" if condition else "intervention", {"query.intr": intervention, **cond}))
    if term:
        if condition:
            out.append(("term+condition", {"query.term": term, **cond}))
        out.append(("term", {"query.term": term}))
    elif intervention and condition:
        out.append(("intervention", {"query.intr": intervention}))
    if not out and condition:
        out.append(("condition", dict(cond)))
    return out[:MAX_STRATEGIES]


def _filter_params(phases: List[str], statuses: List[str]) -> Dict[str, str]:
    params: Dict[str, str] = {}
    if phases:
        params["filter.advanced"] = f"AREA[Phase]{phases[0]}" if len(phases) == 1 else f"AREA[Phase]({' OR '.join(phases)})"
    if statuses:
        params["filter.overallStatus"] = ",".join(statuses)
    return params


# -----------------------------------------------------------------------------
# Clinical setting terms and local ranking
# -----------------------------------------------------------------------------

# canonical term -> (pattern for the term in a question or a registry record, query.term alternatives,
# whether the term filters the registry search; the others only steer ranking).
SETTING_TERMS: Dict[str, Tuple[str, Tuple[str, ...], bool]] = {
    "neoadjuvant": (r"\bneo[\s\-]?adjuvant\b", ("neoadjuvant", '"neo-adjuvant"'), True),
    "adjuvant": (r"(?<![\w\-])(?<!neo\s)adjuvant\b", ("adjuvant",), True),
    "perioperative": (r"\bperi[\s\-]?operative\b", ("perioperative",), True),
    "maintenance": (r"\bmaintenance\b", ("maintenance",), True),
    "first-line": (
        r"\b(?:first|1st|front)[\s\-]line\b|\b1L\b|\bpreviously\s+untreated\b|\btreatment[\s\-]na[iï]ve\b",
        ('"first-line"', '"previously untreated"'), True,
    ),
    "second-line": (r"\b(?:second|2nd)[\s\-]line\b|\b2L\b|\bpreviously\s+treated\b", ('"second-line"', '"previously treated"'), True),
    "resected": (r"\bresect(?:ed|ion)\b|\bcompletely\s+(?:surgically\s+)?removed\b", ("resected", "resection"), True),
    "unresectable": (r"\bunresectable\b|\binoperable\b", ("unresectable",), True),
    "metastatic": (r"\bmetasta(?:tic|sis|ses)\b", ("metastatic",), True),
    "castration-resistant": (r"\bcastrat\w*[\s\-]+resistant\b|\b(?:m|nm)?CRPC\b", ('"castration-resistant"', "CRPC"), True),
    "castration-sensitive": (
        r"\b(?:castrat\w*|hormone)[\s\-]+sensitive\b|\bm?(?:CSPC|HSPC)\b", ('"castration-sensitive"', '"hormone-sensitive"'), True,
    ),
    "randomized": (r"\brandomi[sz]ed\b", ("randomized",), False),
}
_STAGE_TERM_RE = re.compile(r"^stage\s+(IV|I{1,3})([A-D]?)$", re.IGNORECASE)

_PHASE_POINTS = {"PHASE3": 50, "PHASE2/3": 44, "PHASE4": 38, "PHASE2": 30, "PHASE1/2": 20, "PHASE1": 10, "EARLY_PHASE1": 6}


def _setting_key(term: str) -> str:
    """The ranking feature a term belongs to: all stage terms are one feature ('stage IIB/IIC')."""
    return "stage" if _STAGE_TERM_RE.match(term) else term.lower()


def setting_pattern(term: str) -> re.Pattern:
    """A record-text pattern for a setting term; 'stage IIB' matches 'Stage II', 'Stage IIB/C', not 'Stage III'."""
    known = SETTING_TERMS.get(term.lower())
    if known:
        return re.compile(known[0], re.IGNORECASE)
    m = _STAGE_TERM_RE.match(term)
    if m:
        return re.compile(rf"\bstage\s*{m.group(1)}(?:[A-D]\b|\b)", re.IGNORECASE)
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def setting_query(terms: List[str]) -> Optional[str]:
    """query.term expression requiring any one filtering setting term ('adjuvant OR resected OR resection')."""
    alts: List[str] = []
    for term in terms:
        known = SETTING_TERMS.get(term.lower())
        if known:
            if known[2]:
                alts.extend(a for a in known[1] if a not in alts)
        elif not _STAGE_TERM_RE.match(term):
            alt = f'"{term}"' if re.search(r"[\s\-/]", term) else term
            if alt not in alts:
                alts.append(alt)
    return " OR ".join(alts) or None


def _with_setting(params: Dict[str, str], setting_q: str) -> Dict[str, str]:
    out = dict(params)
    out["query.term"] = f"({params['query.term']}) AND ({setting_q})" if params.get("query.term") else setting_q
    return out


def _phase_points(phases: List[str]) -> int:
    ps = set(phases or [])
    if "PHASE3" in ps:
        return _PHASE_POINTS["PHASE2/3" if "PHASE2" in ps else "PHASE3"]
    if "PHASE4" in ps:
        return _PHASE_POINTS["PHASE4"]
    if "PHASE2" in ps:
        return _PHASE_POINTS["PHASE1/2" if ("PHASE1" in ps or "EARLY_PHASE1" in ps) else "PHASE2"]
    if "PHASE1" in ps:
        return _PHASE_POINTS["PHASE1"]
    if "EARLY_PHASE1" in ps:
        return _PHASE_POINTS["EARLY_PHASE1"]
    return 0


def _record_texts(study: Dict[str, Any], record: TrialRecord) -> Tuple[str, str]:
    """(title, acronym, conditions and keywords; brief summary) of a search record, for setting-term matching."""
    ps = study.get("protocolSection") or {} if isinstance(study, dict) else {}
    keywords = (ps.get("conditionsModule") or {}).get("keywords") or []
    head = " | ".join(str(x) for x in [record.brief_title or "", record.acronym or "", *record.conditions, *keywords] if x)
    summary = str((ps.get("descriptionModule") or {}).get("briefSummary") or "")
    return head, summary


def _allocation(study: Dict[str, Any]) -> str:
    ps = study.get("protocolSection") or {} if isinstance(study, dict) else {}
    return str((((ps.get("designModule") or {}).get("designInfo")) or {}).get("allocation") or "")


def rank_score(
    record: TrialRecord, study: Dict[str, Any], setting_terms: List[str], phase_requested: bool = False,
    focus_terms: Sequence[str] = (),
) -> float:
    """
    Local ranking score: phase 3 > phase 2/3 > phase 4 > phase 2 > phase 1/2 > phase 1 > N/A (ignored when the user
    asked for a phase), interventional over observational, setting-term match (title/conditions/keywords count
    twice the brief summary), larger enrollment, posted results. Withdrawn trials sink.
    """
    score = 0.0 if phase_requested else float(_phase_points(record.phases))
    if (record.study_type or "").upper() == "INTERVENTIONAL":
        score += 15
    head, summary = _record_texts(study, record)
    features: Dict[str, float] = {}
    for term in setting_terms:
        key = _setting_key(term)
        if key == "randomized" and _allocation(study).upper() == "RANDOMIZED":
            features[key] = 12.0
            continue
        pat = setting_pattern(term)
        pts = 12.0 if pat.search(head) else 6.0 if pat.search(summary) else 0.0
        features[key] = max(features.get(key, 0.0), pts)
    score += sum(sorted(features.values(), reverse=True)[:4])
    # The searched term ("KRAS" for a KRAS-mutant question) in the title, conditions, keywords or interventions marks a
    # trial built around it; ClinicalTrials.gov also matches the term in eligibility text, which generic trials carry.
    # +25 lets a KRAS-targeted phase 2 (30+25) edge out a generic phase 3 (50) that only mentions KRAS in passing.
    if focus_terms:
        where = head + " | " + " | ".join(str(i.get("name") or "") for i in (record.interventions or []))
        if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", where, re.I) for t in focus_terms if t):
            score += 25
    if record.enrollment_count:
        score += min(10.0, 3.0 * math.log10(record.enrollment_count + 1))
    if record.has_results:
        score += 5
    if (record.overall_status or "").upper() == "WITHDRAWN":
        score -= 15
    return score


def _study_pairs(payload: Dict[str, Any]) -> List[Tuple[Dict[str, Any], TrialRecord]]:
    return [(s, parse_trial(s)) for s in payload.get("studies") or [] if re.fullmatch(r"NCT\d{8}", _study_nct(s))]


def trial_name_pattern(name: str) -> Optional[re.Pattern]:
    """'SOLO-1' -> matches SOLO-1, SOLO 1, SOLO1 (not SOLO-12); 'CheckMate 76K' -> CheckMate-76K, CHECKMATE 76 K."""
    chunks = re.findall(r"[A-Za-z]+|\d+", name or "")
    if not chunks:
        return None
    body = r"[\s\-_/]?".join(re.escape(c) for c in chunks)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def named_trial(record: TrialRecord, names: Sequence[str]) -> Optional[str]:
    """The question-named trial this record is (its acronym, or its brief or official title, names it), or None."""
    for name in names:
        pat = trial_name_pattern(name)
        if pat is None:
            continue
        if record.acronym and pat.search(record.acronym):
            return name
        # A title "names" a trial only with a number-bearing name: a bare word ("PRIMA") also occurs in prose.
        if re.search(r"\d", name) and any(pat.search(t) for t in (record.brief_title, record.official_title) if t):
            return name
    return None


def named_first(trials: List[TrialRecord], names: Sequence[str]) -> List[TrialRecord]:
    """Question-named trials first (in the order the question names them), the rest in their existing order."""
    if not names:
        return trials
    hits = [(names.index(n), i) for i, t in enumerate(trials) if (n := named_trial(t, names)) is not None]
    first = [trials[i] for _, i in sorted(hits)]
    picked = {i for _, i in hits}
    return first + [t for i, t in enumerate(trials) if i not in picked]


# -----------------------------------------------------------------------------
# HTTP client
# -----------------------------------------------------------------------------

class CTGovError(Exception):
    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class _Budget:
    def __init__(self, limit: int, deadline_s: Optional[float] = None) -> None:
        self.limit = limit
        self.calls = 0
        self.cache_hits = 0
        self.deadline = time.monotonic() + deadline_s if deadline_s else None

    @property
    def remaining(self) -> int:
        return self.limit - self.calls

    @property
    def seconds_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    @property
    def exhausted(self) -> bool:
        return self.remaining < 1 or self.seconds_left <= 0.5


@dataclass
class _ConditionOutcome:
    """One condition's search: its trials (ranked, or the legacy relevance+companion list) and bookkeeping."""

    condition: Optional[str]
    trials: List[TrialRecord]
    total_count: int
    chosen_name: str
    chosen_params: Dict[str, str]
    tried: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    companion_ids: Set[str] = field(default_factory=set)


class ClinicalTrialsGovClient:
    """httpx client for /api/v2/studies; pass transport=httpx.MockTransport(...) in tests.

    fetch_size > max_trials turns on ranked mode (a larger page plus companion searches, re-ranked locally);
    search_trials() builds its default client with FETCH_SIZE. The default None keeps the plain relevance list."""

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        *,
        cache: Optional[TTLCache] = _CACHE,
        max_calls: int = MAX_HTTP_CALLS,
        retry_backoff_s: float = 0.5,
        deadline_s: Optional[float] = SEARCH_DEADLINE_S,
        fetch_size: Optional[int] = None,
        probe_version: Optional[bool] = None,
    ) -> None:
        self.transport = transport
        self.cache = cache
        self.max_calls = max_calls
        self.retry_backoff_s = retry_backoff_s
        self.deadline_s = deadline_s
        self.fetch_size = fetch_size
        # The data-version probe is on for the real network and off for injected (test) transports unless asked for.
        self.probe_version = (transport is None) if probe_version is None else bool(probe_version)

    async def data_version(self) -> Optional[str]:
        """The registry's data version (cached VERSION_TTL_S); None on any failure, never raises."""
        cached = _VERSION_CACHE.get(VERSION_URL)
        if cached is not None and time.monotonic() - cached[0] < VERSION_TTL_S:
            return cached[1]
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=httpx.Timeout(VERSION_TIMEOUT_S), headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(VERSION_URL)
            if resp.status_code != 200:
                return None
            label = version_label(resp.json())
        except Exception as exc:  # noqa: BLE001 - provenance is best effort
            logger.info("ClinicalTrials.gov version probe failed: %s", type(exc).__name__)
            return None
        if label:
            _VERSION_CACHE[VERSION_URL] = (time.monotonic(), label)
        return label

    async def _get(self, client: httpx.AsyncClient, params: Dict[str, str], budget: _Budget) -> Dict[str, Any]:
        key = tuple(sorted(params.items()))
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                budget.cache_hits += 1
                return hit
        last_error = ""
        for attempt in range(2):
            if attempt > 0 and budget.exhausted:
                break
            if budget.seconds_left <= 0:
                raise CTGovError(ErrorType.TIMEOUT, f"ClinicalTrials.gov search exceeded its {self.deadline_s:g}s deadline")
            budget.calls += 1
            timeout = min(TIMEOUT.read or 12.0, max(1.0, budget.seconds_left))
            try:
                async with get_limiter("clinicaltrials", 0.0):
                    resp = await client.get(
                        BASE_URL, params=params, timeout=httpx.Timeout(timeout, connect=min(8.0, timeout))
                    )
            except httpx.TimeoutException as exc:
                raise CTGovError(ErrorType.TIMEOUT, f"ClinicalTrials.gov did not respond in time ({type(exc).__name__})") from exc
            except httpx.TransportError as exc:
                last_error = f"connection error: {type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise CTGovError(ErrorType.UPSTREAM_API_ERROR, "ClinicalTrials.gov returned a non-JSON body") from exc
                    if self.cache is not None:
                        self.cache.set(key, data)
                    return data
                body = resp.text.strip()[:300]
                if resp.status_code == 400:
                    raise CTGovError(ErrorType.VALIDATION_ERROR, f"ClinicalTrials.gov rejected the query (HTTP 400): {body}")
                if resp.status_code == 404:
                    raise CTGovError(ErrorType.NOT_FOUND, f"ClinicalTrials.gov: {body or 'not found'} (HTTP 404)")
                if resp.status_code == 429:
                    last_error = "HTTP 429 rate limited"
                    wait = retry_after_seconds(resp, default=1.0, cap=MAX_RETRY_AFTER_S)
                    if attempt == 0 and wait < budget.seconds_left - 1.0:
                        await asyncio.sleep(wait)
                        continue
                    raise CTGovError(ErrorType.UPSTREAM_API_ERROR, "ClinicalTrials.gov rate limit reached (HTTP 429)")
                if resp.status_code < 500:
                    raise CTGovError(ErrorType.UPSTREAM_API_ERROR, f"ClinicalTrials.gov HTTP {resp.status_code}: {body}")
                last_error = f"HTTP {resp.status_code}: {body}"
            if attempt == 0 and self.retry_backoff_s:
                await asyncio.sleep(min(self.retry_backoff_s, max(0.0, budget.seconds_left - 1.0)))
        raise CTGovError(ErrorType.UPSTREAM_API_ERROR, f"ClinicalTrials.gov unavailable after one retry ({last_error})")

    def _collect_companions(
        self, comps: List[Tuple[str, Dict[str, str]]], outs: List[Any], limitations: List[str]
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Companion payloads that arrived; a failed companion is a limitation, never fatal."""
        kept: List[Tuple[str, Dict[str, Any]]] = []
        for (kind, _params), out in zip(comps, outs):
            if isinstance(out, BaseException):
                logger.warning("ClinicalTrials.gov %s companion search failed: %s", kind, out)
                limitations.append(
                    "The search for trials with posted results failed; only relevance-ranked trials are listed."
                    if kind == "results"
                    else "The companion search for phase 3 trials failed; trials are ranked from the relevance search only."
                )
            else:
                kept.append((kind, out))
        return kept

    async def _search_condition(
        self,
        client: httpx.AsyncClient,
        inp: ClinicalTrialsSearchInput,
        condition: Optional[str],
        budget: _Budget,
        ranked: bool,
    ) -> _ConditionOutcome:
        """One condition's strategy chain (setting-filtered first when setting terms were given) and companions."""
        strategies = build_strategies(inp.term, inp.intervention, condition, inp.nct_ids)
        setting_q = None if inp.nct_ids else setting_query(list(inp.setting_terms))
        if setting_q:
            name0, params0 = strategies[0]
            strategies = [(f"{name0}+setting", _with_setting(params0, setting_q)), *strategies]
        filters = {} if inp.nct_ids else _filter_params(list(inp.phases), list(inp.statuses))
        if inp.nct_ids:
            page_size = str(len(inp.nct_ids))
        elif ranked:
            page_size = str(max(inp.max_trials, self.fetch_size or 0))
        else:
            page_size = str(inp.max_trials)
        if ranked:
            companion_size = min(RANKED_COMPANION_SIZE, int(page_size)) if inp.max_results_trials > 0 else 0
        else:
            # Results-bearing trials from the companion search take at most half of the list, so the
            # relevance ranking still leads and the list never exceeds max_trials.
            companion_size = min(inp.max_results_trials, inp.max_trials // 2)
        want_results = bool(inp.include_results and companion_size > 0 and not inp.nct_ids)
        # Relevance buries pivotal trials under many small ones, so ranked mode also pulls the phase 3 trials
        # matching the same condition and setting (only when the user did not ask for a phase).
        want_phase3 = bool(ranked and not inp.phases and not inp.nct_ids)
        limitations: List[str] = []
        tried: List[str] = []

        def companion_params(params: Dict[str, str]) -> List[Tuple[str, Dict[str, str]]]:
            out: List[Tuple[str, Dict[str, str]]] = []
            if want_phase3:
                out.append(("phase3", {**params, "filter.advanced": "AREA[Phase]PHASE3"}))
            if want_results:
                out.append(("results", {**params, "aggFilters": "results:with", "pageSize": str(companion_size)}))
            return out

        relevance: Dict[str, Any] = {}
        companions: List[Tuple[str, Dict[str, Any]]] = []
        companions_ran = False
        chosen_name, chosen_params = strategies[0][0], {}
        for i, (name, strategy_params) in enumerate(strategies):
            params = {**strategy_params, **filters, "fields": SEARCH_FIELDS, "pageSize": page_size, "countTotal": "true"}
            chosen_name, chosen_params = name, params
            comps = companion_params(params) if i == 0 else []
            if comps:
                outs = await asyncio.gather(
                    self._get(client, params, budget), *(self._get(client, p, budget) for _, p in comps), return_exceptions=True
                )
                if isinstance(outs[0], BaseException):
                    raise outs[0]
                relevance = outs[0]
                companions = self._collect_companions(comps, list(outs[1:]), limitations)
                companions_ran = True
            else:
                if i > 0 and budget.exhausted:
                    limitations.append(f"Fallback search ({name}) was not run (request budget or deadline reached).")
                    break
                relevance = await self._get(client, params, budget)
            tried.append(name)
            if relevance.get("studies"):
                break
            companions, companions_ran = [], False

        if setting_q and relevance.get("studies") and not chosen_name.endswith("+setting"):
            limitations.append(
                f"No registered trials matched the setting terms ({', '.join(inp.setting_terms)}); the trials listed come "
                f"from the search without them."
            )

        # Ranked mode: the strategy that matched was a fallback, so its companions have not run yet.
        if ranked and relevance.get("studies") and not companions_ran and not budget.exhausted:
            comps = companion_params(chosen_params)
            if comps:
                outs = await asyncio.gather(*(self._get(client, p, budget) for _, p in comps), return_exceptions=True)
                companions = self._collect_companions(comps, list(outs), limitations)

        # Nothing matched under phase/status filters: search once without them and say so, rather than
        # presenting an empty landscape as if no trials existed.
        if filters and not relevance.get("studies") and not budget.exhausted:
            name, strategy_params = strategies[0]
            params = {**strategy_params, "fields": SEARCH_FIELDS, "pageSize": page_size, "countTotal": "true"}
            try:
                unfiltered = await self._get(client, params, budget)
            except CTGovError as exc:
                logger.warning("ClinicalTrials.gov unfiltered retry failed: %s", exc)
            else:
                if unfiltered.get("studies"):
                    shown = ", ".join([*inp.phases, *inp.statuses])
                    limitations.append(
                        f"No registered trials matched the requested filters ({shown}); the trials listed come "
                        f"from the same search without those filters and do not meet them."
                    )
                    relevance = unfiltered
                    chosen_name, chosen_params = f"{name} (filters relaxed)", params
                    tried.append(chosen_name)

        pairs = _study_pairs(relevance)
        total = relevance.get("totalCount")
        total_count = int(total) if isinstance(total, int) else len(pairs)
        companion_ids: Set[str] = set()
        if ranked:
            pool: Dict[str, Tuple[int, Dict[str, Any], TrialRecord]] = {}
            for pos, (s, rec) in enumerate(pairs):
                pool.setdefault(rec.nct_id, (pos, s, rec))
            offset = len(pairs)
            for _kind, payload in companions:
                comp_pairs = _study_pairs(payload)
                for j, (s, rec) in enumerate(comp_pairs):
                    if rec.nct_id not in pool:
                        pool[rec.nct_id] = (offset + j, s, rec)
                        companion_ids.add(rec.nct_id)
                offset += len(comp_pairs)
            terms, phase_requested = list(inp.setting_terms), bool(inp.phases)
            focus = [w for w in re.split(r"\s+(?:OR|AND)\s+|[()\"]", inp.term or "") if w.strip()]
            ordered = sorted(
                pool.values(), key=lambda x: (-rank_score(x[2], x[1], terms, phase_requested, focus), x[0])
            )
            # Trials the question names ("SOLO-1") lead the ranked list.
            trials = named_first([rec for _, _, rec in ordered], inp.trial_names)
        else:
            rel_trials = named_first([rec for _, rec in pairs], inp.trial_names)
            if not inp.nct_ids:
                rel_trials = rel_trials[: inp.max_trials]
            trials = rel_trials
            results_payload = next((p for kind, p in companions if kind == "results"), None)
            if results_payload and rel_trials:
                seen = {t.nct_id for t in rel_trials}
                extras = [t for t in parse_studies(results_payload) if t.nct_id not in seen][:companion_size]
                trials = named_first(rel_trials[: max(0, inp.max_trials - len(extras))] + extras, inp.trial_names)
                companion_ids = {t.nct_id for t in extras}
        return _ConditionOutcome(
            condition=condition,
            trials=trials,
            total_count=total_count,
            chosen_name=chosen_name,
            chosen_params=chosen_params,
            tried=tried,
            limitations=limitations,
            companion_ids=companion_ids,
        )

    async def _add_named_trials(
        self,
        client: httpx.AsyncClient,
        inp: ClinicalTrialsSearchInput,
        trials: List[TrialRecord],
        budget: _Budget,
        limitations: List[str],
    ) -> List[TrialRecord]:
        """Trials the question names but the searches did not list are looked up once by title/acronym
        (query.titles) and put first; a name still unmatched is reported, never guessed."""
        missing = [n for n in inp.trial_names if not any(named_trial(t, [n]) for t in trials)]
        found: List[TrialRecord] = []
        if missing and budget.exhausted:
            limitations.append(
                f"The trials named in the question ({', '.join(missing)}) were not among the registry matches and could "
                "not be looked up by name (request budget or deadline reached)."
            )
            return trials
        if missing:
            params = {
                "query.titles": " OR ".join(f'"{n}"' for n in missing),
                "fields": SEARCH_FIELDS, "pageSize": str(NAMED_TRIAL_PAGE), "countTotal": "true",
            }
            try:
                payload = await self._get(client, params, budget)
            except CTGovError as exc:
                logger.warning("ClinicalTrials.gov named-trial lookup failed: %s", exc)
                limitations.append(
                    f"The lookup of the trials named in the question ({', '.join(missing)}) failed "
                    f"({exc.error_type.value}); they are not listed."
                )
                return trials
            seen = {t.nct_id for t in trials}
            found = [t for t in parse_studies(payload) if t.nct_id not in seen and named_trial(t, missing)]
            if found:
                limitations.append(
                    "Added by a title/acronym lookup because the question names them: "
                    + ", ".join(f"{named_trial(t, missing)} ({t.nct_id})" for t in found) + "."
                )
        still = [n for n in missing if not any(named_trial(t, [n]) for t in found)]
        if still:
            limitations.append(
                f"No registered trial with the acronym or title {', '.join(still)} was found on ClinicalTrials.gov "
                f"by this search; do not attribute registry data to it."
            )
        if not found:
            return trials
        merged = named_first([*found, *trials], inp.trial_names)
        return merged[: max(inp.max_trials, len(found))]

    async def search(self, inp: ClinicalTrialsSearchInput) -> Tuple[ClinicalTrialsReport, Dict[str, Any]]:
        """The search (see _search) plus the registry data version, probed concurrently and best effort."""
        task = asyncio.ensure_future(self.data_version()) if self.probe_version else None
        try:
            report, meta = await self._search(inp)
        except BaseException:
            if task is not None:
                task.cancel()
            raise
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=VERSION_WAIT_S)
            if task in done and not task.cancelled() and task.exception() is None and task.result():
                report.source_release = task.result()
                meta["source_release"] = report.source_release
            else:
                task.cancel()
        return report, meta

    async def _search(self, inp: ClinicalTrialsSearchInput) -> Tuple[ClinicalTrialsReport, Dict[str, Any]]:
        ranked = bool(not inp.nct_ids and self.fetch_size and self.fetch_size > inp.max_trials)
        conditions: List[Optional[str]] = [None] if inp.nct_ids else inp.search_conditions()
        multi = len(conditions) > 1
        per_condition = self.max_calls + (RANKED_EXTRA_CALLS if ranked else 0)
        budget = _Budget(per_condition * len(conditions), self.deadline_s)
        limitations: List[str] = []

        async with httpx.AsyncClient(
            transport=self.transport, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        ) as client:
            if not multi:
                outcomes = [await self._search_condition(client, inp, conditions[0], budget, ranked)]
            else:
                outs = await asyncio.gather(
                    *(self._search_condition(client, inp, c, budget, ranked) for c in conditions), return_exceptions=True
                )
                outcomes = []
                failures: List[Tuple[Optional[str], CTGovError]] = []
                for cond, out in zip(conditions, outs):
                    if isinstance(out, CTGovError):
                        failures.append((cond, out))
                    elif isinstance(out, BaseException):
                        raise out
                    else:
                        outcomes.append(out)
                if not outcomes:
                    raise failures[0][1]
                for cond, exc in failures:
                    limitations.append(
                        f"The ClinicalTrials.gov search for {cond} failed ({exc.error_type.value}: {str(exc)[:160]}); "
                        f"no trials are listed for it."
                    )
            for o in outcomes:
                prefix = f"{o.condition}: " if multi else ""
                limitations.extend(prefix + n for n in o.limitations)

            if multi:
                # Round-robin across conditions keeps every condition represented in the shown list.
                trials: List[TrialRecord] = []
                seen_ids: Set[str] = set()
                # Question-named trials from any condition's search come first.
                for o in outcomes:
                    for t in o.trials:
                        if inp.trial_names and t.nct_id not in seen_ids and named_trial(t, inp.trial_names) and len(trials) < inp.max_trials:
                            seen_ids.add(t.nct_id)
                            trials.append(t)
                trials = named_first(trials, inp.trial_names)
                depth = 0
                while len(trials) < inp.max_trials and any(depth < len(o.trials) for o in outcomes):
                    for o in outcomes:
                        if depth < len(o.trials) and o.trials[depth].nct_id not in seen_ids and len(trials) < inp.max_trials:
                            seen_ids.add(o.trials[depth].nct_id)
                            trials.append(o.trials[depth])
                    depth += 1
            else:
                trials = outcomes[0].trials[: inp.max_trials] if ranked else outcomes[0].trials
            total_count = sum(o.total_count for o in outcomes)
            companion_ids = set().union(*(o.companion_ids for o in outcomes))

            if inp.nct_ids:
                by_id = {t.nct_id: t for t in trials}
                trials = [by_id[n] for n in inp.nct_ids if n in by_id]
                total_count = len(trials)
                missing = [n for n in inp.nct_ids if n not in by_id]
                if missing:
                    limitations.append(f"Not found in the ClinicalTrials.gov registry: {', '.join(missing)}.")

            if inp.trial_names and not inp.nct_ids:
                trials = await self._add_named_trials(client, inp, trials, budget, limitations)

            results_fetched_for: List[str] = []
            with_results = [t for t in trials if t.has_results]
            # Posted results of the trials the question names are fetched first, then in list order.
            prioritised = named_first(with_results, inp.trial_names)
            fetch_ids = [t.nct_id for t in prioritised][: inp.max_results_trials] if inp.include_results else []
            if fetch_ids and not budget.exhausted:
                res_params = {"filter.ids": ",".join(fetch_ids), "fields": RESULT_FIELDS, "pageSize": str(len(fetch_ids))}
                try:
                    res_data = await self._get(client, res_params, budget)
                except CTGovError as exc:
                    limitations.append(f"Posted results could not be retrieved ({exc.error_type.value}: {str(exc)[:160]}).")
                else:
                    # filter.ids does not preserve order, so results are mapped back by NCT ID.
                    studies = {}
                    for s in res_data.get("studies") or []:
                        nct = _study_nct(s)
                        if nct:
                            studies[nct] = s
                    for t in trials:
                        study = studies.get(t.nct_id)
                        if t.nct_id in fetch_ids and study is not None:
                            t.posted_results = parse_posted_results(study)
                            t.posted_results_total = count_posted_results(study)
                            t.result_pmids = parse_result_pmids(study)
                            results_fetched_for.append(t.nct_id)
            elif fetch_ids:
                limitations.append("Posted results were not retrieved (request budget or deadline reached).")

        first = outcomes[0]
        searched = [c for c in conditions if c]
        query = TrialSearchQuery(
            term=inp.term,
            intervention=inp.intervention,
            condition=inp.condition or (searched[0] if searched else None),
            conditions=searched if multi else [],
            setting_terms=list(inp.setting_terms),
            nct_ids=list(inp.nct_ids),
            phases=list(inp.phases),
            statuses=list(inp.statuses),
            trial_names=list(inp.trial_names),
            strategy=first.chosen_name,
            api_params=first.chosen_params,
        )
        tried = [f"{o.condition}: {t}" if multi else t for o in outcomes for t in o.tried]
        if not trials:
            if len(tried) > 1:
                limitations.append(f"No registered trials matched any search strategy ({', '.join(tried)}).")
            elif not inp.nct_ids:
                limitations.append(f"No registered trials matched {describe_strategy(query)}.")
        else:
            if not inp.nct_ids and total_count > len(trials):
                scope = " across the per-condition searches (a trial can match more than one)" if multi else ""
                if ranked:
                    limitations.append(
                        f"Showing {len(trials)} of {total_count} registered trials matching {describe_strategy(query)}{scope}, "
                        f"re-ranked locally (phase, design, setting match, enrollment, posted results) from the first "
                        f"{self.fetch_size} per search plus phase 3 and results companions; the list is not exhaustive."
                    )
                else:
                    limitations.append(
                        f"Showing {len(trials)} of {total_count} registered trials matching {describe_strategy(query)}{scope}; "
                        f"the list is not exhaustive."
                    )
            for o in outcomes:
                if len(o.tried) > 1:
                    prefix = f"{o.condition}: " if multi else ""
                    limitations.append(f"{prefix}Earlier strategies matched nothing ({', '.join(o.tried[:-1])}); used {o.chosen_name}.")
            added = sum(1 for t in trials if t.nct_id in companion_ids)
            if added and ranked:
                limitations.append(
                    f"{added} listed trial(s) came from companion searches (phase 3 only: filter.advanced=AREA[Phase]PHASE3; "
                    f"posted results only: aggFilters=results:with) with the same condition and setting, ranked with the rest."
                )
            elif added:
                limitations.append(
                    f"{added} trial(s) with posted results were added from a results-only search "
                    f"(aggFilters=results:with)."
                )
            not_fetched = [t.nct_id for t in with_results if t.nct_id not in results_fetched_for]
            if not_fetched and inp.include_results:
                limitations.append(f"Results are posted but were not retrieved for: {', '.join(not_fetched)}.")
            no_results = sum(1 for t in trials if not t.has_results)
            if no_results:
                limitations.append(f"{no_results} of {len(trials)} listed trials have no results posted on ClinicalTrials.gov.")
            if any(t.enrollment_type == "ESTIMATED" for t in trials):
                limitations.append("Enrollment marked 'est.' is the planned (ESTIMATED) size, not actual accrual.")
            if results_fetched_for:
                limitations.append("Posted results are sponsor-submitted registry data, not peer-reviewed publications.")

        report = ClinicalTrialsReport(
            query=query,
            total_count=total_count,
            trials=trials,
            results_fetched_for=results_fetched_for,
            limitations=limitations,
        )
        meta = {"api_calls": budget.calls, "cache_hits": budget.cache_hits, "strategies_tried": tried, "ranked": ranked}
        return report, meta


def report_to_tool_result(report: ClinicalTrialsReport, meta: Optional[Dict[str, Any]] = None) -> ToolResult:
    data = report.model_dump()
    data["citation_records"] = [
        {"type": "nct", "id": t.nct_id, "title": t.brief_title, "url": t.url} for t in report.trials
    ]
    pmids: List[str] = []
    for t in report.trials:
        pmids.extend(p for p in t.result_pmids if p not in pmids)
    return ToolResult(
        success=True,
        tool_name="lithoscribe_clinical_trials_search",
        data=data,
        canonical_ids={"nct_ids": ",".join(t.nct_id for t in report.trials), "pmids": ",".join(pmids)},
        metadata={"adapter": "DIRECT_REST", **(meta or {})},
    )


async def search_trials(*, client: Optional[ClinicalTrialsGovClient] = None, **kwargs: Any) -> ToolResult:
    """Registry executor: validate, search, and return a ToolResult (typed error on failure, never raises)."""
    try:
        inp = ClinicalTrialsSearchInput(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            success=False,
            tool_name="lithoscribe_clinical_trials_search",
            error=f"Input validation error: {exc}",
            error_type=ErrorType.VALIDATION_ERROR,
        )
    try:
        report, meta = await (client or ClinicalTrialsGovClient(fetch_size=FETCH_SIZE)).search(inp)
    except CTGovError as exc:
        return ToolResult(
            success=False,
            tool_name="lithoscribe_clinical_trials_search",
            error=str(exc),
            error_type=exc.error_type,
            metadata={"adapter": "DIRECT_REST"},
        )
    return report_to_tool_result(report, meta)
