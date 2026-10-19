"""Offline license verification for the packaged (frozen) Sharp binary.

A license is a small JSON document signed with the vendor's Ed25519 private
key. The matching public key is embedded below and compiled into the binary, so
the check works fully offline. This deters casual copying and enforces a time-
limited trial; it is NOT tamper-proof against someone who patches the binary
(that is an accepted trade-off for a trial build — see docs/adr).

License file format (base64url of the JSON, then ".", then base64url signature):
    {
      "subject": "friend@example",   # who it's for (informational)
      "issued_at": "2026-07-14T00:00:00Z",
      "expires_at": "2026-08-14T00:00:00Z",
      "license_id": "..."
    }
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Vendor public key (base64 of the 32-byte raw Ed25519 public key). The private
# counterpart lives only on the vendor's machine and signs each license.
LICENSE_PUBLIC_KEY_B64 = "k8w13z/cvnJYj8G2pjssI9RfuRbk76N/BeC1waPMvms="


class LicenseError(RuntimeError):
    """Raised when a license is missing, malformed, unsigned, or expired."""


@dataclass(frozen=True)
class License:
    subject: str
    issued_at: datetime
    expires_at: datetime
    license_id: str

    @property
    def days_remaining(self) -> int:
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0, delta.days)


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _parse_ts(value: str) -> datetime:
    # Accept trailing 'Z' (UTC) as well as explicit offsets.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def verify_license_text(token: str, *, now: datetime | None = None) -> License:
    """Verify a license token string and return the License, or raise LicenseError."""
    now = now or datetime.now(timezone.utc)
    token = token.strip()
    if not token or "." not in token:
        raise LicenseError("license is empty or malformed")
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad license
        raise LicenseError("license encoding is invalid") from exc

    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSE_PUBLIC_KEY_B64))
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise LicenseError("license signature does not match; this build rejects it") from exc

    try:
        data = json.loads(payload_bytes.decode("utf-8"))
        license_obj = License(
            subject=str(data["subject"]),
            issued_at=_parse_ts(str(data["issued_at"])),
            expires_at=_parse_ts(str(data["expires_at"])),
            license_id=str(data["license_id"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise LicenseError("license payload is invalid") from exc

    if now < license_obj.issued_at:
        raise LicenseError("license is not yet valid (system clock may be wrong)")
    if now >= license_obj.expires_at:
        raise LicenseError(
            f"license expired on {license_obj.expires_at:%Y-%m-%d}; contact the vendor for a new key"
        )
    return license_obj


def verify_license_file(path, *, now: datetime | None = None) -> License:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise LicenseError(
            f"license file not found: {p}\nPlace the license key file the vendor gave you at this path."
        )
    return verify_license_text(p.read_text(encoding="utf-8"), now=now)
