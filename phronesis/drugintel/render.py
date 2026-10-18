"""Evidence XML and deterministic fallback Markdown for the ChEMBL and openFDA sources (all values escaped)."""

from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any, Dict, Iterable, List, Optional

from .drugsfda import COVERAGE_NOTE, approval_phrase
from .schemas import FAERS_CAVEAT

EHR_NOTE = (
    "FAERS is the FDA's spontaneous adverse-event reporting system, not EHR (electronic health record) or other "
    "real-world clinical-records data. No EHR source is available in this run, so no EHR safety data was retrieved."
)


def _attr(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return escape(fmt_num(value), quote=True)
    return escape(str(value), quote=True)


def _attrs(**pairs: Any) -> str:
    """' key="value"' pairs, skipping None values."""
    return "".join(f' {k}="{_attr(v)}"' for k, v in pairs.items() if v is not None)


def _text(value: Any) -> str:
    return escape(str(value or ""), quote=False)


def fmt_num(value: Optional[float]) -> str:
    """The exact value as ChEMBL stores it (38.0 -> '38', 31622.78 -> '31622.78'); never rounded or in e-notation."""
    if value is None:
        return ""
    v = float(value)
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return format(Decimal(repr(v)), "f")


def safety_entries(data: Any) -> List[Dict[str, Any]]:
    """The per-drug openFDA records in a source result, whether one record or a list under 'safety'."""
    if not isinstance(data, dict):
        return []
    if "top_reactions" in data:
        return [data]
    entries = data.get("safety")
    if isinstance(entries, dict):
        return [entries]
    return [e for e in entries or [] if isinstance(e, dict)]


# -----------------------------------------------------------------------------
# ChEMBL
# -----------------------------------------------------------------------------

# Shown for an approved drug whose ChEMBL preferred name could not be retrieved: never its mechanism text.
UNNAMED_DRUG = "name not retrieved from ChEMBL"


def _target_label(target: Dict[str, Any]) -> str:
    return str(target.get("symbol_query") or target.get("pref_name") or target.get("target_chembl_id") or "the target")


def _compound_label(c: Dict[str, Any]) -> str:
    name = c.get("pref_name")
    bits = [str(c.get("chembl_id"))]
    if c.get("best_pchembl") is not None:
        bits.append(f"pChEMBL {fmt_num(c['best_pchembl'])}")
    return f"{name} ({', '.join(bits)})" if name else f"{bits[0]}" + (f" ({bits[1]})" if len(bits) > 1 else "")


def approved_lookup_failed(data: Dict[str, Any]) -> bool:
    """True when the approved-drugs-for-target lookup failed, so its empty list means unknown, not none."""
    return isinstance(data, dict) and data.get("target_approved_status") == "failed"


def approved_failure_phrase(data: Dict[str, Any]) -> str:
    """'timed out' / 'failed (ChEMBL HTTP 500 ...)' for a failed approved-drugs-for-target lookup."""
    if data.get("target_approved_error_kind") == "timeout":
        return "timed out"
    err = str(data.get("target_approved_error") or "").strip()
    return f"failed ({err[:120]})" if err else "failed"


def approved_unknown_text(data: Dict[str, Any]) -> str:
    """The sentence that replaces an approved-drug count when the lookup failed."""
    label = _target_label(data.get("target") or {})
    return (
        f"The ChEMBL lookup of approved drugs acting on {label} {approved_failure_phrase(data)}, so whether any "
        f"approved drug acts on {label} is unknown; this is not evidence that none does"
    )


def bioactive_summary(data: Dict[str, Any]) -> str:
    """One sentence that keeps 'no approved drug' apart from 'no compound': '0 approved drugs act on MYC; ChEMBL
    lists 25 compounds with measured activity against MYC (top: ...)'. A failed approved-drug lookup is reported
    as unknown, never as 0."""
    target = data.get("target") or {}
    if not target:
        return ""
    label = _target_label(target)
    approved = data.get("target_approved_drugs") or []
    a_total = data.get("target_approved_total")
    n_approved = a_total if a_total is not None else len(approved)
    if approved_lookup_failed(data):
        approved_text = approved_unknown_text(data)
    else:
        approved_text = (
            f"{'at least ' if a_total is None and approved else ''}{n_approved} approved "
            f"drug{'' if n_approved == 1 else 's'} act{'s' if n_approved == 1 else ''} on {label} (ChEMBL max phase 4 mechanisms)"
        )
    fetched = data.get("target_activity_records_fetched")
    if fetched is None:
        return approved_text[0].upper() + approved_text[1:] + "."
    compounds = data.get("target_bioactive_compounds") or []
    if not compounds:
        return (f"{approved_text[0].upper()}{approved_text[1:]}; ChEMBL lists no compound with a measured IC50/Ki/Kd/EC50 "
                f"(pChEMBL) value against {label}.")
    distinct = int(data.get("target_bioactive_distinct") or len(compounds))
    total = data.get("target_activity_records_total")
    exact = total is not None and total <= fetched
    count = f"{distinct}" if exact else f"at least {distinct}"
    scope = "" if exact else f" among the top {fetched} of {total if total is not None else 'its'} activity records"
    top = "; ".join(_compound_label(c) for c in compounds[:5])
    return (
        f"{approved_text[0].upper()}{approved_text[1:]}; ChEMBL lists {count} compound{'' if distinct == 1 else 's'} with "
        f"measured IC50/Ki/Kd/EC50 activity against {label}{scope} (top: {top}). These are research compounds by "
        "measured activity, not approved drugs."
    )

def chembl_evidence_xml(data: Dict[str, Any]) -> str:
    release = data.get("chembl_release") or "release unknown"
    parts: List[str] = [f'<drug_intelligence source="ChEMBL REST {_attr(release)}">']
    target = data.get("target") or None
    if target:
        parts.append(
            f"<target{_attrs(chembl_id=target.get('target_chembl_id'), pref_name=target.get('pref_name'), symbol=target.get('symbol_query'))}/>"
        )
    for d in data.get("drugs") or []:
        parts.append(
            "<drug"
            + _attrs(
                chembl_id=d.get("chembl_id"),
                pref_name=d.get("pref_name"),
                query_name=d.get("query_name"),
                user_name=d.get("mention"),
                matched_via=d.get("matched_via"),
                max_phase=d.get("max_phase"),
                max_phase_meaning=d.get("max_phase_meaning"),
                first_approval=d.get("first_approval"),
                molecule_type=d.get("molecule_type"),
                withdrawn=d.get("withdrawn_flag"),
                chembl_black_box_flag=d.get("chembl_black_box_flag"),
                forms=",".join(d.get("forms") or []) or None,
                trade_names=", ".join(d.get("trade_names") or []) or None,
            )
            + ">"
        )
        for m in d.get("mechanisms") or []:
            parts.append(
                "<mechanism"
                + _attrs(
                    target_chembl_id=m.get("target_chembl_id"),
                    target_pref_name=m.get("target_pref_name"),
                    action_type=m.get("action_type"),
                    form_chembl_id=m.get("form_chembl_id"),
                )
                + f">{_text(m.get('mechanism_of_action'))}</mechanism>"
            )
        for b in d.get("bioactivities") or []:
            parts.append(
                "<bioactivity"
                + _attrs(
                    activity_id=b.get("activity_id"),
                    assay_chembl_id=b.get("assay_chembl_id"),
                    document_chembl_id=b.get("document_chembl_id"),
                    target_chembl_id=b.get("target_chembl_id"),
                    type=b.get("standard_type"),
                    relation=b.get("standard_relation"),
                    value=b.get("standard_value"),
                    units=b.get("standard_units"),
                    pchembl=b.get("pchembl_value"),
                    validity=b.get("data_validity_comment"),
                )
                + f">{_text(b.get('assay_description'))}</bioactivity>"
            )
        if target:
            shown = len(d.get("bioactivities") or [])
            parts.append(f"<bioactivity_summary{_attrs(shown=shown, total=d.get('bioactivity_total'))}/>")
        parts.append("</drug>")
    approved = data.get("target_approved_drugs") or []
    if approved and target:
        parts.append(
            f"<approved_drugs_for_target{_attrs(target_chembl_id=target.get('target_chembl_id'), shown=len(approved), total=data.get('target_approved_total'))}>"
        )
        for a in approved:
            parts.append(
                f"<approved_drug{_attrs(chembl_id=a.get('chembl_id'), pref_name=a.get('pref_name'), name=None if a.get('pref_name') else UNNAMED_DRUG, first_approval=a.get('first_approval'), molecule_type=a.get('molecule_type'))}>"
                f"{_text(a.get('mechanism_of_action'))}</approved_drug>"
            )
        parts.append("</approved_drugs_for_target>")
    elif target and approved_lookup_failed(data):
        parts.append(
            f"<approved_drugs_for_target{_attrs(target_chembl_id=target.get('target_chembl_id'), status='failed', error=data.get('target_approved_error'))}>"
            f"{_text(approved_unknown_text(data))}. Approved drugs: unknown.</approved_drugs_for_target>"
        )
    compounds = data.get("target_bioactive_compounds") or []
    if target and not (data.get("drugs") or []) and data.get("target_activity_records_fetched") is not None:
        parts.append(f"<target_summary>{_text(bioactive_summary(data))}</target_summary>")
    if target and compounds:
        parts.append(
            "<bioactive_compounds_for_target"
            + _attrs(
                target_chembl_id=target.get("target_chembl_id"),
                shown=len(compounds),
                distinct_compounds=data.get("target_bioactive_distinct"),
                activity_records_total=data.get("target_activity_records_total"),
                activity_records_fetched=data.get("target_activity_records_fetched"),
            )
            + ">"
        )
        for c in compounds:
            parts.append(
                "<compound"
                + _attrs(
                    chembl_id=c.get("chembl_id"),
                    pref_name=c.get("pref_name"),
                    max_phase=c.get("max_phase"),
                    molecule_type=c.get("molecule_type"),
                    best_pchembl=c.get("best_pchembl"),
                    type=c.get("standard_type"),
                    relation=c.get("standard_relation"),
                    value=c.get("standard_value"),
                    units=c.get("standard_units"),
                    assay_chembl_id=c.get("assay_chembl_id"),
                    records=c.get("records"),
                )
                + "/>"
            )
        parts.append("</bioactive_compounds_for_target>")
    if data.get("degrader_question"):
        parts.append(
            "<note>ChEMBL does not label compounds as PROTACs or degraders; these compounds are listed by measured "
            "activity only.</note>"
        )
    for name in data.get("unresolved_names") or []:
        parts.append(f"<unresolved{_attrs(name=name)}/>")
    for cid in data.get("invalid_chembl_ids") or []:
        parts.append(f"<invalid_chembl_id{_attrs(id=cid)}/>")
    parts.append("</drug_intelligence>")
    return "\n".join(parts)


def chembl_fallback_markdown(data: Dict[str, Any]) -> str:
    lines: List[str] = ["#### Drug Pharmacology (🧬 Synapta · ChEMBL)"]
    target = data.get("target") or None
    for d in data.get("drugs") or []:
        approval = f", first approval {d['first_approval']}" if d.get("first_approval") else ""
        lines.append(
            f"- **{d.get('pref_name') or d.get('mention') or d.get('query_name')}** ({d.get('chembl_id')}): {d.get('molecule_type') or 'molecule'}, "
            f"max phase {fmt_num(d.get('max_phase')) or 'none'} ({d.get('max_phase_meaning')}){approval}"
        )
        for m in (d.get("mechanisms") or [])[:5]:
            lines.append(f"  - {m.get('action_type') or 'MECHANISM'}: {m.get('mechanism_of_action')} ({m.get('target_chembl_id')})")
        for b in (d.get("bioactivities") or [])[:5]:
            lines.append(
                f"  - {b.get('standard_type')} {b.get('standard_relation') or ''} {fmt_num(b.get('standard_value'))} "
                f"{b.get('standard_units') or ''} (pChEMBL {fmt_num(b.get('pchembl_value'))}; assay {b.get('assay_chembl_id')})".replace("  ", " ")
            )
    if target and data.get("target_approved_drugs"):
        names = ", ".join(f"{a.get('pref_name') or UNNAMED_DRUG} ({a.get('chembl_id')})" for a in data["target_approved_drugs"][:10])
        label = target.get("pref_name") or target.get("symbol_query") or target.get("target_chembl_id")
        lines.append(f"- Approved drugs acting on {label}: {names}")
    elif target and approved_lookup_failed(data) and ((data.get("drugs") or []) or data.get("target_activity_records_fetched") is None):
        lines.append(f"- {approved_unknown_text(data)}.")  # otherwise the summary line below says it
    if target and not (data.get("drugs") or []) and data.get("target_activity_records_fetched") is not None:
        lines.append(f"- {bioactive_summary(data)}")
    for name in data.get("unresolved_names") or []:
        lines.append(f"- No exact ChEMBL match for '{name}'.")
    return "\n".join(lines) if len(lines) > 1 else ""


# -----------------------------------------------------------------------------
# openFDA
# -----------------------------------------------------------------------------

def _label_xml(label: Dict[str, Any]) -> List[str]:
    parts = [
        "<label"
        + _attrs(
            set_id=label.get("set_id"),
            effective_time=label.get("effective_time"),
            brand=label.get("brand_name") or "unknown (not harmonized by openFDA)",
            application_number=label.get("application_number") or "unknown",
            manufacturer=label.get("manufacturer_name"),
            generic_name=label.get("generic_name"),
            combination_product=label.get("combination_product"),
            harmonized=bool(label.get("harmonized")),
        )
        + ">"
    ]
    if label.get("indications_and_usage"):
        parts.append(
            f"<indications_and_usage{_attrs(truncated=bool(label.get('indications_truncated')))}>"
            f"{_text(label['indications_and_usage'])}</indications_and_usage>"
        )
    if label.get("boxed_warning"):
        parts.append(
            f"<boxed_warning{_attrs(truncated=bool(label.get('boxed_warning_truncated')))}>{_text(label['boxed_warning'])}</boxed_warning>"
        )
    else:
        parts.append('<boxed_warning present="false"/>')
    if label.get("warnings"):
        section = label.get("warnings_section") or "warnings"
        parts.append(
            f"<warnings_and_cautions{_attrs(section=section, truncated=bool(label.get('warnings_truncated')))}>"
            f"{_text(label['warnings'])}</warnings_and_cautions>"
        )
    if label.get("contraindications"):
        parts.append(
            f"<contraindications{_attrs(truncated=bool(label.get('contraindications_truncated')))}>"
            f"{_text(label['contraindications'])}</contraindications>"
        )
    parts.append("</label>")
    return parts


def approval_xml(approval: Optional[Dict[str, Any]], drug: Any) -> List[str]:
    """The Drugs@FDA approval element of one drug: approved (application, first approval, efficacy supplements),
    not in Drugs@FDA, approved only in combination products, or a failed lookup (status unknown)."""
    if not isinstance(approval, dict) or not approval.get("status"):
        return []
    status = approval["status"]
    if status == "error":
        return [
            f"<fda_approval{_attrs(drug=drug, status='lookup_failed', source='Drugs@FDA (openFDA)')}>The Drugs@FDA "
            f"lookup failed ({_text(approval.get('error'))}), so FDA approval status is unknown from this source: do not "
            "state that the drug is or is not FDA-approved on the basis of Drugs@FDA.</fda_approval>"
        ]
    if status == "not_found":
        unapproved = ", ".join(approval.get("unapproved_applications") or [])
        extra = f" Applications without an approved original submission (e.g. tentative approval): {unapproved}." if unapproved else ""
        return [
            f"<fda_approval{_attrs(drug=drug, status='not_in_drugs_at_fda', source='Drugs@FDA (openFDA)', search=approval.get('search'))}>"
            f"Drugs@FDA lists no approved NDA or BLA for {_text(drug)}: it is not FDA-approved as a CDER drug "
            f"(e.g. investigational, or approved only outside the US).{_text(extra)} Do not call it FDA-approved. "
            f"{_text(COVERAGE_NOTE)}</fda_approval>"
        ]
    apps = approval.get("applications") or []
    lines = [
        "<fda_approval"
        + _attrs(
            drug=drug,
            status="approved" if status == "approved" else "approved_only_in_combination_products",
            source="Drugs@FDA (openFDA)",
            application_number=approval.get("application_number"),
            brand=", ".join(approval.get("brand_names") or []) or None,
            sponsor=approval.get("sponsor"),
            marketed=approval.get("marketed"),
            first_approval_date=approval.get("first_approval_date"),
            first_approval_application=approval.get("first_approval_application"),
            nda_bla_applications=approval.get("applications_total"),
            efficacy_supplements=approval.get("efficacy_supplements_total") if status == "approved" else None,
        )
        + ">"
    ]
    if status == "approved":
        lines.append(f"<summary>{_text(approval_phrase(approval))}</summary>")
    else:
        lines.append(
            f"<summary>{_text(drug)} appears in Drugs@FDA only within combination products; no single-ingredient NDA/BLA "
            "approval was found. Do not call it FDA-approved as a single agent.</summary>"
        )
    for a in apps:
        lines.append(
            "<application"
            + _attrs(
                number=a.get("application_number"),
                brand=", ".join(a.get("brand_names") or []) or None,
                sponsor=a.get("sponsor"),
                original_approval=a.get("original_approval_date"),
                marketing_status=", ".join(a.get("marketing_status") or []) or None,
                combination_product=True if a.get("combination") else None,
            )
            + "/>"
        )
    supplements = approval.get("efficacy_supplements") or []
    if supplements:
        total = approval.get("efficacy_supplements_total") or len(supplements)
        shown = f"the {len(supplements)} most recent of {total}" if total > len(supplements) else f"all {total}"
        lines.append(
            f"<efficacy_supplements{_attrs(shown=shown)} note=\"Approved efficacy supplements (submission class EFFICACY) "
            "of the first sponsor's applications: typically new or changed indications. Drugs@FDA does not name the "
            "indication; cite the dates and supplement numbers, not specific indications, from this list.\">"
            + "; ".join(
                f"{_text(x.get('application_number'))} S-{_text(x.get('submission_number'))} approved {_text(x.get('date'))}"
                + (f" ({_text(x.get('review_priority')).lower()} review)" if x.get("review_priority") else "")
                for x in supplements
            )
            + "</efficacy_supplements>"
        )
    lines.append("</fda_approval>")
    return lines


def safety_evidence_xml(data: Dict[str, Any]) -> str:
    entries = safety_entries(data)
    skipped = data.get("skipped") or [] if isinstance(data, dict) else []
    selection = (data.get("candidate_selection") or None) if isinstance(data, dict) else None
    if not entries and not skipped and not selection:
        return ""
    last_updated = next((e.get("last_updated") for e in entries if e.get("last_updated")), None)
    disclaimer = next((e.get("disclaimer") for e in entries if e.get("disclaimer")), None)
    parts: List[str] = [f'<drug_safety source="openFDA"{_attrs(last_updated=last_updated)}>']
    if disclaimer:
        parts.append(f"<disclaimer>{_text(disclaimer)}</disclaimer>")
    if selection and selection.get("status") == "none":
        details = "; ".join(selection.get("details") or [])
        parts.append(
            f"<candidate_selection{_attrs(status='none')}>The question asks about "
            f"{'regulatory status' if selection.get('purpose') == 'regulatory' else 'safety'} but names no drug, and no drug "
            f"candidate was found{_text(' (' + details + ')') if details else ''}; openFDA was not queried, so no FAERS "
            "or label data is available.</candidate_selection>"
        )
    elif selection:
        names = ", ".join(str(d.get("drug")) for d in selection.get("drugs") or [] if d.get("drug"))
        parts.append(
            f"<candidate_selection{_attrs(status='selected', source=selection.get('source'), drugs=names)}>No drug was "
            f"named in the question. openFDA was queried for these candidate drugs, taken from evidence earlier in this "
            f"run: {_text(selection.get('basis'))}. The FAERS counts below are these candidates' FAERS reports only."
            + (f" {_text(selection['allele_note'])}." if selection.get('allele_note') else "")
            + (f" {_text(selection['coverage_note'])}" if selection.get('coverage_note') else "")
            + "</candidate_selection>"
        )
    if isinstance(data, dict) and data.get("ehr_note"):
        parts.append(f"<ehr_data available=\"false\">{_text(data['ehr_note'])}</ehr_data>")
    down = (data.get("chembl_unavailable") or None) if isinstance(data, dict) else None
    if isinstance(down, dict) and down.get("drugs"):
        names = ", ".join(str(d) for d in down.get("drugs") or [])
        parts.append(
            f"<chembl_unavailable{_attrs(drugs=names)}>ChEMBL could not be reached ({_text(down.get('error'))}), so "
            "openFDA was searched by the drug name as written; the drug's ChEMBL identity and approval phase were "
            "not checked.</chembl_unavailable>"
        )
    if entries:
        parts.append(f"<caveat>{_text(FAERS_CAVEAT)}</caveat>")
    for e in entries:
        parts.append(
            "<faers"
            + _attrs(
                drug=e.get("ingredient"),
                candidate=True if e.get("candidate") else None,
                matched_by="name (ChEMBL unavailable)" if e.get("matched_by_name") else None,
                search=e.get("faers_search"),
                searched_as=faers_alias(e),
                match_field=e.get("faers_match_field") if faers_alias(e) else None,
                total_reports=e.get("total_reports"),
                serious_reports=e.get("serious_reports"),
                reports_listing_death=e.get("deaths_reported"),
            )
            + ">"
        )
        for r in e.get("top_reactions") or []:
            parts.append(f"<reaction{_attrs(term=r.get('term'), count=r.get('count'))}/>")
        excluded = e.get("excluded_reactions") or []
        if excluded:
            parts.append(
                f"<excluded_non_reaction_terms{_attrs(count=e.get('excluded_terms') or len(excluded))}>"
                + _text("; ".join(f"{r.get('term')} ({r.get('count')})" for r in excluded))
                + " (outcomes, disease progression, off-label or product-use terms; not adverse reactions)"
                + "</excluded_non_reaction_terms>"
            )
        parts.append("</faers>")
        parts.extend(approval_xml(e.get("approval"), e.get("ingredient")))
        if e.get("label"):
            parts.extend(_label_xml(e["label"]))
        else:
            parts.append(f"<label_not_found{_attrs(drug=e.get('ingredient'), search=e.get('label_search'))}/>")
    for s in skipped:
        fda = None
        if s.get("max_phase") is not None and float(s["max_phase"]) < 4:
            fda = ("not queried in Drugs@FDA; ChEMBL lists it as not approved by any regulator, so do not call it "
                   "FDA-approved unless another retrieved source shows an FDA approval")
        parts.append(f"<not_queried{_attrs(drug=s.get('drug'), reason=s.get('reason'), fda_approval=fda)}/>")
    parts.append("</drug_safety>")
    return "\n".join(parts)


def faers_alias(entry: Dict[str, Any]) -> Optional[str]:
    """The other name FAERS reports were found under ("mesalamine" for mesalazine), or None."""
    name = entry.get("faers_matched_name")
    if name and str(name).strip().lower() != str(entry.get("ingredient") or "").strip().lower():
        return str(name)
    return None


def label_product(label: Optional[Dict[str, Any]]) -> str:
    """'Gleevec NDA021588', or a statement that openFDA did not harmonize the label."""
    if not label:
        return ""
    bits = [b for b in (label.get("brand_name"), label.get("application_number")) if b]
    product = " ".join(bits) if bits else "a label not harmonized by openFDA (brand/application unknown)"
    if label.get("combination_product") and label.get("generic_name"):
        product += f" (combination product: {label['generic_name']})"
    return product


def safety_fallback_markdown(data: Dict[str, Any]) -> str:
    entries = safety_entries(data)
    if not entries:
        return ""
    lines: List[str] = ["#### FDA Adverse-Event Reports & Label (📚 Lithoscribe · openFDA)"]
    selection = data.get("candidate_selection") if isinstance(data, dict) else None
    if selection:
        lines.append(f"- No drug was named; these are FAERS reports for candidate drugs from {selection.get('basis')}.")
    for e in entries:
        top = ", ".join(f"{r['term']} ({r['count']})" for r in (e.get("top_reactions") or [])[:5])
        serious = f", {e['serious_reports']:,} flagged serious" if e.get("serious_reports") is not None else ""
        alias = faers_alias(e)
        as_name = f" (searched as {alias})" if alias else ""
        lines.append(f"- **{e.get('ingredient')}**{as_name}: {e.get('total_reports', 0):,} FAERS reports{serious}. Most reported reactions: {top or 'none'}.")
        if isinstance(e.get("approval"), dict) and e["approval"].get("status"):
            lines.append(f"  - Drugs@FDA: {approval_phrase(e['approval'])}.")
        if e.get("deaths_reported") is not None:
            lines.append(f"  - Outcome: {e['deaths_reported']:,} reports list death (an outcome, not a reaction to the drug).")
        if e.get("excluded_terms"):
            lines.append(f"  - Excluded {e['excluded_terms']} non-reaction terms (e.g. off-label use, disease progression).")
        label = e.get("label")
        if label:
            boxed = "boxed warning present" if label.get("boxed_warning") else "no boxed warning"
            lines.append(f"  - Label: {label_product(label)}, effective {label.get('effective_time') or 'unknown'}; {boxed}. {label.get('dailymed_url')}")
    lines.append(f"- {FAERS_CAVEAT}")
    if isinstance(data, dict) and data.get("ehr_note"):
        lines.append(f"- {data['ehr_note']}")
    disclaimer = next((e.get("disclaimer") for e in entries if e.get("disclaimer")), None)
    if disclaimer:
        lines.append(f"- openFDA: {disclaimer}")
    return "\n".join(lines)


def join_names(names: Iterable[str]) -> str:
    items = [n for n in names if n]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
