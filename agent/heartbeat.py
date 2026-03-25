from __future__ import annotations

"""
Heartbeat reporter.
Posts to yacht_heartbeats table in Supabase so the cloud knows this yacht is alive.
Tracks consecutive failures and writes a local marker file after 3.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import SyncConfig
from .retry import retry_with_backoff

logger = logging.getLogger("agent.heartbeat")

WORKER_TYPE = "filesync"
def _get_failure_marker_path() -> Path:
    from .platform import get_config_dir
    return get_config_dir() / "heartbeat_failure"

FAILURE_MARKER_PATH = _get_failure_marker_path()
MAX_CONSECUTIVE_FAILURES = 3

# Module-level counter for consecutive heartbeat failures
_consecutive_failures = 0


@retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=8.0)
def _post_heartbeat(cfg: SyncConfig, row: dict) -> requests.Response:
    """POST heartbeat with retry. Raises on network failure."""
    return requests.post(
        f"{cfg.supabase_url}/rest/v1/yacht_heartbeats",
        headers={
            "apikey": cfg.supabase_key,
            "Authorization": f"Bearer {cfg.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        json=row,
        timeout=10,
    )


def send_heartbeat(
    cfg: SyncConfig,
    files_pending: int = 0,
    files_completed: int = 0,
    errors: int = 0,
    metadata: dict | None = None,
) -> bool:
    """
    Upsert a heartbeat row. Returns True on success.
    Best-effort — failures are logged but never raised.
    After MAX_CONSECUTIVE_FAILURES, writes a local marker file for the watchdog.
    """
    global _consecutive_failures

    row = {
        "yacht_id": cfg.yacht_id,
        "worker_type": WORKER_TYPE,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "files_pending": files_pending,
        "files_completed": files_completed,
        "errors": errors,
        "metadata": metadata or {},
    }

    try:
        resp = _post_heartbeat(cfg, row)
        if resp.status_code in (200, 201):
            if _consecutive_failures > 0:
                logger.info("Heartbeat recovered after %d consecutive failures", _consecutive_failures)
            _consecutive_failures = 0
            _clear_failure_marker()
            return True
        logger.warning("Heartbeat failed %d: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logger.debug("Heartbeat unreachable: %s", exc)

    _consecutive_failures += 1
    logger.warning("Heartbeat failure #%d", _consecutive_failures)

    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        _write_failure_marker()

    return False


def _write_failure_marker() -> None:
    """Write a marker file so the launchd watchdog knows heartbeat is failing."""
    try:
        FAILURE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        FAILURE_MARKER_PATH.write_text(
            f"heartbeat_failures={_consecutive_failures}\n"
            f"since={datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        logger.error(
            "Heartbeat failed %d consecutive times — wrote failure marker: %s",
            _consecutive_failures, FAILURE_MARKER_PATH,
        )
    except OSError as exc:
        logger.error("Failed to write heartbeat failure marker: %s", exc)


def _clear_failure_marker() -> None:
    """Remove the failure marker if it exists."""
    try:
        if FAILURE_MARKER_PATH.exists():
            FAILURE_MARKER_PATH.unlink()
            logger.info("Cleared heartbeat failure marker")
    except OSError:
        pass


def report_error(
    cfg: SyncConfig,
    error_type: str,
    error_message: str,
    file_path: str = "",
) -> bool:
    """
    Best-effort POST to yacht_sync_errors table.
    Returns True on success.
    """
    row = {
        "yacht_id": cfg.yacht_id,
        "worker_type": WORKER_TYPE,
        "error_type": error_type,
        "error_message": str(error_message)[:2000],
        "file_path": file_path,
    }

    try:
        resp = requests.post(
            f"{cfg.supabase_url}/rest/v1/yacht_sync_errors",
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
                "Content-Type": "application/json",
            },
            json=row,
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except requests.RequestException:
        return False
