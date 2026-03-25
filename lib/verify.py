"""
CelesteOS Installation Verification
====================================
End-to-end verification of installation security.

Verifies:
1. Manifest integrity (yacht_id_hash matches yacht_id)
2. Credential retrieval security (one-time only)
3. HMAC signature verification (timestamp, payload)
4. Response signature verification (optional)

Usage:
    python -m lib.verify --yacht-id YACHT_001 --api-endpoint https://xxx.supabase.co
"""

import sys
import json
import time
import hashlib
import hmac
import requests
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass

from .crypto import CryptoIdentity, compute_yacht_hash


@dataclass
class VerificationResult:
    """Result of a verification check."""
    name: str
    passed: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class InstallationVerifier:
    """
    Comprehensive verification of installation security.
    """

    def __init__(self, api_endpoint: str, timeout: int = 30):
        self.api_endpoint = api_endpoint.rstrip('/')
        self.timeout = timeout
        self.results: list[VerificationResult] = []

    def verify_manifest_integrity(self, yacht_id: str, yacht_id_hash: str) -> VerificationResult:
        """Verify manifest hasn't been tampered with."""
        expected = compute_yacht_hash(yacht_id)
        passed = expected == yacht_id_hash

        result = VerificationResult(
            name="Manifest Integrity",
            passed=passed,
            message="Hash matches" if passed else "Hash mismatch - manifest tampered",
            details={
                "expected": expected[:16] + "...",
                "provided": yacht_id_hash[:16] + "...",
            }
        )
        self.results.append(result)
        return result

    def verify_registration(self, yacht_id: str, yacht_id_hash: str) -> VerificationResult:
        """Test registration endpoint."""
        try:
            resp = requests.post(
                f"{self.api_endpoint}/functions/v1/register",
                json={"yacht_id": yacht_id, "yacht_id_hash": yacht_id_hash},
                timeout=self.timeout
            )

            if resp.status_code == 200:
                data = resp.json()
                result = VerificationResult(
                    name="Registration Endpoint",
                    passed=True,
                    message=f"Registration successful: {data.get('status')}",
                    details=data
                )
            elif resp.status_code == 409:
                result = VerificationResult(
                    name="Registration Endpoint",
                    passed=True,
                    message="Already registered (expected for activated yachts)",
                    details=resp.json()
                )
            else:
                result = VerificationResult(
                    name="Registration Endpoint",
                    passed=False,
                    message=f"Registration failed: {resp.status_code}",
                    details=resp.json() if resp.text else None
                )

        except requests.RequestException as e:
            result = VerificationResult(
                name="Registration Endpoint",
                passed=False,
                message=f"Network error: {e}"
            )

        self.results.append(result)
        return result

    def verify_one_time_retrieval(self, yacht_id: str) -> VerificationResult:
        """Verify credentials can only be retrieved once."""
        try:
            # First retrieval
            resp1 = requests.post(
                f"{self.api_endpoint}/functions/v1/check-activation",
                json={"yacht_id": yacht_id},
                timeout=self.timeout
            )

            first_status = resp1.json().get("status") if resp1.status_code == 200 else None
            first_has_secret = "shared_secret" in resp1.json() if resp1.status_code == 200 else False

            # Second retrieval
            resp2 = requests.post(
                f"{self.api_endpoint}/functions/v1/check-activation",
                json={"yacht_id": yacht_id},
                timeout=self.timeout
            )

            second_status = resp2.json().get("status") if resp2.status_code == 200 else None
            second_has_secret = "shared_secret" in resp2.json() if resp2.status_code == 200 else False

            # Verify second call never returns secret
            if second_status == "already_retrieved" and not second_has_secret:
                result = VerificationResult(
                    name="One-Time Retrieval Security",
                    passed=True,
                    message="Credentials correctly blocked on second retrieval",
                    details={
                        "first_status": first_status,
                        "first_had_secret": first_has_secret,
                        "second_status": second_status,
                        "second_had_secret": second_has_secret,
                    }
                )
            elif second_has_secret:
                result = VerificationResult(
                    name="One-Time Retrieval Security",
                    passed=False,
                    message="CRITICAL: Secret returned on second retrieval!",
                    details={
                        "first_status": first_status,
                        "second_status": second_status,
                        "security_violation": True,
                    }
                )
            else:
                result = VerificationResult(
                    name="One-Time Retrieval Security",
                    passed=True,
                    message=f"Status: {second_status} (yacht may be pending/not activated)",
                    details={
                        "first_status": first_status,
                        "second_status": second_status,
                    }
                )

        except requests.RequestException as e:
            result = VerificationResult(
                name="One-Time Retrieval Security",
                passed=False,
                message=f"Network error: {e}"
            )

        self.results.append(result)
        return result

    def verify_hmac_signature(
        self,
        yacht_id: str,
        shared_secret: str,
        payload: Dict[str, Any]
    ) -> VerificationResult:
        """Test HMAC signature verification."""
        try:
            crypto = CryptoIdentity(yacht_id, shared_secret)
            headers = crypto.sign_request(payload)

            resp = requests.post(
                f"{self.api_endpoint}/functions/v1/verify-credentials",
                json=payload,
                headers=headers,
                timeout=self.timeout
            )

            if resp.status_code == 200:
                result = VerificationResult(
                    name="HMAC Signature Verification",
                    passed=True,
                    message="Signature verified successfully",
                    details=resp.json()
                )
            else:
                result = VerificationResult(
                    name="HMAC Signature Verification",
                    passed=False,
                    message=f"Verification failed: {resp.status_code}",
                    details=resp.json() if resp.text else None
                )

        except Exception as e:
            result = VerificationResult(
                name="HMAC Signature Verification",
                passed=False,
                message=f"Error: {e}"
            )

        self.results.append(result)
        return result

    def verify_invalid_signature_rejected(
        self,
        yacht_id: str,
        shared_secret: str,
        payload: Dict[str, Any]
    ) -> VerificationResult:
        """Verify invalid signatures are rejected."""
        try:
            # Create valid signature then corrupt it
            crypto = CryptoIdentity(yacht_id, shared_secret)
            headers = crypto.sign_request(payload)

            # Corrupt the signature
            headers["X-Signature"] = "0" * 64

            resp = requests.post(
                f"{self.api_endpoint}/functions/v1/verify-credentials",
                json=payload,
                headers=headers,
                timeout=self.timeout
            )

            if resp.status_code == 401:
                result = VerificationResult(
                    name="Invalid Signature Rejection",
                    passed=True,
                    message="Invalid signature correctly rejected",
                    details=resp.json() if resp.text else None
                )
            else:
                result = VerificationResult(
                    name="Invalid Signature Rejection",
                    passed=False,
                    message=f"SECURITY: Invalid signature was accepted! Status: {resp.status_code}",
                    details=resp.json() if resp.text else None
                )

        except Exception as e:
            result = VerificationResult(
                name="Invalid Signature Rejection",
                passed=False,
                message=f"Error: {e}"
            )

        self.results.append(result)
        return result

    def verify_timestamp_drift_rejected(
        self,
        yacht_id: str,
        shared_secret: str,
        payload: Dict[str, Any]
    ) -> VerificationResult:
        """Verify old timestamps are rejected (replay attack prevention)."""
        try:
            crypto = CryptoIdentity(yacht_id, shared_secret)

            # Sign with timestamp 10 minutes in the past
            old_timestamp = int(time.time()) - 600
            headers = crypto.sign_request(payload, timestamp=old_timestamp)

            resp = requests.post(
                f"{self.api_endpoint}/functions/v1/verify-credentials",
                json=payload,
                headers=headers,
                timeout=self.timeout
            )

            if resp.status_code == 401:
                result = VerificationResult(
                    name="Timestamp Drift Rejection",
                    passed=True,
                    message="Old timestamp correctly rejected (replay attack prevented)",
                    details={"timestamp_age_seconds": 600}
                )
            else:
                result = VerificationResult(
                    name="Timestamp Drift Rejection",
                    passed=False,
                    message=f"SECURITY: Old timestamp was accepted! Status: {resp.status_code}",
                    details=resp.json() if resp.text else None
                )

        except Exception as e:
            result = VerificationResult(
                name="Timestamp Drift Rejection",
                passed=False,
                message=f"Error: {e}"
            )

        self.results.append(result)
        return result

    def run_all(
        self,
        yacht_id: str,
        yacht_id_hash: str,
        shared_secret: Optional[str] = None
    ) -> Tuple[int, int]:
        """
        Run all verification checks.

        Returns:
            (passed_count, total_count)
        """
        print("=" * 60)
        print("CelesteOS Installation Verification")
        print("=" * 60)
        print(f"API Endpoint: {self.api_endpoint}")
        print(f"Yacht ID:     {yacht_id}")
        print()

        # Basic checks
        self.verify_manifest_integrity(yacht_id, yacht_id_hash)
        self.verify_registration(yacht_id, yacht_id_hash)
        self.verify_one_time_retrieval(yacht_id)

        # Signature verification (requires shared_secret)
        if shared_secret:
            test_payload = {"action": "verify", "test": True}
            self.verify_hmac_signature(yacht_id, shared_secret, test_payload)
            self.verify_invalid_signature_rejected(yacht_id, shared_secret, test_payload)
            self.verify_timestamp_drift_rejected(yacht_id, shared_secret, test_payload)

        # Print results
        print("\nResults:")
        print("-" * 60)

        passed = 0
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            icon = "✓" if r.passed else "✗"
            print(f"  {icon} [{status}] {r.name}")
            print(f"           {r.message}")
            if r.passed:
                passed += 1

        total = len(self.results)
        print()
        print("=" * 60)
        print(f"Results: {passed}/{total} passed")
        print("=" * 60)

        return passed, total


def run_verification():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Verify CelesteOS installation security")
    parser.add_argument("--yacht-id", required=True, help="Yacht identifier")
    parser.add_argument("--yacht-id-hash", help="Yacht ID hash (computed if not provided)")
    parser.add_argument("--shared-secret", help="Shared secret for signature tests")
    parser.add_argument("--api-endpoint", default="https://qvzmkaamzaqxpzbewjxe.supabase.co",
                        help="Supabase API endpoint")

    args = parser.parse_args()

    yacht_id_hash = args.yacht_id_hash or compute_yacht_hash(args.yacht_id)

    verifier = InstallationVerifier(args.api_endpoint)
    passed, total = verifier.run_all(
        args.yacht_id,
        yacht_id_hash,
        args.shared_secret
    )

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    run_verification()
