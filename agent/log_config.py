from __future__ import annotations

"""
Logging configuration for the filesync daemon.
RotatingFileHandler: 10MB per file, 5 rotations.
Writes to ~/.celesteos/logs/filesync.log + stdout for launchd capture.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

def _get_log_dir() -> Path:
    """Get log directory using platform abstraction (lazy to avoid circular import)."""
    from .platform import get_config_dir
    return get_config_dir() / "logs"

LOG_DIR = _get_log_dir()
LOG_FILE = LOG_DIR / "filesync.log"
MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Set up logging with both file rotation and stdout output.
    Call this once at daemon startup, before any logging happens.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (from basicConfig)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    use_json = os.environ.get("LOG_FORMAT_JSON", "") == "1"

    if use_json:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(LOG_FORMAT)

    # Stdout handler (for launchd capture and foreground mode)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(level)
    root.addHandler(stdout_handler)

    # File handler with rotation
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except OSError as exc:
        # Can't write to log directory — log to stdout only
        root.warning("Cannot set up file logging at %s: %s", LOG_FILE, exc)
