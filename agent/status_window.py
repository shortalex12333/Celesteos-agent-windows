"""
CelesteOS Branded Status Window
================================
A single branded window (like Dropbox / Time Machine) that shows sync progress,
file activity, errors, and actions. Opens when the tray icon is clicked.

Uses pywebview + embedded HTML (same pattern as installer_ui.py).

Architecture:
    Menu bar icon (rumps) → click → pywebview status window (HTML/CSS/JS)
                                        → StatusAPI bridge → SyncStatus (shared)
"""

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("agent.status_window")

# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

STATUS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CelesteOS</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

  * { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    /* Brand tokens — CelesteOS design system */
    --surface-base: #0c0b0a;
    --surface: #181614;
    --surface-el: #1e1b18;
    --surface-hover: #242424;
    --bg: #0c0b0a;
    --mark: #5AABCC;
    --teal: #3A7C9D;
    --teal-bg: rgba(58,124,157,0.12);
    --mark-hover: rgba(58,124,157,0.22);
    --border: rgba(255,255,255,0.07);
    --border-bright: rgba(255,255,255,0.11);
    --txt: rgba(255,255,255,0.92);
    --txt2: rgba(255,255,255,0.55);
    --txt3: rgba(255,255,255,0.70);
    --txt-ghost: rgba(255,255,255,0.40);
    --red: #C0503A;
    --green: #4A9468;
    --amber: #C4893B;
    --blue: #5B8DEF;
    --mono: 'SF Mono', ui-monospace, 'Fira Code', monospace;
    --sans: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
    /* Shadows */
    --shadow-tip: 0 4px 12px rgba(0,0,0,0.40);
    --shadow-drop: 0 8px 24px rgba(0,0,0,0.50);
  }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--txt);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
    user-select: none;
    position: relative;
  }

  /* Subtle brand texture — very low opacity, non-decorative */
  body::before {
    content: '';
    position: absolute;
    inset: 0;
    background: url('data:image/jpeg;base64,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') center/cover no-repeat;
    opacity: 0.04;
    z-index: 0;
    pointer-events: none;
  }

  /* Header — glass effect */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px 12px;
    border-bottom: 1px solid var(--border);
    background: rgba(24,22,20,0.75);
    -webkit-backdrop-filter: blur(16px);
    backdrop-filter: blur(16px);
    position: relative;
    z-index: 1;
  }
  .header-left {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .header-logo {
    width: 24px; height: 24px;
    opacity: 0.7;
  }
  .header-text h1 {
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.3px;
    line-height: 1.2;
  }
  .header-text .yacht-name {
    font-size: 11px;
    color: var(--txt3);
    margin-top: 1px;
  }
  .state-badge {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    font-weight: 600;
    padding: 5px 12px;
    border-radius: 6px;
    background: var(--surface-el);
    border: 1px solid var(--border);
  }
  .state-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--txt-ghost);
    transition: background 0.3s;
  }
  .state-dot.idle { background: var(--green); box-shadow: 0 0 6px rgba(74,148,104,0.4); }
  .state-dot.syncing { background: var(--mark); box-shadow: 0 0 6px rgba(90,171,204,0.4); animation: pulse 1.5s ease-in-out infinite; }
  .state-dot.error { background: var(--red); box-shadow: 0 0 6px rgba(192,80,58,0.4); }
  .state-dot.paused { background: var(--amber); box-shadow: 0 0 6px rgba(196,137,59,0.4); }
  .state-dot.starting { background: var(--txt3); animation: pulse 1.5s ease-in-out infinite; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }

  /* Stats grid */
  .stats {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
    position: relative;
    z-index: 1;
  }
  .stat {
    background: var(--bg);
    padding: 14px 16px;
    text-align: center;
  }
  .stat-value {
    font-size: 22px;
    font-weight: 700;
    font-family: var(--mono);
    line-height: 1;
    letter-spacing: -0.5px;
  }
  .stat-value.error { color: var(--red); }
  .stat-label {
    font-size: 10px;
    color: var(--txt3);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-top: 5px;
    font-weight: 500;
  }

  /* Meta row */
  .meta {
    padding: 10px 20px;
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--txt3);
    font-family: var(--mono);
    border-bottom: 1px solid var(--border);
    position: relative; z-index: 1;
  }

  /* Activity list */
  .activity-header {
    padding: 12px 20px 8px;
    font-size: 11px;
    font-weight: 600;
    color: var(--txt2);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    position: relative; z-index: 1;
  }
  .activity-list {
    height: 200px;
    overflow-y: auto;
    padding: 0 12px;
    position: relative; z-index: 1;
  }
  .activity-list::-webkit-scrollbar { width: 3px; }
  .activity-list::-webkit-scrollbar-track { background: transparent; }
  .activity-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
  .activity-list::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.14); }

  .activity-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    min-height: 36px;
    border-radius: 8px;
    font-size: 12px;
    transition: background 0.15s;
  }
  .activity-row:hover { background: var(--surface); }
  .activity-time {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--txt-ghost);
    flex-shrink: 0;
    width: 50px;
  }
  .activity-icon {
    flex-shrink: 0;
    width: 16px;
    text-align: center;
    font-size: 12px;
  }
  .activity-icon.synced { color: var(--green); }
  .activity-icon.failed { color: var(--red); }
  .activity-icon.pending { color: var(--amber); }
  .activity-file {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--txt2);
  }
  .activity-retry {
    font-size: 10px;
    color: var(--red);
    cursor: pointer;
    flex-shrink: 0;
    padding: 3px 8px;
    border-radius: 6px;
    border: 1px solid rgba(192,80,58,0.4);
    background: transparent;
    transition: background 0.15s, border-color 0.15s;
    font-family: var(--sans);
    font-weight: 500;
  }
  .activity-retry:hover { background: rgba(192,80,58,0.12); border-color: var(--red); }

  .empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--txt-ghost);
    font-size: 12px;
  }

  /* Syncing current file */
  .current-file {
    padding: 8px 20px;
    font-size: 11px;
    font-family: var(--mono);
    color: var(--mark);
    border-bottom: 1px solid var(--border);
    display: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    background: var(--teal-bg);
    position: relative; z-index: 1;
  }
  .current-file.active { display: block; }
  .current-file::before {
    content: '\\2191 ';
    opacity: 0.6;
  }

  /* Action buttons */
  .actions {
    display: flex;
    gap: 8px;
    padding: 12px 20px 16px;
    border-top: 1px solid var(--border);
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    background: var(--bg);
    z-index: 2;
  }
  .action-btn {
    flex: 1;
    padding: 9px 0;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--txt2);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
    font-family: var(--sans);
    min-height: 36px;
  }
  .action-btn:hover {
    border-color: var(--mark);
    color: var(--txt);
    background: var(--surface-el);
  }
  .action-btn:active { opacity: 0.85; }
  .action-btn.primary {
    background: var(--teal);
    border-color: var(--teal);
    color: #fff;
  }
  .action-btn.primary:hover { opacity: 0.88; }
  .action-btn.warn {
    border-color: rgba(196,137,59,0.4);
    color: var(--amber);
  }
  .action-btn.warn:hover { background: rgba(196,137,59,0.08); border-color: var(--amber); }
