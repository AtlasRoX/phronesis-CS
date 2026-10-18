"""Extension module owned by UPLOADS (feature 8, dataset and model upload). Registers that feature's hooks, sources and tools."""

from __future__ import annotations

import re
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from phronesis.extensions import hooks

_DATASET_ID_RE = re.compile(r"^ds_[0-9a-f]{12}$")

SCOPE_NOTE = (
    "Dataset/model provenance fields are uploader-declared; only geo_verified=true accessions were confirmed "
    "against NCBI GEO."
)

PROMPT_RULE = (
    "Uploaded datasets and models carry uploader-declared provenance (<dataset_provenance declared_by=\"uploader\">, "
    "<vivarix model_source=\"user_upload\">). Attribute it (\"the uploader reports…\"), cite a GEO accession only when "
    "geo_verified=\"true\", never present an uploaded model's sbml_model_id as a BioModels accession, and never infer "
    "consent, IRB approval or de-identification beyond the declared consent_status."
)

_CONSENT_LABELS = {
    "public_deidentified": "public, de-identified",
    "irb_approved": "IRB-approved",
    "consent_obtained_no_irb": "consent obtained, no IRB",
    "not_human_subjects": "not human subjects",
    "unknown": "unknown",
}


def _upload_provenance(lum: Any) -> Optional[Dict[str, Any]]:
    prov = (lum or {}).get("dataset_provenance") if isinstance(lum, dict) else None
    if isinstance(prov, dict) and _DATASET_ID_RE.match(str(prov.get("id") or "")):
        return prov
    return None


def _model_provenance(viv: Any) -> Optional[Dict[str, Any]]:
    prov = (viv or {}).get("provenance") if isinstance(viv, dict) else None
    if isinstance(prov, dict) and prov.get("source") == "user_upload":
        return prov
    return None


def _bool(value: Any) -> str:
    return "true" if value is True else "false"


# -----------------------------------------------------------------------------
# Evidence block
# -----------------------------------------------------------------------------

def lumicell_provenance_xml(lum: Dict[str, Any]) -> str:
    prov = _upload_provenance(lum)
    if prov is None:
        return ""
    attrs = {
        "id": prov.get("id"),
        "name": prov.get("display_name"),
        "sha256_prefix": str(prov.get("sha256") or "")[:12] or None,
        "source": prov.get("source"),
        # An accession NCBI did not confirm is withheld: the verifier treats it as unobserved.
        "geo_accession": prov.get("geo_accession") if prov.get("geo_verified") is True else None,
        "geo_verified": _bool(prov.get("geo_verified")),
        "geo_status": prov.get("geo_status") if prov.get("geo_accession") and prov.get("geo_verified") is not True else None,
        "organism": prov.get("organism"),
        "consent_status": prov.get("consent_status") or "unknown",
        "declared_by": "uploader",
    }
    if prov.get("geo_verified") is True:
        attrs["geo_title"] = prov.get("geo_title")
        attrs["geo_taxon"] = prov.get("geo_taxon")
        attrs["geo_pubmed_ids"] = ",".join(prov.get("geo_pubmedids") or []) or None
    rendered = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in attrs.items() if v is not None)
    return f"<dataset_provenance{rendered}/>"


def vivarix_upload_attrs(viv: Dict[str, Any]) -> Dict[str, str]:
    prov = _model_provenance(viv)
    if prov is None:
        return {}
    out = {"model_source": "user_upload", "declared_by": "uploader"}
    if prov.get("sbml_model_id"):
        out["sbml_model_id"] = str(prov["sbml_model_id"])
    return out


# -----------------------------------------------------------------------------
# Serialization filter: uploader-declared identifiers are not observed evidence
# -----------------------------------------------------------------------------

def _strip_declared_ids(prov: Any, *, model: bool) -> None:
    if not isinstance(prov, dict):
        return
    if prov.get("geo_verified") is not True:
        prov.pop("geo_accession", None)
    if model:
        prov.pop("sbml_model_id", None)


