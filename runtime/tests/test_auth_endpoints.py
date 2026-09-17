"""认证端点测试（P1 覆盖率补强）——重点是**安全语义**而不是 happy path：

- 口令复杂度强校验（弱口令必须进不来，否则"10 位+四类字符"形同虚设）
- setup 只能执行一次（否则任何人都能重置管理员口令）
- 登录失败锁定（5 次 → 429），成功后计数归零
- 改密必须提供旧密码；改密后旧口令立即失效
- 登录同时下发 HttpOnly Cookie

注意：锁定计数是 **router 模块级全局状态**，测试之间必须重置，否则会互相污染
（也让"锁定"测试不至于把后续测试全锁死）。
"""

from __future__ import annotations

import pytest

from sharp.server import db

SERVER_TOKEN = "server-token-xyz"
GOOD_PW = "Str0ng!Passw0rd"
NEW_PW = "An0ther!Secret9"


@pytest.fixture(autouse=True)
def _reset_lockout():
    from sharp.server.routers import auth as auth_router

    auth_router._reset_failures()
    yield
    auth_router._reset_failures()


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)  # bare：不跑 lifespan，保持 fixture 的 tmp DB
    yield c
    c.close()


def _setup(client, password: str = GOOD_PW):
    return client.post("/auth/setup", json={"password": password})


# ── status / setup ────────────────────────────────────────────────────────────

def test_status_reports_uninitialized_on_fresh_db(client):
    r = client.get("/auth/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": False}


def test_status_reports_initialized_after_setup(client):
    assert _setup(client).status_code == 201
    assert client.get("/auth/status").json() == {"initialized": True}


@pytest.mark.parametrize(
    "weak",
    [
        "short1!A",           # 长度不足
        "alllowercase1!",     # 缺大写
        "ALLUPPERCASE1!",     # 缺小写
        "NoDigitsHere!!",     # 缺数字
        "NoSpecial12345",     # 缺特殊字符
    ],
)
def test_setup_rejects_weak_passwords(client, weak):
    r = _setup(client, weak)
    assert r.status_code == 422, f"弱口令 {weak!r} 不应被接受"
    assert client.get("/auth/status").json() == {"initialized": False}


def test_setup_cannot_run_twice(client):
    """已初始化后再 setup 必须 409 —— 否则等于任何人可重置管理员口令。"""
    assert _setup(client).status_code == 201
    r = _setup(client, NEW_PW)
    assert r.status_code == 409
    # 原口令仍然有效
    assert client.post("/auth/login", json={"password": GOOD_PW}).status_code == 200
    assert client.post("/auth/login", json={"password": NEW_PW}).status_code == 401


# ── login ─────────────────────────────────────────────────────────────────────

def test_login_before_setup_returns_400(client):
    r = client.post("/auth/login", json={"password": GOOD_PW})
    assert r.status_code == 400
    assert "初始化" in r.json()["detail"]


def test_login_success_returns_token_and_httponly_cookie(client):
    _setup(client)
    r = client.post("/auth/login", json={"password": GOOD_PW})
    assert r.status_code == 200
    assert r.json()["token"].count(".") == 2
    set_cookie = r.headers.get("set-cookie", "")
    assert "sharp_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_login_wrong_password_401(client):
    _setup(client)
    r = client.post("/auth/login", json={"password": "Wr0ng!Passw0rd"})
    assert r.status_code == 401


def test_login_lockout_after_five_failures(client):
    """5 次失败 → 第 6 次直接 429（阻断在线爆破）。"""
    _setup(client)
    for i in range(5):
        r = client.post("/auth/login", json={"password": "Wr0ng!Passw0rd"})
        assert r.status_code == 401, f"第 {i + 1} 次失败应返回 401"
    r = client.post("/auth/login", json={"password": "Wr0ng!Passw0rd"})
    assert r.status_code == 429
    assert "秒后重试" in r.json()["detail"]
    # 锁定期内即使口令正确也拒绝（锁定不区分口令对不对）
    assert client.post("/auth/login", json={"password": GOOD_PW}).status_code == 429


def test_successful_login_resets_failure_count(client):
    _setup(client)
    for _ in range(4):
        client.post("/auth/login", json={"password": "Wr0ng!Passw0rd"})
    assert client.post("/auth/login", json={"password": GOOD_PW}).status_code == 200
    # 计数已归零：再失败 4 次不应触发锁定
    for _ in range(4):
        assert client.post("/auth/login", json={"password": "Wr0ng!Passw0rd"}).status_code == 401
    assert client.post("/auth/login", json={"password": GOOD_PW}).status_code == 200


def test_logout_clears_cookie(client):
    _setup(client)
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert "sharp_token=" in r.headers.get("set-cookie", "")


# ── change-password ───────────────────────────────────────────────────────────

def test_change_password_requires_authentication(client):
    """未认证改密必须被中间件拦下（否则可绕过登录直接改管理员口令）。"""
    _setup(client)
    r = client.post("/auth/change-password", json={"old_password": GOOD_PW, "new_password": NEW_PW})
    assert r.status_code == 401


def test_change_password_rejects_wrong_old_password(client):
    _setup(client)
    token = client.post("/auth/login", json={"password": GOOD_PW}).json()["token"]
    r = client.post(
        "/auth/change-password",
        json={"old_password": "Wr0ng!Passw0rd", "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401


def test_change_password_enforces_complexity(client):
    _setup(client)
    token = client.post("/auth/login", json={"password": GOOD_PW}).json()["token"]
    r = client.post(
        "/auth/change-password",
        json={"old_password": GOOD_PW, "new_password": "weak"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_change_password_switches_credentials(client):
    _setup(client)
    token = client.post("/auth/login", json={"password": GOOD_PW}).json()["token"]
    r = client.post(
        "/auth/change-password",
        json={"old_password": GOOD_PW, "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert client.post("/auth/login", json={"password": GOOD_PW}).status_code == 401
    assert client.post("/auth/login", json={"password": NEW_PW}).status_code == 200


def test_cookie_alone_authenticates_protected_route(client):
    """Cookie 通道（HttpOnly）也应能通过中间件 —— 前端丢弃 header 时不至于失联。"""
    _setup(client)
    login = client.post("/auth/login", json={"password": GOOD_PW})
    assert login.status_code == 200
    r = client.get("/projects")  # TestClient 会自动带上 set-cookie
    assert r.status_code == 200
