#!/usr/bin/env python3
from __future__ import annotations

"""
CelesteOS Local File Sync Daemon — Thin Pipe Version.

Continuously transfers files from a mounted NAS → Supabase Storage → search_index.
NO text extraction or chunking — that happens in the Docker extraction worker.
Cross-platform: runs as launchd daemon (macOS) or startup app (Windows).

Status is written to config_dir/status.json for external consumers.
System tray icon shows live sync status (rumps on macOS, pystray on Windows).

Usage:
    python -m agent              # foreground
    python -m agent --once       # single cycle then exit
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .classifier import classify_path
from .config import load_config, SyncConfig
from .constants import (
    INDEXABLE_SIZE_LIMIT,
    classify_extension,
    get_mime_type,
)
from .hasher import sha256_file
from .heartbeat import report_error, send_heartbeat
from .indexer import soft_delete, upsert_doc_metadata, upsert_search_index
from .manifest_db import ManifestDB
from .platform import acquire_pid_lock, get_config_dir, register_reload_signal
from .platform import install_autostart, is_autostart_installed
from .scanner import ScanItem, scan_nas
from .uploader import check_remote_exists, cleanup_orphaned_temps, probe_connectivity, sanitize_storage_key, upload_file

# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------
STATUS_FILE = get_config_dir() / "status.json"


def _write_status(status: dict) -> None:
    """Write daemon status to ~/.celesteos/status.json for external consumers."""
    try:
        STATUS_FILE.write_text(json.dumps(status, default=str))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Watcher trigger event — lets watcher callbacks wake the sync loop early
# ---------------------------------------------------------------------------
_watcher_trigger = threading.Event()

# ---------------------------------------------------------------------------
# Config reload flag for SIGHUP
# ---------------------------------------------------------------------------
_reload_config = False

# ---------------------------------------------------------------------------
# Logging — configure_logging() is called in main() for production;
# basicConfig here is a fallback for direct imports / tests.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("agent.daemon")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("Received signal %d, finishing current file then shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _signal_handler)


def _sighup_handler(signum, frame):
    global _reload_config
    logger.info("Config reload requested, will reload at next cycle start")
    _reload_config = True


# SIGHUP on macOS, sentinel file watcher on Windows
register_reload_signal(_sighup_handler)


# ---------------------------------------------------------------------------
# Disk-full detection
# ---------------------------------------------------------------------------
_disk_full_paused = False


def _safe_manifest_write(manifest: ManifestDB, func_name: str, *args, **kwargs):
    """
    Call a manifest write method, catching disk-full errors gracefully.
    Returns True if the write succeeded, False if disk is full.
    """
    global _disk_full_paused
    try:
        method = getattr(manifest, func_name)
        method(*args, **kwargs)
        if _disk_full_paused:
            _disk_full_paused = False
            logger.info("Disk space recovered, resuming normal operation")
        return True
    except sqlite3.OperationalError as exc:
        err_msg = str(exc).lower()
        if "disk" in err_msg or "full" in err_msg or "no space" in err_msg:
            _disk_full_paused = True
            logger.error("DISK FULL — manifest write failed: %s. Pausing sync cycle.", exc)
            return False
        raise  # Re-raise non-disk errors


# ---------------------------------------------------------------------------
# Idempotent crash recovery
# ---------------------------------------------------------------------------
def _recover_interrupted(cfg: SyncConfig, manifest: ManifestDB) -> int:
    """
    Reset interrupted uploads and check if any pending files already exist
    in Supabase Storage (from a crash during manifest-write-after-upload).
    Returns count of recovered uploads.
    """
    recovered = manifest.reset_interrupted()
    if recovered:
        logger.info("Reset %d interrupted uploads to pending", recovered)

    # Check pending files that might already be uploaded
    pending = manifest.get_pending(limit=100)
    skip_count = 0

    for row in pending:
        rel = row["relative_path"]
        storage_path = sanitize_storage_key(f"{cfg.yacht_id}/{rel}")
        abs_path = os.path.join(cfg.nas_root, rel)

        if not os.path.exists(abs_path):
            continue

        local_size = os.path.getsize(abs_path)
        remote_size = check_remote_exists(cfg, storage_path)

        if remote_size is not None and remote_size == local_size:
            logger.info("Crash recovery: %s already in storage (size match %d), marking completed", rel, local_size)
            content_hash = sha256_file(abs_path)
            doc_type, system_tag = classify_path(rel)

            try:
                filename = os.path.basename(rel)
                content_type = get_mime_type(filename)
                upsert_doc_metadata(
                    cfg=cfg, yacht_id=cfg.yacht_id, relative_path=rel,
                    filename=filename, doc_type=doc_type, storage_path=storage_path,
                    size_bytes=local_size, content_type=content_type, system_type=system_tag,
                )
                upsert_search_index(
                    cfg=cfg, yacht_id=cfg.yacht_id, relative_path=rel,
                    filename=filename, doc_type=doc_type, system_tag=system_tag,
                    storage_path=storage_path,
                )
                manifest.mark_completed(
                    relative_path=rel, content_hash=content_hash,
                    storage_path=storage_path, doc_type=doc_type, system_tag=system_tag,
                )
                skip_count += 1
            except Exception as exc:
                logger.warning("Crash recovery metadata upsert failed for %s: %s", rel, exc)

    if skip_count:
        logger.info("Crash recovery: skipped re-upload for %d files already in storage", skip_count)

    return recovered + skip_count


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------
def _process_file(cfg: SyncConfig, manifest: ManifestDB, item: ScanItem) -> bool:
    """
    Process one scan item. Returns True on success, False on failure.
    Thin pipe: upload + index only, NO extraction.
    """
    rel = item.relative_path
    filename = os.path.basename(rel)

    # Notify status window that we're processing this file
    from .status_tray import sync_status
    sync_status.set_syncing(filename)

    try:
        # 1. Extension/size gate
        tier = classify_extension(filename)
        if tier == "skip":
            logger.debug("Skipping by extension: %s", rel)
            return True

        # Indexable files → pending_extraction (extraction worker will process)
        # Storage-only files → storage_only (no extraction needed)
        embedding_status = "pending_extraction" if tier == "indexable" else "storage_only"

        # Downgrade large indexable files
        if tier == "indexable" and item.size_bytes > INDEXABLE_SIZE_LIMIT:
            embedding_status = "storage_only"
            logger.info("Large file downgraded to storage_only: %s (%.1f MB)",
                        rel, item.size_bytes / (1024 * 1024))

        # Satellite override
        if cfg.max_satellite_upload_mb and item.size_bytes > cfg.max_satellite_upload_mb * 1024 * 1024:
            logger.info("Skipping due to satellite limit: %s (%.1f MB)",
                        rel, item.size_bytes / (1024 * 1024))
            return True

        # 2. Hash
        if not _safe_manifest_write(manifest, "mark_uploading", rel):
            return False  # Disk full
        content_hash = sha256_file(item.absolute_path)

        # Check if content unchanged despite mtime change
        existing = manifest.get(rel)
        if (
            existing
            and existing["content_hash"] == content_hash
            and existing["sync_status"] == "completed"
        ):
            logger.debug("Content unchanged (hash match), updating mtime: %s", rel)
            manifest.update_mtime(rel, item.mtime_ns)
            return True

        # 3. Upload to Supabase Storage (streaming, with verification)
        storage_path = sanitize_storage_key(f"{cfg.yacht_id}/{rel}")
        upload_file(cfg, item.absolute_path, storage_path)

        # 4. Classify
        doc_type, system_tag = classify_path(rel)

        # 5. doc_metadata upsert
        content_type = get_mime_type(filename)
        obj_id = upsert_doc_metadata(
            cfg=cfg,
            yacht_id=cfg.yacht_id,
            relative_path=rel,
            filename=filename,
            doc_type=doc_type,
            storage_path=storage_path,
            size_bytes=item.size_bytes,
            content_type=content_type,
            system_type=system_tag,
        )

        # 6. search_index upsert (NO extraction — Docker handles that)
        # If this fails after doc_metadata succeeded, roll back doc_metadata
        try:
            upsert_search_index(
                cfg=cfg,
                yacht_id=cfg.yacht_id,
                relative_path=rel,
                filename=filename,
                doc_type=doc_type,
                system_tag=system_tag,
                storage_path=storage_path,
                embedding_status=embedding_status,
            )
        except Exception as idx_exc:
            logger.error("search_index upsert failed for %s, rolling back doc_metadata: %s", rel, idx_exc)
            try:
                from .indexer import delete_doc_metadata
                delete_doc_metadata(cfg, obj_id)
            except Exception as rb_exc:
                logger.warning("doc_metadata rollback also failed for %s: %s", rel, rb_exc)
            raise

        # 7. Mark completed in manifest
        if not _safe_manifest_write(
            manifest, "mark_completed",
            relative_path=rel,
            content_hash=content_hash,
            storage_path=storage_path,
            doc_type=doc_type,
            system_tag=system_tag,
        ):
            return False  # Disk full — upload succeeded but manifest write failed
        manifest.update_mtime(rel, item.mtime_ns)

        logger.info("Synced: %s → %s [%s/%s]", rel, storage_path, doc_type, system_tag)
        sync_status.add_activity(filename, "synced")
        return True

    except Exception as exc:
        logger.error("Failed to process %s: %s", rel, exc, exc_info=True)
        _safe_manifest_write(manifest, "mark_failed", rel)
        _safe_manifest_write(manifest, "log_error", rel, type(exc).__name__, str(exc))
        report_error(cfg, type(exc).__name__, str(exc), file_path=rel)
        sync_status.add_error(f"{filename}: {exc}")
        sync_status.add_activity(filename, "failed")
        return False


def _process_delete(cfg: SyncConfig, manifest: ManifestDB, item: ScanItem) -> bool:
    """Soft-delete a file that was removed from NAS."""
    try:
        soft_delete(cfg, cfg.yacht_id, item.relative_path)
        _safe_manifest_write(manifest, "mark_deleted", item.relative_path)
        return True
    except Exception as exc:
        logger.error("Failed to soft-delete %s: %s", item.relative_path, exc)
        _safe_manifest_write(manifest, "log_error", item.relative_path, "SoftDeleteError", str(exc))
        return False


# ---------------------------------------------------------------------------
# Main poll cycle
# ---------------------------------------------------------------------------
def run_cycle(cfg: SyncConfig, manifest: ManifestDB) -> dict:
    """Run one scan+process cycle. Returns stats dict."""
    global _disk_full_paused

    stats = {"new": 0, "modified": 0, "deleted": 0, "failed": 0, "skipped": 0}

    if _disk_full_paused:
        send_heartbeat(cfg, errors=1, metadata={"disk_full": True, "reason": "disk_full_paused"})
        logger.warning("Disk full — skipping cycle. Free disk space to resume.")
        return stats

    if not probe_connectivity(cfg):
        logger.warning("Supabase unreachable — skipping cycle")
        send_heartbeat(cfg, errors=1, metadata={"reason": "unreachable"})
        return stats

    items = scan_nas(cfg.nas_root, manifest)

    for item in items:
        if _shutdown:
            break
        if item.action in ("new", "modified"):
            if not _safe_manifest_write(manifest, "upsert_new", item.relative_path, item.size_bytes, item.mtime_ns):
                break

    if _disk_full_paused:
        send_heartbeat(cfg, errors=1, metadata={"disk_full": True})
        return stats

    bytes_uploaded = 0
    pending = manifest.get_pending(limit=500)

    for row in pending:
        if _shutdown or _disk_full_paused:
            break

        if cfg.max_upload_bytes_per_cycle and bytes_uploaded >= cfg.max_upload_bytes_per_cycle:
            logger.info("Bandwidth cap reached, deferring remaining files")
            stats["skipped"] += 1
            continue

        rel = row["relative_path"]
        matching = [i for i in items if i.relative_path == rel and i.action != "deleted"]
        if matching:
            scan_item = matching[0]
        else:
            abs_path = os.path.join(cfg.nas_root, rel)
            if not os.path.exists(abs_path):
                continue
            stat = os.stat(abs_path)
            scan_item = ScanItem(
                relative_path=rel,
                absolute_path=abs_path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                action="modified",
            )

        ok = _process_file(cfg, manifest, scan_item)
        if ok:
            action_key = "new" if scan_item.action == "new" else "modified"
            stats[action_key] += 1
            bytes_uploaded += scan_item.size_bytes
        else:
            stats["failed"] += 1

    for item in items:
        if _shutdown or _disk_full_paused:
            break
        if item.action == "deleted":
            ok = _process_delete(cfg, manifest, item)
            if ok:
                stats["deleted"] += 1
            else:
                stats["failed"] += 1

    status_counts = manifest.count_by_status()
    metadata = {"cycle_stats": stats}
    if _disk_full_paused:
        metadata["disk_full"] = True
    send_heartbeat(
        cfg,
        files_pending=status_counts.get("pending", 0) + status_counts.get("failed", 0),
        files_completed=status_counts.get("completed", 0),
        errors=stats["failed"],
        metadata=metadata,
    )

    logger.info(
        "Cycle done: %d new, %d modified, %d deleted, %d failed",
        stats["new"], stats["modified"], stats["deleted"], stats["failed"],
    )
    return stats


# ---------------------------------------------------------------------------
# First-launch installation flow
# ---------------------------------------------------------------------------
def _run_installation_flow(cfg: SyncConfig) -> bool:
    """
    Handle first-launch: register yacht, verify 2FA, store credentials.
    Tries GUI first (pywebview in subprocess), falls back to CLI prompts.
    Returns True if activation succeeded.
    """
    try:
        from lib.installer import InstallConfig, InstallState

        install_config = InstallConfig.load_embedded()

        # Launch the GUI installer.
        # Inside a PyInstaller bundle, sys.executable is the frozen binary,
        # not Python, so subprocess mode doesn't work. Import directly instead.
        try:
            logger.info("Launching installer GUI...")
            from .installer_ui import run_installer_ui
            nas_root = run_installer_ui(install_config)
            if nas_root and os.path.isdir(nas_root):
                logger.info("GUI installer completed, NAS root: %s", nas_root)
                cfg.nas_root = nas_root
                return True
            # Check if installer wrote config even if it didn't return a folder
            env_file = get_config_dir() / ".env.local"
            if env_file.exists():
                from .config import _read_env_file
                env = _read_env_file(env_file)
                nas_root = env.get("NAS_ROOT", "")
                if nas_root and os.path.isdir(nas_root):
                    logger.info("GUI installer completed (from env), NAS root: %s", nas_root)
                    cfg.nas_root = nas_root
                    return True
            logger.warning("GUI installer finished but config incomplete")
            # Fall through to CLI mode
        except Exception as exc:
            logger.warning("GUI installer failed: %s, falling back to CLI", exc)

        # CLI fallback
        from lib.installer import InstallationOrchestrator
        orchestrator = InstallationOrchestrator(install_config)

        # Check if we already have a pending code (registered externally)
        code_file = get_config_dir() / "pending_code"
        has_pending_code = code_file.exists() and code_file.read_text().strip()

        if has_pending_code:
            # Skip registration — code was provided externally
            orchestrator.state = InstallState.PENDING_2FA
            logger.info("Found pending code file — skipping registration")
        else:
            state = orchestrator.initialize()

            if state == InstallState.OPERATIONAL:
                logger.info("Already activated — skipping installation flow")
                return True

            if state == InstallState.UNREGISTERED:
                logger.info("First launch — registering with cloud...")
                success, message = orchestrator.register()
                logger.info("Registration: %s", message)
                if not success:
                    logger.error("Registration failed: %s", message)
                    return False

        if orchestrator.state == InstallState.PENDING_2FA:
            # Try multiple input methods for 2FA code entry
            code = None

            # Method 1: Environment variable (set by portal or config)
            code = os.environ.get("CELESTEOS_2FA_CODE", "")

            # Method 2: File-based (portal writes code to ~/.celesteos/pending_code)
            if not code:
                code_file = get_config_dir() / "pending_code"
                if code_file.exists():
                    code = code_file.read_text().strip()
                    code_file.unlink()  # one-time use

            # Method 3: Tkinter dialog
            if not code:
                try:
                    import tkinter as tk
                    from tkinter import simpledialog
                    root = tk.Tk()
                    root.withdraw()
                    code = simpledialog.askstring(
                        "CelesteOS — Verify",
                        "Enter the 6-digit code sent to your email:",
                        parent=root,
                    )
                    root.destroy()
                except Exception as exc:
                    logger.debug("Tkinter unavailable: %s", exc)

            # Method 4: stdin
            if not code:
                try:
                    code = input("  Enter 6-digit code: ").strip()
                except EOFError:
                    pass

            if not code:
                logger.error("No 2FA code provided. Write code to ~/.celesteos/pending_code and relaunch.")
                return False

            if not code or len(code) != 6:
                logger.error("Invalid code format")
                return False

            success, message = orchestrator.verify_2fa(code)
            logger.info("2FA verification: %s", message)
            if not success:
                return False

        return orchestrator.state in (InstallState.ACTIVE, InstallState.OPERATIONAL)

    except FileNotFoundError:
        logger.warning("No install manifest found — running in dev/env mode")
        return False
    except Exception as exc:
        logger.error("Installation flow error: %s", exc, exc_info=True)
        return False


def _ensure_nas_root(cfg: SyncConfig) -> SyncConfig:
    """
    If NAS_ROOT is not set, run the folder selector.
    Returns an updated SyncConfig with nas_root populated.
    """
    if cfg.nas_root and os.path.isdir(cfg.nas_root):
        return cfg

    logger.info("NAS root not configured")

    # Try environment variable first
    nas_root = os.environ.get("NAS_ROOT", "")

    # Try folder selector (may fail if Tkinter unavailable)
    if not nas_root:
        try:
            from .folder_selector import run_folder_selector
            nas_root = run_folder_selector()
        except Exception as exc:
            logger.warning("Folder selector failed: %s", exc)

    if not nas_root:
        logger.error("NAS_ROOT not set. Add NAS_ROOT=/path/to/nas to ~/.celesteos/.env.local")
        sys.exit(1)

    if not nas_root or not os.path.isdir(nas_root):
        logger.error("Selected path is not a valid directory: %s", nas_root)
        sys.exit(1)

    # Save to .env.local for persistence
    env_dir = get_config_dir()
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / ".env.local"

    # Read existing, update NAS_ROOT
    lines = []
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if not line.strip().startswith("NAS_ROOT="):
                lines.append(line)
    lines.append(f"NAS_ROOT={nas_root}")
    env_file.write_text("\n".join(lines) + "\n")
    if sys.platform != "win32":
        os.chmod(str(env_file), 0o600)

    logger.info("NAS root saved: %s", nas_root)
    cfg.nas_root = nas_root
    return cfg


def _install_autostart_if_needed() -> None:
    """Install platform auto-start if not already installed."""
    if is_autostart_installed():
        return

    try:
        install_autostart()
        logger.info("Auto-start installed")
    except Exception as exc:
        logger.warning("Could not install auto-start: %s", exc)


# ---------------------------------------------------------------------------
# Sync loop (headless — no GUI)
# ---------------------------------------------------------------------------
def _run_sync_loop(cfg: SyncConfig, once: bool = False) -> None:
    """Run the main file sync loop. Pure background, no GUI."""
    if not os.path.isdir(cfg.nas_root):
        logger.error("NAS_ROOT does not exist: %s", cfg.nas_root)
        sys.exit(1)

    manifest = ManifestDB(cfg.manifest_path)
    manifest.open()

    # Wire retry callback for the "Retry All Failed" button in status window
    from .status_tray import sync_status
    def _retry_failed():
        count = manifest.reset_failed_to_pending()
        if count:
            logger.info("Retry: reset %d failed items to pending", count)
            sync_status.files_failed = 0
            sync_status.clear_errors()
            _watcher_trigger.set()  # wake the sync loop immediately
        return count
    sync_status.retry_callback = _retry_failed

    if probe_connectivity(cfg):
        _recover_interrupted(cfg, manifest)
        # Cleanup orphaned .tmp files from interrupted large uploads
        try:
            cleanup_orphaned_temps(cfg)
        except Exception as exc:
            logger.warning("Orphaned temp cleanup failed: %s", exc)
    else:
        recovered = manifest.reset_interrupted()
        if recovered:
            logger.info("Recovered %d interrupted uploads (offline, skipping storage check)", recovered)

    # Start file watcher for real-time detection (alongside poll cycle)
    watcher = None
    try:
        from .watcher import FileWatcher

        def _watcher_callback(p):
            logger.debug("Watcher: change detected %s", p)
            _watcher_trigger.set()

        watcher = FileWatcher(
            watch_paths=[cfg.nas_root],
            on_file_created=_watcher_callback,
            on_file_modified=_watcher_callback,
            on_file_deleted=_watcher_callback,
        )
        watcher.start()
        logger.info("File watcher started for %s", cfg.nas_root)
    except ImportError:
        logger.info("watchdog not available, using poll-only mode")
    except Exception as exc:
        logger.warning("File watcher failed to start: %s (falling back to poll-only)", exc)

    send_heartbeat(cfg, metadata={"event": "startup"})

    logger.info("File sync agent started — NAS: %s, yacht: %s, poll: %ds, source: %s",
                cfg.nas_root, cfg.yacht_id, cfg.poll_interval_s, cfg.source_type)

    # Write initial status
    _write_status({
        "state": "idle", "yacht_id": cfg.yacht_id, "nas_root": cfg.nas_root,
        "files_synced": 0, "files_pending": 0, "files_failed": 0, "files_dlq": 0,
        "last_sync": None, "pid": os.getpid(),
    })

    total_synced = 0
    try:
        while not _shutdown:
            # SIGHUP config reload
            global _reload_config
            if _reload_config:
                _reload_config = False
                try:
                    cfg = load_config()
                    logger.info("Config reloaded via SIGHUP")
                except Exception as exc:
                    logger.error("Config reload failed: %s", exc)

            # Check NAS is still accessible
            if not os.path.isdir(cfg.nas_root):
                logger.error("NAS disconnected: %s", cfg.nas_root)
                _write_status({"state": "error", "error": f"NAS disconnected: {cfg.nas_root}"})
                for _ in range(cfg.poll_interval_s):
                    if _shutdown or os.path.isdir(cfg.nas_root):
                        break
                    time.sleep(1)
                continue

            _write_status({"state": "syncing", "yacht_id": cfg.yacht_id, "pid": os.getpid()})

            stats = run_cycle(cfg, manifest)

            # Update status file
            status_counts = manifest.count_by_status()
            total_synced += stats.get("new", 0) + stats.get("modified", 0)

            # Update in-memory status for the status window
            from .status_tray import sync_status as _sync_status
            _sync_status.update_cycle(stats)
            _sync_status.files_dlq = status_counts.get("dlq", 0)

            _write_status({
                "state": "error" if stats.get("failed", 0) > 0 else "idle",
                "yacht_id": cfg.yacht_id,
                "nas_root": cfg.nas_root,
                "files_synced": total_synced,
                "files_pending": status_counts.get("pending", 0),
                "files_failed": status_counts.get("failed", 0),
                "files_dlq": status_counts.get("dlq", 0),
                "last_sync": datetime.now().isoformat(),
                "last_cycle": stats,
                "pid": os.getpid(),
            })

            if once:
                break

            # Sleep with watcher trigger — file changes wake the loop early
            _watcher_trigger.clear()
            for _ in range(cfg.poll_interval_s):
                if _shutdown:
                    break
                if _watcher_trigger.wait(timeout=1):
                    _watcher_trigger.clear()
                    logger.debug("Watcher triggered early sync cycle")
                    break
    finally:
        if watcher:
            watcher.stop()
        manifest.close()
        _write_status({"state": "stopped", "pid": os.getpid()})
        logger.info("File sync agent stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="CelesteOS Local File Sync Agent")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    args = parser.parse_args()

    from .log_config import configure_logging
    configure_logging()

    # 0. Prevent double-launch
    acquire_pid_lock()

    # 1. Check for existing credentials
    cfg = load_config()

    if not cfg.is_configured:
        # Might be first launch — try installation flow
        logger.info("Configuration incomplete — checking for first-launch installation")
        activated = _run_installation_flow(cfg)
        if activated:
            # Reload config after activation (Keychain now has secret)
            cfg = load_config()

    # 2. Ensure NAS folder is selected
    cfg = _ensure_nas_root(cfg)

    if not cfg.is_configured:
        logger.error("Configuration still incomplete after setup — exiting")
        logger.error("Set YACHT_ID, NAS_ROOT, SUPABASE_URL, SUPABASE_SERVICE_KEY")
        sys.exit(1)

    # 3. Install platform auto-start (first successful run only)
    _install_autostart_if_needed()

    # 3b. Wire sync_status with yacht metadata + retry callback
    from .status_tray import sync_status
    sync_status.yacht_name = cfg.yacht_name or cfg.yacht_id
    sync_status.yacht_id = cfg.yacht_id
    sync_status.nas_root = cfg.nas_root

    # 4. Start sync loop in background thread
    import threading
    sync_thread = threading.Thread(
        target=_run_sync_loop,
        args=(cfg,),
        kwargs={"once": args.once},
        daemon=True,
        name="sync-loop",
    )
    sync_thread.start()

    # 5. Run system tray on main thread (macOS requires NSApplication on main thread)
    try:
        from .status_tray import run_tray
        logger.info("Starting system tray icon")
        run_tray()  # blocks main thread — sync runs in background
    except Exception as exc:
        logger.warning("Status tray unavailable: %s — running headless", exc)
        sync_thread.join()  # no tray, just wait for sync


if __name__ == "__main__":
    main()
