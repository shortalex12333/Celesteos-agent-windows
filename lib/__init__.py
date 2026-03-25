"""
CelesteOS Cloud Library
=======================
Shared modules for installation, authentication, and verification.
"""

from .crypto import (
    CryptoIdentity,
    SecretGenerator,
    RequestVerifier,
    compute_yacht_hash,
    generate_installation_manifest,
)

from .installer import (
    InstallState,
    InstallConfig,
    KeychainStore,
    InstallationOrchestrator,
    SecurityError,
)

__all__ = [
    # Crypto
    'CryptoIdentity',
    'SecretGenerator',
    'RequestVerifier',
    'compute_yacht_hash',
    'generate_installation_manifest',
    # Installer
    'InstallState',
    'InstallConfig',
    'KeychainStore',
    'InstallationOrchestrator',
    'SecurityError',
    # Re-export for convenience
    'run_installation',
]
