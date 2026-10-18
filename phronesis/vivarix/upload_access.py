"""
Ownership check for uploaded files that Vivarix tools read by path (uploaded SBML models, calibration tables).

The upload store keeps every file at <root>/<sha256(user_id)[:16]>/ds_<12 hex>/<file>. A path is accepted only when
it is the file of a dataset that the current run attached: the run binds (user id, attached dataset ids) with
`phronesis.agentic.sandbox.bound_scope` (the orchestrator does this for every inquiry with attachments), and the
path must resolve, through DatasetStore.resolve_for_user for that user, to exactly that dataset's file. A path in
another user's directory, a dataset the run did not attach, or a call with no bound run scope is rejected
(SEC-008, ORC-013). Unknown and foreign datasets produce the same error.
"""

from __future__ import annotations

import os
from typing import Optional

TABLE_NAMES = ("original.csv", "original.tsv")


class UploadAccessError(ValueError):
    code = "DATA_PATH_REJECTED"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def owned_upload_path(path: Optional[str], *, kind: str) -> str:
    """
    The real path of an uploaded file the current run may read, or UploadAccessError.

    kind: "model" (the dataset's uploaded SBML file) or "table" (the original CSV/TSV of an uploaded table).
    """
    if kind not in ("model", "table"):
        raise ValueError(f"unknown upload kind {kind!r}")
    if not path or "\x00" in str(path):
        raise UploadAccessError("no uploaded file was given")
    from ..agentic.sandbox import DATASET_ID_RE, current_scope
    from ..datasets.store import get_dataset_store

    scope = current_scope()
    if scope is None:
        raise UploadAccessError("uploaded files can only be read by the run they were attached to (no run scope is bound)")
    store = get_dataset_store()
    try:
        real_path = os.path.realpath(str(path))
        real = os.path.normcase(real_path)
        root = _norm(str(store.root))
        inside = os.path.commonpath([real, root]) == root
    except (OSError, ValueError):
        inside = False
    if not inside:
        raise UploadAccessError("the file is not in the upload store")
    parts = os.path.relpath(real, root).split(os.sep)
    if len(parts) != 3:
        raise UploadAccessError("the path is not an uploaded dataset file")
    user_segment, ds_id, filename = parts
    if os.path.normcase(store.user_dir(scope.user_id).name) != user_segment or not DATASET_ID_RE.match(ds_id) or ds_id not in scope.dataset_ids:
        raise UploadAccessError("the file is not one of this question's attachments")
    resolved, _errors = store.resolve_for_user(scope.user_id, [ds_id])
    if not resolved:
        raise UploadAccessError("the file is not one of this question's attachments")
    att = resolved[0]
    if kind == "model":
        expected = [att.model_path] if att.model_path else []
    else:
        folder = os.path.dirname(att.analysis_path) if att.analysis_path else None
        expected = [os.path.join(folder, name) for name in TABLE_NAMES] if folder else []
    if not any(_norm(e) == real for e in expected) or not os.path.isfile(real):
        raise UploadAccessError("the file is not the uploaded " + ("SBML model" if kind == "model" else "table") + " of an attached dataset")
    if kind == "table" and filename.lower() not in TABLE_NAMES:
        raise UploadAccessError("the file is not an uploaded CSV/TSV table")
    return real_path


__all__ = ["TABLE_NAMES", "UploadAccessError", "owned_upload_path"]
