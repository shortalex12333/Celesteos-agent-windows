"""
CelesteOS Cryptographic Identity Module
=======================================
Zero-trust authentication using HMAC-SHA256.

Security Model:
- yacht_id: Public identifier, embedded in DMG at build time
- yacht_id_hash: SHA256(yacht_id) - proves possession without exposing ID in transit
- shared_secret: 256-bit random, retrieved ONCE during activation, stored in Keychain
- request_signature: HMAC-SHA256(request_body, shared_secret) - authenticates every API call

Flow:
1. DMG contains yacht_id (immutable, signed into binary)
2. First run: Agent sends yacht_id + yacht_id_hash to /api/register
3. Cloud validates hash, sends 2FA code to buyer email
4. Buyer enters 6-digit code in installer
5. Agent sends code to /api/verify-2fa, receives shared_secret ONE TIME
6. Agent stores shared_secret in macOS Keychain
7. All subsequent requests signed with HMAC-SHA256
"""

import hashlib
import hmac
import secrets
import time
import json
import uuid
from typing import Optional, Tuple, Dict, Any


class CryptoIdentity:
    """Cryptographic identity for yacht authentication."""

    def __init__(self, yacht_id: str, shared_secret: Optional[str] = None):
        """
        Initialize crypto identity.

        Args:
            yacht_id: Yacht identifier (embedded in DMG)
            shared_secret: 256-bit hex string (from activation, stored in Keychain)
        """
        self.yacht_id = yacht_id
        self._shared_secret = shared_secret

    @property
    def yacht_id_hash(self) -> str:
        """SHA256 hash of yacht_id."""
        return hashlib.sha256(self.yacht_id.encode('utf-8')).hexdigest()

    @property
    def has_secret(self) -> bool:
        """Check if shared_secret is available."""
        return self._shared_secret is not None

    def sign_request(self, payload: Dict[str, Any], timestamp: Optional[int] = None) -> Dict[str, str]:
        """
        Sign a request payload with HMAC-SHA256.

        Args:
            payload: Request body as dict
            timestamp: Unix timestamp (defaults to now)

        Returns:
            Dict with signature headers:
            - X-Yacht-ID: yacht_id
            - X-Timestamp: Unix timestamp
            - X-Signature: HMAC-SHA256(timestamp + payload, shared_secret)

        Raises:
            ValueError: If shared_secret not available
        """
        if not self._shared_secret:
            raise ValueError("Cannot sign request: shared_secret not available")

        ts = timestamp or int(time.time())
        request_id = str(uuid.uuid4())

        # Canonical payload: sorted JSON with timestamp and nonce prepended
        canonical = f"{ts}:{request_id}:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"

        # HMAC-SHA256
        signature = hmac.new(
            bytes.fromhex(self._shared_secret),
            canonical.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return {
            'X-Yacht-ID': self.yacht_id,
            'X-Timestamp': str(ts),
            'X-Request-ID': request_id,
            'X-Signature': signature
        }

    def verify_response(self, response_body: bytes, signature: str, timestamp: str) -> bool:
        """
        Verify a signed response from the server.

        Args:
            response_body: Raw response bytes
            signature: X-Signature header value
            timestamp: X-Timestamp header value

        Returns:
            True if signature is valid
        """
        if not self._shared_secret:
            return False

        canonical = f"{timestamp}:{response_body.decode('utf-8')}"
        expected = hmac.new(
            bytes.fromhex(self._shared_secret),
            canonical.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)


class SecretGenerator:
    """Server-side secret generation (for Edge Functions)."""

    @staticmethod
    def generate_shared_secret() -> str:
        """Generate 256-bit cryptographically secure random secret."""
        return secrets.token_hex(32)  # 32 bytes = 256 bits = 64 hex chars

    @staticmethod
    def generate_download_token() -> str:
        """Generate secure download token."""
        return secrets.token_hex(32)

    @staticmethod
    def generate_2fa_code() -> str:
        """Generate 6-digit 2FA code."""
        return f"{secrets.randbelow(1000000):06d}"

    @staticmethod
    def hash_2fa_code(code: str) -> str:
        """Hash 2FA code for storage."""
        return hashlib.sha256(code.encode('utf-8')).hexdigest()


class RequestVerifier:
    """Server-side request verification (for Edge Functions)."""

    # Maximum allowed timestamp drift (5 minutes)
    MAX_TIMESTAMP_DRIFT = 300

    @classmethod
    def verify_signature(
        cls,
        yacht_id: str,
        shared_secret: str,
        payload: Dict[str, Any],
        signature: str,
        timestamp: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify a signed request.

        Args:
            yacht_id: Expected yacht_id
            shared_secret: Yacht's shared_secret from database
            payload: Request body
            signature: X-Signature header
            timestamp: X-Timestamp header

        Returns:
            (is_valid, error_message)
        """
        # Validate timestamp
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False, "Invalid timestamp format"

        now = int(time.time())
        if abs(now - ts) > cls.MAX_TIMESTAMP_DRIFT:
            return False, "Timestamp outside acceptable window"

        # Compute expected signature
        canonical = f"{ts}:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
        expected = hmac.new(
            bytes.fromhex(shared_secret),
            canonical.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        # Constant-time comparison
        if not hmac.compare_digest(signature, expected):
            return False, "Invalid signature"

        return True, None

    @staticmethod
    def verify_yacht_hash(yacht_id: str, provided_hash: str) -> bool:
        """Verify yacht_id_hash matches yacht_id."""
        expected = hashlib.sha256(yacht_id.encode('utf-8')).hexdigest()
        return hmac.compare_digest(expected.lower(), provided_hash.lower())


# ── Recovery key encryption (machine-bound) ──────────────────────────


def _get_machine_key() -> bytes:
    """Derive a Fernet key from this machine's hardware UUID.

    The hardware UUID is unique per machine, stable across OS reinstalls,
    and available without elevated permissions. If someone copies the
    recovery file to a different machine, decryption will fail — correct
    behaviour, since they'd need to re-activate via 2FA.
    """
    import base64
    import sys

    hw_uuid = _get_hardware_uuid_mac() if sys.platform == "darwin" else _get_hardware_uuid_win()

    dk = hashlib.pbkdf2_hmac(
        "sha256", hw_uuid.encode(), b"celesteos-recovery-v1", 100_000,
    )
    return base64.urlsafe_b64encode(dk)  # 32 bytes → Fernet-compatible key


def _get_hardware_uuid_mac() -> str:
    """Read IOPlatformUUID via ioreg (macOS)."""
    import subprocess

    result = subprocess.run(
        ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        if "IOPlatformUUID" in line:
            return line.split('"')[-2]
    raise RuntimeError("Cannot read hardware UUID (ioreg)")


def _get_hardware_uuid_win() -> str:
    """Read MachineGuid from Windows registry."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        if value:
            return value
    raise RuntimeError("Cannot read MachineGuid from Windows registry")


def encrypt_recovery_key(shared_secret: str) -> bytes:
    """Encrypt shared_secret with machine-bound key. Returns ciphertext bytes."""
    from cryptography.fernet import Fernet

    return Fernet(_get_machine_key()).encrypt(shared_secret.encode("utf-8"))


def decrypt_recovery_key(ciphertext: bytes) -> str:
    """Decrypt shared_secret with machine-bound key. Returns hex string."""
    from cryptography.fernet import Fernet

    return Fernet(_get_machine_key()).decrypt(ciphertext).decode("utf-8")


# Convenience functions
def compute_yacht_hash(yacht_id: str) -> str:
    """Compute SHA256 hash of yacht_id."""
    return hashlib.sha256(yacht_id.encode('utf-8')).hexdigest()


def generate_installation_manifest(yacht_id: str) -> Dict[str, Any]:
    """
    Generate manifest to embed in DMG.

    This manifest is signed into the binary and cannot be modified
    without invalidating the code signature.
    """
    return {
        'yacht_id': yacht_id,
        'yacht_id_hash': compute_yacht_hash(yacht_id),
        'version': '1.0.0',
        'build_timestamp': int(time.time()),
        'api_endpoint': 'https://qvzmkaamzaqxpzbewjxe.supabase.co',  # Supabase for verify-credentials
        'registration_api_endpoint': 'https://registration.celeste7.ai',  # Registration API for 2FA
    }
