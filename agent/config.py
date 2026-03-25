from __future__ import annotations

"""
Configuration loader for the CelesteOS agent.

Priority: embedded manifest (DMG) > environment variables > ~/.celesteos/.env.local > Keychain
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .platform import get_config_dir, get_keychain_password as _platform_get_keychain_password

logger = logging.getLogger("agent.config")

ENV_DIR = get_config_dir()
ENV_FILE = ENV_DIR / ".env.local"
MANIFEST_DIR = ENV_DIR
KEYCHAIN_SERVICE = "com.celeste7.celesteos"


@dataclass
class SyncConfig:
    yacht_id: str = ""
    nas_root: str = ""
    supabase_url: str = ""
    supabase_key: str = ""  # service-role key
    poll_interval_s: int = 300
    max_upload_bytes_per_cycle: int = 0  # 0 = unlimited
    max_satellite_upload_mb: int = 0     # 0 = use default thresholds
    manifest_path: str = ""
    source_type: str = "nas"  # 'nas', 'onedrive', 'local'
    yacht_name: str = ""
    registration_api_endpoint: str = "https://registration.celeste7.ai"

    @property
    def is_configured(self) -> bool:
        return bool(self.yacht_id and self.nas_root and self.supabase_url and self.supabase_key)


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores comments and blank lines."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip optional quotes
        value = value.strip().strip("'\"")
        env[key.strip()] = value
    return env


def _get_keychain_password(service: str, account: str) -> str:
    """Retrieve a password from the platform credential store with recovery key fallback.

    Uses macOS Keychain or Windows Credential Manager via the platform abstraction layer.
    Returns empty string on failure.
    """
    pw = _platform_get_keychain_password(service, account)
    if pw:
        return pw

    # Fallback: recovery key file — ONLY for the HMAC shared_secret,
    # not for Supabase keys or other credentials
    if service == "com.celeste7.celesteos":
        recovery_path = get_config_dir() / ".recovery_key"
        if recovery_path.exists():
            try:
                from lib.crypto import decrypt_recovery_key, encrypt_recovery_key
                raw = recovery_path.read_bytes()
                try:
                    secret = decrypt_recovery_key(raw)
                except Exception:
                    # Legacy plaintext fallback: 64-char hex string
                    text = raw.decode("utf-8", errors="replace").strip()
                    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
                        secret = text
                        # Re-encrypt in place so next read is encrypted
                        try:
                            recovery_path.write_bytes(encrypt_recovery_key(secret))
                            logger.info("Migrated legacy recovery key to encrypted format")
                        except Exception as enc_exc:
                            logger.warning("Failed to re-encrypt legacy recovery key: %s", enc_exc)
                    else:
                        raise
                if secret:
                    logger.warning("Using recovery key — credential store may need repair (%s/%s)", service, account)
                    return secret
            except OSError as exc:
                logger.warning("Failed to read recovery key: %s", exc)

    return ""


def load_from_manifest() -> SyncConfig | None:
    """
    Load config from embedded install_manifest.json (DMG installs).
    Returns None if manifest not found.
    """
    # Check for manifest in the app bundle or ~/.celesteos
    manifest_paths = [
        Path(__file__).parent.parent / "install_manifest.json",
        ENV_DIR / "install_manifest.json",
    ]

    for manifest_path in manifest_paths:
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text())
                logger.info("Loaded config from manifest: %s", manifest_path)

                # Manifest uses tenant_supabase_* fields; fall back to short names
                supabase_key = data.get("tenant_supabase_service_key", "") or data.get("supabase_service_key", "")
                if not supabase_key:
                    supabase_key = _get_keychain_password(KEYCHAIN_SERVICE, "SUPABASE_SERVICE_KEY")

                supabase_url = data.get("tenant_supabase_url", "") or data.get("supabase_url", "")

                # NAS root comes from .env.local after installer, not from manifest
                env = _read_env_file(ENV_FILE)
                nas_root = env.get("NAS_ROOT", data.get("nas_root", ""))

                return SyncConfig(
                    yacht_id=data.get("yacht_id", ""),
                    nas_root=nas_root,
                    supabase_url=supabase_url.rstrip("/"),
                    supabase_key=supabase_key,
                    poll_interval_s=int(data.get("poll_interval_s", 300)),
                    manifest_path=str(MANIFEST_DIR / "filesync_manifest.db"),
                    source_type=data.get("source_type", "nas"),
                    yacht_name=data.get("yacht_name", ""),
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to parse manifest %s: %s", manifest_path, exc)

    return None


def load_from_env() -> SyncConfig:
    """Load configuration from .env.local + Keychain + environment overrides."""
    env = _read_env_file(ENV_FILE)

    # Environment variables override .env.local
    yacht_id = os.environ.get("YACHT_ID", env.get("YACHT_ID", ""))
    nas_root = os.environ.get("NAS_ROOT", env.get("NAS_ROOT", ""))
    supabase_url = os.environ.get("SUPABASE_URL", env.get("SUPABASE_URL", ""))
    source_type = os.environ.get("SOURCE_TYPE", env.get("SOURCE_TYPE", "nas"))

    # Service key: env var → Keychain → .env.local (least preferred, plaintext)
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_key:
        supabase_key = _get_keychain_password(KEYCHAIN_SERVICE, "SUPABASE_SERVICE_KEY")
    if not supabase_key:
        supabase_key = env.get("SUPABASE_SERVICE_KEY", "")
        if supabase_key:
            logger.warning("Using SUPABASE_SERVICE_KEY from .env.local — migrate to Keychain")

    poll_interval = int(os.environ.get(
        "POLL_INTERVAL_S", env.get("POLL_INTERVAL_S", "300")
    ))
    max_upload = int(os.environ.get(
        "MAX_UPLOAD_BYTES_PER_CYCLE", env.get("MAX_UPLOAD_BYTES_PER_CYCLE", "0")
    ))
    max_sat = int(os.environ.get(
        "MAX_SATELLITE_UPLOAD_MB", env.get("MAX_SATELLITE_UPLOAD_MB", "0")
    ))

    manifest_path = str(MANIFEST_DIR / "filesync_manifest.db")

    yacht_name = os.environ.get("YACHT_NAME", env.get("YACHT_NAME", ""))

    return SyncConfig(
        yacht_id=yacht_id,
        nas_root=nas_root,
        supabase_url=supabase_url.rstrip("/"),
        supabase_key=supabase_key,
        poll_interval_s=poll_interval,
        max_upload_bytes_per_cycle=max_upload,
        max_satellite_upload_mb=max_sat,
        manifest_path=manifest_path,
        source_type=source_type,
        yacht_name=yacht_name,
    )


def _fetch_yacht_name(cfg: SyncConfig) -> str:
    """Fetch yacht name from the tenant yacht_registry table.

    Best-effort lookup — returns empty string on any failure.
    Caches the result in .env.local so we don't query every startup.
    """
    if not cfg.yacht_id or not cfg.supabase_key or not cfg.supabase_url:
        return ""

    # Check .env.local cache first
    env = _read_env_file(ENV_FILE)
    cached = env.get("YACHT_NAME", "")
    if cached:
        return cached

    try:
        import requests
        resp = requests.get(
            f"{cfg.supabase_url}/rest/v1/yacht_registry",
            params={"id": f"eq.{cfg.yacht_id}", "select": "name"},
            headers={
                "apikey": cfg.supabase_key,
                "Authorization": f"Bearer {cfg.supabase_key}",
            },
            timeout=10,
        )
        if resp.ok:
            rows = resp.json()
            if rows and rows[0].get("name"):
                name = rows[0]["name"]
                # Cache in .env.local
                lines = []
                if ENV_FILE.exists():
                    for line in ENV_FILE.read_text().splitlines():
                        if not line.strip().startswith("YACHT_NAME="):
                            lines.append(line)
                lines.append(f"YACHT_NAME={name}")
                ENV_FILE.write_text("\n".join(lines) + "\n")
                logger.info("Fetched yacht name: %s", name)
                return name
    except Exception as exc:
        logger.debug("Could not fetch yacht name: %s", exc)

    return ""


def load_config() -> SyncConfig:
    """
    Load configuration with priority: manifest > env > .env.local > Keychain.
    """
    # Try manifest first (DMG install)
    cfg = load_from_manifest()
    if cfg and cfg.is_configured:
        return cfg

    # Fall back to env-based config
    cfg = load_from_env()

    if not cfg.is_configured:
        missing = []
        if not cfg.yacht_id:
            missing.append("YACHT_ID")
        if not cfg.nas_root:
            missing.append("NAS_ROOT")
        if not cfg.supabase_url:
            missing.append("SUPABASE_URL")
        if not cfg.supabase_key:
            missing.append("SUPABASE_SERVICE_KEY")
        logger.error("Missing required config: %s", ", ".join(missing))

    # Resolve yacht_name if not already set
    if not cfg.yacht_name and cfg.is_configured:
        cfg.yacht_name = _fetch_yacht_name(cfg)

    return cfg
