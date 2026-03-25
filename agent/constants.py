"""
Constants for the local file sync daemon.
Extension lists, size thresholds, MIME maps.
"""

# ---------------------------------------------------------------------------
# Size thresholds (bytes)
# ---------------------------------------------------------------------------
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024        # 5 GB — skip entirely
INDEXABLE_SIZE_LIMIT = 100 * 1024 * 1024       # 100 MB — above this, storage_only
LARGE_UPLOAD_THRESHOLD = 10 * 1024 * 1024      # 10 MB — longer timeout

HASH_CHUNK_SIZE = 65_536                        # 64 KB SHA-256 streaming chunks

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
DEFAULT_POLL_INTERVAL_S = 300                   # 5 minutes
UPLOAD_TIMEOUT_NORMAL = 120                     # seconds
UPLOAD_TIMEOUT_LARGE = 300                      # seconds
import os
SPEED_PROBE_TIMEOUT = int(os.environ.get("SPEED_PROBE_TIMEOUT", "10"))  # seconds
BACKOFF_BASE_S = 60                             # retry delay multiplier
BACKOFF_MAX_S = 3600                            # max retry delay
MAX_RETRY_COUNT = 10                            # after this → dlq

# ---------------------------------------------------------------------------
# Extension tiers
# ---------------------------------------------------------------------------
SKIP_EXTENSIONS = frozenset({
    ".tmp", ".part", ".crdownload", ".ds_store", ".swp", ".swo",
    ".lock", ".bak", ".pyc", ".pyo",
})

INDEXABLE_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md", ".json",
    ".xml", ".html", ".htm", ".pptx", ".rtf", ".odt", ".ods",
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif",
    ".webp", ".heic",
    ".pages", ".numbers", ".keynote", ".eml",
})

STORAGE_ONLY_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv",
    ".mp3", ".wav", ".aac", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".dmg", ".iso",
    ".psd", ".ai", ".indd", ".dwg", ".dxf",
    ".exe", ".msi", ".pkg", ".app",
    ".msg",
})

# ---------------------------------------------------------------------------
# Hidden / temp prefixes and directory names to skip
# ---------------------------------------------------------------------------
SKIP_PREFIXES = (".", "~$")
SKIP_DIRS = frozenset({
    "@eaDir", "@tmp", ".Spotlight-V100", ".fseventsd",
    ".Trashes", "__MACOSX", "Thumbs.db", ".TemporaryItems",
    "#recycle", "@Recycle",
})

# ---------------------------------------------------------------------------
# MIME type map
# ---------------------------------------------------------------------------
MIME_MAP = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "xml": "application/xml",
    "html": "text/html",
    "htm": "text/html",
    "rtf": "application/rtf",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "zip": "application/zip",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "7z": "application/x-7z-compressed",
    "dmg": "application/x-apple-diskimage",
    "pages": "application/x-iwork-pages-sffpages",
    "numbers": "application/x-iwork-numbers-sffnumbers",
    "keynote": "application/x-iwork-keynote-sffkey",
    "eml": "message/rfc822",
    "msg": "application/vnd.ms-outlook",
}


def get_mime_type(filename: str) -> str:
    """Return MIME type for a filename, falling back to application/octet-stream."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MIME_MAP.get(ext, "application/octet-stream")


def classify_extension(filename: str) -> str:
    """Return 'skip', 'indexable', or 'storage_only' based on extension."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext in SKIP_EXTENSIONS:
        return "skip"
    if ext in INDEXABLE_EXTENSIONS:
        return "indexable"
    if ext in STORAGE_ONLY_EXTENSIONS:
        return "storage_only"
    # Unknown extension → storage_only (upload but don't try to embed)
    return "storage_only"