def declared_ids_filter(_tool_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unverified GEO accessions and uploaded SBML model ids before identifier harvest and memory."""
    if _upload_provenance(data) is not None:
        _strip_declared_ids(data["dataset_provenance"], model=False)
    if _model_provenance(data) is not None:
        _strip_declared_ids(data["provenance"], model=True)
    return data


# -----------------------------------------------------------------------------
# Activity steps, artifacts, scope notes
# -----------------------------------------------------------------------------

def lumicell_step(res: Any, _title_ctx: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    if not getattr(res, "success", False):
        return None, []
    prov = _upload_provenance(res.data)
    if prov is None:
        return None, []
    if prov.get("geo_verified") is True and prov.get("geo_accession"):
        return None, [f"GEO {prov['geo_accession']} (verified)"]
    return None, ["provenance declared by uploader"]


def vivarix_step(res: Any, _title_ctx: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    if not getattr(res, "success", False):
        return None, []
    data = res.data if isinstance(res.data, dict) else {}
    prov = _model_provenance(data)
    if prov is None:
        return None, []
    return f"Simulated uploaded model {data.get('model_name') or prov.get('display_name') or data.get('model_id')}", [
        "provenance declared by uploader"
    ]


def datasets_artifact(_tool_results: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    attachments = list(getattr(ctx, "attachments", None) or [])
    if not attachments:
        return {}
    return {"datasets": [a.to_public() for a in attachments]}


def upload_scope_notes(ctx: Any, _tool_results: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    attachments = list(getattr(ctx, "attachments", None) or [])
    if attachments:
        notes.append(SCOPE_NOTE)
    # Lumicell analyzes only the first dataset and Vivarix simulates only the first model.
    used = {id(getattr(ctx, "dataset_attachment", None)), id(getattr(ctx, "model_attachment", None))}
    idle = [a for a in attachments if id(a) not in used]
    if idle:
        names = ", ".join(f"{a.display_name} ({a.id})" for a in idle)
        notes.append(f"Attached but not analyzed in this turn (only one dataset and one model are used per question): {names}.")
    for err in getattr(ctx, "attachment_errors", None) or []:
        notes.append(f"{err}; it was not analyzed, so describe no results from it.")
    return notes


# -----------------------------------------------------------------------------
# Verifier: consent/IRB claims must match the declared consent_status
# -----------------------------------------------------------------------------

_IRB_CLAIM = re.compile(r"\bIRB[\s\-]*(?:approv\w*|cleared)|\bapproved\s+by\s+(?:an?\s+|the\s+)?(?:IRB|institutional\s+review\s+board|ethics\s+committee)", re.I)
_CONSENT_CLAIM = re.compile(r"\b(?:informed\s+consent|consented|patient\s+consent)\b", re.I)
# Only sentences about the upload itself: generic words such as "patients" or "samples" usually describe a cited study.
_DATA_SUBJECT = re.compile(
    r"\b(?:upload\w*|attach\w*|(?:this|the|your)\s+(?:data(?:set)?|atlas|matrix|model|file|cohort)|ds_[0-9a-f]{12})\b", re.I
)
# Negated or absent claims ("not IRB-approved", "no consent was declared", "consent status is unknown").
_NEGATION = re.compile(
    r"\b(?:not|no|never|without|lacks?|unknown|unverified|undeclared|absent|neither|nor|cannot|can't|isn't|wasn't|aren't|weren't)\b",
    re.I,
)


def consent_claims_check(vctx: Any) -> List[Dict[str, Any]]:
    ctx = getattr(vctx, "query_context", None)
    attachments = list(getattr(ctx, "attachments", None) or [])
    if not attachments:
        return []
    statuses = {str((a.public_provenance or {}).get("consent_status") or "unknown") for a in attachments}
    names = [a.display_name.lower() for a in attachments if a.display_name]
    hedge = getattr(vctx, "hedge_pattern", None)
    defects: List[Dict[str, Any]] = []
    for sentence in vctx.sentences:
        lowered = sentence.lower()
        if not (_DATA_SUBJECT.search(sentence) or any(n in lowered for n in names)):
            continue
        if (hedge is not None and hedge.search(sentence)) or _NEGATION.search(sentence):
            continue
        claims_irb = bool(_IRB_CLAIM.search(sentence))
        claims_consent = bool(_CONSENT_CLAIM.search(sentence))
        if claims_irb and "irb_approved" not in statuses:
            kind = "IRB approval"
        elif claims_consent and not statuses & {"irb_approved", "consent_obtained_no_irb"}:
            kind = "informed consent"
        else:
            continue
        declared = ", ".join(_CONSENT_LABELS.get(s, s) for s in sorted(statuses))
        defects.append(
            {
                "category": "PROVENANCE",
                "severity": "MAJOR",
                "description": f"The answer asserts {kind} for the uploaded data, but the uploader declared consent status: {declared}.",
                "suggested_correction": "State only the declared consent status and attribute it to the uploader; do not infer IRB approval or consent.",
                "claim": sentence[:300],
            }
        )
    return defects


def register_hooks() -> None:
    """Register every UPLOADS hook (idempotent); runs at import and can be re-run inside isolated test hooks."""
    hooks.register_evidence_decorator("lumicell_tail", "uploads", lumicell_provenance_xml)
    hooks.register_evidence_decorator("vivarix_attrs", "uploads", vivarix_upload_attrs)
    hooks.register_step_augmenter("lumicell", "uploads", lumicell_step)
    hooks.register_step_augmenter("vivarix", "uploads", vivarix_step)
    hooks.register_artifact_builder("uploads", datasets_artifact)
    hooks.register_scope_note_provider("uploads", upload_scope_notes)
    hooks.register_prompt_rule("uploads", PROMPT_RULE)
    hooks.register_verification_check("consent_claims", consent_claims_check)
    hooks.register_serialization_filter("uploads", declared_ids_filter)


register_hooks()


def register_tools(registry: Any) -> None:
    return None
