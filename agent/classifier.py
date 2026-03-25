from __future__ import annotations

"""
Path-based document classifier.
Maps NAS directory structure to doc_type and system_tag.
Ported from Cloud_DMG_Onedrive metadata_extractor.py + Synology numbered-folder adapter.
"""

from typing import Tuple

# ---------------------------------------------------------------------------
# System tag mapping — directory names → normalised tag
# ---------------------------------------------------------------------------
SYSTEM_TAG_MAPPING: dict[str, str] = {
    "electrical": "electrical",
    "hvac": "hvac",
    "plumbing": "plumbing",
    "engines": "propulsion",
    "engine": "propulsion",
    "generators": "power",
    "generator": "power",
    "navigation": "navigation",
    "communications": "communications",
    "comms": "communications",
    "fire": "safety",
    "safety": "safety",
    "galley": "galley",
    "kitchen": "galley",
    "sanitation": "sanitation",
    "water": "water",
    "fuel": "fuel",
    "hydraulic": "hydraulic",
    "hydraulics": "hydraulic",
    "deck": "deck",
    "hull": "hull",
    "interior": "interior",
    "av": "av",
    "audio": "av",
    "video": "av",
    "entertainment": "entertainment",
    "cctv": "security",
    "security": "security",
    "stabilizers": "stabilization",
    "stabilisers": "stabilization",
    "thrusters": "propulsion",
    "tender": "tender",
    "tenders": "tender",
    "bridge": "bridge",
    "propulsion": "propulsion",
    "steering": "steering",
    "anchoring": "deck",
    "laundry": "laundry",
}

# ---------------------------------------------------------------------------
# Numbered folder → doc_type (Synology / QNAP standard)
# Prefix number stripped, remainder matched case-insensitively.
# ---------------------------------------------------------------------------
NUMBERED_DOC_TYPE: dict[str, str] = {
    "general": "general",
    "bridge": "chart",
    "engineering": "schematic",
    "systems": "schematic",
    "manuals": "manual",
    "drawings": "drawing",
    "procedures": "sop",
    "safety": "sop",
    "maintenance": "maintenance_log",
    "logs": "log",
    "inspections": "inspection",
    "vendors": "vendor_doc",
    "warranties": "warranty",
    "certifications": "certification",
    "certs": "certification",
    "photos": "photo",
    "videos": "video",
    "schematics": "schematic",
}

# ---------------------------------------------------------------------------
# File-extension overrides (some types are self-evident)
# ---------------------------------------------------------------------------
EXT_DOC_TYPE: dict[str, str] = {
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".tiff": "photo",
    ".tif": "photo",
    ".heic": "photo",
    ".gif": "photo",
    ".bmp": "photo",
    ".webp": "photo",
    ".mp4": "video",
    ".mov": "video",
    ".avi": "video",
    ".mkv": "video",
}


def _strip_number_prefix(name: str) -> str:
    """'01_BRIDGE' → 'bridge', '02_Engineering' → 'engineering'."""
    # Strip leading digits + separator
    i = 0
    while i < len(name) and (name[i].isdigit() or name[i] in ("_", "-", ".")):
        i += 1
    return name[i:].lower().strip() if i < len(name) else name.lower().strip()


def classify_path(relative_path: str) -> Tuple[str, str]:
    """
    Classify a file's relative NAS path into (doc_type, system_tag).

    Returns:
        (doc_type, system_tag) e.g. ("manual", "engineering") or ("general", "general")
    """
    parts = relative_path.replace("\\", "/").strip("/").split("/")
    if not parts:
        return ("general", "general")

    filename = parts[-1]
    dir_parts = parts[:-1]

    # 1) doc_type from top-level directory
    doc_type = "general"
    if dir_parts:
        top_clean = _strip_number_prefix(dir_parts[0])
        doc_type = NUMBERED_DOC_TYPE.get(top_clean, "general")

    # 2) Extension override (photos/videos always win)
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    ext_type = EXT_DOC_TYPE.get(ext)
    if ext_type:
        doc_type = ext_type

    # 3) system_tag from any directory component
    #    Pass 1: exact match (preferred). Pass 2: substring match (fallback).
    #    Skip top-level numbered dirs in substring match to avoid
    #    "engineering" matching "engine" → propulsion.
    system_tag = "general"

    # Pass 1 — exact match on stripped dir names
    for part in dir_parts:
        clean = _strip_number_prefix(part)
        if clean in SYSTEM_TAG_MAPPING:
            system_tag = SYSTEM_TAG_MAPPING[clean]
            break

    # Pass 2 — substring match, but only on non-top-level dirs
    if system_tag == "general" and len(dir_parts) > 1:
        for part in dir_parts[1:]:
            clean = _strip_number_prefix(part)
            for key, tag in SYSTEM_TAG_MAPPING.items():
                if key in clean:
                    system_tag = tag
                    break
            if system_tag != "general":
                break

    return (doc_type, system_tag)