</style>
</head>
<body>

  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <img class="header-logo" src="data:image/png;base64,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" alt="">
      <div class="header-text">
        <h1>CelesteOS</h1>
        <div class="yacht-name" id="yacht-name">&mdash;</div>
      </div>
    </div>
    <div class="state-badge">
      <div class="state-dot" id="state-dot"></div>
      <span id="state-label">Starting</span>
    </div>
  </div>

  <!-- Stats grid -->
  <div class="stats">
    <div class="stat">
      <div class="stat-value" id="stat-synced">0</div>
      <div class="stat-label">Synced</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="stat-pending">0</div>
      <div class="stat-label">Pending</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="stat-failed">0</div>
      <div class="stat-label">Failed</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="stat-dlq">0</div>
      <div class="stat-label">DLQ</div>
    </div>
  </div>

  <!-- Meta -->
  <div class="meta">
    <span>Last sync: <span id="meta-last-sync">Never</span></span>
    <span>NAS: <span id="meta-nas">&mdash;</span></span>
  </div>

  <!-- Current file indicator -->
  <div class="current-file" id="current-file"></div>

  <!-- Recent activity -->
  <div class="activity-header">Recent Activity</div>
  <div class="activity-list" id="activity-list">
    <div class="empty-state">No file activity yet</div>
  </div>

  <!-- Action buttons -->
  <div class="actions">
    <button class="action-btn" onclick="doOpenNAS()">Open NAS</button>
    <button class="action-btn" onclick="doOpenLogs()">View Logs</button>
    <button class="action-btn" id="btn-retry" onclick="doRetry()" style="display:none;">Retry All Failed</button>
    <button class="action-btn warn" id="btn-pause" onclick="doTogglePause()">Pause Sync</button>
  </div>

