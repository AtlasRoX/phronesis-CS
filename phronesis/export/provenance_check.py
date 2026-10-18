"""
Check browser-posted report content against the stored run manifest (PRV-001).

An export names a run id; when that run is in the caller's RunStore the report is labelled "server run store".
The body of the report (tables, citations, task status, verification, question) still arrives from the browser,
so every section that the stored manifest can confirm is compared with it here:

- answer text (sha256, in report_model), verification passed/score/defect count, Vivarix final concentrations;
- clinical-trial rows (NCT id, phases, status, results posted) against the ClinicalTrials.gov digests;
- ChEMBL drug rows (ChEMBL id, max phase) against the ChEMBL profile digests;
- FAERS rows (total and serious report counts) against the openFDA digests;
- ClinVar rows (accession, classification) and cBioPortal rows (mutated/profiled samples, prevalence) against the
  variant digests;
- citation identifiers (PMID, DOI, typed id, URL) against every identifier the run's tool outputs recorded;
- the task status (the stored one is used) and the question (the stored query is used).

Rows that do not match are marked in the table itself; mismatches become notices; sections the manifest cannot
confirm (plots, mechanism text, gnomAD frequencies, claim lists, retraction flags) are listed as "not checked".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

NOT_IN_RUN = "not in stored run"


@dataclass
class CheckResult:
    notices: List[str] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)
    unchecked: List[str] = field(default_factory=list)
    unmatched_citation_keys: Set[str] = field(default_factory=set)
    bad_row_keys: Set[str] = field(default_factory=set)
    mismatch: bool = False

    def mismatch_notice(self, text: str) -> None:
        self.mismatch = True
        self.notices.append(text)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _calls(manifest: Dict[str, Any], *names: str) -> List[Dict[str, Any]]:
    return [
        c for c in manifest.get("tool_calls") or []
        if isinstance(c, dict) and c.get("success") and (not names or c.get("tool_name") in names)
    ]


def _digests(manifest: Dict[str, Any], *names: str) -> List[Dict[str, Any]]:
    return [c.get("output_digest") or {} for c in _calls(manifest, *names) if isinstance(c.get("output_digest"), dict)]


def _norm_phase(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text(value).upper())


def _num(value: Any) -> Optional[float]:
    try:
        if isinstance(value, bool):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _same_number(a: Any, b: Any, rel: float = 1e-9) -> bool:
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= rel * max(1.0, abs(y))


# --- identifiers the run recorded -------------------------------------------------------------------------------

def _collect_strings(value: Any, out: Set[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k).strip().upper())
            _collect_strings(v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_strings(v, out, depth + 1)
    elif isinstance(value, str) and value.strip():
        out.add(value.strip().upper())


def recorded_identifiers(manifest: Dict[str, Any]) -> Set[str]:
    """Every identifier-like string in the run's successful tool outputs (digests and canonical ids), upper-cased."""
    out: Set[str] = set()
    for call in _calls(manifest):
        _collect_strings(call.get("output_digest") or {}, out)
        _collect_strings(call.get("canonical_ids") or {}, out)
    # A PMID is recorded bare; citations may carry "PMID:123".
    out |= {f"PMID:{v}" for v in list(out) if v.isdigit()}
    return out


def _citation_found(row: Dict[str, Any], ids: Set[str]) -> bool:
    candidates = []
    if _text(row.get("pmid")):
        candidates += [_text(row["pmid"]).upper(), f"PMID:{_text(row['pmid'])}".upper()]
    if _text(row.get("doi")):
        candidates.append(_text(row["doi"]).upper())
    if _text(row.get("id")):
        candidates.append(_text(row["id"]).upper())
    if _text(row.get("url")):
        candidates.append(_text(row["url"]).upper().rstrip("/"))
        candidates.append(_text(row["url"]).upper())
    return any(c in ids for c in candidates if c)


# --- per-section checks -----------------------------------------------------------------------------------------

def check_trials(art: Any, manifest: Dict[str, Any]) -> Tuple[Optional[Set[str]], int]:
    """(NCT ids of posted rows the run does not confirm, rows checked), or (None, 0) when the run has no digest."""
    trials = [t for t in (art.get("trials") or []) if isinstance(t, dict)] if isinstance(art, dict) else []
    digests = _digests(manifest, "lithoscribe_clinical_trials_search")
    if not trials or not digests:
        return None, 0
    recorded: Dict[str, str] = {}
    with_results: Set[str] = set()
    for d in digests:
        recorded.update({str(k): str(v) for k, v in (d.get("trials") or {}).items()})
        with_results |= {str(x) for x in d.get("with_results") or []}
    bad: Set[str] = set()
    for t in trials:
        nct = _text(t.get("nct_id"))
        if nct not in recorded:
            bad.add(nct)
            continue
        phases, _, status = recorded[nct].partition("|")
        posted_phases = "/".join(_text(p) for p in t.get("phases") or [])
        if _norm_phase(posted_phases) != _norm_phase(phases) or _text(t.get("overall_status")) != status:
            bad.add(nct)
        elif bool(t.get("has_results")) != (nct in with_results):
            bad.add(nct)
    return bad, len(trials)


