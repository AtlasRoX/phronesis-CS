"""
Integrity checks before deserialising pickle-based artefacts on the legacy Synapta path (KG-015 / DEP-001).

joblib.load / pickle.load execute code embedded in the file, so a pickle is only loaded after its bytes have been
matched against a SHA-256 the operator pinned. Two policies:

- Downloaded artefacts (BioBridge embeddings, StarkQA node_info.pkl): load_verified_pickle() requires an expected
  hash; there is no "trust anyway" switch, because the bytes come from a third-party, mutable location.
- Operator-configured local graph files (kg_pyg_path / kg_text_path): load_local_pickle() takes the expected hash
  from the caller (source-graph config), else from a `<file>.sha256` sidecar, else only when the operator has set
  SYNAPTA_TRUST_LOCAL_PICKLES=1 for files they built themselves. Otherwise it refuses.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

TRUST_LOCAL_PICKLES_ENV = "SYNAPTA_TRUST_LOCAL_PICKLES"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

PathLike = Union[str, "os.PathLike[str]"]


class IntegrityError(ValueError):
    """A file's bytes do not match the pinned SHA-256, or no hash was pinned for a file that needs one."""


def sha256_file(path: PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalise(expected: Optional[str]) -> Optional[str]:
    if expected is None:
        return None
    value = str(expected).strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    if not _HEX64.match(value):
        raise IntegrityError(f"invalid SHA-256 value {expected!r}: expected 64 hexadecimal characters")
    return value


def verify_sha256(path: PathLike, expected: Optional[str]) -> str:
    """Return the file's SHA-256 when it equals `expected`; raise IntegrityError otherwise (also when no
    expected value is given)."""
    want = _normalise(expected)
    if want is None:
        raise IntegrityError(f"no pinned SHA-256 for {os.fspath(path)}; refusing to trust it")
    if not Path(path).is_file():
        raise IntegrityError(f"{os.fspath(path)} does not exist")
    got = sha256_file(path)
    if got != want:
        raise IntegrityError(
            f"SHA-256 mismatch for {os.fspath(path)}: expected {want}, got {got}. The file may be truncated, "
            "replaced upstream or tampered with; it was not loaded."
        )
    return got


def _joblib_load(path: PathLike) -> Any:
    import joblib

    # Reviewed: called only after verify_sha256 (or an explicit operator trust flag) succeeded.
    return joblib.load(path)  # nosec B301


def load_verified_pickle(path: PathLike, expected_sha256: Optional[str]) -> Any:
    """joblib.load a downloaded artefact only after its SHA-256 matches the pinned value."""
    verify_sha256(path, expected_sha256)
    return _joblib_load(path)


def _sidecar_hash(path: PathLike) -> Optional[str]:
    sidecar = Path(os.fspath(path) + ".sha256")
    if not sidecar.is_file():
        return None
    text = sidecar.read_text(encoding="utf-8").strip()
    return text.split()[0] if text else None


def _trust_flag() -> bool:
    return os.environ.get(TRUST_LOCAL_PICKLES_ENV, "").strip().lower() in {"1", "true", "yes"}


def load_local_pickle(path: PathLike, expected_sha256: Optional[str] = None) -> Any:
    """joblib.load an operator-configured local file: verified against `expected_sha256`, else against a
    `<file>.sha256` sidecar; without either, only when SYNAPTA_TRUST_LOCAL_PICKLES is set."""
    expected = expected_sha256 or _sidecar_hash(path)
    if expected:
        verify_sha256(path, expected)
        return _joblib_load(path)
    if _trust_flag():
        logger.warning("Loading unverified pickle %s because %s is set", os.fspath(path), TRUST_LOCAL_PICKLES_ENV)
        return _joblib_load(path)
    raise IntegrityError(
        f"refusing to unpickle {os.fspath(path)}: no SHA-256 pinned. Provide the expected hash in the source-graph "
        f"config (kg_pyg_sha256 / kg_text_sha256), write a '{Path(path).name}.sha256' sidecar, or set "
        f"{TRUST_LOCAL_PICKLES_ENV}=1 for files you built yourself."
    )
