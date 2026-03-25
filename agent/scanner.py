from __future__ import annotations

"""
Recursive NAS scanner.
Walks NAS_ROOT, compares against SQLite manifest, yields work items.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .constants import (
    MAX_FILE_SIZE,
    SKIP_DIRS,
    SKIP_EXTENSIONS,
    SKIP_PREFIXES,
)
from .manifest_db import ManifestDB

logger = logging.getLogger("agent.scanner")


@dataclass
class ScanItem:
    relative_path: str
    absolute_path: str
    size_bytes: int
    mtime_ns: int
    action: str  # "new", "modified", "deleted"


def _should_skip_entry(name: str) -> bool:
    """Return True if a file/dir name should be skipped."""
    if name in SKIP_DIRS:
        return True
    for prefix in SKIP_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _should_skip_file(name: str, size: int) -> bool:
    """Return True if a file should be skipped based on name/extension/size."""
    if _should_skip_entry(name):
        return True
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext in SKIP_EXTENSIONS:
        return True
    if name.lower() == "thumbs.db":
        return True
    if size > MAX_FILE_SIZE:
        logger.warning("Skipping oversized file (%.1f GB): %s", size / (1024**3), name)
        return True
    return False


MAX_SCAN_DEPTH = int(os.environ.get("MAX_SCAN_DEPTH", "50"))


def scan_nas(nas_root: str, manifest: ManifestDB, max_depth: int = MAX_SCAN_DEPTH) -> list[ScanItem]:
    """
    Walk NAS_ROOT, compare against manifest, return list of work items.

    Returns items with action = "new", "modified", or "deleted".
    """
    root = Path(nas_root)
    if not root.is_dir():
        logger.error("NAS_ROOT does not exist or is not a directory: %s", nas_root)
        return []

    root_depth = nas_root.rstrip(os.sep).count(os.sep)
    items: list[ScanItem] = []
    disk_paths: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(nas_root, followlinks=False):
        # Enforce max depth
        current_depth = dirpath.count(os.sep) - root_depth
        if current_depth >= max_depth:
            dirnames[:] = []
            continue

        # Prune hidden/system directories in-place
        dirnames[:] = [d for d in dirnames if not _should_skip_entry(d)]

        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            try:
                stat = os.stat(abs_path)
            except OSError as exc:
                logger.debug("Cannot stat %s: %s", abs_path, exc)
                continue

            size = stat.st_size
            mtime_ns = stat.st_mtime_ns

            if _should_skip_file(fname, size):
                continue

            rel_path = os.path.relpath(abs_path, nas_root)
            disk_paths.add(rel_path)

            existing = manifest.get(rel_path)

            if existing is None:
                # New file
                items.append(ScanItem(
                    relative_path=rel_path,
                    absolute_path=abs_path,
                    size_bytes=size,
                    mtime_ns=mtime_ns,
                    action="new",
                ))
            elif existing["sync_status"] == "deleted":
                # Re-appeared after deletion
                items.append(ScanItem(
                    relative_path=rel_path,
                    absolute_path=abs_path,
                    size_bytes=size,
                    mtime_ns=mtime_ns,
                    action="new",
                ))
            elif existing["mtime_ns"] != mtime_ns:
                # Modified
                items.append(ScanItem(
                    relative_path=rel_path,
                    absolute_path=abs_path,
                    size_bytes=size,
                    mtime_ns=mtime_ns,
                    action="modified",
                ))
            # else: unchanged, skip

    # Detect deletions: manifest rows not on disk
    active_paths = manifest.get_all_active_paths()
    deleted_paths = active_paths - disk_paths

    for rel_path in deleted_paths:
        items.append(ScanItem(
            relative_path=rel_path,
            absolute_path="",
            size_bytes=0,
            mtime_ns=0,
            action="deleted",
        ))

    logger.info(
        "Scan complete: %d new, %d modified, %d deleted",
        sum(1 for i in items if i.action == "new"),
        sum(1 for i in items if i.action == "modified"),
        sum(1 for i in items if i.action == "deleted"),
    )
    return items
