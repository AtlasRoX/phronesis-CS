#!/usr/bin/env python3
"""
Utility functions for downloading PDFs from Zotero.
"""

import concurrent.futures
import logging
import os
import re
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

ZOTERO_API_HOST = "api.zotero.org"
MAX_PDF_BYTES = 100 * 1024 * 1024
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5


def safe_pdf_filename(header_value: str, default: str = "downloaded.pdf") -> str:
    """Base filename from a Content-Disposition header; never a path, never control characters."""
    match = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')?([^;]+)", header_value or "")
    raw = unquote(match.group(1)) if match else ""
    if not raw:
        match = re.search(r'filename\s*=\s*"?([^";]+)"?', header_value or "")
        raw = match.group(1) if match else ""
    name = os.path.basename(raw.replace("\\", "/")).strip().strip('"')
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).lstrip(".")[:200]
    if not name:
        return default
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def _library_prefix(library_type: str) -> str:
    return "groups" if str(library_type).lower() == "group" else "users"


def _get_following_redirects(
    session: requests.Session, url: str, api_key: str, timeout: float
) -> requests.Response:
    """GET with redirects followed by hand: the API key is sent to api.zotero.org only, never to the storage
    host a file redirect points to (requests keeps custom headers across hosts)."""
    for _ in range(_MAX_REDIRECTS + 1):
        headers = {"Zotero-API-Key": api_key} if urlparse(url).hostname == ZOTERO_API_HOST else {}
        response = session.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=False)
        location = response.headers.get("Location") if response.status_code in _REDIRECT_CODES else None
        if not location:
            return response
        response.close()
        url = urljoin(url, location)
    raise requests.exceptions.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects for {url}")


def download_zotero_pdf(
    session: requests.Session,
    user_id: str,
    api_key: str,
    attachment_key: str,
    **kwargs,
) -> tuple[str, str] | None:
    """
    Download a PDF from Zotero by attachment key.

    Args:
        session: requests.Session for HTTP requests.
        user_id: Zotero user or group ID.
        api_key: Zotero API key.
        attachment_key: Zotero attachment item key.
        kwargs:
            timeout (int): Request timeout in seconds (default: 10).
            chunk_size (int, optional): Chunk size for streaming.
            library_type (str): 'user' (default) or 'group'.
            max_bytes (int): Largest accepted file (default: 100 MB).

    Returns:
        Tuple of (local_file_path, filename) if successful, else None. A response that is not a PDF
        (an HTML error or login page) or that exceeds max_bytes is discarded.
    """
    # Extract optional parameters
    timeout = kwargs.get("timeout") or 10
    chunk_size = kwargs.get("chunk_size")
    library_type = kwargs.get("library_type") or "user"
    max_bytes = kwargs.get("max_bytes") or MAX_PDF_BYTES
    # Log configured parameters for verification
    logger.info("download_zotero_pdf params -> timeout=%s, chunk_size=%s", timeout, chunk_size)
    # Log download start
    logger.info("Downloading Zotero PDF for attachment %s from Zotero API", attachment_key)
    zotero_pdf_url = (
        f"https://{ZOTERO_API_HOST}/{_library_prefix(library_type)}/{user_id}/items/{attachment_key}/file"
    )

    temp_file_path = None
    try:
        response = _get_following_redirects(session, zotero_pdf_url, api_key, timeout)
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > max_bytes:
            raise ValueError(f"file is {declared} bytes, above the {max_bytes}-byte limit")
        content_type = str(response.headers.get("Content-Type", "")).lower()

        # Download to a temporary file first
        written = 0
        head = b""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file_path = temp_file.name
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                if len(head) < 5:
                    head += chunk[: 5 - len(head)]
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"file exceeds the {max_bytes}-byte limit")
                temp_file.write(chunk)
        if not head.startswith(b"%PDF") and "pdf" not in content_type:
            raise ValueError(f"response is not a PDF (Content-Type '{content_type or 'unknown'}')")
        logger.info("Zotero PDF downloaded to temporary file: %s", temp_file_path)

        filename = safe_pdf_filename(response.headers.get("Content-Disposition", ""))
        return temp_file_path, filename

    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        logger.error("Failed to download Zotero PDF for attachment %s: %s", attachment_key, e)
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        return None


def download_pdfs_in_parallel(
    session: requests.Session,
    user_id: str,
    api_key: str,
    attachment_item_map: dict[str, str],
    **kwargs,
) -> dict[str, tuple[str, str, str]]:
    """
    Download multiple PDFs in parallel using ThreadPoolExecutor.

    Args:
        session: requests.Session for HTTP requests.
        user_id: Zotero user or group ID.
        api_key: Zotero API key.
        attachment_item_map: Mapping of attachment_key to parent item_key.
        kwargs:
            max_workers (int, optional): Maximum number of worker threads (default: min(10, n)).
            chunk_size (int, optional): Chunk size for streaming.
            timeout (int, optional): Per-request timeout in seconds.
            library_type (str, optional): 'user' or 'group'.

    Returns:
        Mapping of parent item_key to (local_file_path, filename, attachment_key).
    """
    # Extract optional parameters
    max_workers = kwargs.get("max_workers")
    chunk_size = kwargs.get("chunk_size")
    timeout = kwargs.get("timeout")
    library_type = kwargs.get("library_type")
    # Log configured parameters for verification
    logger.info(
        "download_pdfs_in_parallel params -> max_workers=%s, chunk_size=%s",
        max_workers,
        chunk_size,
    )
    results: dict[str, tuple[str, str, str]] = {}
    if not attachment_item_map:
        return results

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=(max_workers if max_workers is not None else min(10, len(attachment_item_map)))
    ) as executor:
        future_to_keys = {
            executor.submit(
                download_zotero_pdf,
                session,
                user_id,
                api_key,
                attachment_key,
                chunk_size=chunk_size,
                timeout=timeout,
                library_type=library_type,
            ): (attachment_key, item_key)
            for attachment_key, item_key in attachment_item_map.items()
        }

        for future in concurrent.futures.as_completed(future_to_keys):
            attachment_key, item_key = future_to_keys[future]
            try:
                res = future.result()
                if res:
                    results[item_key] = (*res, attachment_key)
            except (requests.exceptions.RequestException, OSError) as e:
                logger.error("Failed to download PDF for key %s: %s", attachment_key, e)

    return results
