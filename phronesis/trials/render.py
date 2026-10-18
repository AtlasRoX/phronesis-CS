"""
Evidence-block XML, activity-step text and the deterministic fallback section for ClinicalTrials.gov
results. Everything is rendered from the tool's data dict; every value is escaped.
"""

from __future__ import annotations

from html import escape
from typing import Any, List, Optional, Tuple

from .clinical_trials import (
    MAX_ANALYSES,
    MAX_MEASUREMENTS,
    ClinicalTrialsReport,
    TrialAnalysis,
    TrialOutcomeResult,
    TrialSearchQuery,
    describe_strategy,
    phase_label,
)

MAX_EVIDENCE_TRIALS = 10

# The registry lists an analysis's groups in an order that does not say which arm is the numerator (DAT-002).
GROUP_ORDER_NOTE = "registry order; does not encode the direction of the estimate"
NO_DIRECTION_NOTE = (
    "not stated in the registry: do not infer which arm the estimate favours from the group order"
)
# ClinicalTrials.gov nonInferiorityType values and what they say about the analysis.
HYPOTHESIS_LABELS = {
    "SUPERIORITY": "superiority test",
    "NON_INFERIORITY": "non-inferiority test",
    "EQUIVALENCE": "equivalence test",
    "OTHER": "other: not registered as a superiority, non-inferiority or equivalence test; do not present it as a "
    "confirmatory hypothesis test",
    "NON_INFERIORITY_OR_EQUIVALENCE": "non-inferiority or equivalence test (legacy category)",
    "SUPERIORITY_OR_OTHER": "superiority or other (legacy category: whether this was a confirmatory test is not encoded)",
    "NON_INFERIORITY_OR_EQUIVALENCE_LEGACY": "non-inferiority or equivalence test (legacy category)",
    "SUPERIORITY_OR_OTHER_LEGACY": "superiority or other (legacy category: whether this was a confirmatory test is not encoded)",
}
_CONFIRMATORY_TYPES = {"SUPERIORITY", "NON_INFERIORITY", "EQUIVALENCE", "NON_INFERIORITY_OR_EQUIVALENCE",
                       "NON_INFERIORITY_OR_EQUIVALENCE_LEGACY"}


def _a(value: Any) -> str:
    return escape(str(value), quote=True)


def _t(value: Any) -> str:
    return escape(str(value), quote=False)


def _attrs(pairs: List[Tuple[str, Any]]) -> str:
    return "".join(f' {k}="{_a(v)}"' for k, v in pairs if v is not None and v != "")


def _report(data: Any) -> Optional[ClinicalTrialsReport]:
    if not isinstance(data, dict):
        return None
    payload = {k: v for k, v in data.items() if k != "citation_records"}
    try:
        return ClinicalTrialsReport.model_validate(payload)
    except Exception:  # noqa: BLE001
        return None


def query_label(query: TrialSearchQuery) -> str:
    return describe_strategy(query)


def analysis_group_label(an: TrialAnalysis, fallback_titles: Optional[dict] = None) -> str:
    """'Osimertinib; Placebo': arm titles from the measure's group list (DAT-011), raw ids only when no title is
    known. Joined with '; ' rather than ' vs ', which would suggest a numerator/denominator order (DAT-002)."""
    fallback_titles = fallback_titles or {}
    names = []
    for i, gid in enumerate(an.group_ids):
        title = an.group_titles[i] if i < len(an.group_titles) else None
        names.append(title or fallback_titles.get(gid) or gid)
    return "; ".join(names)


