"""Rendering of a synapta_variant_intelligence result: evidence XML, activity step text, fallback markdown, artifact."""

from __future__ import annotations

import copy
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from ..sources.names import VARIANT_EVIDENCE_SOURCES, labels as source_labels
from .models import review_stars

# {"clinvar": "ClinVar", "civic": "CIViC", "cbioportal": "cBioPortal", "gnomad": "gnomAD"}
SOURCE_LABELS = source_labels("public", VARIANT_EVIDENCE_SOURCES)
GNOMAD_NOTE = "germline population allele frequency (gnomAD v4), not tumour prevalence"
CLINVAR_CONDITION_NOTE = (
    "Each ClinVar classification applies only to the condition(s) listed with it, at its review status; it is not a "
    "property of the variant in other conditions."
)
# ClinVar classification kinds: (classification key, review-status key, conditions key, stars key, label).
CLINVAR_KINDS = (
    ("germline_classification", "germline_review_status", "germline_traits", "germline_review_stars", "germline"),
    ("oncogenicity_classification", "oncogenicity_review_status", "oncogenicity_traits", "oncogenicity_review_stars", "oncogenicity"),
    ("somatic_clinical_impact", "somatic_clinical_impact_review_status", "somatic_clinical_impact_traits",
     "somatic_clinical_impact_review_stars", "somatic clinical impact"),
)
_ARTIFACT_DESCRIPTION_CHARS = 280


