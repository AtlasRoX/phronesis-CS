"""Resolve dataset ids attached to a question into DatasetRefs (owned by UPLOADS, feature 8)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import List, Sequence

from phronesis.agents.query_context import DatasetRef

TYPED_ID_RE = re.compile(r"\bds_[0-9a-f]{12}\b")
MAX_ATTACHMENTS = 5


@dataclass
class AttachmentResolution:
    attachments: List[DatasetRef] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _requested_ids(dataset_ids: Sequence[str], query: str) -> List[str]:
    ids = [str(i) for i in (dataset_ids or [])] + TYPED_ID_RE.findall(query or "")
    return list(dict.fromkeys(ids))[:MAX_ATTACHMENTS]


async def resolve_attachments(user_id: str, dataset_ids: Sequence[str], query: str) -> AttachmentResolution:
    """Resolve the caller's dataset ids; unknown and foreign ids get the same 'not found' error."""
    ids = _requested_ids(dataset_ids, query)
    if not ids:
        return AttachmentResolution()
    from .store import existing_dataset_store

    store = existing_dataset_store()
    if store is None:
        return AttachmentResolution(errors=[f"Attached dataset {i[:40]} not found" for i in ids])
    resolved, errors = await asyncio.to_thread(store.resolve_for_user, user_id, ids)
    refs: List[DatasetRef] = []
    for att in resolved:
        record = att.record
        geo_verified = record.geo_verified
        refs.append(
            DatasetRef(
                id=record.id,
                kind=record.kind.value,
                display_name=record.display_name,
                analysis_path=att.analysis_path,
                model_path=att.model_path,
                public_provenance=record.public_provenance(),
                dims=record.dims(),
                geo_accession=record.provenance.geo_accession,
                geo_verified=geo_verified,
                pmids=record.geo_declared_pmids,  # declared source only (SCI-014)
                sbml_model_id=record.validation.sbml_model_id,
            )
        )
    return AttachmentResolution(attachments=refs, errors=errors)
