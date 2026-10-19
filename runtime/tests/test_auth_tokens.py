"""认证原语的安全分支测试（P1 覆盖率补强）。

`auth.py` 是入口安全的关键：JWT 签名校验、口令哈希、token 提取。此前只有
"正常路径"被间接覆盖，**失败分支（篡改 / 过期 / 畸形 / 损坏哈希）几乎没测** ——
而恰恰是这些分支决定了"伪造 token 能不能进来"。

这里直接对函数做单元测试（不经 HTTP），因为要精确构造畸形输入。
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from sharp.server.auth import (
    extract_token,
    hash_password,
    sign_token,
    verify_password,
    verify_token,
)

KEY = b"unit-test-key-0123456789abcdef"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ── JWT ───────────────────────────────────────────────────────────────────────

def test_sign_verify_roundtrip():
    token = sign_token(KEY)
    assert token.count(".") == 2
    assert verify_token(token, KEY) is True


def test_token_rejected_with_other_key():
    """换密钥就验不过（JWT 密钥每次启动重新生成 → 重启即失效）。"""
    assert verify_token(sign_token(KEY), b"another-key-entirely") is False


def test_token_with_tampered_signature_rejected():
    header, payload, sig = sign_token(KEY).split(".")
    bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert verify_token(f"{header}.{payload}.{bad_sig}", KEY) is False


def test_token_with_escalated_payload_rejected():
    """改 payload（例如延长 exp）但签名不变 → 必须拒绝。"""
    header, payload, sig = sign_token(KEY).split(".")
    forged = _b64u(json.dumps({"sub": "sharp", "exp": int(time.time()) + 10**6}).encode())
    assert verify_token(f"{header}.{forged}.{sig}", KEY) is False


def test_expired_token_rejected():
    assert verify_token(sign_token(KEY, expire_hours=-1), KEY) is False


def test_expiry_boundary_is_not_accepted_at_now():
    """exp 恰好等于当前秒时不再视为有效（严格大于）。"""
    assert verify_token(sign_token(KEY, expire_hours=0), KEY) is False


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", "...", "not-a-jwt-at-all"])
def test_malformed_tokens_rejected(bad):
    assert verify_token(bad, KEY) is False


def test_token_with_non_json_payload_rejected():
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64u(b"not-json")
    import hashlib
    import hmac
    msg = f"{header}.{payload}"
    sig = _b64u(hmac.digest(KEY, msg.encode(), "sha256"))
    # 签名正确但 payload 不是 JSON → 解析失败必须走 except 分支返回 False
    assert verify_token(f"{msg}.{sig}", KEY) is False


# ── 口令哈希 ──────────────────────────────────────────────────────────────────

def test_hash_password_roundtrip():
    stored = hash_password("Str0ng!Passw0rd")
    assert ":" in stored
    assert verify_password("Str0ng!Passw0rd", stored) is True
    assert verify_password("wrong-password", stored) is False


def test_hash_password_uses_random_salt():
    a = hash_password("Str0ng!Passw0rd")
    b = hash_password("Str0ng!Passw0rd")
    assert a != b, "相同口令必须产生不同哈希（随机盐）"
    assert verify_password("Str0ng!Passw0rd", a)
    assert verify_password("Str0ng!Passw0rd", b)


@pytest.mark.parametrize("broken", ["", "no-colon", "zz:not-hex", ":empty-salt"])
def test_verify_password_survives_corrupted_storage(broken):
    """库里哈希损坏时应返回 False，而不是抛异常打断登录流程。"""
    assert verify_password("whatever", broken) is False


# ── token 提取 ────────────────────────────────────────────────────────────────

def _request(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "headers": raw})


def test_extract_token_prefers_authorization_header():
    req = _request({"Authorization": "Bearer from-header"}, {"sharp_token": "from-cookie"})
    assert extract_token(req) == "from-header"


def test_extract_token_falls_back_to_cookie():
    assert extract_token(_request(cookies={"sharp_token": "cookie-jwt"})) == "cookie-jwt"


def test_extract_token_returns_none_when_absent():
    assert extract_token(_request()) is None


def test_extract_token_ignores_non_bearer_authorization():
    """Basic/Bearer 前缀缺失时不应把头部内容当 token（防解析混淆）。"""
    req = _request({"Authorization": "Basic dXNlcjpwYXNz"})
    assert extract_token(req) is None
