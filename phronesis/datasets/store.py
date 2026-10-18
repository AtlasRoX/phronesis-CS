"""
Per-user dataset store on local disk:

    <root>/<sha256(user_id)[:16]>/ds_<12 hex>/{original.<ext>, data.h5ad (tables only), manifest.json}

User ids may contain ':' (valid in auth ids, illegal in Windows paths), so user directories are hashed.
Every path is server-generated and checked to lie under the root; uploader file names are kept only as a
sanitized display_name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterable, Dict, Iterable, List, Optional, Tuple, Union

from .geo import lookup_geo, organism_matches
from .models import (
    DATASET_ID_RE,
    DatasetKind,
    DatasetProvenanceIn,
    DatasetRecord,
    GeoVerification,
    ResolvedAttachment,
)
from .validators import ValidationFailure, validate_h5ad, validate_sbml, validate_table

logger = logging.getLogger(__name__)

EXTENSION_KINDS = {
    ".h5ad": DatasetKind.H5AD,
    ".csv": DatasetKind.TABLE,
    ".tsv": DatasetKind.TABLE,
    ".xml": DatasetKind.SBML,
    ".sbml": DatasetKind.SBML,
}
CHUNK_BYTES = 1 << 20
_MB = 1024 * 1024
_ORPHAN_AGE_S = 3600
_MANIFEST = "manifest.json"
_PART = "upload.part"
_ANALYSIS = "data.h5ad"

Chunks = Union[Iterable[bytes], AsyncIterable[bytes]]


class DatasetStoreError(Exception):
    """An upload or library failure with the HTTP status and stable reason code the API returns."""

    def __init__(self, status_code: int, detail: str, reason_code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.reason_code = reason_code


class DatasetNotFound(DatasetStoreError):
    def __init__(self, ds_id: str) -> None:
        super().__init__(404, f"Dataset {str(ds_id)[:40]} not found", "not_found")


def default_upload_root() -> Path:
    env = os.getenv("PHRONESIS_UPLOAD_ROOT")
    if env:
        return Path(env)
    from phronesis.utils.paths import data_dir

    return data_dir("uploads") or Path(__file__).resolve().parents[2] / ".phronesis_uploads"


def _env_mb(name: str, default_mb: float) -> int:
    try:
        return int(float(os.getenv(name, default_mb)) * _MB)
    except ValueError:
        return int(default_mb * _MB)


def kind_cap_bytes(kind: DatasetKind) -> int:
    if kind == DatasetKind.H5AD:
        return _env_mb("PHRONESIS_UPLOAD_MAX_MB_H5AD", 500)
    if kind == DatasetKind.TABLE:
        return _env_mb("PHRONESIS_UPLOAD_MAX_MB_CSV", 100)
    return _env_mb("PHRONESIS_UPLOAD_MAX_MB_SBML", 10)


def max_upload_bytes() -> int:
    """The largest single upload any kind allows; the API rejects larger Content-Length before parsing."""
    return max(kind_cap_bytes(k) for k in DatasetKind)


def total_cap_bytes() -> int:
    return _env_mb("PHRONESIS_UPLOAD_MAX_TOTAL_MB", 2048)


def max_datasets() -> int:
    try:
        return int(os.getenv("PHRONESIS_UPLOAD_MAX_DATASETS", "50"))
    except ValueError:
        return 50


def sanitize_display_name(filename: str) -> str:
    """Basename only, with path separators, control characters and reserved characters removed."""
    name = (filename or "").replace("\x00", "")
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[^\w.\-()+ ]", "_", name).strip(" .")
    if len(name) > 128:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 127 - len(ext)] + "." + ext) if dot and len(ext) <= 8 else name[:128]
    return name or "upload"


def extension_of(filename: str) -> str:
    name = sanitize_display_name(filename)
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        real, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([real, base]) == base
    except ValueError:  # different drives on Windows
        return False


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


async def _iterate(chunks: Chunks):
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:  # type: ignore[union-attr]
            yield chunk
    else:
        for chunk in chunks:  # type: ignore[union-attr]
            yield chunk


class DatasetStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or default_upload_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._user_locks: Dict[str, threading.Lock] = {}
        self.sweep_orphans()

    # -- layout ---------------------------------------------------------------
    def user_dir(self, user_id: str) -> Path:
        path = self.root / hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
        if not _is_within(path, self.root):
            raise DatasetStoreError(500, "Upload storage path check failed", "storage_error")
        return path

    def _dataset_dir(self, user_id: str, ds_id: str) -> Path:
        if not isinstance(ds_id, str) or not DATASET_ID_RE.match(ds_id):
            raise DatasetNotFound(ds_id)
        path = self.user_dir(user_id) / ds_id
        if not _is_within(path, self.root):
            raise DatasetNotFound(ds_id)
        return path

    @staticmethod
    def _original_name(record: DatasetRecord) -> str:
        return f"original{record.original_extension}"

    def file_paths(self, user_id: str, record: DatasetRecord) -> Tuple[Optional[str], Optional[str]]:
        """(analysis_path, model_path) for one of the user's records, after the root check."""
        base = self._dataset_dir(user_id, record.id)
        if record.kind == DatasetKind.SBML:
            model = base / self._original_name(record)
            return None, str(model) if _is_within(model, self.root) and model.exists() else None
        target = base / (_ANALYSIS if record.kind == DatasetKind.TABLE else self._original_name(record))
        return (str(target) if _is_within(target, self.root) and target.exists() else None), None

    def sweep_orphans(self) -> None:
        """Remove .part files and manifest-less dataset dirs left by crashed uploads (older than an hour)."""
        cutoff = time.time() - _ORPHAN_AGE_S
        try:
            user_dirs = [p for p in self.root.iterdir() if p.is_dir()]
        except OSError:
            return
        for udir in user_dirs:
            for entry in udir.iterdir():
                try:
                    if entry.is_dir() and entry.name.endswith(".deleting"):
                        shutil.rmtree(entry, ignore_errors=True)
                        continue
                    if entry.stat().st_mtime > cutoff:
                        continue  # possibly an upload in progress
                    if entry.is_dir() and not (entry / _MANIFEST).exists():
                        shutil.rmtree(entry, ignore_errors=True)
                    elif entry.is_file():
                        entry.unlink()
                    elif entry.is_dir():
                        for leftover in [*entry.glob("*.part"), *entry.glob("*.tmp")]:
                            leftover.unlink()
                except OSError as exc:
                    logger.debug("Orphan sweep skipped %s: %s", entry.name, exc)

    # -- quota ----------------------------------------------------------------
    def usage_bytes(self, user_id: str) -> int:
        udir = self.user_dir(user_id)
        if not udir.exists():
            return 0
        total = 0
        for dirpath, _dirs, files in os.walk(udir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def _committed_usage_bytes(self, user_id: str, own_dir: Path) -> int:
        """Bytes of the user's committed datasets plus own_dir (other in-flight uploads are not counted)."""
        udir = self.user_dir(user_id)
        if not udir.exists():
            return 0
        total = 0
        for entry in udir.iterdir():
            if not entry.is_dir() or not (entry == own_dir or (entry / _MANIFEST).exists()):
                continue
            for dirpath, _dirs, files in os.walk(entry):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass
        return total

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._user_locks.setdefault(self.user_dir(user_id).name, threading.Lock())

    # -- create ---------------------------------------------------------------
    async def create_from_stream(
        self,
        user_id: str,
        chunks: Chunks,
        filename: str,
        provenance: DatasetProvenanceIn,
    ) -> DatasetRecord:
        display_name = sanitize_display_name(filename)
        ext = extension_of(filename)
        kind = EXTENSION_KINDS.get(ext)
        if kind is None:
            raise DatasetStoreError(
                415, f"Unsupported file type '{ext or display_name}'. Upload .h5ad, .csv, .tsv, .xml or .sbml.", "unsupported_type"
            )
        # Disk work (directory scans, file writes, fsync) runs in worker threads so uploads never block the
        # event loop that serves other requests.
        if len(await asyncio.to_thread(self.list, user_id)) >= max_datasets():
            raise DatasetStoreError(507, f"Dataset limit reached ({max_datasets()} per user); delete one first.", "quota_exceeded")
        kind_cap = kind_cap_bytes(kind)
        remaining = total_cap_bytes() - await asyncio.to_thread(self.usage_bytes, user_id)

        ds_id = "ds_" + secrets.token_hex(6)
        ds_dir = self._dataset_dir(user_id, ds_id)
        ds_dir.mkdir(parents=True, exist_ok=False)
        part = ds_dir / _PART
        try:
            digest = hashlib.sha256()
            size = 0
            fh = await asyncio.to_thread(open, part, "wb")
            try:
                async for chunk in _iterate(chunks):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > kind_cap:
                        raise DatasetStoreError(
                            413, f"{kind.value} uploads are limited to {kind_cap // _MB} MB.", "too_large"
                        )
                    if size > remaining:
                        raise DatasetStoreError(
                            507, f"Upload storage quota ({total_cap_bytes() // _MB} MB per user) exceeded.", "quota_exceeded"
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(fh.write, chunk)
            finally:
                await asyncio.to_thread(fh.close)
            if size == 0:
                raise DatasetStoreError(422, "The uploaded file is empty.", "empty_file")

            final = ds_dir / f"original{ext}"
            derived_sha: Optional[str] = None
            try:
                if kind == DatasetKind.H5AD:
                    summary = await asyncio.to_thread(validate_h5ad, part)
                elif kind == DatasetKind.TABLE:
                    summary, derived_sha = await asyncio.to_thread(
                        validate_table, part, ext, provenance.table_orientation, ds_dir / _ANALYSIS
                    )
                else:
                    summary = await asyncio.to_thread(validate_sbml, part)
            except ValidationFailure as exc:
                raise DatasetStoreError(422, exc.detail, exc.reason_code) from exc
            if kind == DatasetKind.TABLE and provenance.table_orientation is None and summary.table_orientation:
                # The orientation inferred from the labels is the one applied; record it like a declared one.
                provenance = provenance.model_copy(update={"table_orientation": summary.table_orientation})

            geo = await lookup_geo(provenance.geo_accession, provenance.organism) if provenance.geo_accession else GeoVerification()
            if geo.status == "verified" and geo.organism_matches is False:
                summary.warnings.append(f"declared organism '{provenance.organism}' differs from GEO taxon '{geo.taxon}'")
            record = DatasetRecord(
                id=ds_id,
                kind=kind,
                display_name=display_name,
                original_extension=ext,
                size_bytes=size,
                sha256=digest.hexdigest(),
                derived_sha256=derived_sha,
                uploaded_at=datetime.now(timezone.utc),
                provenance=provenance,
                geo_verification=geo,
                validation=summary,
            )
            await asyncio.to_thread(self._commit, user_id, ds_dir, part, final, record)
            return record
        except BaseException:
            shutil.rmtree(ds_dir, ignore_errors=True)
            raise

    def _commit(self, user_id: str, ds_dir: Path, part: Path, final: Path, record: DatasetRecord) -> None:
        # Concurrent uploads each passed the early checks; re-check against what is committed now and
        # commit (manifest written) under the user's lock so limits hold across parallel requests.
        with self._user_lock(user_id):
            if len(self.list(user_id)) >= max_datasets():
                raise DatasetStoreError(507, f"Dataset limit reached ({max_datasets()} per user); delete one first.", "quota_exceeded")
            if self._committed_usage_bytes(user_id, ds_dir) > total_cap_bytes():
                raise DatasetStoreError(
                    507, f"Upload storage quota ({total_cap_bytes() // _MB} MB per user) exceeded.", "quota_exceeded"
                )
            os.replace(part, final)
            _write_json_atomic(ds_dir / _MANIFEST, record.model_dump(mode="json"))

    # -- read -----------------------------------------------------------------
    def _read_manifest(self, ds_dir: Path) -> Optional[DatasetRecord]:
        try:
            with open(ds_dir / _MANIFEST, "r", encoding="utf-8") as fh:
                return DatasetRecord.model_validate(json.load(fh))
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable dataset manifest in %s: %s", ds_dir.name, exc)
            return None

    def list(self, user_id: str) -> List[DatasetRecord]:
        udir = self.user_dir(user_id)
        if not udir.exists():
            return []
        out = []
        for entry in udir.iterdir():
            if entry.is_dir() and DATASET_ID_RE.match(entry.name) and (entry / _MANIFEST).exists():
                record = self._read_manifest(entry)
                if record is not None and record.id == entry.name:
                    out.append(record)
        return sorted(out, key=lambda r: r.uploaded_at, reverse=True)

    def get(self, user_id: str, ds_id: str) -> DatasetRecord:
        ds_dir = self._dataset_dir(user_id, ds_id)
        if not (ds_dir / _MANIFEST).exists():
            raise DatasetNotFound(ds_id)
        record = self._read_manifest(ds_dir)
        if record is None or record.id != ds_id:
            raise DatasetNotFound(ds_id)
        return record

    def resolve_for_user(self, user_id: str, ids: Iterable[str]) -> Tuple[List[ResolvedAttachment], List[str]]:
        """Resolve ids owned by user_id; unknown and foreign ids produce the same error."""
        found: List[ResolvedAttachment] = []
        errors: List[str] = []
        for ds_id in ids:
            try:
                record = self.get(user_id, ds_id)
                analysis_path, model_path = self.file_paths(user_id, record)
                if not (analysis_path or model_path):
                    raise DatasetNotFound(ds_id)
            except DatasetStoreError:
                errors.append(f"Attached dataset {str(ds_id)[:40]} not found")
                continue
            found.append(ResolvedAttachment(record=record, analysis_path=analysis_path, model_path=model_path))
        return found, errors

    # -- update / delete --------------------------------------------------------
    async def update_provenance(self, user_id: str, ds_id: str, provenance: DatasetProvenanceIn) -> DatasetRecord:
        snapshot = await asyncio.to_thread(self.get, user_id, ds_id)
        # The orientation was applied when the table was converted; it cannot change afterwards.
        provenance = provenance.model_copy(update={"table_orientation": snapshot.provenance.table_orientation})
        geo = snapshot.geo_verification
        if provenance.geo_accession != snapshot.provenance.geo_accession or geo.status == "lookup_failed":
            geo = await lookup_geo(provenance.geo_accession, provenance.organism) if provenance.geo_accession else GeoVerification()
        elif geo.status == "verified":
            geo = geo.model_copy(update={"organism_matches": organism_matches(provenance.organism, geo.taxon)})
        return await asyncio.to_thread(self._write_provenance, user_id, ds_id, provenance, geo)

    def _write_provenance(
        self, user_id: str, ds_id: str, provenance: DatasetProvenanceIn, geo: GeoVerification
    ) -> DatasetRecord:
        # Read-modify-write under the user's lock so concurrent edits each land in the history.
        with self._user_lock(user_id):
            record = self.get(user_id, ds_id)
            previous = {
                "provenance": record.provenance.model_dump(mode="json"),
                "geo_status": record.geo_verification.status,
            }
            history = list(record.provenance_history) + [
                {"changed_at": datetime.now(timezone.utc).isoformat(), "previous": previous}
            ]
            updated = record.model_copy(update={"provenance": provenance, "geo_verification": geo, "provenance_history": history[-50:]})
            updated = DatasetRecord.model_validate(updated.model_dump(mode="json"))
            _write_json_atomic(self._dataset_dir(user_id, ds_id) / _MANIFEST, updated.model_dump(mode="json"))
            return updated

    def delete(self, user_id: str, ds_id: str) -> None:
        self.get(user_id, ds_id)
        ds_dir = self._dataset_dir(user_id, ds_id)
        # Renaming first makes the delete all-or-nothing: Windows refuses it while a file inside is open.
        doomed = ds_dir.with_name(f"{ds_id}.{secrets.token_hex(3)}.deleting")
        try:
            os.replace(ds_dir, doomed)
        except PermissionError as exc:
            raise DatasetStoreError(409, "The dataset is in use by a running analysis; try again shortly.", "in_use") from exc
        shutil.rmtree(doomed, ignore_errors=True)


_STORE: Optional[DatasetStore] = None
_STORE_LOCK = threading.Lock()


def get_dataset_store() -> DatasetStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DatasetStore()
        return _STORE


def existing_dataset_store() -> Optional[DatasetStore]:
    """The store if anything was ever uploaded (or it is already open); never creates the upload root."""
    if _STORE is not None or default_upload_root().exists():
        return get_dataset_store()
    return None


def reset_dataset_store() -> None:
    """Forget the singleton so the next call re-reads PHRONESIS_UPLOAD_ROOT (tests)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
