"""
Dataset upload and library endpoints (owned by UPLOADS, feature 8).

The upload route takes the raw Request instead of declaring UploadFile/Form parameters: FastAPI parses
declared multipart bodies before running dependencies, so an unauthenticated client could otherwise spool
an arbitrarily large body to disk before receiving 401. Authentication runs first, then the Content-Length
cap, then a bounded multipart parse. There is deliberately no download endpoint.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.backend.auth import get_current_user
from phronesis.datasets.geo import GSE_RE, lookup_geo
from phronesis.datasets.models import DATASET_ID_RE, DatasetProvenanceIn
from phronesis.datasets.store import (
    CHUNK_BYTES,
    EXTENSION_KINDS,
    DatasetStoreError,
    extension_of,
    get_dataset_store,
    max_upload_bytes,
)

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

_MULTIPART_OVERHEAD = 64 * 1024


def _error(status_code: int, detail: str, reason_code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "reason_code": reason_code})


def _store_error(exc: DatasetStoreError) -> JSONResponse:
    return _error(exc.status_code, exc.detail, exc.reason_code)


def _check_id(ds_id: str) -> None:
    if not DATASET_ID_RE.match(ds_id or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Invalid provenance: " + "; ".join(parts)


def _parse_provenance(raw: Any) -> DatasetProvenanceIn:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("The 'provenance' field (a JSON object) is required.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The 'provenance' field is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The 'provenance' field must be a JSON object.")
    try:
        return DatasetProvenanceIn.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_dataset(request: Request, user_id: str = Depends(get_current_user)):
    length_header = request.headers.get("content-length")
    if length_header is None:
        return _error(status.HTTP_411_LENGTH_REQUIRED, "Content-Length is required for uploads.", "length_required")
    try:
        declared = int(length_header)
    except ValueError:
        return _error(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length.", "bad_request")
    if declared > max_upload_bytes() + _MULTIPART_OVERHEAD:
        limit_mb = max_upload_bytes() // (1024 * 1024)
        return _error(status.HTTP_413_CONTENT_TOO_LARGE, f"Uploads are limited to {limit_mb} MB.", "too_large")
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return _error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send the file as multipart/form-data.", "unsupported_type")

    store = get_dataset_store()
    try:
        async with request.form(max_files=1, max_fields=8, max_part_size=64 * 1024) as form:
            upload = form.get("file")
            if upload is None or isinstance(upload, str) or not getattr(upload, "filename", None):
                return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "The 'file' field is required.", "missing_file")
            ext = extension_of(upload.filename)
            if ext not in EXTENSION_KINDS:
                return _error(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "Unsupported file type. Upload .h5ad, .csv, .tsv, .xml or .sbml.",
                    "unsupported_type",
                )
            try:
                provenance = _parse_provenance(form.get("provenance"))
            except ValueError as exc:
                return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc), "invalid_provenance")

            async def chunks() -> AsyncIterator[bytes]:
                while True:
                    block = await upload.read(CHUNK_BYTES)
                    if not block:
                        return
                    yield block

            record = await store.create_from_stream(user_id, chunks(), upload.filename, provenance)
    except DatasetStoreError as exc:
        return _store_error(exc)
    except AssertionError:
        # Starlette asserts that python-multipart is importable; malformed bodies surface as HTTPException 400.
        return _error(status.HTTP_501_NOT_IMPLEMENTED, "Multipart uploads are not available on this server.", "multipart_unavailable")
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=record.to_api())


@router.get("")
async def list_datasets(user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    records = await asyncio.to_thread(get_dataset_store().list, user_id)
    return {"datasets": [r.to_api() for r in records], "count": len(records)}


@router.get("/{ds_id}")
async def get_dataset(ds_id: str, user_id: str = Depends(get_current_user)):
    _check_id(ds_id)
    try:
        record = await asyncio.to_thread(get_dataset_store().get, user_id, ds_id)
    except DatasetStoreError as exc:
        return _store_error(exc)
    return record.to_api()


@router.patch("/{ds_id}/provenance")
async def update_dataset_provenance(ds_id: str, request: Request, user_id: str = Depends(get_current_user)):
    _check_id(ds_id)
    store = get_dataset_store()
    try:
        await asyncio.to_thread(store.get, user_id, ds_id)
    except DatasetStoreError as exc:
        return _store_error(exc)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "The body must be a JSON object.", "invalid_provenance")
    if not isinstance(body, dict):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "The body must be a JSON object.", "invalid_provenance")
    try:
        provenance = DatasetProvenanceIn.model_validate(body)
    except ValidationError as exc:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, _validation_message(exc), "invalid_provenance")
    try:
        record = await store.update_provenance(user_id, ds_id, provenance)
    except DatasetStoreError as exc:
        return _store_error(exc)
    return record.to_api()


@router.delete("/{ds_id}")
async def delete_dataset(ds_id: str, user_id: str = Depends(get_current_user)):
    _check_id(ds_id)
    try:
        await asyncio.to_thread(get_dataset_store().delete, user_id, ds_id)
    except DatasetStoreError as exc:
        return _store_error(exc)
    return {"status": "deleted", "id": ds_id}


class GeoLookupRequest(BaseModel):
    accession: str = Field(..., max_length=32)


@router.post("/geo-lookup")
async def geo_lookup(req: GeoLookupRequest, user_id: str = Depends(get_current_user)):
    accession = req.accession.strip().upper()
    if not GSE_RE.match(accession):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a GEO series accession such as GSE132465.", "invalid_accession")
    result = await lookup_geo(accession)
    return result.model_dump(mode="json")


__all__ = ["router"]
