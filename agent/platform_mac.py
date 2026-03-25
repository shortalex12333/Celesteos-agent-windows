"""
macOS Platform Implementation
==============================
Implements the platform abstraction interface for macOS (darwin).
Extracted from daemon.py, config.py, folder_selector.py, status_tray.py.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("agent.platform_mac")

# ---------------------------------------------------------------------------
# PID lock (fcntl.flock)
# ---------------------------------------------------------------------------
_pid_lock_fd = None


def acquire_pid_lock() -> None:
    """Acquire an exclusive lock on ~/.celesteos/agent.pid to prevent double-launch."""
    global _pid_lock_fd
    pid_dir = get_config_dir()
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_dir / "agent.pid"

    _pid_lock_fd = open(pid_file, "w")
    try:
        fcntl.flock(_pid_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _pid_lock_fd.write(str(os.getpid()))
        _pid_lock_fd.flush()
    except OSError:
        logger.error("Another instance is already running (pid lock: %s)", pid_file)
        sys.exit(0)


def release_pid_lock() -> None:
    """Release the PID lock (usually happens on process exit)."""
    global _pid_lock_fd
    if _pid_lock_fd:
        try:
            fcntl.flock(_pid_lock_fd.fileno(), fcntl.LOCK_UN)
            _pid_lock_fd.close()
        except OSError:
            pass
        _pid_lock_fd = None


# ---------------------------------------------------------------------------
# Config reload signal (SIGHUP)
# ---------------------------------------------------------------------------
def register_reload_signal(handler: Callable) -> None:
    """Register SIGHUP handler for config reload."""
    signal.signal(signal.SIGHUP, handler)


# ---------------------------------------------------------------------------
# Config directory
# ---------------------------------------------------------------------------
def get_config_dir() -> Path:
    """Return ~/.celesteos/ on macOS."""
    return Path.home() / ".celesteos"


# ---------------------------------------------------------------------------
# Keychain (macOS security CLI)
# ---------------------------------------------------------------------------
def get_keychain_password(service: str, account: str) -> str:
    """Retrieve a password from macOS Keychain. Returns empty string on failure."""
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", service,
                "-a", account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            pw = result.stdout.strip()
            if pw:
                return pw
        logger.debug("Keychain lookup failed for %s/%s: rc=%d", service, account, result.returncode)
    except Exception as exc:
        logger.warning("Keychain retrieval error: %s", exc)
    return ""


def set_keychain_password(service: str, account: str, password: str) -> bool:
    """Store a password in macOS Keychain. Returns True on success."""
    # Delete existing entry if present
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
    )
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-s", service,
            "-a", account,
            "-w", password,
            "-U",
        ],
        capture_output=True,
    )
    return result.returncode == 0


def delete_keychain_password(service: str, account: str) -> bool:
    """Delete a password from macOS Keychain."""
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Auto-start (launchd)
# ---------------------------------------------------------------------------
def install_autostart() -> bool:
    """Install launchd plist for auto-start."""
    try:
        from .launchd import install_launchd
        return install_launchd()
    except Exception as exc:
        logger.warning("Could not install launchd auto-start: %s", exc)
        return False


def uninstall_autostart() -> bool:
    """Remove launchd plist."""
    try:
        from .launchd import uninstall_launchd
        return uninstall_launchd()
    except Exception as exc:
        logger.warning("Could not uninstall launchd auto-start: %s", exc)
        return False


def is_autostart_installed() -> bool:
    """Check if launchd auto-start plist exists."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.celeste7.celesteos.agent.plist"
    return plist_path.exists()


# ---------------------------------------------------------------------------
# Open folder (Finder)
# ---------------------------------------------------------------------------
def open_folder(path: str) -> None:
    """Open a folder in Finder."""
    subprocess.run(["open", str(path)], capture_output=True)


# ---------------------------------------------------------------------------
# Notifications (rumps / osascript)
# ---------------------------------------------------------------------------
def send_notification(title: str, message: str, sound: bool = True) -> None:
    """Send a macOS notification."""
    try:
        try:
            import rumps
            rumps.notification(
                title="CelesteOS",
                subtitle=title,
                message=message,
                sound=sound,
            )
            return
        except ImportError:
            pass

        # Fallback: osascript
        sound_clause = ' sound name "default"' if sound else ""
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "CelesteOS" subtitle "{title}"{sound_clause}'
        ], capture_output=True, timeout=5)
    except Exception as exc:
        logger.debug("Notification failed: %s", exc)


# ---------------------------------------------------------------------------
# NAS discovery (/Volumes)
# ---------------------------------------------------------------------------
NAS_PATTERNS = [
    r"(?i)synology", r"(?i)qnap", r"(?i)nas", r"(?i)diskstation",
    r"(?i)turbonas", r"(?i)yacht", r"(?i)vessel", r"(?i)marine", r"(?i)celeste",
]


def get_nas_candidates() -> list[str]:
    """Scan /Volumes/ for directories that look like NAS mounts."""
    volumes_dir = Path("/Volumes")
    if not volumes_dir.is_dir():
        return []

    candidates = []
    try:
        for entry in volumes_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == "Macintosh HD":
                continue
            if not os.access(str(entry), os.W_OK):
                continue
            name = entry.name
            for pattern in NAS_PATTERNS:
                if re.search(pattern, name):
                    candidates.append(str(entry))
                    break
            else:
                if entry.is_mount():
                    candidates.append(str(entry))
    except PermissionError:
        pass

    return sorted(candidates)


def get_default_browse_dir() -> str:
    """Default directory for file browser on macOS."""
    return "/Volumes"
