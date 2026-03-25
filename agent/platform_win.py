"""
Windows Platform Implementation
=================================
Implements the platform abstraction interface for Windows (win32).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import string
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("agent.platform_win")

# ---------------------------------------------------------------------------
# PID lock (named mutex)
# ---------------------------------------------------------------------------
_mutex_handle = None
MUTEX_NAME = "Global\\CelesteOS_Agent_Mutex"


def acquire_pid_lock() -> None:
    """Acquire a named mutex to prevent double-launch."""
    global _mutex_handle

    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)

    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        logger.error("Another instance is already running (mutex: %s)", MUTEX_NAME)
        if _mutex_handle:
            kernel32.CloseHandle(_mutex_handle)
            _mutex_handle = None
        sys.exit(0)

    # Write PID file for diagnostics (not used for locking)
    pid_dir = get_config_dir()
    pid_dir.mkdir(parents=True, exist_ok=True)
    try:
        (pid_dir / "agent.pid").write_text(str(os.getpid()))
    except OSError:
        pass


def release_pid_lock() -> None:
    """Release the named mutex."""
    global _mutex_handle
    if _mutex_handle:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except OSError:
            pass
        _mutex_handle = None


# ---------------------------------------------------------------------------
# Config reload signal (sentinel file watcher — no SIGHUP on Windows)
# ---------------------------------------------------------------------------
_reload_watcher_thread: Optional[threading.Thread] = None


def register_reload_signal(handler: Callable) -> None:
    """Watch for %APPDATA%\\CelesteOS\\reload_config sentinel file.

    When the file appears, call *handler* and delete the sentinel.
    """
    global _reload_watcher_thread

    sentinel = get_config_dir() / "reload_config"

    def _watch():
        while True:
            try:
                if sentinel.exists():
                    sentinel.unlink(missing_ok=True)
                    # handler signature matches signal handler: (signum, frame)
                    handler(None, None)
            except Exception as exc:
                logger.debug("Reload watcher error: %s", exc)
            time.sleep(2)

    _reload_watcher_thread = threading.Thread(target=_watch, daemon=True, name="reload-watcher")
    _reload_watcher_thread.start()


# ---------------------------------------------------------------------------
# Config directory
# ---------------------------------------------------------------------------
def get_config_dir() -> Path:
    """Return %APPDATA%\\CelesteOS on Windows."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "CelesteOS"
    return Path.home() / "AppData" / "Roaming" / "CelesteOS"


# ---------------------------------------------------------------------------
# Credential storage (keyring → Windows Credential Manager)
# ---------------------------------------------------------------------------
def get_keychain_password(service: str, account: str) -> str:
    """Retrieve a password from Windows Credential Manager. Returns empty string on failure."""
    try:
        import keyring
        result = keyring.get_password(service, account)
        return result or ""
    except Exception as exc:
        logger.warning("Credential retrieval error: %s", exc)
    return ""


def set_keychain_password(service: str, account: str, password: str) -> bool:
    """Store a password in Windows Credential Manager."""
    try:
        import keyring
        keyring.set_password(service, account, password)
        return True
    except Exception as exc:
        logger.warning("Credential storage error: %s", exc)
        return False


def delete_keychain_password(service: str, account: str) -> bool:
    """Delete a password from Windows Credential Manager."""
    try:
        import keyring
        keyring.delete_password(service, account)
        return True
    except Exception as exc:
        logger.warning("Credential deletion error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Auto-start (Registry Run key)
# ---------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "CelesteOS"


def _get_exe_path() -> str:
    """Return the executable path for the registry entry."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle
        return sys.executable
    # Development: python -m agent
    return f'"{sys.executable}" -m agent'


def install_autostart() -> bool:
    """Add CelesteOS to Windows startup via registry."""
    try:
        import winreg
        exe_path = _get_exe_path()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe_path)
        logger.info("Auto-start installed: %s", exe_path)
        return True
    except Exception as exc:
        logger.warning("Could not install auto-start: %s", exc)
        return False


def uninstall_autostart() -> bool:
    """Remove CelesteOS from Windows startup."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _APP_NAME)
        return True
    except FileNotFoundError:
        return True  # Already removed
    except Exception as exc:
        logger.warning("Could not uninstall auto-start: %s", exc)
        return False


def is_autostart_installed() -> bool:
    """Check if CelesteOS is registered for auto-start."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Open folder (Explorer)
# ---------------------------------------------------------------------------
def open_folder(path: str) -> None:
    """Open a folder in Windows Explorer."""
    try:
        os.startfile(str(path))
    except OSError as exc:
        logger.warning("Could not open folder %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Notifications (plyer)
# ---------------------------------------------------------------------------
def send_notification(title: str, message: str, sound: bool = True) -> None:
    """Send a Windows toast notification."""
    try:
        from plyer import notification
        notification.notify(
            title="CelesteOS",
            message=f"{title}: {message}" if title else message,
            app_name="CelesteOS",
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Notification failed: %s", exc)


# ---------------------------------------------------------------------------
# NAS discovery (drive letters + network drives)
# ---------------------------------------------------------------------------
NAS_PATTERNS = [
    r"(?i)synology", r"(?i)qnap", r"(?i)nas", r"(?i)diskstation",
    r"(?i)turbonas", r"(?i)yacht", r"(?i)vessel", r"(?i)marine", r"(?i)celeste",
]


def get_nas_candidates() -> list[str]:
    """Scan drive letters D:-Z: for directories that look like NAS mounts."""
    candidates = []

    # Check mapped network drives and external volumes
    for letter in string.ascii_uppercase[3:]:  # D through Z
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        # Check if writable
        if not os.access(drive, os.W_OK):
            continue

        # Check drive label against NAS patterns
        label = _get_volume_label(letter)
        for pattern in NAS_PATTERNS:
            if re.search(pattern, label):
                candidates.append(drive)
                break
        else:
            # Any network drive is a candidate
            if _is_network_drive(letter):
                candidates.append(drive)

    return sorted(candidates)


def _get_volume_label(letter: str) -> str:
    """Get the volume label for a drive letter."""
    try:
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(256)
        result = kernel32.GetVolumeInformationW(
            f"{letter}:\\", buf, 256, None, None, None, None, 0
        )
        if result:
            return buf.value
    except Exception:
        pass
    return ""


def _is_network_drive(letter: str) -> bool:
    """Check if a drive letter is a network (mapped) drive."""
    try:
        DRIVE_REMOTE = 4
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
        return drive_type == DRIVE_REMOTE
    except Exception:
        return False


def get_default_browse_dir() -> str:
    """Default directory for file browser on Windows."""
    return str(Path.home())
