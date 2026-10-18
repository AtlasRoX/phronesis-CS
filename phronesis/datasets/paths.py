"""Typed dataset path policy (owned by UPLOADS, feature 8)."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

_warned_default_root = False


def _is_within(real: str, root: str) -> bool:
    try:
        base = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([os.path.normcase(real), base]) == base
    except ValueError:  # different drives on Windows
        return False


def allowed_dataset_roots() -> List[str]:
    """PHRONESIS_DATASET_ROOTS (os.pathsep-separated); <working directory>/data when unset."""
    global _warned_default_root
    raw = os.getenv("PHRONESIS_DATASET_ROOTS", "")
    roots = [r for r in raw.split(os.pathsep) if r.strip()]
    if roots:
        return roots
    if not _warned_default_root:
        _warned_default_root = True
        logger.warning(
            "PHRONESIS_DATASET_ROOTS is not set; typed dataset paths are limited to %s",
            os.path.join(os.getcwd(), "data"),
        )
    return [os.path.join(os.getcwd(), "data")]


def _candidates(path: str) -> List[str]:
    """Real paths a typed path may refer to: absolute as given, relative under each root."""
    if os.path.isabs(path):
        return [os.path.realpath(path)]
    out = [os.path.realpath(os.path.join(root, path)) for root in allowed_dataset_roots()]
    out.append(os.path.realpath(path))  # relative to the working directory, if that lies under a root
    return list(dict.fromkeys(out))


def _allowed(real: str) -> bool:
    from .store import default_upload_root

    if _is_within(real, str(default_upload_root())):
        return False
    return any(_is_within(real, root) for root in allowed_dataset_roots())


def is_allowed_typed_path(path: str) -> bool:
    """Whether a dataset path typed into a question may be read by the server.

    Only paths under the configured dataset roots qualify, and never paths inside the upload store:
    uploads are reachable only through their owner's dataset id. Relative paths are taken relative to each
    root (and to the working directory when that lies under a root).
    """
    return resolve_typed_path(path) is not None


def resolve_typed_path(path: str) -> Optional[str]:
    """The allowed real path a typed dataset path refers to (an existing file first), or None."""
    if not path or "\x00" in path:
        return None
    try:
        allowed = [real for real in _candidates(path) if _allowed(real)]
    except (OSError, ValueError):
        return None
    if not allowed:
        return None
    return next((real for real in allowed if os.path.exists(real)), allowed[0])