<script>
  const ICONS = { synced: '\\u2713', failed: '\\u2717', pending: '\\u2026' };
  const STATE_LABELS = {
    starting: 'Starting',
    idle: 'Idle',
    syncing: 'Syncing',
    error: 'Error',
    paused: 'Paused',
  };

  function pyCall(method, ...args) {
    return window.pywebview.api[method](...args);
  }

  async function refresh() {
    try {
      const raw = await pyCall('get_status');
      const s = JSON.parse(raw);

      // State badge
      const dot = document.getElementById('state-dot');
      dot.className = 'state-dot ' + s.state;
      document.getElementById('state-label').textContent = STATE_LABELS[s.state] || s.state;

      // Yacht name
      document.getElementById('yacht-name').textContent = s.yacht_name || s.yacht_id || '—';

      // Stats
      document.getElementById('stat-synced').textContent = s.files_synced;
      document.getElementById('stat-pending').textContent = s.files_pending;
      const failedEl = document.getElementById('stat-failed');
      failedEl.textContent = s.files_failed;
      failedEl.className = 'stat-value' + (s.files_failed > 0 ? ' error' : '');

      // DLQ count
      const dlqEl = document.getElementById('stat-dlq');
      dlqEl.textContent = s.files_dlq || 0;
      dlqEl.className = 'stat-value' + ((s.files_dlq || 0) > 0 ? ' error' : '');

      // Show retry button if there are failed or DLQ files
      const retryBtn = document.getElementById('btn-retry');
      retryBtn.style.display = (s.files_failed > 0 || (s.files_dlq || 0) > 0) ? '' : 'none';

      // Meta
      document.getElementById('meta-last-sync').textContent = s.last_sync;
      const nasPath = s.nas_root || '—';
      document.getElementById('meta-nas').textContent =
        nasPath.length > 28 ? '...' + nasPath.slice(-25) : nasPath;

      // Current file
      const cfEl = document.getElementById('current-file');
      if (s.state === 'syncing' && s.current_file) {
        cfEl.textContent = s.current_file;
        cfEl.classList.add('active');
      } else {
        cfEl.classList.remove('active');
      }

      // Pause button
      const pauseBtn = document.getElementById('btn-pause');
      if (s.is_paused) {
        pauseBtn.textContent = 'Resume';
        pauseBtn.className = 'action-btn primary';
      } else {
        pauseBtn.textContent = 'Pause Sync';
        pauseBtn.className = 'action-btn warn';
      }

      // Activity list
      renderActivity(s.recent_activity || []);

    } catch (e) {
      // pywebview not ready yet — retry
    }
  }

  function renderActivity(items) {
    const list = document.getElementById('activity-list');
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">No file activity yet</div>';
      return;
    }

    // Reverse so newest is on top
    const reversed = items.slice().reverse();
    let html = '';
    for (const item of reversed) {
      const icon = ICONS[item.status] || '?';
      const shortFile = item.filename.length > 45
        ? '...' + item.filename.slice(-42)
        : item.filename;
      const retryBtn = item.status === 'failed'
        ? '<button class="activity-retry" onclick="doRetry()">retry</button>'
        : '';
      html += '<div class="activity-row">'
        + '<span class="activity-time">' + item.time + '</span>'
        + '<span class="activity-icon ' + item.status + '">' + icon + '</span>'
        + '<span class="activity-file" title="' + item.filename + '">' + shortFile + '</span>'
        + retryBtn
        + '</div>';
    }
    list.innerHTML = html;
  }

  async function doOpenNAS() { await pyCall('open_nas'); }
  async function doOpenLogs() { await pyCall('open_logs'); }
  async function doTogglePause() { await pyCall('toggle_pause'); refresh(); }
  async function doRetry() { await pyCall('retry_failed'); }

  // Auto-refresh every 2 seconds
  setInterval(refresh, 2000);

  // Initial load (wait for pywebview bridge)
  window.addEventListener('pywebviewready', refresh);
  setTimeout(refresh, 500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Python API bridge — exposed to JavaScript via pywebview
# ---------------------------------------------------------------------------

class StatusAPI:
    """
    Bridge between the HTML status window and the Python daemon.
    Methods are called from JavaScript via window.pywebview.api.
    """

    def get_status(self) -> str:
        """Return a JSON snapshot of the current sync status."""
        from .status_tray import sync_status
        return json.dumps(sync_status.snapshot())

    def open_nas(self) -> str:
        """Open the NAS root folder in file manager."""
        from .platform import open_folder
        from .status_tray import sync_status
        snap = sync_status.snapshot()
        nas = snap["nas_root"]
        if nas and os.path.isdir(nas):
            open_folder(nas)
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "NAS folder not found"})

    def open_logs(self) -> str:
        """Open the log directory in file manager."""
        from .platform import get_config_dir, open_folder
        log_dir = get_config_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        open_folder(str(log_dir))
        return json.dumps({"ok": True})

    def toggle_pause(self) -> str:
        """Toggle the pause state."""
        from .status_tray import sync_status
        with sync_status._lock:
            sync_status.is_paused = not sync_status.is_paused
            if sync_status.is_paused:
                sync_status.state = "paused"
            else:
                sync_status.state = "idle"
        logger.info("Sync %s", "paused" if sync_status.is_paused else "resumed")
        return json.dumps({"paused": sync_status.is_paused})

    def retry_failed(self) -> str:
        """Reset failed/DLQ files to pending and clear errors."""
        from .status_tray import sync_status
        reset_count = 0
        if sync_status.retry_callback:
            try:
                reset_count = sync_status.retry_callback()
            except Exception as exc:
                logger.warning("Retry callback failed: %s", exc)
        sync_status.clear_errors()
        logger.info("Retry requested — %d files reset to pending, errors cleared", reset_count)
        return json.dumps({"ok": True, "reset": reset_count})


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------

