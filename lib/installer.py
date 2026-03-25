"""
CelesteOS Installation Orchestrator
====================================
Handles the complete installation lifecycle using 2FA verification.

States:
    UNREGISTERED -> PENDING_2FA -> ACTIVE -> FOLDER_SELECT -> OPERATIONAL

Transitions:
    UNREGISTERED: Fresh install, no credentials
        -> POST /api/register with yacht_id + yacht_id_hash
        -> Cloud sends 2FA code to buyer email
        -> State becomes PENDING_2FA

    PENDING_2FA: Waiting for buyer to enter 6-digit code
        -> POST /api/verify-2fa with yacht_id + code
        -> Returns shared_secret ONE TIME
        -> Store in Keychain
        -> State becomes ACTIVE

    ACTIVE: Has shared_secret, can authenticate
        -> All API calls signed with HMAC-SHA256
        -> State becomes FOLDER_SELECT

    FOLDER_SELECT: Needs NAS folder assignment
        -> User picks folder via folder selector
        -> State becomes OPERATIONAL

    OPERATIONAL: Fully operational
        -> Normal operation, periodic health checks
"""

import os
import json
import logging
import sys
import requests
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass

from .crypto import CryptoIdentity, compute_yacht_hash


def _get_config_dir() -> Path:
    """Config directory — inline to avoid circular import with agent.platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "CelesteOS"
        return Path.home() / "AppData" / "Roaming" / "CelesteOS"
    return Path.home() / ".celesteos"

logger = logging.getLogger("lib.installer")


class InstallState(Enum):
    """Installation state machine."""
    UNREGISTERED = "unregistered"
    PENDING_2FA = "pending_2fa"
    ACTIVE = "active"
    FOLDER_SELECT = "folder_select"
    OPERATIONAL = "operational"
    ERROR = "error"


@dataclass
class InstallConfig:
    """Installation configuration embedded in DMG."""
    yacht_id: str
    yacht_id_hash: str
    api_endpoint: str  # Supabase for verify-credentials
    registration_api_endpoint: str = "https://registration.celeste7.ai"
    yacht_name: str = ""
    version: str = "1.0.0"
    build_timestamp: int = 0
    tenant_supabase_url: str = ""
    tenant_supabase_service_key: str = ""  # Build-time secret from DMG manifest

    @classmethod
    def load_embedded(cls) -> 'InstallConfig':
        """Load config embedded in application bundle."""
        import sys

        # Determine if running in PyInstaller bundle
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundle_dir = Path(sys._MEIPASS)
            bundle_path = bundle_dir / 'Resources' / 'install_manifest.json'
        else:
            bundle_path = Path(__file__).parent.parent / 'Resources' / 'install_manifest.json'

            if not bundle_path.exists():
                # Development fallback
                bundle_path = _get_config_dir() / 'install_manifest.json'

        if not bundle_path.exists():
            raise FileNotFoundError(
                f"Installation manifest not found at {bundle_path}. "
                "This binary was not properly built or the manifest is missing."
            )

        try:
            with open(bundle_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid manifest JSON: {e}")

        required_fields = ['yacht_id', 'yacht_id_hash', 'api_endpoint']
        missing = [f for f in required_fields if f not in data]
        if missing:
            raise ValueError(f"Manifest missing required fields: {missing}")

        return cls(
            yacht_id=data['yacht_id'],
            yacht_id_hash=data['yacht_id_hash'],
            api_endpoint=data['api_endpoint'],
            registration_api_endpoint=data.get(
                'registration_api_endpoint', 'https://registration.celeste7.ai'
            ),
            yacht_name=data.get('yacht_name', ''),
            version=data.get('version', '1.0.0'),
            build_timestamp=data.get('build_timestamp', 0),
            tenant_supabase_url=data.get('tenant_supabase_url', ''),
            tenant_supabase_service_key=data.get('tenant_supabase_service_key', ''),
        )

    def verify_integrity(self) -> bool:
        """Verify manifest hasn't been tampered with."""
        expected_hash = compute_yacht_hash(self.yacht_id)
        return expected_hash == self.yacht_id_hash


