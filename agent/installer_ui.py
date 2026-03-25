"""
CelesteOS Installer UI
=======================
Native macOS window wrapping a local HTML UI for the first-launch experience.
Uses pywebview to render a branded setup wizard.

Flow:
    Step 1: Welcome + Register → sends registration request, triggers 2FA email
    Step 2: Enter 2FA code → verifies code, receives shared_secret
    Step 3: Select NAS folder → saves to ~/.celesteos/.env.local
    Step 4: Success → shows confirmation, starts sync

Design:
    - Dark theme matching Celeste design tokens
    - Teal (#5AABCC) for interactive elements
    - Inter font for human text
    - Functional MVP — correctness over polish
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent.installer_ui")


# ---------------------------------------------------------------------------
# HTML Template — complete single-page app
# ---------------------------------------------------------------------------

INSTALLER_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CelesteOS Setup</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    /* Brand tokens — CelesteOS design system */
    --surface-base: #0c0b0a;
    --surface: rgba(24,22,20,0.55);
    --surface-el: rgba(30,27,24,0.65);
    --surface-hover: #242424;
    --mark: #5AABCC;
    --teal: #3A7C9D;
    --teal-bg: rgba(58,124,157,0.12);
    --mark-hover: rgba(58,124,157,0.22);
    --bg: #0c0b0a;
    --border: rgba(255,255,255,0.07);
    --border-bright: rgba(255,255,255,0.12);
    --txt: rgba(255,255,255,0.92);
    --txt2: rgba(255,255,255,0.55);
    --txt3: rgba(255,255,255,0.38);
    --txt-ghost: rgba(255,255,255,0.20);
    --red: #C0503A;
    --green: #4A9468;
    --mono: 'SF Mono', ui-monospace, 'Fira Code', monospace;
    --sans: -apple-system, BlinkMacSystemFont, system-ui, 'Segoe UI', Roboto, sans-serif;
    /* Shadows */
    --shadow-card: 0 0 0 1px rgba(0,0,0,0.50), 0 8px 32px rgba(0,0,0,0.55);
    --shadow-btn: 0 1px 3px rgba(0,0,0,0.4);
    --shadow-input: 0 0 0 1px rgba(0,0,0,0.3) inset;
  }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--txt);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 32px 32px 28px;
    -webkit-font-smoothing: antialiased;
    position: relative;
    overflow: hidden;
  }

  /* Background — brand wave image */
  body::before {
    content: '';
    position: absolute;
    inset: 0;
    background: url('data:image/jpeg;base64,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') center/cover no-repeat;
    opacity: 0.18;
    z-index: 0;
  }

  /* Dark vignette over the background */
  body::after {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(ellipse at center, transparent 30%, var(--bg) 85%);
    z-index: 1;
  }

  .container {
    width: 100%; max-width: 400px;
    position: relative; z-index: 2;
    animation: fadeIn 0.5s ease-out;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Brand header */
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 24px;
  }
  .brand-logo {
    width: 32px; height: 32px;
    opacity: 0.85;
  }
  .brand-text .logo {
    font-size: 18px; font-weight: 700; letter-spacing: -0.4px;
    line-height: 1.2;
  }
  .brand-text .subtitle {
    color: var(--txt3); font-size: 11px; letter-spacing: 1.2px;
    text-transform: uppercase; font-weight: 500;
  }

  .step { display: none; }
  .step.active { display: block; animation: fadeIn 0.35s ease-out; }

  /* Glass card */
  .card {
    background: var(--surface);
    -webkit-backdrop-filter: blur(24px) saturate(1.2);
    backdrop-filter: blur(24px) saturate(1.2);
    border-radius: 16px;
    padding: 28px;
    border: 1px solid var(--border-bright);
    border-top-color: rgba(255,255,255,0.14);
    border-bottom-color: rgba(0,0,0,0.25);
    box-shadow: var(--shadow-card);
  }

  .card h2 {
    font-size: 16px; font-weight: 600; margin-bottom: 6px;
    letter-spacing: -0.2px;
  }
  .card p {
    color: var(--txt2); font-size: 13px; line-height: 1.55;
    margin-bottom: 16px;
  }

  .yacht-name { color: var(--mark); font-weight: 600; }
  .email-masked { color: var(--mark); font-weight: 500; }

  label {
    display: block; font-size: 11px; color: var(--txt3);
    margin-bottom: 6px; text-transform: uppercase;
    letter-spacing: 0.8px; font-weight: 500;
  }

  input[type="text"] {
    width: 100%; padding: 11px 14px; border-radius: 10px;
    border: 1px solid var(--border); background: rgba(12,11,10,0.6);
    color: var(--txt); font-size: 15px; outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
    box-shadow: var(--shadow-input);
  }
  input:focus {
    border-color: var(--mark);
    box-shadow: 0 0 0 3px rgba(90,171,204,0.12);
  }

  .code-input {
    text-align: center; font-size: 26px; letter-spacing: 8px;
    font-weight: 600; font-family: var(--mono);
    padding: 14px;
  }

  .btn {
    width: 100%; padding: 12px; border-radius: 10px; border: none;
    background: var(--mark); color: #fff; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: opacity 0.15s, box-shadow 0.15s;
    margin-top: 16px; box-shadow: var(--shadow-btn);
    font-family: var(--sans);
    min-height: 44px;
  }
  .btn:hover { opacity: 0.88; box-shadow: 0 2px 8px rgba(90,171,204,0.25); }
  .btn:active { opacity: 0.8; transform: scale(0.99); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; box-shadow: none; }

  .btn-folder {
    background: var(--surface-el);
    -webkit-backdrop-filter: blur(12px);
    backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    color: var(--txt); text-align: left; padding: 14px 16px;
    font-size: 13px; margin-bottom: 8px; cursor: pointer;
    border-radius: 10px; width: 100%; min-height: 44px;
    transition: border-color 0.2s, background 0.2s;
    font-family: var(--sans);
  }
  .btn-folder:hover { border-color: var(--mark); background: var(--mark-hover); }
  .btn-folder .path {
    display: block; color: var(--mark); font-family: var(--mono);
    font-size: 11px; margin-top: 4px; opacity: 0.85;
  }

  .btn-browse {
    background: transparent; border: 1px dashed rgba(255,255,255,0.12);
    color: var(--txt2);
  }
  .btn-browse:hover { border-color: var(--mark); color: var(--txt); background: var(--teal-bg); }

  .msg { font-size: 12px; min-height: 18px; margin-top: 10px; }
  .msg.error { color: var(--red); font-weight: 500; }
  .msg.success { color: var(--green); }

  /* Progress dots */
  .progress {
    display: flex; align-items: center; gap: 6px; margin-bottom: 20px;
  }
  .progress .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: rgba(255,255,255,0.10);
    transition: background 0.3s, box-shadow 0.3s;
  }
  .progress .dot.done { background: var(--green); box-shadow: 0 0 6px rgba(74,148,104,0.35); }
  .progress .dot.current { background: var(--mark); box-shadow: 0 0 8px rgba(90,171,204,0.4); }
  .progress .line {
    flex: 1; height: 1px;
    background: rgba(255,255,255,0.06);
  }

  .success-icon {
    width: 56px; height: 56px; margin: 12px auto 16px;
    border-radius: 50%;
    background: rgba(74,148,104,0.15);
    border: 2px solid var(--green);
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; color: var(--green);
    box-shadow: 0 0 20px rgba(74,148,104,0.2);
  }

  .spinner {
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,0.15); border-top-color: #fff;
    border-radius: 50%; animation: spin 0.6s linear infinite;
    vertical-align: middle; margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <div class="brand">
    <img class="brand-logo" src="data:image/png;base64,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" alt="CelesteOS">
    <div class="brand-text">
      <div class="logo">CelesteOS</div>
      <div class="subtitle">Setup</div>
    </div>
  </div>

  <!-- Step 1: Welcome + Register -->
  <div class="step active" id="step-welcome">
    <div class="progress">
      <div class="dot current"></div><div class="line"></div>
      <div class="dot"></div><div class="line"></div>
      <div class="dot"></div><div class="line"></div>
      <div class="dot"></div>
    </div>
    <div class="card">
      <h2>Welcome</h2>
      <p>This installer will activate CelesteOS for your yacht and connect it to your document storage.</p>
      <p>Yacht: <span class="yacht-name" id="yacht-name-display">Loading...</span></p>
      <button class="btn" id="btn-register" onclick="doRegister()">Begin setup</button>
      <div class="msg" id="msg-register"></div>
    </div>
  </div>

  <!-- Step 2: Enter 2FA Code -->
  <div class="step" id="step-2fa">
    <div class="progress">
      <div class="dot done"></div><div class="line"></div>
      <div class="dot current"></div><div class="line"></div>
      <div class="dot"></div><div class="line"></div>
      <div class="dot"></div>
    </div>
    <div class="card">
      <h2>Verify your identity</h2>
      <p>A 6-digit code has been sent to <span class="email-masked" id="email-display"></span></p>
      <label for="code-input">Verification code</label>
      <input type="text" id="code-input" class="code-input" maxlength="6"
             placeholder="000000" inputmode="numeric" pattern="[0-9]*"
             autocomplete="one-time-code">
      <div id="code-timer" style="text-align:center;font-size:12px;color:var(--txt3);margin-top:10px;font-family:var(--mono);"></div>
      <button class="btn" id="btn-verify" onclick="doVerify()">Verify</button>
      <div class="msg" id="msg-verify"></div>
    </div>
  </div>

  <!-- Step 3: Select Folder -->
  <div class="step" id="step-folder">
    <div class="progress">
      <div class="dot done"></div><div class="line"></div>
      <div class="dot done"></div><div class="line"></div>
      <div class="dot current"></div><div class="line"></div>
      <div class="dot"></div>
    </div>
    <div class="card">
      <h2>Select document folder</h2>
      <p>Choose the root folder of your yacht's NAS or document storage.</p>
      <div id="folder-candidates"></div>
      <button class="btn-folder btn-browse" onclick="doBrowse()">Browse for folder...</button>
      <div class="msg" id="msg-folder"></div>
    </div>
  </div>

  <!-- Step 4: Success -->
  <div class="step" id="step-success">
    <div class="progress">
      <div class="dot done"></div><div class="line"></div>
      <div class="dot done"></div><div class="line"></div>
      <div class="dot done"></div><div class="line"></div>
      <div class="dot done"></div>
    </div>
    <div class="card" style="text-align:center;">
      <div class="success-icon">&#10003;</div>
      <h2>CelesteOS is ready</h2>
      <p>Your documents will begin syncing automatically. CelesteOS will start on login and run in the background.</p>
      <p style="color:var(--txt3);font-size:11px;margin-top:12px;">You can close this window.</p>
      <button class="btn" onclick="doFinish()" style="margin-top:16px;">Done</button>
    </div>
  </div>
</div>

<script>
  // Bridge to Python via pywebview
  function pyCall(method, ...args) {
    return window.pywebview.api[method](...args);
  }

  function showStep(id) {
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
  }

  function setMsg(id, text, isError) {
    const el = document.getElementById(id);
    el.textContent = text;
    el.className = 'msg ' + (isError ? 'error' : 'success');
  }

  function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    if (loading) {
      btn.disabled = true;
      btn.dataset.orig = btn.textContent;
      btn.innerHTML = '<span class="spinner"></span>Please wait...';
    } else {
      btn.disabled = false;
      btn.textContent = btn.dataset.orig || btn.textContent;
    }
  }

  // Step 1: Register
  async function doRegister() {
    setLoading('btn-register', true);
    setMsg('msg-register', '', false);
    try {
      const result = await pyCall('register');
      const data = JSON.parse(result);
      if (data.success) {
        document.getElementById('email-display').textContent = data.email_sent_to || 'your email';
        showStep('step-2fa');
        startCodeTimer();
        document.getElementById('code-input').focus();
      } else {
        setMsg('msg-register', data.error || 'Registration failed', true);
      }
    } catch (e) {
      setMsg('msg-register', 'Connection error: ' + e.message, true);
    } finally {
      setLoading('btn-register', false);
    }
  }

  // 2FA countdown timer
  let _timerInterval = null;
  function startCodeTimer() {
    let remaining = 600; // 10 minutes
    const timerEl = document.getElementById('code-timer');
    function update() {
      if (remaining <= 0) {
        timerEl.innerHTML = '<span style="color:var(--red);font-weight:600;">Code expired — restart setup</span>';
        clearInterval(_timerInterval);
        document.getElementById('btn-verify').disabled = true;
        return;
      }
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      timerEl.textContent = 'Code expires in ' + m + ':' + (s < 10 ? '0' : '') + s;
      remaining--;
    }
    update();
    _timerInterval = setInterval(update, 1000);
  }

  // Step 2: Verify 2FA
  async function doVerify() {
    const code = document.getElementById('code-input').value.trim();
    if (code.length !== 6) {
      setMsg('msg-verify', 'Enter the full 6-digit code', true);
      return;
    }
    setLoading('btn-verify', true);
    setMsg('msg-verify', '', false);
    try {
      const result = await pyCall('verify_2fa', code);
      const data = JSON.parse(result);
      if (data.success) {
        // Load folder candidates
        const foldersJson = await pyCall('get_folder_candidates');
        const folders = JSON.parse(foldersJson);
        renderFolders(folders);
        showStep('step-folder');
      } else {
        setMsg('msg-verify', data.error || 'Invalid code', true);
      }
    } catch (e) {
      setMsg('msg-verify', 'Connection error: ' + e.message, true);
    } finally {
      setLoading('btn-verify', false);
    }
  }

  // Step 3: Folder selection
  function renderFolders(folders) {
    const container = document.getElementById('folder-candidates');
    container.innerHTML = '';
    folders.forEach(path => {
      const btn = document.createElement('button');
      btn.className = 'btn-folder';
      const name = path.split('/').pop();
      btn.innerHTML = name + '<span class="path">' + path + '</span>';
      btn.onclick = () => selectFolder(path);
      container.appendChild(btn);
    });
  }

  async function selectFolder(path) {
    setMsg('msg-folder', '', false);
    try {
      const result = await pyCall('select_folder', path);
      const data = JSON.parse(result);
      if (data.success) {
        showStep('step-success');
      } else {
        setMsg('msg-folder', data.error || 'Invalid folder', true);
      }
    } catch (e) {
      setMsg('msg-folder', 'Error: ' + e.message, true);
    }
  }

  async function doBrowse() {
    try {
      const result = await pyCall('browse_folder');
      const data = JSON.parse(result);
      if (data.path) {
        await selectFolder(data.path);
      }
    } catch (e) {
      setMsg('msg-folder', 'Browse failed: ' + e.message, true);
    }
  }

  // Step 4: Done
  async function doFinish() {
    await pyCall('finish');
  }

  // Auto-advance on 6 digits
  document.getElementById('code-input').addEventListener('input', e => {
    e.target.value = e.target.value.replace(/\\D/g, '');
    if (e.target.value.length === 6) doVerify();
  });

  // Init: load yacht info
  (async () => {
    try {
      const info = await pyCall('get_yacht_info');
      const data = JSON.parse(info);
      document.getElementById('yacht-name-display').textContent = data.yacht_name || data.yacht_id;
    } catch (e) {
      document.getElementById('yacht-name-display').textContent = 'Unknown';
    }
  })();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Python API exposed to JavaScript via pywebview
# ---------------------------------------------------------------------------

class InstallerAPI:
    """
    Bridge between the HTML UI and the Python installer logic.
    Methods are called from JavaScript via window.pywebview.api.
    """

    def __init__(self, config):
        """
        Args:
            config: InstallConfig instance from lib.installer
        """
        from lib.installer import InstallationOrchestrator, KeychainStore
        self.config = config
        self.orchestrator = InstallationOrchestrator(config)
        self.orchestrator.initialize()
        self._selected_folder: Optional[str] = None
        self._window = None  # set after window creation

    def get_yacht_info(self) -> str:
        """Return yacht info for the welcome screen."""
        return json.dumps({
            "yacht_id": self.config.yacht_id,
            "yacht_name": getattr(self.config, 'yacht_name', self.config.yacht_id),
            "version": self.config.version,
        })

    def register(self) -> str:
        """Trigger registration and 2FA email."""
        success, message = self.orchestrator.register()
        if success:
            email_sent_to = message.split("to ")[-1] if "to " in message else ""
            return json.dumps({"success": True, "email_sent_to": email_sent_to})
        return json.dumps({"success": False, "error": message})

    def _show_simulated_email(self) -> None:
        """Open a second window showing the 2FA code (simulates email delivery)."""
        try:
            import httpx
            import hashlib

            # Fetch the latest unverified code from the database
            sb_url = os.getenv("MASTER_SUPABASE_URL", "https://qvzmkaamzaqxpzbewjxe.supabase.co")
            sb_key = os.getenv("MASTER_SUPABASE_SERVICE_KEY", "")
            if not sb_key:
                return  # Can't fetch without key

            headers = {
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
            }
            resp = httpx.get(
                f"{sb_url}/rest/v1/installation_2fa_codes",
                params={
                    "yacht_id": f"eq.{self.config.yacht_id}",
                    "purpose": "eq.installation",
                    "verified": "eq.false",
                    "order": "created_at.desc",
                    "limit": "1",
                    "select": "code_hash",
                },
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200 or not resp.json():
                return

            code_hash = resp.json()[0]["code_hash"]
            # Brute-force the 6-digit code from hash (instant for 6 digits)
            code = None
            for i in range(1000000):
                candidate = f"{i:06d}"
                if hashlib.sha256(candidate.encode()).hexdigest() == code_hash:
                    code = candidate
                    break

            if not code:
                return

            yacht_name = getattr(self.config, 'yacht_name', self.config.yacht_id) or self.config.yacht_id

            # Open a second window showing the simulated email
            import webview
            email_html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Simulated Email</title>
<style>
body {{ margin:0; padding:24px; background:#0c0b0a; color:rgba(255,255,255,0.92);
  font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; }}
.banner {{ background:#C4893B; color:#000; padding:8px 16px; border-radius:6px;
  font-size:11px; font-weight:600; margin-bottom:20px; text-align:center;
  letter-spacing:0.3px; }}
.from {{ color:rgba(255,255,255,0.38); font-size:12px; margin-bottom:4px; }}
.subject {{ font-size:15px; font-weight:600; margin-bottom:20px; letter-spacing:-0.2px; }}
.body {{ background:#181614; border-radius:12px; padding:24px;
  border:1px solid rgba(255,255,255,0.07); }}
.code {{ font-size:36px; letter-spacing:10px; font-weight:700; color:#5AABCC;
  text-align:center; margin:20px 0;
  font-family:'SF Mono',ui-monospace,'Fira Code',monospace; }}
.hint {{ color:rgba(255,255,255,0.38); font-size:13px; line-height:1.5; }}
.yacht {{ color:#5AABCC; font-weight:600; }}
</style></head><body>
<div class="banner">SIMULATED EMAIL — In production this arrives in the buyer's inbox</div>
<div class="from">From: noreply@celeste7.ai</div>
<div class="subject">CelesteOS — Your verification code</div>
<div class="body">
  <p>Your verification code for <span class="yacht">{yacht_name}</span>:</p>
  <div class="code">{code}</div>
  <p class="hint">Enter this code in the CelesteOS installer to complete activation.<br>
  This code expires in 10 minutes.</p>
</div>
</body></html>'''

            webview.create_window(
                "Simulated Email",
                html=email_html,
                width=420,
                height=340,
                x=600,
                y=100,
                resizable=False,
                on_top=True,
                background_color="#0c0b0a",
            )

        except Exception as exc:
            logger.warning("Could not show simulated email: %s", exc)

    def verify_2fa(self, code: str) -> str:
        """Verify the 2FA code."""
        success, message = self.orchestrator.verify_2fa(code)
        return json.dumps({"success": success, "error": "" if success else message})

    def get_folder_candidates(self) -> str:
        """Return list of detected NAS folder paths."""
        from .folder_selector import _find_nas_candidates
        candidates = _find_nas_candidates()
        return json.dumps(candidates)

    def browse_folder(self) -> str:
        """Open a native folder picker dialog."""
        if self._window:
            result = self._window.create_file_dialog(
                dialog_type=20,  # FOLDER_DIALOG
                allow_multiple=False,
            )
            if result and len(result) > 0:
                path = result[0] if isinstance(result, (list, tuple)) else str(result)
                return json.dumps({"path": path})
        return json.dumps({"path": None})

    def select_folder(self, path: str) -> str:
        """Validate and save the selected folder."""
        if not os.path.isdir(path):
            return json.dumps({"success": False, "error": "Folder does not exist"})

        if not os.access(path, os.W_OK):
            return json.dumps({"success": False, "error": "Folder is not writable. Check permissions."})

        self._selected_folder = path

        # Save to config dir .env.local
        from .platform import get_config_dir
        env_dir = get_config_dir()
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / ".env.local"

        lines = []
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if not line.strip().startswith("NAS_ROOT="):
                    lines.append(line)
        lines.append(f"NAS_ROOT={path}")
        env_file.write_text("\n".join(lines) + "\n")
        if sys.platform != "win32":
            os.chmod(str(env_file), 0o600)

        # NOTE: launchd is installed by the daemon on first successful run,
        # not here. The installer subprocess exits cleanly after writing config.

        return json.dumps({"success": True})

    def finish(self) -> str:
        """Close the window after the callback returns (avoids macOS deadlock)."""
        if self._window:
            w = self._window
            threading.Timer(0.3, lambda: w.destroy()).start()
        return json.dumps({"success": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_installer_ui(config) -> Optional[str]:
    """
    Launch the installer UI window.

    Args:
        config: InstallConfig instance

    Returns:
        Selected NAS folder path, or None if cancelled
    """
    # Prevent Python/pywebview from showing a Dock icon on macOS
    try:
        import AppKit
        info = AppKit.NSBundle.mainBundle().infoDictionary()
        info["LSBackgroundOnly"] = "1"
    except ImportError:
        pass

    import webview

    api = InstallerAPI(config)
    window = webview.create_window(
        "CelesteOS Setup",
        html=INSTALLER_HTML,
        js_api=api,
        width=500,
        height=620,
        resizable=False,
        background_color="#0c0b0a",
    )
    api._window = window

    webview.start(debug=False)

    return api._selected_folder


if __name__ == "__main__":
    # Test mode: run with a mock config
    from lib.installer import InstallConfig
    try:
        config = InstallConfig.load_embedded()
    except FileNotFoundError:
        print("No manifest found. Create ~/.celesteos/install_manifest.json for testing.")
        exit(1)

    result = run_installer_ui(config)
    print(f"Selected folder: {result}")