def check_drugs(art: Any, manifest: Dict[str, Any]) -> Tuple[Optional[Set[str]], int]:
    chembl = art.get("chembl") if isinstance(art, dict) and isinstance(art.get("chembl"), dict) else {}
    drugs = [d for d in chembl.get("drugs") or [] if isinstance(d, dict)]
    digests = _digests(manifest, "chembl_drug_profile")
    if not drugs or not digests:
        return None, 0
    recorded: Dict[str, Any] = {}
    for d in digests:
        recorded.update({str(k).upper(): v for k, v in (d.get("drugs") or {}).items()})
    bad: Set[str] = set()
    for d in drugs:
        cid = _text(d.get("chembl_id")).upper()
        if cid not in recorded:
            bad.add(cid)
            continue
        phase, stored = d.get("max_phase"), recorded[cid]
        if phase is not None and stored is not None and not _same_number(phase, stored):
            bad.add(cid)
    return bad, len(drugs)


def check_safety(art: Any, manifest: Dict[str, Any]) -> Tuple[Optional[Set[str]], int]:
    safety = [e for e in (art.get("safety") or []) if isinstance(e, dict)] if isinstance(art, dict) else []
    digests = _digests(manifest, "openfda_drug_safety")
    if not safety or not digests:
        return None, 0
    pairs = {(_num(d.get("total_reports")), _num(d.get("serious_reports"))) for d in digests}
    bad: Set[str] = set()
    for e in safety:
        if (_num(e.get("total_reports")), _num(e.get("serious_reports"))) not in pairs:
            bad.add(_text(e.get("ingredient")))
    return bad, len(safety)


def check_variants(art: Any, manifest: Dict[str, Any]) -> Tuple[Optional[Set[str]], int]:
    if not isinstance(art, dict):
        return None, 0
    digests = _digests(manifest, "synapta_variant_intelligence")
    clinvar = [c for c in art.get("clinvar") or [] if isinstance(c, dict)]
    cbio = [c for c in art.get("cbioportal") or [] if isinstance(c, dict)]
    if not (clinvar or cbio) or not digests:
        return None, 0
    rec_clinvar: Dict[str, Any] = {}
    rec_cbio: Dict[str, str] = {}
    for d in digests:
        rec_clinvar.update({str(k): v for k, v in (d.get("clinvar") or {}).items()})
        rec_cbio.update({str(k): str(v) for k, v in (d.get("cbioportal") or {}).items()})
    bad: Set[str] = set()
    for c in clinvar:
        acc = _text(c.get("accession"))
        posted = c.get("germline_classification") or c.get("somatic_clinical_impact")
        if acc not in rec_clinvar or _text(posted) != _text(rec_clinvar[acc]):
            bad.add(acc)
    for c in cbio:
        sid = _text(c.get("study_id"))
        key = _text(c.get("study_name")) or sid  # the cell the table shows
        mut, prof = c.get("mutated_samples"), c.get("profiled_samples")
        if sid not in rec_cbio or rec_cbio[sid] != f"{mut}/{prof}":
            bad.add(key)
            continue
        pct, m, p = _num(c.get("prevalence_percent")), _num(mut), _num(prof)
        if pct is not None and m is not None and p:
            if abs(pct - 100.0 * m / p) > 0.051:  # prevalence is reported to one decimal
                bad.add(key)
    return bad, len(clinvar) + len(cbio)


def _norm_question(text: str) -> str:
    return " ".join(_text(text).split()).strip("\"'“”‘’ ").casefold()


