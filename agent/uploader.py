from __future__ import annotations

"""
Supabase Storage uploader.
POST to create, PUT to overwrite if exists.
Streams files in chunks to avoid loading entire files into RAM.
"""

import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterator

import requests

from .config import SyncConfig
from .constants import (
    LARGE_UPLOAD_THRESHOLD,
    SPEED_PROBE_TIMEOUT,
    UPLOAD_TIMEOUT_LARGE,
    UPLOAD_TIMEOUT_NORMAL,
    get_mime_type,
)
from .retry import retry_with_backoff

logger = logging.getLogger("agent.uploader")

BUCKET = "yacht-documents"


def sanitize_storage_key(path: str) -> str:
    """Transliterate non-ASCII chars to ASCII for Supabase Storage keys.

    Supabase rejects keys with characters like ö, ü, â, é.
    NFKD decomposition splits accented chars into base + combining mark,
    then we drop the marks to get the ASCII base letter.
    Original filenames are preserved in doc_metadata for display.
    """
    # Windows paths use backslashes — normalize to forward slashes FIRST
    path = path.replace("\\", "/")
    normalized = unicodedata.normalize("NFKD", path)
    ascii_path = normalized.encode("ascii", "ignore").decode("ascii")
    # Replace any remaining non-portable chars (keep alphanumeric, /, -, _, .)
    ascii_path = re.sub(r"[^\w/\-.]", "_", ascii_path)
    ascii_path = re.sub(r"_+", "_", ascii_path)
    return ascii_path

STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MB chunks for streaming uploads
RESUMABLE_THRESHOLD = 100 * 1024 * 1024  # 100 MB — use temp path + rename


def _headers(cfg: SyncConfig, content_type: str = "application/json") -> dict[str, str]:
    return {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": content_type,
    }


def _iter_file_chunks(file_path: str, chunk_size: int = STREAM_CHUNK_SIZE) -> Iterator[bytes]:
    """Yield file contents in fixed-size chunks. Never loads full file into RAM."""
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def probe_connectivity(cfg: SyncConfig) -> bool:
    """HEAD request to Supabase to check reachability. Returns True if OK (2xx/3xx only)."""
    try:
        resp = requests.head(
            f"{cfg.supabase_url}/storage/v1/bucket",
            headers={"apikey": cfg.supabase_key, "Authorization": f"Bearer {cfg.supabase_key}"},
            timeout=SPEED_PROBE_TIMEOUT,
        )
        return resp.status_code < 400
    except requests.RequestException:
        return False


@retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=8.0)
def verify_upload(cfg: SyncConfig, storage_path: str, expected_size: int) -> bool:
    """
    HEAD the uploaded object and verify content-length matches local file size.
    Returns True if verified, False if mismatch or unreachable.
    """
    try:
        url = f"{cfg.supabase_url}/storage/v1/object/info/{BUCKET}/{storage_path}"
        resp = requests.head(
            url,
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            # Try the object URL directly — some Supabase versions use this
            url = f"{cfg.supabase_url}/storage/v1/object/{BUCKET}/{storage_path}"
            resp = requests.head(
                url,
                headers={
                    "apikey": cfg.supabase_key,
                    "Authorization": f"Bearer {cfg.supabase_key}",
                },
                timeout=15,
            )
        if resp.status_code != 200:
            logger.warning("Verify HEAD returned %d for %s", resp.status_code, storage_path)
            return False

        remote_size = int(resp.headers.get("content-length", -1))
        if remote_size == -1:
            logger.debug("No content-length header for %s, skipping size check", storage_path)
            return True

        if remote_size != expected_size:
            logger.error(
                "Upload size mismatch for %s: local=%d remote=%d",
                storage_path, expected_size, remote_size,
            )
            return False

        return True
    except requests.RequestException as exc:
        logger.warning("Verify request failed for %s: %s", storage_path, exc)
        return False


def delete_object(cfg: SyncConfig, storage_path: str) -> bool:
    """Delete an object from Supabase Storage. Returns True on success."""
    try:
        resp = requests.delete(
            f"{cfg.supabase_url}/storage/v1/object/{BUCKET}/{storage_path}",
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
            },
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except requests.RequestException:
        return False


def check_remote_exists(cfg: SyncConfig, storage_path: str) -> int | None:
    """
    Check if a file exists in Supabase Storage.
    Returns the content-length if it exists, None if not found or error.
    """
    try:
        url = f"{cfg.supabase_url}/storage/v1/object/{BUCKET}/{storage_path}"
        resp = requests.head(
            url,
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return int(resp.headers.get("content-length", 0))
        return None
    except requests.RequestException:
        return None


@retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=8.0)
def _do_upload(
    cfg: SyncConfig,
    local_path: str,
    storage_path: str,
    content_type: str,
    file_size: int,
    timeout: int,
) -> str:
    """
    Upload a file to a specific storage_path.
    Uses file handle (not generator) so requests sends Content-Length
    without chunked transfer encoding — Cloudflare rejects chunked+length.
    Returns the storage_path on success, raises RuntimeError on failure.
    """
    storage_url = f"{cfg.supabase_url}/storage/v1/object/{BUCKET}/{storage_path}"

    headers = _headers(cfg, content_type)
    headers["Content-Length"] = str(file_size)

    # Use open file handle — requests reads it without chunked encoding
    with open(local_path, "rb") as f:
        resp = requests.post(
            storage_url,
            headers=headers,
            data=f,
            timeout=timeout,
        )

    # If already exists, PUT to overwrite
    if resp.status_code == 400 and "already exists" in (resp.text or "").lower():
        logger.debug("File exists, overwriting: %s", storage_path)
        with open(local_path, "rb") as f:
            resp = requests.put(
                storage_url,
                headers=headers,
                data=f,
                timeout=timeout,
            )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Storage upload failed {resp.status_code}: {resp.text[:300]}"
        )

    return storage_path


