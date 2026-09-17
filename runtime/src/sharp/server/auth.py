"""Stdlib-only auth utilities — no external dependencies.

Password: hashlib.scrypt  (available in Python 3.6+ with OpenSSL)
JWT     : HS256 built from base64 + hmac.digest (Python 3.7+)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

# ── JWT key ────────────────────────────────────────────────────────────────────

_jwt_key: bytes | None = None


def get_jwt_key() -> bytes:
    """Return the JWT signing key for this process.

    Generated fresh in memory on first use and NOT persisted: when the server
    restarts, a new key is created, so every token signed before the restart
    stops verifying and all users must log in again. This is the intended
    "re-auth on server restart" behaviour — tokens still persist in the browser
    across page reloads (localStorage), so a normal refresh does not log out.
    """
    global _jwt_key
    if _jwt_key is None:
        _jwt_key = os.urandom(32)
    return _jwt_key


def server_token() -> str:
    """Shared token for trusted backend clients (the dispatcher).

    Priority: SHARP_SERVER_TOKEN env var > persisted file next to the DB.
    Server and dispatcher resolve the same value: the dispatcher reads the env
    var (set in docker-compose / shell), the server falls back to the file it
    persists on first run. When the env var is set on both sides they match; in
    the single-host launcher case both read the same file.
    """
    env = os.environ.get("SHARP_SERVER_TOKEN")
    if env:
        return env
    # The dispatcher runs as a separate process that never calls db.configure(),
    # so current_path() would assert. Fall back to DEFAULT_DB's dir: on a single
    # host the server (which DID configure the DB to the default path) and the
    # dispatcher both resolve the same server.token file.
    from sharp.server.db import DEFAULT_DB, current_path
    try:
        base = current_path().parent
    except Exception:
        base = DEFAULT_DB.parent
    path = base / "server.token"
    try:
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val
        val = os.urandom(24).hex()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(val)
        # Backend auth secret — keep it owner-only (best-effort; no-op on FS
        # without POSIX perms, e.g. Windows).
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return val
    except Exception:
        return ""


# ── JWT sign / verify ──────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def sign_token(key: bytes, expire_hours: int = 168) -> str:
    """Return a signed HS256 JWT valid for `expire_hours` hours."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"sub": "sharp", "exp": int(time.time()) + expire_hours * 3600},
            separators=(",", ":"),
        ).encode()
    )
    msg = f"{header}.{payload}"
    sig = _b64url(hmac.digest(key, msg.encode(), "sha256"))
    return f"{msg}.{sig}"


def verify_token(token: str, key: bytes) -> bool:
    """Return True iff the token has a valid signature and has not expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        h_b64, p_b64, s_b64 = parts
        msg = f"{h_b64}.{p_b64}"
        expected = _b64url(hmac.digest(key, msg.encode(), "sha256"))
        if not hmac.compare_digest(s_b64, expected):
            return False
        data = json.loads(_b64url_decode(p_b64))
        return data.get("exp", 0) > time.time()
    except Exception:
        return False


# ── Password hash / verify ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a scrypt-derived hash: '<salt_hex>:<dk_hex>'."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison against a stored scrypt hash."""
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── Token extraction ───────────────────────────────────────────────────────────

def extract_token(request) -> str | None:  # type: ignore[type-arg]
    """Extract the JWT from Authorization: Bearer or sharp_token cookie."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    cookie = request.cookies.get("sharp_token")
    return cookie or None