class KeychainStore:
    """
    Secure secret storage — macOS Keychain or Windows Credential Manager.

    On macOS: uses security(1) command for Keychain access.
    On Windows: uses keyring library (Windows Credential Manager / DPAPI).
    """

    SERVICE_NAME = "com.celeste7.celesteos"

    @classmethod
    def store_secret(cls, yacht_id: str, shared_secret: str) -> bool:
        """Store shared_secret in the platform credential store."""
        import sys

        if sys.platform == "win32":
            try:
                import keyring
                keyring.set_password(cls.SERVICE_NAME, yacht_id, shared_secret)
                return True
            except Exception:
                return False
        else:
            import subprocess
            # Delete existing entry if present
            subprocess.run(
                ['security', 'delete-generic-password', '-s', cls.SERVICE_NAME, '-a', yacht_id],
                capture_output=True,
            )
            result = subprocess.run(
                [
                    'security', 'add-generic-password',
                    '-s', cls.SERVICE_NAME,
                    '-a', yacht_id,
                    '-w', shared_secret,
                    '-U',
                ],
                capture_output=True,
            )
            return result.returncode == 0

    @classmethod
    def retrieve_secret(cls, yacht_id: str) -> Optional[str]:
        """Retrieve shared_secret from the platform credential store."""
        import sys

        if sys.platform == "win32":
            try:
                import keyring
                return keyring.get_password(cls.SERVICE_NAME, yacht_id)
            except Exception:
                return None
        else:
            import subprocess
            result = subprocess.run(
                ['security', 'find-generic-password', '-s', cls.SERVICE_NAME, '-a', yacht_id, '-w'],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None

    @classmethod
    def delete_secret(cls, yacht_id: str) -> bool:
        """Delete secret from the platform credential store."""
        import sys

        if sys.platform == "win32":
            try:
                import keyring
                keyring.delete_password(cls.SERVICE_NAME, yacht_id)
                return True
            except Exception:
                return False
        else:
            import subprocess
            result = subprocess.run(
                ['security', 'delete-generic-password', '-s', cls.SERVICE_NAME, '-a', yacht_id],
                capture_output=True,
            )
            return result.returncode == 0


class InstallationOrchestrator:
    """
    Orchestrates the complete installation flow.

    This is the main entry point for installation operations.
    Uses 2FA codes instead of activation link polling.
    """

    def __init__(self, config: InstallConfig):
        self.config = config
        self.state = InstallState.UNREGISTERED
        self._crypto: Optional[CryptoIdentity] = None
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': f'CelesteOS-Installer/{config.version}',
            'Accept-Encoding': 'identity',  # Disable gzip (PyInstaller bundling issue)
        })

    def initialize(self) -> InstallState:
        """
        Initialize installation state.

        Checks:
        1. Manifest integrity
        2. Existing credentials in Keychain
        3. Credential validity with server
        """
        if not self.config.verify_integrity():
            self.state = InstallState.ERROR
            raise SecurityError("Installation manifest integrity check failed")

        secret = KeychainStore.retrieve_secret(self.config.yacht_id)

        if secret:
            self._crypto = CryptoIdentity(self.config.yacht_id, secret)

            if self._verify_credentials():
                self.state = InstallState.OPERATIONAL
            else:
                KeychainStore.delete_secret(self.config.yacht_id)
                self._crypto = None
                self.state = InstallState.UNREGISTERED
        else:
            self._crypto = CryptoIdentity(self.config.yacht_id)
            self.state = InstallState.UNREGISTERED

        return self.state

    def register(self) -> Tuple[bool, str]:
        """
        Register yacht with cloud — triggers 2FA code email to buyer.

        Returns:
            (success, message)
        """
        if self.state not in [InstallState.UNREGISTERED, InstallState.ERROR]:
            return False, f"Cannot register from state: {self.state.value}"

        payload = {
            'yacht_id': self.config.yacht_id,
            'yacht_id_hash': self.config.yacht_id_hash
        }

        try:
            resp = self._session.post(
                f"{self.config.registration_api_endpoint}/api/register",
                json=payload,
                timeout=30
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get('success'):
                    self.state = InstallState.PENDING_2FA
                    masked_email = data.get('email_sent_to', 'your email')
                    return True, f"Verification code sent to {masked_email}"
                else:
                    return False, data.get('error', 'Registration failed')

            try:
                error = resp.json().get('error', resp.json().get('message', 'Unknown error'))
            except Exception:
                error = f"HTTP {resp.status_code}"
            return False, f"Registration failed: {error}"

        except requests.RequestException as e:
            return False, f"Network error: {e}"

    def verify_2fa(self, code: str) -> Tuple[bool, str]:
        """
        Verify 2FA code and receive shared_secret.

        Args:
            code: 6-digit code entered by buyer

        Returns:
            (success, message)
        """
        if self.state != InstallState.PENDING_2FA:
            return False, f"Cannot verify 2FA from state: {self.state.value}"

        payload = {
            'yacht_id': self.config.yacht_id,
            'code': code
        }

        try:
            resp = self._session.post(
                f"{self.config.registration_api_endpoint}/api/verify-2fa",
                json=payload,
                timeout=30
            )

            if resp.status_code == 200:
                data = resp.json()
                shared_secret = data.get('shared_secret')

                if not shared_secret:
                    self.state = InstallState.ERROR
                    return False, "Server did not return credentials"

                # Store shared_secret in Keychain
                if not KeychainStore.store_secret(self.config.yacht_id, shared_secret):
                    self.state = InstallState.ERROR
                    return False, "Failed to store credentials in Keychain"

                # Write recovery key in case Keychain is reset
                try:
                    recovery_dir = _get_config_dir()
                    recovery_dir.mkdir(parents=True, exist_ok=True)
                    recovery_path = recovery_dir / ".recovery_key"
                    from .crypto import encrypt_recovery_key
                    recovery_path.write_bytes(encrypt_recovery_key(shared_secret))
                    if sys.platform != "win32":
                        os.chmod(str(recovery_path), 0o600)
                    logger.info("Recovery key written to %s", recovery_path)
                except OSError as exc:
                    logger.warning("Failed to write recovery key: %s", exc)

                self._crypto = CryptoIdentity(self.config.yacht_id, shared_secret)

                # Save tenant credentials to ~/.celesteos/.env.local
                # URL: prefer API response (fleet_registry), fall back to manifest
                # Key: ALWAYS from manifest (build-time secret, never sent over wire)
                supabase_url = data.get('supabase_url', '') or self.config.tenant_supabase_url
                supabase_key = self.config.tenant_supabase_service_key
                if supabase_url and supabase_key:
                    self._save_tenant_config(supabase_url, supabase_key)
                elif not supabase_key:
                    logger.warning("No tenant service key in manifest — agent will not be able to sync")

                self.state = InstallState.ACTIVE
                return True, "Activation successful. Credentials stored."

            try:
                data = resp.json()
                error = data.get('error', 'Invalid code')
                remaining = data.get('attempts_remaining')
                if remaining is not None:
                    error += f" ({remaining} attempts remaining)"
            except Exception:
                error = f"HTTP {resp.status_code}"
            return False, error

        except requests.RequestException as e:
            return False, f"Network error: {e}"

    def _save_tenant_config(self, supabase_url: str, supabase_key: str) -> None:
        """Save tenant Supabase credentials to config dir .env.local after activation."""
        env_dir = _get_config_dir()
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / ".env.local"

        lines = []
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                key = line.strip().split("=")[0] if "=" in line else ""
                if key not in ("YACHT_ID", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
                    lines.append(line)

        lines.append(f"YACHT_ID={self.config.yacht_id}")
        lines.append(f"SUPABASE_URL={supabase_url}")
        lines.append(f"SUPABASE_SERVICE_KEY={supabase_key}")
        env_file.write_text("\n".join(lines) + "\n")
        if sys.platform != "win32":
            os.chmod(str(env_file), 0o600)
        logger.info("Tenant credentials saved to %s", env_file)

    def _verify_credentials(self) -> bool:
        """Verify stored credentials are still valid."""
        if not self._crypto or not self._crypto.has_secret:
            return False

        try:
            payload = {'action': 'verify'}
            headers = self._crypto.sign_request(payload)

            resp = self._session.post(
                f"{self.config.api_endpoint}/functions/v1/verify-credentials",
                json=payload,
                headers=headers,
                timeout=10
            )

            return resp.status_code == 200

        except requests.RequestException:
            return False

    def get_signed_headers(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """Get HMAC-signed headers for an API request."""
        if not self._crypto or not self._crypto.has_secret:
            raise SecurityError("No credentials available for signing")

        return self._crypto.sign_request(payload)


class SecurityError(Exception):
    """Security-related errors during installation."""
    pass


# CLI Entry Point
def run_installation():
    """Run interactive installation from command line."""
    print("=" * 60)
    print("CelesteOS Installation")
    print("=" * 60)

    try:
        config = InstallConfig.load_embedded()
        print(f"Yacht ID: {config.yacht_id}")
        print(f"Version:  {config.version}")

        orchestrator = InstallationOrchestrator(config)
        state = orchestrator.initialize()

        print(f"State:    {state.value}")

        if state == InstallState.OPERATIONAL:
            print("\n✓ Already activated and operational")
            return True

        if state == InstallState.UNREGISTERED:
            print("\nRegistering with cloud...")
            success, message = orchestrator.register()
            print(f"  {message}")

            if not success:
                return False

        if orchestrator.state == InstallState.PENDING_2FA:
            print("\nEnter the 6-digit verification code from your email:")
            code = input("  Code: ").strip()

            if not code or len(code) != 6:
                print("\n✗ Invalid code format (must be 6 digits)")
                return False

            success, message = orchestrator.verify_2fa(code)
            print(f"  {message}")

            if success:
                print("\n✓ Activation successful!")
                print("  Credentials stored in Keychain")
                return True
            else:
                print(f"\n✗ Verification failed: {message}")
                return False

    except FileNotFoundError as e:
        print(f"\n✗ Error: {e}")
        return False

    except SecurityError as e:
        print(f"\n✗ Security Error: {e}")
        return False


if __name__ == '__main__':
    run_installation()
