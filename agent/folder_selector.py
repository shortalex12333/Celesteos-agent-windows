"""
NAS Folder Selector
====================
Simple Tkinter dialog for selecting the NAS root folder.
Auto-suggests mounted volumes that look like NAS devices (Synology, QNAP).

This is a temporary UI — will be replaced with a branded native UI later.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent.folder_selector")

# Patterns that suggest a mounted NAS volume
NAS_PATTERNS = [
    r"(?i)synology",
    r"(?i)qnap",
    r"(?i)nas",
    r"(?i)diskstation",
    r"(?i)turbonas",
    r"(?i)yacht",
    r"(?i)vessel",
    r"(?i)marine",
    r"(?i)celeste",
]


def _find_nas_candidates() -> list[str]:
    """Scan for directories that look like NAS mounts (cross-platform)."""
    from .platform import get_nas_candidates
    return get_nas_candidates()


def run_folder_selector() -> Optional[str]:
    """
    Show a folder picker dialog and return the selected path.

    Falls back to CLI prompt if Tkinter is not available (e.g., SSH sessions).
    """
    try:
        return _run_tk_selector()
    except Exception as exc:
        logger.info("Tkinter not available (%s), falling back to CLI", exc)
        return _run_cli_selector()


def _run_tk_selector() -> Optional[str]:
    """Tkinter-based folder picker."""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    from .platform import get_default_browse_dir
    candidates = _find_nas_candidates()
    initial_dir = candidates[0] if candidates else get_default_browse_dir()

    if candidates:
        suggestion = "\n".join(f"  - {c}" for c in candidates)
        messagebox.showinfo(
            "CelesteOS — Select NAS Folder",
            f"Detected possible NAS volumes:\n{suggestion}\n\n"
            "Select the root folder of your yacht's NAS share.",
        )

    folder = filedialog.askdirectory(
        title="Select NAS Root Folder",
        initialdir=initial_dir,
        mustexist=True,
    )

    root.destroy()

    if folder:
        logger.info("User selected NAS root: %s", folder)
        return folder

    return None


def _run_cli_selector() -> Optional[str]:
    """CLI fallback for headless environments."""
    candidates = _find_nas_candidates()

    print("\nCelesteOS — Select NAS Root Folder")
    print("=" * 40)

    if candidates:
        print("\nDetected volumes:")
        for i, path in enumerate(candidates, 1):
            print(f"  [{i}] {path}")
        print(f"  [0] Browse manually...")
        print()

        choice = input("Select [1]: ").strip()
        if not choice or choice == "1":
            return candidates[0]
        if choice == "0":
            pass  # fall through to manual entry
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx]
            except ValueError:
                pass

    path = input("Enter full path to NAS root: ").strip()
    if path and os.path.isdir(path):
        return path

    print("Invalid path or directory does not exist.")
    return None
