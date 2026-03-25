"""
SHA-256 streaming hasher.
Reads files in 64KB chunks to avoid loading large files into memory.
"""

import hashlib
import logging

from .constants import HASH_CHUNK_SIZE

logger = logging.getLogger("agent.hasher")


def sha256_file(file_path: str) -> str:
    """Compute SHA-256 hex digest of a file using streaming reads."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
