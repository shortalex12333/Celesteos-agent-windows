from __future__ import annotations

"""
Supabase REST indexer — thin pipe version.
Upserts doc_metadata and search_index rows.
NO text extraction (moved to Docker extraction worker).
NO chunk writing (moved to Docker extraction worker).
Sets embedding_status='pending_extraction' so the extraction worker picks it up.
"""

import hashlib
import logging
import os
import re
import uuid as uuid_mod
from datetime import datetime, timezone

import requests

from .config import SyncConfig
from .retry import retry_with_backoff

logger = logging.getLogger("agent.indexer")

NAMESPACE = "nas"


def _object_id(yacht_id: str, relative_path: str) -> str:
    """Deterministic UUID5 from nas:{yacht_id}:{relative_path}."""
    identity = f"{NAMESPACE}:{yacht_id}:{relative_path}"
    return str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, identity))


def _headers(cfg: SyncConfig) -> dict[str, str]:
    return {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def _patch_headers(cfg: SyncConfig) -> dict[str, str]:
    return {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": "application/json",
    }


def build_search_text(
    filename: str,
    relative_path: str,
    doc_type: str,
    system_tag: str,
) -> str:
    """
    Build structured search_text from filename, path, and classification.
    Thin version — no extracted text (extraction happens in Docker).
    """
    # Clean filename: strip extension, split on _ and -, rejoin with spaces
    name_part = filename.rsplit(".", 1)[0] if "." in filename else filename
    clean_name = re.sub(r"[_\-\.]+", " ", name_part).strip()

    # Build directory breadcrumb from relative_path
    parts = relative_path.replace("\\", "/").strip("/").split("/")
    dir_parts = parts[:-1]

    breadcrumb = ""
    if dir_parts:
        clean_dirs = []
        for d in dir_parts:
            stripped = re.sub(r"^\d+[_\-\.\s]*", "", d).strip()
            if stripped:
                clean_dirs.append(stripped)
        breadcrumb = " > ".join(clean_dirs)

    # Assemble: name | breadcrumb | doc_type | system_tag
    segments = [clean_name]
    if breadcrumb:
        segments.append(breadcrumb)
    if doc_type and doc_type != "general":
        segments.append(doc_type)
    if system_tag and system_tag != "general":
        segments.append(system_tag)

    return " | ".join(segments)


def _find_equipment_id(cfg: SyncConfig, yacht_id: str, filename: str, system_tag: str) -> str | None:
    """
    Try to match a document to equipment by manufacturer/model in filename.
    Returns equipment_id if found, None otherwise. Best-effort.
    """
    name_part = filename.rsplit(".", 1)[0] if "." in filename else filename
    tokens = re.sub(r"[_\-\.]+", " ", name_part).split()

    if len(tokens) < 2:
        return None

    manufacturer_guess = tokens[0]
    model_guess = tokens[1]

    try:
        resp = requests.get(
            f"{cfg.supabase_url}/rest/v1/equipment",
            params={
                "yacht_id": f"eq.{yacht_id}",
                "or": f"(manufacturer.ilike.%{manufacturer_guess}%,model.ilike.%{model_guess}%)",
                "select": "id,manufacturer,model",
                "limit": "1",
            },
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows:
                eq_id = rows[0]["id"]
                logger.info(
                    "Auto-linked %s to equipment %s (%s %s)",
                    filename, eq_id, rows[0].get("manufacturer", ""), rows[0].get("model", ""),
                )
                return eq_id
    except Exception as exc:
        logger.debug("Equipment lookup failed for %s: %s", filename, exc)

    return None


@retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=8.0)
def upsert_doc_metadata(
    cfg: SyncConfig,
    yacht_id: str,
    relative_path: str,
    filename: str,
    doc_type: str,
    storage_path: str,
    size_bytes: int,
    content_type: str = "application/octet-stream",
    system_type: str = "general",
) -> str:
    """Upsert a row in doc_metadata. Returns the object_id."""
    obj_id = _object_id(yacht_id, relative_path)

    row = {
        "id": obj_id,
        "yacht_id": yacht_id,
        "source": "nas",
        "filename": filename,
        "doc_type": doc_type,
        "storage_path": storage_path,
        "original_path": relative_path,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "system_type": system_type,
        "indexed": False,
        "deleted_at": None,
    }

    eq_id = _find_equipment_id(cfg, yacht_id, filename, system_type)
    if eq_id:
        row["equipment_id"] = eq_id

    resp = requests.post(
        f"{cfg.supabase_url}/rest/v1/doc_metadata",
        headers=_headers(cfg),
        json=row,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        logger.error("doc_metadata upsert failed %d: %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"doc_metadata upsert failed: {resp.status_code}")

    return obj_id


@retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=8.0)
def upsert_search_index(
    cfg: SyncConfig,
    yacht_id: str,
    relative_path: str,
    filename: str,
    doc_type: str,
    system_tag: str,
    storage_path: str,
    embedding_status: str = "pending_extraction",
) -> str:
    """Upsert a row in search_index. Returns the object_id."""
    obj_id = _object_id(yacht_id, relative_path)

    search_text = build_search_text(
        filename=filename,
        relative_path=relative_path,
        doc_type=doc_type,
        system_tag=system_tag,
    )

    row = {
        "object_type": "document",
        "object_id": obj_id,
        "org_id": yacht_id,
        "yacht_id": yacht_id,
        "search_text": search_text,
        "embedding_status": embedding_status,
        "payload": {
            "filename": filename,
            "doc_type": doc_type,
            "system_tag": system_tag,
            "storage_path": storage_path,
            "source": "nas",
        },
    }

    # Try POST first (new row)
    resp = requests.post(
        f"{cfg.supabase_url}/rest/v1/search_index",
        headers=_headers(cfg),
        json=row,
        timeout=15,
    )

    # If duplicate, PATCH the existing row
    if resp.status_code == 409 or (resp.status_code == 400 and "already exists" in (resp.text or "")):
        patch_data = {
            "search_text": row["search_text"],
            "embedding_status": row["embedding_status"],
            "payload": row["payload"],
        }
        resp = requests.patch(
            f"{cfg.supabase_url}/rest/v1/search_index",
            params={"object_id": f"eq.{obj_id}", "object_type": "eq.document"},
            headers=_patch_headers(cfg),
            json=patch_data,
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            logger.error("search_index patch failed %d: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"search_index patch failed: {resp.status_code}")
    elif resp.status_code not in (200, 201):
        logger.error("search_index upsert failed %d: %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"search_index upsert failed: {resp.status_code}")

    return obj_id


def delete_doc_metadata(cfg: SyncConfig, object_id: str) -> bool:
    """Delete a doc_metadata row by object_id. Used for rollback when search_index upsert fails."""
    try:
        resp = requests.delete(
            f"{cfg.supabase_url}/rest/v1/doc_metadata",
            params={"id": f"eq.{object_id}"},
            headers=_patch_headers(cfg),
            timeout=15,
        )
        if resp.status_code in (200, 204):
            logger.info("Rolled back doc_metadata for %s", object_id)
            return True
        logger.warning("doc_metadata delete returned %d for %s", resp.status_code, object_id)
        return False
    except requests.RequestException as exc:
        logger.warning("doc_metadata delete failed for %s: %s", object_id, exc)
        return False


def soft_delete(cfg: SyncConfig, yacht_id: str, relative_path: str) -> None:
    """Mark a file as deleted in search_index and doc_metadata."""
    obj_id = _object_id(yacht_id, relative_path)

    # search_index: set embedding_status='deleted'
    resp = requests.patch(
        f"{cfg.supabase_url}/rest/v1/search_index",
        params={"object_id": f"eq.{obj_id}"},
        headers=_patch_headers(cfg),
        json={"embedding_status": "deleted"},
        timeout=15,
    )
    if resp.status_code not in (200, 204):
        logger.warning("search_index soft-delete returned %d for %s", resp.status_code, relative_path)

    # doc_metadata: set deleted_at
    resp = requests.patch(
        f"{cfg.supabase_url}/rest/v1/doc_metadata",
        params={"id": f"eq.{obj_id}"},
        headers=_patch_headers(cfg),
        json={"deleted_at": datetime.now(timezone.utc).isoformat()},
        timeout=15,
    )
    if resp.status_code not in (200, 204):
        logger.warning("doc_metadata soft-delete returned %d for %s", resp.status_code, relative_path)

    logger.info("Soft-deleted: %s", relative_path)