def analysis_xml(an: TrialAnalysis, fallback_titles: Optional[dict] = None) -> str:
    """One <analysis> element: estimate, CI, p-value, the hypothesis type and the registry's own statement of the
    direction of the estimate. Direction may be stated only from estimate_comment / group_description."""
    ni = (an.non_inferiority_type or "").upper() or None
    hypothesis = HYPOTHESIS_LABELS.get(ni, ni.lower().replace("_", " ") if ni else None) if ni else None
    attrs = [
        ("groups", analysis_group_label(an, fallback_titles)),
        ("group_order", GROUP_ORDER_NOTE if len(an.group_ids) > 1 else None),
        ("param", an.param_type),
        ("value", an.param_value),
        ("ci_pct", an.ci_pct),
        ("ci_sides", an.ci_num_sides),
        ("ci_lower", an.ci_lower),
        ("ci_upper", an.ci_upper),
        ("p_value", an.p_value),
        ("method", an.method),
        ("hypothesis", hypothesis),
        ("confirmatory_test", None if ni is None else ("true" if ni in _CONFIRMATORY_TYPES else "false")),
        ("direction", None if (an.estimate_comment or an.group_description) else NO_DIRECTION_NOTE),
    ]
    children = [
        ("estimate_comment", an.estimate_comment),
        ("group_description", an.group_description),
        ("p_value_comment", an.p_value_comment),
        ("non_inferiority_comment", an.non_inferiority_comment),
        ("statistical_comment", an.statistical_comment),
    ]
    body = "".join(f"<{k}>{_t(v)}</{k}>" for k, v in children if v)
    return "<analysis" + _attrs(attrs) + (f">{body}</analysis>" if body else "/>")


def posted_result_counts(r: TrialOutcomeResult) -> List[Tuple[str, Any]]:
    """Attributes that disclose values and analyses dropped by the MAX_MEASUREMENTS / MAX_ANALYSES caps (DAT-011)."""
    m_total = r.measurements_total
    a_total = r.analyses_total
    m_cut = m_total is not None and m_total > len(r.measurements)
    a_cut = a_total is not None and a_total > len(r.analyses)
    return [
        ("truncated", "true" if (m_cut or a_cut) else None),
        ("values_shown", len(r.measurements) if m_cut else None),
        ("values_total", m_total if m_cut else None),
        ("analyses_shown", len(r.analyses) if a_cut else None),
        ("analyses_total", a_total if a_cut else None),
    ]


def truncation_note(r: TrialOutcomeResult) -> Optional[str]:
    m_total, a_total = r.measurements_total, r.analyses_total
    bits = []
    if m_total is not None and m_total > len(r.measurements):
        bits.append(f"only the first {len(r.measurements)} of {m_total} posted values are shown")
    if a_total is not None and a_total > len(r.analyses):
        bits.append(f"only the first {len(r.analyses)} of {a_total} statistical analyses are shown")
    if not bits:
        return None
    return (
        "; ".join(bits) + ". Arms, time points or analyses that are not shown were posted but not retrieved: do not "
        "report them as missing or not posted."
    )