def _attr(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _attrs(pairs: List[Tuple[str, Any]]) -> str:
    return " ".join(f'{k}="{_attr(v)}"' for k, v in pairs if v is not None and v != "" and v != [])


def fmt_af(value: Optional[float]) -> str:
    """Allele frequency in scientific notation with two significant figures ('2.7e-06')."""
    if value is None:
        return ""
    return "0" if value == 0 else f"{value:.1e}"


def fmt_int(value: Optional[int]) -> str:
    return "" if value is None else f"{int(value):,}"


def clinvar_stars(rec: Dict[str, Any], stars_key: str, status_key: str) -> Optional[int]:
    """The 0-4 ClinVar review-star rating of one classification (stored, else derived from the review status)."""
    stars = rec.get(stars_key)
    return int(stars) if stars is not None else review_stars(rec.get(status_key))


def _stars_text(stars: Optional[int]) -> str:
    return "" if stars is None else f"{stars} of 4 review stars"


def _conditions_text(traits: List[str], limit: int = 6) -> str:
    named = [t for t in traits if t]
    shown = named[:limit]
    more = len(named) - len(shown)
    return "; ".join(shown) + (f"; +{more} more" if more > 0 else "")


def gnomad_qc(g: Dict[str, Any]) -> Dict[str, List[str]]:
    """{'exome': [...], 'genome': [...]}: the QC filters each gnomAD dataset's call FAILED (empty = PASS)."""
    out = {"exome": [str(f) for f in g.get("exome_filters") or [] if f],
           "genome": [str(f) for f in g.get("genome_filters") or [] if f]}
    if not out["exome"] and not out["genome"] and g.get("filters") and "exome_filters" not in g:
        # An older result with only the merged list: the failing dataset is unknown, so mark every present one.
        for ds in ("exome", "genome"):
            if g.get(f"{ds}_af") is not None or g.get(f"{ds}_ac") is not None:
                out[ds] = [str(f) for f in g["filters"]]
    return out


def gnomad_qc_warning(g: Dict[str, Any]) -> str:
    """'gnomAD marks the exome call as failing QC filter(s) AC0; ...' or '' when every call passed."""
    qc = gnomad_qc(g)
    parts = [f"the {ds} call as failing QC filter(s) {', '.join(f)}" for ds, f in qc.items() if f]
    if not parts:
        return ""
    return ("gnomAD marks " + " and ".join(parts) + "; treat that frequency as unreliable and do not report it "
            "without this QC caveat")


def variant_label(data: Dict[str, Any]) -> str:
    gene = data.get("gene") or ""
    change = data.get("protein_change") or data.get("hgvs_c") or data.get("rsid") or ""
    return f"{gene} {change}".strip()


def variant_datas(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every variant's data in a result: the top-level variant, then data['additional_variants'] (multi-variant
    lookups); a single-variant result is a one-item list."""
    if not isinstance(data, dict) or not data:
        return []
    extra = [d for d in data.get("additional_variants") or [] if isinstance(d, dict)]
    return [data, *extra]


def _status_map(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {s.get("name"): s for s in data.get("sources") or [] if isinstance(s, dict)}


# -----------------------------------------------------------------------------
# Evidence XML
# -----------------------------------------------------------------------------

def _kind_stars(rec: Dict[str, Any], index: int) -> Optional[int]:
    cls_key, status_key, _traits, stars_key, _kind = CLINVAR_KINDS[index]
    return clinvar_stars(rec, stars_key, status_key) if rec.get(cls_key) else None


def render_variant_evidence(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    head = _attrs([
        ("gene", data.get("gene")), ("protein_change", data.get("protein_change")), ("hgvs_p", data.get("hgvs_p")),
        ("hgvs_c", data.get("hgvs_c")), ("rsid", data.get("rsid")),
        ("cancer_filter", data.get("cancer_filter") or "none (pan-cancer default)"),
    ])
    parts = [f"<variant_intelligence {head}>"]
    for rec in data.get("clinvar") or []:
        attrs = _attrs([
            ("accession", rec.get("accession")), ("variation_id", rec.get("variation_id")), ("title", rec.get("title")),
            ("rsids", ",".join(rec.get("rsids") or [])), ("protein_change", ",".join(rec.get("protein_change") or [])),
            ("germline_classification", rec.get("germline_classification")),
            ("germline_review_status", rec.get("germline_review_status")),
            ("germline_review_stars", _kind_stars(rec, 0)),
            ("germline_conditions", _conditions_text(rec.get("germline_traits") or []) if rec.get("germline_classification") else None),
            ("germline_last_evaluated", rec.get("germline_last_evaluated")),
            ("oncogenicity_classification", rec.get("oncogenicity_classification")),
            ("oncogenicity_review_status", rec.get("oncogenicity_review_status")),
            ("oncogenicity_review_stars", _kind_stars(rec, 1)),
            ("somatic_clinical_impact", rec.get("somatic_clinical_impact")),
            ("somatic_clinical_impact_review_status", rec.get("somatic_clinical_impact_review_status")),
            ("somatic_clinical_impact_review_stars", _kind_stars(rec, 2)),
            ("url", rec.get("url")),
        ])
        # The conditions of each classification (DAT-006): a germline "Pathogenic" for a RASopathy is not a
        # statement about a tumour type.
        lines = []
        for cls_key, status_key, traits_key, stars_key, kind in CLINVAR_KINDS:
            if not rec.get(cls_key):
                continue
            somatic = kind == "somatic clinical impact"
            conditions = _conditions_text(rec.get(traits_key) or [], 4 if somatic else 6)
            review = ", ".join(x for x in (rec.get(status_key) or "review status not given",
                                           _stars_text(clinvar_stars(rec, stars_key, status_key))) if x)
            if somatic:
                lines.append(f"Somatic clinical impact traits: {conditions or 'not given'} ({review}).")
            else:
                lines.append(f"{kind.capitalize()} classification {rec[cls_key]} for condition(s): "
                             f"{conditions or 'not given'} ({review}).")
        if lines:
            lines.append(CLINVAR_CONDITION_NOTE)
        body = escape(" ".join(lines), quote=False)
        parts.append(f"<clinvar_record {attrs}>{body}</clinvar_record>")

    civic = data.get("civic")
    if isinstance(civic, dict):
        parts.append(
            f"<civic_summary {_attrs([('molecular_profile', civic.get('molecular_profile_name')), ('accepted_evidence_total', civic.get('accepted_evidence_total')), ('accepted_assertion_total', civic.get('accepted_assertion_total')), ('question_disease', civic.get('context_disease')), ('question_disease_items', civic.get('context_matched')), ('url', civic.get('url'))])}/>"
        )
        for ev in civic.get("evidence") or []:
            match = ev.get("matches_context")
            attrs = _attrs([
                ("eid", ev.get("eid")),
                ("matches_question_disease", None if match is None else ("true" if match else "false")), ("level", ev.get("evidence_level")), ("type", ev.get("evidence_type")),
                ("direction", ev.get("evidence_direction")), ("significance", ev.get("significance")),
                ("rating", ev.get("evidence_rating")), ("origin", ev.get("variant_origin")), ("disease", ev.get("disease")),
                ("therapies", ", ".join(ev.get("therapies") or [])), ("pmid", ev.get("pmid")),
                ("citation", ev.get("citation")), ("year", ev.get("publication_year")), ("url", ev.get("url")),
            ])
            parts.append(f"<civic_evidence {attrs}>{escape(str(ev.get('description') or ''), quote=False)}</civic_evidence>")
        for a in civic.get("assertions") or []:
            attrs = _attrs([
                ("aid", a.get("aid")), ("amp_level", a.get("amp_level")), ("type", a.get("assertion_type")),
                ("direction", a.get("assertion_direction")), ("significance", a.get("significance")),
                ("disease", a.get("disease")), ("therapies", ", ".join(a.get("therapies") or [])),
                ("fda_companion_test", "true" if a.get("fda_companion_test") else None),
                ("regulatory_approval", "true" if a.get("regulatory_approval") else None),
                ("nccn_guideline", a.get("nccn_guideline")), ("status", a.get("status")), ("url", a.get("url")),
            ])
            parts.append(f"<civic_assertion {attrs}/>")

    for st in data.get("cbioportal") or []:
        attrs = _attrs([
            ("mutated_samples", st.get("mutated_samples")), ("profiled_samples", st.get("profiled_samples")),
            ("percent", st.get("prevalence_percent")), ("study", st.get("study_name")), ("study_id", st.get("study_id")),
            # Patients over patients only (DAT-015): profiled_patients is the denominator for mutated_patients.
            ("mutated_patients", st.get("mutated_patients")), ("profiled_patients", st.get("profiled_patients")),
            ("gene_mutated_samples", st.get("gene_mutated_samples")), ("study_import_date", st.get("study_import_date")),
            ("cancer_type", st.get("cancer_type")), ("denominator_basis", st.get("denominator_basis")),
            ("reference_genome", st.get("reference_genome")), ("url", st.get("url")),
        ])
        parts.append(f"<cbioportal_prevalence {attrs}/>")

    g = data.get("gnomad")
    if isinstance(g, dict):
        attrs = _attrs([
            ("variant_id", g.get("variant_id")), ("dataset", g.get("dataset")),
            ("exome_ac", g.get("exome_ac")), ("exome_an", g.get("exome_an")), ("exome_af", g.get("exome_af")),
            ("genome_ac", g.get("genome_ac")), ("genome_an", g.get("genome_an")), ("genome_af", g.get("genome_af")),
            ("faf95_popmax", g.get("faf95_popmax")), ("faf95_popmax_population", g.get("faf95_popmax_population")),
            ("homozygotes", g.get("exome_homozygotes")), ("hgvsp", g.get("hgvsp")), ("url", g.get("url")),
            ("note", GNOMAD_NOTE),
        ])
        qc = gnomad_qc(g)
        qc_attrs = _attrs([
            ("exome_qc", ("FAIL: " + ", ".join(qc["exome"])) if qc["exome"] else ("PASS" if g.get("exome_ac") is not None else None)),
            ("genome_qc", ("FAIL: " + ", ".join(qc["genome"])) if qc["genome"] else ("PASS" if g.get("genome_ac") is not None else None)),
            ("flags", ", ".join(str(f) for f in g.get("flags") or [])),
        ])
        attrs = f"{attrs} {qc_attrs}" if qc_attrs else attrs
        warning = gnomad_qc_warning(g)
        if warning:
            parts.append(f"<gnomad_frequency {attrs}>{escape(warning[0].upper() + warning[1:] + '.', quote=False)}</gnomad_frequency>")
        else:
            parts.append(f"<gnomad_frequency {attrs}/>")

    for s in data.get("sources") or []:
        attrs = _attrs([("name", s.get("name")), ("status", s.get("status")), ("elapsed_seconds", s.get("elapsed_seconds"))])
        parts.append(f"<source_status {attrs}>{escape(str(s.get('detail') or ''), quote=False)}</source_status>")
    parts.append("</variant_intelligence>")
    return "\n".join(parts)


# -----------------------------------------------------------------------------
# Activity step and fallback text
# -----------------------------------------------------------------------------

def _clinvar_phrase(rec: Dict[str, Any]) -> str:
    bits = []
    for cls_key, status_key, traits_key, stars_key, kind in CLINVAR_KINDS:
        if not rec.get(cls_key):
            continue
        extra = [kind]
        traits = [t for t in rec.get(traits_key) or [] if t]
        if traits and kind != "somatic clinical impact":
            extra.append("for " + ", ".join(traits[:2]) + (f" +{len(traits) - 2} more" if len(traits) > 2 else ""))
        stars = clinvar_stars(rec, stars_key, status_key)
        if stars is not None:
            extra.append(f"{stars}/4 stars")
        bits.append(f"{rec[cls_key]} ({'; '.join(extra)})")
    return f"ClinVar {rec.get('accession')}: " + (" · ".join(bits) if bits else "no classification")


def _study_phrase(st: Dict[str, Any]) -> str:
    pct = st.get("prevalence_percent")
    pct_txt = f" ({pct}%)" if pct is not None else ""
    return f"{st.get('mutated_samples')}/{st.get('profiled_samples')} samples{pct_txt} in {st.get('study_name')}"


def describe_variant_result(data: Dict[str, Any], label: Optional[str] = None) -> Tuple[str, str]:
    """(title, detail) for a completed lookup; every part is read from the result data."""
    label = label or variant_label(data)
    statuses = _status_map(data)
    n_ok = sum(1 for s in statuses.values() if s.get("status") == "ok")
    parts: List[str] = []
    for rec in (data.get("clinvar") or [])[:1]:
        parts.append(_clinvar_phrase(rec))
    civic = data.get("civic")
    if isinstance(civic, dict):
        parts.append(f"CIViC: {civic.get('accepted_evidence_total', 0)} accepted evidence items")
    studies = data.get("cbioportal") or []
    if studies:
        parts.append("cBioPortal: " + _study_phrase(studies[0]) + (f" (+{len(studies) - 1} more stud{'ies' if len(studies) > 2 else 'y'})" if len(studies) > 1 else ""))
    g = data.get("gnomad")
    if isinstance(g, dict) and g.get("exome_af") is not None:
        failed = gnomad_qc(g)["exome"]
        qc = f"; failed QC: {', '.join(failed)}" if failed else ""
        parts.append(f"gnomAD AF {fmt_af(g.get('exome_af'))} (exome, germline{qc})")
    for name in ("clinvar", "civic", "cbioportal", "gnomad"):
        s = statuses.get(name)
        if s and s.get("status") != "ok":
            parts.append(f"{SOURCE_LABELS[name]}: {s.get('detail') or s.get('status')}")
    return f"Looked up {label} in {n_ok} of 4 variant databases", " · ".join(parts)


def variant_markdown_summary(data: Dict[str, Any]) -> str:
    """Deterministic section for the no-LLM fallback answer."""
    label = variant_label(data)
    lines = [f"#### Variant & Biomarker Evidence (🧬 Synapta): {label}"]
    for rec in data.get("clinvar") or []:
        lines.append(f"- **ClinVar {rec.get('accession')}** ({rec.get('title')})")
        for cls_key, status_key, traits_key, stars_key, kind in CLINVAR_KINDS:
            if not rec.get(cls_key):
                continue
            review = ", ".join(x for x in (rec.get(status_key) or "review status not given",
                                           _stars_text(clinvar_stars(rec, stars_key, status_key))) if x)
            conditions = _conditions_text(rec.get(traits_key) or [], 4)
            where = f" for {conditions}" if conditions else ""
            lines.append(f"  - {kind.capitalize()}: {rec[cls_key]}{where} ({review})")
    civic = data.get("civic")
    if isinstance(civic, dict):
        lines.append(f"- **CIViC {civic.get('molecular_profile_name')}**: {civic.get('accepted_evidence_total', 0)} accepted evidence items")
        for ev in (civic.get("evidence") or [])[:5]:
            therapy = f", {', '.join(ev.get('therapies') or [])}" if ev.get("therapies") else ""
            pmid = f", PMID {ev['pmid']}" if ev.get("pmid") else ""
            lines.append(
                f"  - {ev.get('eid')} (level {ev.get('evidence_level')}, {ev.get('evidence_type')}, {ev.get('significance')}): "
                f"{ev.get('disease') or 'disease not given'}{therapy}{pmid}"
            )
        for a in civic.get("assertions") or []:
            lines.append(f"  - {a.get('aid')} ({a.get('amp_level') or 'no AMP level'}, {a.get('assertion_type')}): {a.get('disease') or ''}")
    for st in data.get("cbioportal") or []:
        lines.append(f"- **cBioPortal**: {_study_phrase(st)} ({st.get('denominator_basis')})")
    if data.get("cbioportal"):
        lines.append("  - Studies are reported separately because cohorts can overlap.")
    g = data.get("gnomad")
    if isinstance(g, dict):
        lines.append(
            f"- **gnomAD {g.get('variant_id')}**: exome AC {fmt_int(g.get('exome_ac'))} / AN {fmt_int(g.get('exome_an'))}"
            f" (AF {fmt_af(g.get('exome_af'))}); genome AC {fmt_int(g.get('genome_ac'))} / AN {fmt_int(g.get('genome_an'))}; {GNOMAD_NOTE}"
        )
        warning = gnomad_qc_warning(g)
        if warning:
            lines.append(f"  - QC: {warning}.")
        if g.get("flags"):
            lines.append(f"  - gnomAD flags: {', '.join(str(f) for f in g['flags'])}")
    for s in data.get("sources") or []:
        if s.get("status") != "ok":
            lines.append(f"- {SOURCE_LABELS.get(s.get('name'), s.get('name'))} {s.get('status')}: {s.get('detail') or ''}".rstrip(": "))
    return "\n".join(lines)


def compact_artifact(data: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregates only (no citation records, shortened CIViC descriptions) to keep saved chats small."""
    out = copy.deepcopy({k: v for k, v in data.items() if k not in ("citation_records", "additional_variants")})
    civic = out.get("civic")
    if isinstance(civic, dict):
        for ev in civic.get("evidence") or []:
            desc = str(ev.get("description") or "")
            if len(desc) > _ARTIFACT_DESCRIPTION_CHARS:
                ev["description"] = desc[: _ARTIFACT_DESCRIPTION_CHARS - 1].rstrip() + "…"
    # Multi-variant lookups: one card per variant, the first at the top level (unchanged shape).
    extra = [compact_artifact(d) for d in data.get("additional_variants") or [] if isinstance(d, dict)]
    if extra:
        out["additional_variants"] = extra
    return out
