"""
CelesteOS Status Tray
======================
Cross-platform system tray icon showing sync status with a status window.

macOS: rumps (NSStatusBar menu bar icon)
Windows: pystray + Pillow (system tray icon)

Features:
- Tray icon with color-coded status (idle/syncing/error)
- Click to see: files synced, last sync, errors, NAS path
- Platform-native notifications for errors
- Runs alongside the sync daemon in the same process
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("agent.status_tray")


# ---------------------------------------------------------------------------
# Status tracking (shared with daemon via this module)
# ---------------------------------------------------------------------------

class SyncStatus:
    """Thread-safe sync status shared between daemon and tray."""

    def __init__(self):
        self.state: str = "starting"  # starting, idle, syncing, error, paused
        self.last_sync: Optional[datetime] = None
        self.files_synced: int = 0
        self.files_pending: int = 0
        self.files_failed: int = 0
        self.files_dlq: int = 0
        self.current_file: str = ""
        self.errors: list = []  # last 10 errors
        self.nas_root: str = ""
        self.yacht_id: str = ""
        self.yacht_name: str = ""
        self.is_paused: bool = False
        self.total_errors_session: int = 0
        self.recent_activity: list = []  # last 20 file operations
        self.retry_callback: Optional[Callable] = None
        self._lock = threading.Lock()

    def update_cycle(self, stats: dict):
        """Called by daemon after each sync cycle."""
        with self._lock:
            self.state = "idle"
            self.last_sync = datetime.now()
            self.files_synced += stats.get("new", 0) + stats.get("modified", 0)
            self.files_pending = stats.get("skipped", 0)
            self.files_failed = stats.get("failed", 0)
            self.current_file = ""

            if stats.get("failed", 0) > 0:
                self.state = "error"
                self.total_errors_session += stats["failed"]

            if self.is_paused:
                self.state = "paused"

    def set_syncing(self, filename: str = ""):
        with self._lock:
            if self.is_paused:
                return
            self.state = "syncing"
            self.current_file = filename

    def add_error(self, error: str):
        with self._lock:
            self.errors.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": error,
            })
            self.errors = self.errors[-10:]  # keep last 10
            self.total_errors_session += 1
            self.state = "error"

    def clear_errors(self):
        with self._lock:
            self.errors = []
            self.state = "idle"

    def add_activity(self, filename: str, status: str):
        """Record a file operation. status: 'synced', 'failed', 'pending'."""
        with self._lock:
            self.recent_activity.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "filename": filename,
                "status": status,
            })
            self.recent_activity = self.recent_activity[-20:]  # keep last 20

    def snapshot(self) -> dict:
        """Thread-safe snapshot of current status."""
        with self._lock:
            return {
                "state": self.state,
                "last_sync": self.last_sync.strftime("%H:%M:%S") if self.last_sync else "Never",
                "files_synced": self.files_synced,
                "files_pending": self.files_pending,
                "files_failed": self.files_failed,
                "files_dlq": self.files_dlq,
                "current_file": self.current_file,
                "errors": list(self.errors),
                "nas_root": self.nas_root,
                "yacht_id": self.yacht_id,
                "yacht_name": self.yacht_name,
                "is_paused": self.is_paused,
                "total_errors_session": self.total_errors_session,
                "recent_activity": list(self.recent_activity),
            }


# Global status instance — daemon writes, tray reads
sync_status = SyncStatus()


# ---------------------------------------------------------------------------
# Notifications (cross-platform via platform module)
# ---------------------------------------------------------------------------

def notify(title: str, message: str, sound: bool = True):
    """Send a platform-native notification."""
    from .platform import send_notification
    send_notification(title, message, sound=sound)


def notify_error(error: str):
    """Notify user of a sync error."""
    sync_status.add_error(error)
    notify("Sync Error", error)


def notify_nas_disconnected(nas_root: str):
    """Notify user that NAS is disconnected."""
    notify("NAS Disconnected", f"Cannot access {nas_root}. Check your network connection.")


def notify_disk_full():
    """Notify user of disk full condition."""
    notify("Disk Full", "Local disk is full. Free space to continue syncing.")


def notify_sync_complete(new_files: int):
    """Notify user of successful sync (only for significant activity)."""
    if new_files > 0:
        notify("Sync Complete", f"{new_files} new file{'s' if new_files != 1 else ''} synced.", sound=False)


# ---------------------------------------------------------------------------
# macOS tray (rumps)
# ---------------------------------------------------------------------------

STATUS_ICONS = {
    "starting": "⏳",
    "idle": "●",
    "syncing": "↑",
    "error": "⚠",
    "paused": "⏸",
}


def _run_tray_mac():
    """Run macOS menu bar app using rumps. Blocks the calling thread."""
    import rumps

    class CelesteOSTray(rumps.App):
        def __init__(self):
            super().__init__(
                name="CelesteOS",
                title="●",
                quit_button=None,
            )
            self._build_menu()

        def _build_menu(self):
            self.menu = [
                rumps.MenuItem("CelesteOS", callback=self._toggle_status_window),
                None,
                rumps.MenuItem("Status: Starting...", callback=None),
                rumps.MenuItem("Last sync: Never", callback=None),
                rumps.MenuItem("Files synced: 0", callback=None),
                None,
                rumps.MenuItem("Open Status Window", callback=self._toggle_status_window),
                rumps.MenuItem("Open NAS Folder", callback=self._open_nas),
                rumps.MenuItem("Open Logs", callback=self._open_logs),
                None,
                rumps.MenuItem("Quit CelesteOS", callback=self._quit),
            ]

        @rumps.timer(3)
        def _update_status(self, _):
            snap = sync_status.snapshot()
            state = snap["state"]
            self.title = STATUS_ICONS.get(state, "●")

            items = list(self.menu.values())
            for item in items:
                if item is None or not hasattr(item, 'title'):
                    continue
                title = getattr(item, 'title', '')
                if title.startswith("Status:"):
                    state_labels = {
                        "starting": "Starting...",
                        "idle": "Idle",
                        "syncing": f"Syncing: {snap['current_file']}" if snap['current_file'] else "Syncing...",
                        "error": f"Error ({snap['files_failed']} failed)",
                        "paused": "Paused",
                    }
                    item.title = f"Status: {state_labels.get(state, state)}"
                elif title.startswith("Last sync:"):
                    item.title = f"Last sync: {snap['last_sync']}"
                elif title.startswith("Files synced:"):
                    item.title = f"Files synced: {snap['files_synced']}"

        def _toggle_status_window(self, _):
            try:
                from .status_window import toggle_status_window
                toggle_status_window()
            except Exception as exc:
                logger.warning("Status window failed: %s", exc)

        def _open_nas(self, _):
            from .platform import open_folder
            snap = sync_status.snapshot()
            nas = snap["nas_root"]
            if nas and os.path.isdir(nas):
                open_folder(nas)
            else:
                rumps.alert("NAS folder not found", f"Path: {nas or 'Not configured'}")

        def _open_logs(self, _):
            from .platform import get_config_dir, open_folder
            log_dir = get_config_dir() / "logs"
            if log_dir.is_dir():
                open_folder(str(log_dir))
            else:
                rumps.alert("No logs", f"Log directory not found: {log_dir}")

        def _quit(self, _):
            try:
                from .status_window import close_status_window
                close_status_window()
            except Exception:
                pass
            rumps.quit_application()

    app = CelesteOSTray()
    app.run()


# ---------------------------------------------------------------------------
# Windows tray (pystray + Pillow)
# ---------------------------------------------------------------------------

def _run_tray_pystray():
    """Run Windows system tray using pystray. Blocks the calling thread."""
    import pystray
    from PIL import Image, ImageDraw

    def _create_icon(color: str) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([8, 8, 56, 56], fill=color)
        return img

    ICONS = {
        "starting": _create_icon("#A1A1AA"),   # gray
        "idle": _create_icon("#4ADE80"),        # green
        "syncing": _create_icon("#60A5FA"),     # blue
        "error": _create_icon("#F87171"),       # red
        "paused": _create_icon("#FBBF24"),      # yellow
    }

    _icon_ref = [None]  # mutable ref for callbacks

    def _toggle_status_window(icon, item):
        try:
            from .status_window import toggle_status_window
            toggle_status_window()
        except Exception as exc:
            logger.warning("Status window failed: %s", exc)

    def _open_nas(icon, item):
        from .platform import open_folder
        snap = sync_status.snapshot()
        nas = snap["nas_root"]
        if nas and os.path.isdir(nas):
            open_folder(nas)

    def _open_logs(icon, item):
        from .platform import get_config_dir, open_folder
        log_dir = get_config_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        open_folder(str(log_dir))

    def _quit(icon, item):
        try:
            from .status_window import close_status_window
            close_status_window()
        except Exception:
            pass
        icon.stop()

    # Cache a single snapshot for all dynamic menu labels
    _cached_snap = [sync_status.snapshot()]

    def _snap():
        return _cached_snap[0]

    menu = pystray.Menu(
        pystray.MenuItem("CelesteOS", _toggle_status_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _: f"Status: {_snap()['state'].title()}",
            None, enabled=False,
        ),
        pystray.MenuItem(
            lambda _: f"Last sync: {_snap()['last_sync']}",
            None, enabled=False,
        ),
        pystray.MenuItem(
            lambda _: f"Files synced: {_snap()['files_synced']}",
            None, enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Status Window", _toggle_status_window),
        pystray.MenuItem("Open NAS Folder", _open_nas),
        pystray.MenuItem("Open Logs", _open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit CelesteOS", _quit),
    )

    icon = pystray.Icon("CelesteOS", ICONS["idle"], "CelesteOS", menu)
    _icon_ref[0] = icon

    # Background thread to update icon + cached snapshot every 3s
    def _updater():
        while icon.visible:
            snap = sync_status.snapshot()
            _cached_snap[0] = snap
            state = snap["state"]
            icon.icon = ICONS.get(state, ICONS["idle"])
            icon.title = f"CelesteOS — {state.title()} | {snap['files_synced']} synced"
            time.sleep(3)

    updater_thread = threading.Thread(target=_updater, daemon=True, name="tray-updater")
    updater_thread.start()

    icon.run()  # blocks


# ---------------------------------------------------------------------------
# Cross-platform entry point
# ---------------------------------------------------------------------------

def run_tray():
    """Run the platform-appropriate system tray. Blocks the calling thread."""
    if sys.platform == "darwin":
        try:
            import rumps  # noqa: F401
            _run_tray_mac()
        except ImportError:
            logger.warning("rumps not available — status tray disabled")
    elif sys.platform == "win32":
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            _run_tray_pystray()
        except ImportError as exc:
            logger.warning("pystray/Pillow not available — status tray disabled: %s", exc)
    else:
        logger.warning("No tray support for platform: %s", sys.platform)


def start_tray(status: Optional[SyncStatus] = None):
    """
    Start the tray app in a background thread.
    Call this from the daemon after setup is complete.
    """
    global sync_status
    if status:
        sync_status = status

    def _run():
        try:
            run_tray()
        except Exception as exc:
            logger.warning("Status tray failed: %s", exc)

    thread = threading.Thread(target=_run, daemon=True, name="status-tray")
    thread.start()
    logger.info("Status tray started")


if __name__ == "__main__":
    # Test mode — run standalone
    logging.basicConfig(level=logging.INFO)

    sync_status.yacht_name = "M/Y Test"
    sync_status.nas_root = "/tmp/test-nas"
    sync_status.state = "idle"
    sync_status.files_synced = 42
    sync_status.last_sync = datetime.now()

    print("Starting status tray (standalone test)...")
    run_tray()