def upload_file(
    cfg: SyncConfig,
    local_path: str,
    storage_path: str,
) -> str:
    """
    Upload a file to Supabase Storage using streaming chunks.

    For files > RESUMABLE_THRESHOLD (100MB): uploads to a .tmp path first,
    then renames on success. If daemon crashes mid-upload, the temp file is
    orphaned (harmless) and a fresh retry uploads cleanly.

    Args:
        cfg: Sync configuration
        local_path: Absolute path to local file
        storage_path: Remote path within the bucket ({yacht_id}/{relative_path})

    Returns:
        The storage_path on success.

    Raises:
        RuntimeError on upload failure.
    """
    filename = Path(local_path).name
    content_type = get_mime_type(filename)
    file_size = Path(local_path).stat().st_size
    timeout = UPLOAD_TIMEOUT_LARGE if file_size > LARGE_UPLOAD_THRESHOLD else UPLOAD_TIMEOUT_NORMAL

    if file_size > RESUMABLE_THRESHOLD:
        # Large file: upload to temp path, verify, then copy to final path
        tmp_path = storage_path + ".tmp"
        logger.info("Large file (%.1f MB), using temp path: %s", file_size / (1024 * 1024), tmp_path)

        _do_upload(cfg, local_path, tmp_path, content_type, file_size, timeout)

        # Verify temp upload
        if not verify_upload(cfg, tmp_path, file_size):
            delete_object(cfg, tmp_path)
            raise RuntimeError(f"Post-upload verification failed for temp file: {tmp_path}")

        _do_upload(cfg, local_path, storage_path, content_type, file_size, timeout)
        delete_object(cfg, tmp_path)
    else:
        # Normal file: direct upload
        _do_upload(cfg, local_path, storage_path, content_type, file_size, timeout)

    # Post-upload verification
    if not verify_upload(cfg, storage_path, file_size):
        delete_object(cfg, storage_path)
        raise RuntimeError(f"Post-upload verification failed: {storage_path}")

    logger.debug("Uploaded %s (%.1f KB, streamed)", storage_path, file_size / 1024)
    return storage_path


def cleanup_orphaned_temps(cfg: SyncConfig) -> int:
    """
    Remove orphaned .tmp files in the yacht's storage bucket from interrupted uploads.
    Returns count of deleted temps.
    """
    try:
        resp = requests.post(
            f"{cfg.supabase_url}/storage/v1/object/list/{BUCKET}",
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
                "Content-Type": "application/json",
            },
            json={"prefix": f"{cfg.yacht_id}/", "search": ".tmp"},
            timeout=15,
        )
        if resp.status_code != 200:
            return 0

        items = resp.json()
        count = 0
        for item in items:
            name = item.get("name", "")
            if name.endswith(".tmp"):
                storage_path = f"{cfg.yacht_id}/{name}"
                if delete_object(cfg, storage_path):
                    count += 1
                    logger.info("Cleaned orphaned temp: %s", storage_path)

        if count:
            logger.info("Cleaned %d orphaned .tmp files from storage", count)
        return count
    except requests.RequestException as exc:
        logger.warning("Orphaned temp cleanup request failed: %s", exc)
        return 0
