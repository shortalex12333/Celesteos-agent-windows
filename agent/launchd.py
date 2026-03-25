"""
Launchd Integration
====================
Installs/uninstalls a launchd plist so the CelesteOS agent
starts automatically on login and restarts if it crashes.
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("agent.launchd")

LABEL = "com.celeste7.celesteos.agent"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LABEL}.plist"
LOG_DIR = Path.home() / ".celesteos" / "logs"


def _is_production() -> bool:
    """Check if running from a bundled .app (vs dev source)."""
    return Path("/Applications/CelesteOS.app/Contents/MacOS/CelesteOS").exists()


def _get_agent_executable() -> str:
    """Find the CelesteOS executable path."""
    if _is_production():
        return str(Path("/Applications/CelesteOS.app/Contents/MacOS/CelesteOS"))

    # Development: use the Python interpreter + module
    import sys
    return f"{sys.executable} -m agent"


def _build_plist() -> str:
    """Generate launchd plist XML."""
    agent_exec = _get_agent_executable()
    log_out = LOG_DIR / "agent.log"
    log_err = LOG_DIR / "agent.error.log"

    # Determine program arguments
    if " -m " in agent_exec:
        # Development mode: python -m agent
        parts = agent_exec.split(" ", 1)
        program_args = f"""\
    <array>
        <string>{parts[0]}</string>
        <string>-m</string>
        <string>agent</string>
    </array>"""
    else:
        # Production: single executable
        program_args = f"""\
    <array>
        <string>{agent_exec}</string>
    </array>"""

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
{program_args}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
    <key>WorkingDirectory</key>
    <string>{Path.home() / ".celesteos"}</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
"""


def install_launchd() -> bool:
    """
    Install the launchd plist and load it.

    Returns True if successful.
    """
    # Ensure directories exist
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Write plist
    plist_content = _build_plist()
    PLIST_PATH.write_text(plist_content)
    logger.info("Wrote plist to %s", PLIST_PATH)

    # Load the agent
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        logger.info("Launchd agent loaded: %s", LABEL)
        return True

    # Already loaded is OK
    if "already loaded" in result.stderr.lower() or "already bootstrapped" in result.stderr.lower():
        logger.info("Launchd agent already loaded")
        return True

    logger.error("Failed to load launchd agent: %s", result.stderr)
    return False


def uninstall_launchd() -> bool:
    """
    Unload and remove the launchd plist.

    Returns True if successful.
    """
    if not PLIST_PATH.exists():
        logger.info("Plist not found, nothing to uninstall")
        return True

    # Unload
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True,
    )

    # Remove file
    try:
        PLIST_PATH.unlink()
        logger.info("Removed plist: %s", PLIST_PATH)
        return True
    except OSError as exc:
        logger.error("Failed to remove plist: %s", exc)
        return False


def is_installed() -> bool:
    """Check if the launchd agent is installed."""
    return PLIST_PATH.exists()