def render_trials_evidence(data: Any) -> str:
    """<clinical_trials> evidence section (at most MAX_EVIDENCE_TRIALS trials)."""
    report = _report(data)
    if report is None:
        return ""
    head = (
        f'<clinical_trials source="{_a(report.source)}" query="{_a(query_label(report.query))}" '
        f'strategy="{_a(report.query.strategy)}" total_registered="{_a(report.total_count)}"'
        + _attrs([("data_version", report.source_release)])
    )
    if not report.trials:
        return head + ">No registered trials matched.</clinical_trials>"
    lines = [head + f' shown="{len(report.trials[:MAX_EVIDENCE_TRIALS])}">']
    for t in report.trials[:MAX_EVIDENCE_TRIALS]:
        lines.append(
            "<trial"
            + _attrs(
                [
                    ("nct_id", t.nct_id),
                    ("acronym", t.acronym),
                    ("phase", phase_label(t.phases)),
                    ("status", t.overall_status),
                    ("enrollment", t.enrollment_count),
                    ("enrollment_type", t.enrollment_type),
                    ("sponsor", t.lead_sponsor),
                    ("study_type", t.study_type),
                    ("start", t.start_date),
                    ("primary_completion", t.primary_completion_date),
                    ("has_results", "true" if t.has_results else "false"),
                ]
            )
            + ">"
        )
        if t.brief_title:
            lines.append(f"<title>{_t(t.brief_title)}</title>")
        if t.conditions:
            lines.append(f"<conditions>{_t('; '.join(t.conditions))}</conditions>")
        if t.interventions:
            text = "; ".join(f"{iv.get('type')}: {iv.get('name')}" if iv.get("type") else str(iv.get("name")) for iv in t.interventions)
            lines.append(f"<interventions>{_t(text)}</interventions>")
        for po in t.primary_outcomes:
            lines.append(f"<primary_outcome{_attrs([('time_frame', po.get('time_frame'))])}>{_t(po.get('measure') or '')}</primary_outcome>")
        if t.why_stopped:
            lines.append(f"<why_stopped>{_t(t.why_stopped)}</why_stopped>")
        if t.posted_results:
            if t.posted_results_total is not None and t.posted_results_total > len(t.posted_results):
                lines.append(
                    f'<posted_results_truncated shown="{len(t.posted_results)}" total="{t.posted_results_total}">'
                    "Only the first posted primary/efficacy outcome measures are shown; the others were posted but "
                    "not retrieved for this answer.</posted_results_truncated>"
                )
            for r in t.posted_results:
                titles = {m.group_id: m.group_title for m in r.measurements}
                # lowerLimit/upperLimit are CI bounds only for a confidence-interval dispersion; a full or
                # inter-quartile range must not be presented to the model as a CI.
                is_ci = "confidence" in (r.dispersion_type or "").lower()
                lo_key, hi_key = ("ci_lower", "ci_upper") if is_ci else ("range_lower", "range_upper")
                lines.append(
                    "<posted_result"
                    + _attrs(
                        [
                            ("type", r.type),
                            ("title", r.title),
                            ("param_type", r.param_type),
                            ("dispersion", r.dispersion_type),
                            ("unit", r.unit),
                            ("time_frame", r.time_frame),
                            *posted_result_counts(r),
                        ]
                    )
                    + ">"
                )
                note = truncation_note(r)
                if note:
                    lines.append(f"<truncation_note>{_t(note)}</truncation_note>")
                for m in r.measurements:
                    lines.append(
                        "<arm"
                        + _attrs(
                            [
                                ("group", m.group_title or m.group_id),
                                ("class", m.class_title),
                                ("category", m.category_title),
                                ("n", m.n),
                                ("value", m.value),
                                (lo_key, m.lower),
                                (hi_key, m.upper),
                                ("spread", m.spread),
                                ("comment", m.comment),
                            ]
                        )
                        + "/>"
                    )
                for an in r.analyses:
                    lines.append(analysis_xml(an, titles))
                lines.append("</posted_result>")
            if t.result_pmids:
                lines.append(f"<result_publications>{_t(', '.join('PMID: ' + p for p in t.result_pmids))}</result_publications>")
        elif t.has_results:
            lines.append("<posted_results>posted on ClinicalTrials.gov but not retrieved for this answer</posted_results>")
        else:
            lines.append("<posted_results>none posted on ClinicalTrials.gov</posted_results>")
        lines.append("</trial>")
    for note in report.limitations:
        lines.append(f"<limitation>{_t(note)}</limitation>")
    lines.append("</clinical_trials>")
    return "\n".join(lines)


def describe_result(data: Any, label: str = "") -> Tuple[str, str]:
    """(step title, detail) for a successful search; sponsor names never go in the title."""
    report = _report(data)
    if report is None:
        return "ClinicalTrials.gov search returned an unreadable payload", ""
    n = len(report.trials)
    strategy = query_label(report.query)
    if n == 0:
        return f"No registered trials matched {label or strategy}", strategy
    title = f"Found {n} registered trial{'s' if n != 1 else ''} (of {report.total_count} on ClinicalTrials.gov)"
    k = sum(1 for t in report.trials if t.has_results)
    parts = [f"{k} with posted results"]
    parts.extend(" ".join(x for x in (t.nct_id, t.acronym) if x) for t in report.trials[:3])
    parts.append(strategy)
    return title, " · ".join(parts)


def fallback_markdown(data: Any) -> str:
    """Deterministic section for the model-unavailable answer, copied from the registry records."""
    report = _report(data)
    if report is None:
        return ""
    body = report.to_markdown_table(max_rows=10)
    notes = "\n".join(f"- {n}" for n in report.limitations)
    return f"#### Registered Clinical Trials (📚 Lithoscribe)\n{body}" + (f"\n\n{notes}" if notes else "")
