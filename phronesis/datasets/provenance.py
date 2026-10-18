"""Post-process Lumicell/Vivarix results for attached datasets (owned by UPLOADS, feature 8).

Absolute server paths never leave the server: attachment paths become 'upload://<id>/<name>' and typed
paths their basename. The uploader's public provenance is merged into the result so the evidence block,
the verifier and the client all see the same declared values.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from phronesis.agents.query_context import QueryContext
from phronesis.tools.registry import ToolResult


def _basename(path: Any) -> str:
    return re.split(r"[\\/]", str(path))[-1]


def _redact_text(text: Optional[str], path: Optional[str], replacement: str) -> Optional[str]:
    if not text or not path:
        return text
    # Library errors may quote the file, its directory or the upload root, with either separator.
    candidates = [path, os.path.dirname(path) if os.path.isabs(path) else ""]
    try:
        from .store import default_upload_root

        candidates.append(str(default_upload_root().resolve()))
    except Exception:  # noqa: BLE001
        pass
    for i, candidate in enumerate(candidates):
        if not candidate or len(candidate) < 4:
            continue
        sub = replacement if i == 0 else "<server path>"
        for variant in dict.fromkeys((candidate, candidate.replace("\\", "/"), candidate.replace("\\", "\\\\"))):
            text = text.replace(variant, sub)
    return text


# Values Lumicell reads from the file itself (adata.uns); a differing declared value must not overwrite them.
_FILE_DERIVED_KEYS = ("organism", "tissue", "disease", "technology", "donor_id")


def _merge_declared(out: Dict[str, Any], declared: Dict[str, Any]) -> None:
    for key, value in declared.items():
        if key in _FILE_DERIVED_KEYS and key in out:
            if value is None or str(value) == str(out[key]):
                continue
            out[f"file_{key}"] = out[key]
        out[key] = value


def _merge_canonical(res: ToolResult, key: str, values: list) -> None:
    values = [str(v) for v in values if v]
    if not values:
        return
    existing = [v for v in str(res.canonical_ids.get(key) or "").split(",") if v]
    res.canonical_ids[key] = ",".join(dict.fromkeys(existing + values))


def postprocess_lumicell_result(res: ToolResult, ctx: QueryContext) -> ToolResult:
    att = getattr(ctx, "dataset_attachment", None) if ctx is not None else None
    source_path = att.analysis_path if att is not None else getattr(ctx, "dataset_path", None)
    public_ref = f"upload://{att.id}/{att.display_name}" if att is not None else None

    def redact_path(value: Any) -> str:
        return public_ref if public_ref else _basename(value)

    def redact_provenance(prov: Any) -> Dict[str, Any]:
        out = dict(prov) if isinstance(prov, dict) else {}
        if "file_path" in out:
            out["file_path"] = att.display_name if att is not None else _basename(out["file_path"])
        if att is not None:
            _merge_declared(out, att.public_provenance)
            out["dataset_ref"] = public_ref
        return out

    if isinstance(res.data, dict):
        data = dict(res.data)
        if data.get("dataset_path"):
            data["dataset_path"] = redact_path(data["dataset_path"])
        if "dataset_provenance" in data or att is not None:
            data["dataset_provenance"] = redact_provenance(data.get("dataset_provenance"))
        res.data = data
    if isinstance(res.metadata, dict):
        meta = dict(res.metadata)
        if meta.get("dataset_path"):
            meta["dataset_path"] = redact_path(meta["dataset_path"])
        if "dataset_provenance" in meta or att is not None:
            meta["dataset_provenance"] = redact_provenance(meta.get("dataset_provenance"))
        res.metadata = meta
    if source_path:
        res.error = _redact_text(res.error, str(source_path), public_ref or _basename(source_path))

    if att is not None:
        _merge_canonical(res, "dataset_ids", [att.id])
        if att.geo_verified:
            # The accession exists in GEO, but only the uploader links it to this file (SCI-014): its PubMed ids are
            # a declared source, not citations for the result, so they are kept apart from the canonical "pmids".
            _merge_canonical(res, "geo_accessions", [att.geo_accession])
            _merge_canonical(res, "declared_source_pmids", list(att.pmids))
    return res


def postprocess_vivarix_result(res: ToolResult, ctx: QueryContext) -> ToolResult:
    att = getattr(ctx, "model_attachment", None) if ctx is not None else None
    if att is None:
        return res
    if isinstance(res.data, dict):
        data = dict(res.data)
        prov = dict(data.get("provenance") or {})
        prov.update(att.public_provenance)
        prov["declared_source"] = att.public_provenance.get("source")
        prov["source"] = "user_upload"
        prov.setdefault("name", att.display_name)
        data["provenance"] = prov
        data["model_source"] = "user_upload"
        res.data = data
    if att.model_path:
        res.error = _redact_text(res.error, att.model_path, f"upload://{att.id}/{att.display_name}")
    _merge_canonical(res, "dataset_ids", [att.id])
    return res