def check_report(
    manifest: Dict[str, Any],
    *,
    artifacts: Dict[str, Any],
    citation_rows: Iterable[Dict[str, Any]],
    posted_task: Any,
    posted_question: str,
    verification: Any,
) -> CheckResult:
    """Compare the browser-posted sections with the stored manifest; see the module docstring."""
    out = CheckResult()

    # Question: the stored query is authoritative.
    stored_query = _text(manifest.get("query"))
    if stored_query and posted_question.strip() and _norm_question(posted_question) != _norm_question(stored_query):
        out.mismatch_notice("The submitted question differs from the stored run's question; the stored question is shown.")
    if stored_query:
        out.checked.append("question")

    # Task status: the stored outcome is authoritative.
    stored_task = manifest.get("task_status") if isinstance(manifest.get("task_status"), dict) else {}
    if stored_task and isinstance(posted_task, dict) and posted_task:
        keys = ("task_status", "requested_operation", "executed_operation", "request_match")
        if any(posted_task.get(k) != stored_task.get(k) for k in keys if k in posted_task or k in stored_task):
            out.mismatch_notice("The submitted task status differs from the stored run; the stored task status is shown.")
    if stored_task:
        out.checked.append("task status")
    elif isinstance(posted_task, dict) and posted_task:
        out.unchecked.append("task status")

    # Verification: defect count (score and pass are compared in report_model._stored_run_mismatches).
    final = (manifest.get("verification") or {}).get("final") or {}
    if verification is not None and final:
        out.checked.append("verification score and defect count")
        n_defects = final.get("n_defects")
        if isinstance(n_defects, int) and len(getattr(verification, "defects", []) or []) != n_defects:
            out.mismatch_notice(
                f"The verification report lists {len(verification.defects)} defect(s); the stored run recorded {n_defects}."
            )
        if getattr(verification, "claims", None):
            out.unchecked.append("claim list")

    # Tables.
    for label, (bad, n) in (
        ("clinical trials", check_trials(artifacts.get("clinical_trials"), manifest)),
        ("ChEMBL drugs", check_drugs(artifacts.get("drug_intel"), manifest)),
        ("FAERS counts", check_safety(artifacts.get("drug_intel"), manifest)),
        ("variant evidence", check_variants(artifacts.get("variant_intel"), manifest)),
    ):
        if bad is None:
            continue
        out.checked.append(label)
        out.bad_row_keys |= {b.upper() for b in bad if b}
        if bad:
            shown = ", ".join(sorted(b for b in bad if b)[:8])
            out.mismatch_notice(
                f"{len(bad)} of {n} {label} row(s) do not match this run's recorded tool outputs ({shown}); they are marked "
                f"'{NOT_IN_RUN}'."
            )
    has_tables = {
        "clinical trials": bool(isinstance(artifacts.get("clinical_trials"), dict) and artifacts["clinical_trials"].get("trials")),
        "ChEMBL drugs": bool(isinstance(artifacts.get("drug_intel"), dict) and (artifacts["drug_intel"].get("chembl") or {}).get("drugs")),
        "FAERS counts": bool(isinstance(artifacts.get("drug_intel"), dict) and artifacts["drug_intel"].get("safety")),
        "variant evidence": bool(isinstance(artifacts.get("variant_intel"), dict)
                                 and (artifacts["variant_intel"].get("clinvar") or artifacts["variant_intel"].get("cbioportal"))),
    }
    for label, present in has_tables.items():
        if present and label not in out.checked:
            out.unchecked.append(label)
    drug = artifacts.get("drug_intel") if isinstance(artifacts.get("drug_intel"), dict) else {}
    if (drug.get("chembl") or {}).get("drugs") if isinstance(drug.get("chembl"), dict) else False:
        out.unchecked.append("drug mechanisms and bioactivities")
    variant = artifacts.get("variant_intel") if isinstance(artifacts.get("variant_intel"), dict) else {}
    if variant.get("gnomad"):
        out.unchecked.append("gnomAD frequencies")
    viv = artifacts.get("vivarix") if isinstance(artifacts.get("vivarix"), dict) else {}
    if viv.get("plot_base64_png"):
        out.unchecked.append("simulation plot")
    if viv.get("initial_concentrations") or viv.get("steady_state"):
        out.unchecked.append("initial and steady-state simulation values")

    # Citations.
    rows = list(citation_rows)
    if rows:
        ids = recorded_identifiers(manifest)
        from .report_model import source_key

        missing = {source_key(r) for r in rows if not _citation_found(r, ids)}
        missing.discard("")
        out.unmatched_citation_keys = missing
        out.checked.append("citation identifiers")
        if missing:
            out.mismatch_notice(
                f"{len(missing)} of {len(rows)} source(s) were not returned by this run's recorded tool calls; they are "
                f"marked '{NOT_IN_RUN}'."
            )
        out.unchecked.append("citation retraction and verification flags")
    out.unchecked = list(dict.fromkeys(out.unchecked))
    return out


def mark_rows(tables: List[Any], bad_keys: Set[str]) -> None:
    """Append the not-in-run marker to the first cell of each table row whose key is in bad_keys."""
    for table in tables:
        for row in getattr(table, "rows", []) or []:
            if not row:
                continue
            cells = [_text(cell).upper() for cell in row[:2]]
            if any(_cell_matches(cell, key) for cell in cells if cell for key in bad_keys):
                row[0] = f"{row[0]} ({NOT_IN_RUN})"


def _cell_matches(cell: str, key: str) -> bool:
    if cell == key:
        return True
    return cell.endswith("…") and len(cell) > 1 and key.startswith(cell[:-1])
