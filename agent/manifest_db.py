from __future__ import annotations

"""
SQLite manifest — local state for file sync.
WAL mode for crash safety. Single-writer, no concurrency issues.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("agent.manifest")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_manifest (
    relative_path TEXT PRIMARY KEY,
    content_hash  TEXT,
    size_bytes    INTEGER,
    mtime_ns      INTEGER,
    sync_status   TEXT DEFAULT 'pending',
    storage_path  TEXT,
    doc_type      TEXT,
    system_tag    TEXT,
    retry_count   INTEGER DEFAULT 0,
    next_retry_at TEXT,
    uploaded_at   TEXT,
    deleted_at    TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_errors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path  TEXT,
    error_type     TEXT,
    error_message  TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_manifest_status ON file_manifest(sync_status);
CREATE INDEX IF NOT EXISTS idx_manifest_retry ON file_manifest(next_retry_at)
    WHERE sync_status = 'failed';
"""


class ManifestDB:
    """SQLite-backed file manifest."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        # Integrity check: if DB is corrupt, rename and recreate
        if os.path.exists(self._db_path):
            try:
                test_conn = sqlite3.connect(self._db_path, timeout=5)
                result = test_conn.execute("PRAGMA integrity_check").fetchone()
                test_conn.close()
                if result[0] != "ok":
                    raise sqlite3.DatabaseError(f"integrity_check: {result[0]}")
            except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
                corrupt_path = self._db_path + ".corrupt"
                logger.error(
                    "Manifest DB corrupt (%s), renaming to %s and recreating",
                    exc, corrupt_path,
                )
                try:
                    os.rename(self._db_path, corrupt_path)
                except OSError:
                    os.remove(self._db_path)

        self._conn = sqlite3.connect(self._db_path, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def reset_interrupted(self) -> int:
        """Reset uploading rows to pending (crash recovery). Returns count."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET sync_status='pending', updated_at=datetime('now') "
                "WHERE sync_status='uploading'"
            )
            return cur.rowcount

    def reset_failed_to_pending(self) -> int:
        """Reset all failed/dlq rows to pending with retry_count=0. Returns count."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET sync_status='pending', retry_count=0, "
                "next_retry_at=NULL, updated_at=datetime('now') "
                "WHERE sync_status IN ('failed', 'dlq')"
            )
            count = cur.rowcount
            if count:
                logger.info("Reset %d failed/DLQ files to pending", count)
            return count

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, relative_path: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM file_manifest WHERE relative_path = ?",
            (relative_path,),
        )
        return cur.fetchone()

    def get_all_active_paths(self) -> set[str]:
        """Return set of all relative_paths that are not deleted."""
        cur = self._conn.execute(
            "SELECT relative_path FROM file_manifest WHERE sync_status != 'deleted'"
        )
        return {row[0] for row in cur.fetchall()}

    def get_pending(self, limit: int = 100) -> list[sqlite3.Row]:
        """Return pending rows, respecting retry backoff."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "SELECT * FROM file_manifest "
            "WHERE sync_status IN ('pending', 'failed') "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (now, limit),
        )
        return cur.fetchall()

    def count_by_status(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT sync_status, COUNT(*) FROM file_manifest GROUP BY sync_status"
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_new(self, relative_path: str, size_bytes: int, mtime_ns: int) -> None:
        """Insert or reset a file as pending."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO file_manifest (relative_path, size_bytes, mtime_ns, sync_status) "
                "VALUES (?, ?, ?, 'pending') "
                "ON CONFLICT(relative_path) DO UPDATE SET "
                "  size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
                "  sync_status='pending', retry_count=0, next_retry_at=NULL, "
                "  deleted_at=NULL, updated_at=datetime('now')",
                (relative_path, size_bytes, mtime_ns),
            )

    def mark_uploading(self, relative_path: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET sync_status='uploading', updated_at=datetime('now') "
                "WHERE relative_path=?",
                (relative_path,),
            )

    def mark_completed(
        self,
        relative_path: str,
        content_hash: str,
        storage_path: str,
        doc_type: str,
        system_tag: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET "
                "  sync_status='completed', content_hash=?, storage_path=?, "
                "  doc_type=?, system_tag=?, uploaded_at=?, "
                "  retry_count=0, next_retry_at=NULL, updated_at=datetime('now') "
                "WHERE relative_path=?",
                (content_hash, storage_path, doc_type, system_tag, now, relative_path),
            )

    def mark_failed(self, relative_path: str) -> None:
        """Increment retry_count, compute next_retry_at, or move to dlq."""
        from .constants import BACKOFF_BASE_S, BACKOFF_MAX_S, MAX_RETRY_COUNT

        row = self.get(relative_path)
        if not row:
            return
        retry_count = (row["retry_count"] or 0) + 1
        if retry_count >= MAX_RETRY_COUNT:
            status = "dlq"
            next_retry = None
        else:
            status = "failed"
            from datetime import timedelta
            delay = min(retry_count * BACKOFF_BASE_S, BACKOFF_MAX_S)
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET "
                "  sync_status=?, retry_count=?, next_retry_at=?, updated_at=datetime('now') "
                "WHERE relative_path=?",
                (status, retry_count, next_retry, relative_path),
            )

    def mark_deleted(self, relative_path: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET "
                "  sync_status='deleted', deleted_at=?, updated_at=datetime('now') "
                "WHERE relative_path=?",
                (now, relative_path),
            )

    def update_mtime(self, relative_path: str, mtime_ns: int) -> None:
        """Update mtime without changing status (touched but content unchanged)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE file_manifest SET mtime_ns=?, updated_at=datetime('now') "
                "WHERE relative_path=?",
                (mtime_ns, relative_path),
            )

    # ------------------------------------------------------------------
    # Error logging
    # ------------------------------------------------------------------

    def log_error(self, relative_path: str, error_type: str, error_message: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sync_errors (relative_path, error_type, error_message) "
                "VALUES (?, ?, ?)",
                (relative_path, error_type, str(error_message)[:2000]),
            )

    def recent_errors(self, limit: int = 50) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM sync_errors ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()