_window = None
_window_lock = threading.Lock()


def toggle_status_window():
    """Open the status window, or focus it if already open."""
    global _window

    with _window_lock:
        if _window is not None:
            try:
                # Window exists — try to bring to front
                _window.show()
                _window.restore()
                return
            except Exception:
                # Window was destroyed
                _window = None

    # Open in a new thread so we don't block rumps
    thread = threading.Thread(target=_open_window, daemon=True, name="status-window")
    thread.start()


def _open_window():
    """Create and show the status window (blocking until closed)."""
    global _window
    try:
        import webview
    except ImportError:
        logger.warning("pywebview not installed — cannot open status window")
        return

    api = StatusAPI()
    _window = webview.create_window(
        "CelesteOS",
        html=STATUS_HTML,
        js_api=api,
        width=420,
        height=560,
        resizable=False,
        background_color="#0c0b0a",
        on_top=False,
    )

    webview.start(debug=False)

    # Window was closed
    with _window_lock:
        _window = None


def close_status_window():
    """Destroy the status window if open."""
    global _window
    with _window_lock:
        if _window is not None:
            try:
                _window.destroy()
            except Exception:
                pass
            _window = None


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time
    from datetime import datetime
    logging.basicConfig(level=logging.INFO)

    # Populate mock data for testing
    from .status_tray import sync_status

    sync_status.yacht_name = "M/Y Freedom"
    sync_status.yacht_id = "test-123"
    sync_status.nas_root = "/Volumes/YachtNAS"
    sync_status.state = "idle"
    sync_status.files_synced = 142
    sync_status.files_pending = 3
    sync_status.files_failed = 0
    sync_status.last_sync = datetime.now()

    # Add some mock activity
    sync_status.add_activity("Engine/CAT_C32_Manual.pdf", "synced")
    sync_status.add_activity("Safety/Fire_System.pdf", "synced")
    sync_status.add_activity("Deck/Gangway_Manual.pdf", "synced")
    sync_status.add_activity("Certs/SOLAS_2024.pdf", "failed")
    sync_status.add_activity("Nav/Radar_Config.pdf", "synced")

    sync_status.add_error("Certs/SOLAS_2024.pdf: upload timeout after 30s")

    print("Opening status window with mock data...")
    _open_window()
