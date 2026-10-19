"""Authentication router — setup, login, logout, status, change-password."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from sharp.server.auth import get_jwt_key, hash_password, sign_token, verify_password
from sharp.server.db import get_conn

router = APIRouter(prefix="/auth", tags=["auth"])

# ── password complexity ──────────────────────────────────────────────────────
_PW_MIN_LEN = 10
_PW_RULES = [
    (re.compile(r"[A-Z]"), "至少 1 个大写字母"),
    (re.compile(r"[a-z]"), "至少 1 个小写字母"),
    (re.compile(r"\d"),    "至少 1 个数字"),
    (re.compile(r"""[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>/?`~]"""), "至少 1 个特殊字符"),
]

def _validate_password(v: str, label: str = "密码") -> str:
    if len(v) < _PW_MIN_LEN:
        raise ValueError(f"{label}长度至少 {_PW_MIN_LEN} 位")
    for pattern, hint in _PW_RULES:
        if not pattern.search(v):
            raise ValueError(f"{label}需包含{hint}")
    return v

# ── login rate limiting (in-memory) ─────────────────────────────────────────────
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 300  # 5 minutes
_lock = threading.Lock()
_fail_count = 0
_locked_until = 0.0


def _check_lockout() -> None:
    with _lock:
        if _locked_until > time.time():
            remaining = int(_locked_until - time.time())
            raise HTTPException(429, f"登录失败次数过多，请 {remaining} 秒后重试")


def _record_failure() -> None:
    global _fail_count, _locked_until
    with _lock:
        _fail_count += 1
        if _fail_count >= _MAX_FAILURES:
            _locked_until = time.time() + _LOCKOUT_SECONDS
            _fail_count = 0


def _reset_failures() -> None:
    global _fail_count, _locked_until
    with _lock:
        _fail_count = 0
        _locked_until = 0.0


class SetupRequest(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_password(v, "密码")


class LoginRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_password(v, "新密码")


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.get("/status")
def auth_status() -> dict:
    """Return whether an admin password has been configured (first-run check)."""
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM auth LIMIT 1").fetchone()
    return {"initialized": row is not None}


@router.post("/setup", status_code=201)
def setup(body: SetupRequest) -> dict:
    """Set the initial admin password. Only succeeds when not yet initialized."""
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM auth LIMIT 1").fetchone():
            raise HTTPException(409, "系统已初始化，请直接登录")
        pw_hash = hash_password(body.password)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO auth (id, password_hash, created_at) VALUES (1, ?, ?)",
            (pw_hash, now),
        )
    return {"ok": True}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    """Verify password and return a signed JWT (also sets an HttpOnly cookie)."""
    _check_lockout()
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM auth WHERE id = 1").fetchone()
    if not row:
        raise HTTPException(400, "系统尚未初始化，请先访问 /setup 设置密码")
    if not verify_password(body.password, row["password_hash"]):
        _record_failure()
        raise HTTPException(401, "密码错误")
    _reset_failures()
    token = sign_token(get_jwt_key())
    # Cookie 的 Secure 标志仅在 HTTPS 请求时设置：本地 HTTP 模式下加 Secure
    # 会导致浏览器拒绝存储 cookie，用户无法登录。
    is_https = request.url.scheme == "https"
    response.set_cookie(
        "sharp_token", token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        secure=is_https,
    )
    return {"token": token}


@router.post("/logout")
def logout(response: Response) -> dict:
    """Clear the auth cookie."""
    response.delete_cookie("sharp_token")
    return {"ok": True}


@router.post("/change-password")
def change_password(body: ChangePasswordRequest) -> dict:
    """Change the admin password. Requires a valid session (protected by middleware)."""
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM auth WHERE id = 1").fetchone()
    if not row:
        raise HTTPException(400, "系统尚未初始化")
    if not verify_password(body.old_password, row["password_hash"]):
        _record_failure()
        raise HTTPException(401, "当前密码错误")
    _reset_failures()
    new_hash = hash_password(body.new_password)
    with get_conn() as conn:
        conn.execute("UPDATE auth SET password_hash = ? WHERE id = 1", (new_hash,))
    return {"ok": True}
