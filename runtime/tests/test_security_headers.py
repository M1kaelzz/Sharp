"""安全响应头回归测试（P0 安全修复）。

背景：2026-08-26 的前端审计报告指出"无 CSP + vendor 无 SRI"，此类问题容易在
后续迭代中回退且肉眼不可见，因此把**响应头契约**固化成测试。

覆盖：鉴权成功的 API、被拒的 401、静态资源、首页 HTML 都必须带同一套安全头
（安全头中间件注册在鉴权中间件之外层，因此 401 响应也不能漏）。

CSP 取舍见 `sharp/server/app.py` 注释：Alpine/Tailwind 运行时要求
`unsafe-inline`/`unsafe-eval`，所以测试断言的是**结构性防护**
（object/base/frame/form 限制 + 只允许同源脚本与连接），而不是内联防护。

DB 与 app 的装配方式与 test_http_api.py 一致：tmp SQLite + bare TestClient。
"""

from __future__ import annotations

import pytest

from sharp.server import db

SERVER_TOKEN = "server-token-xyz"


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)  # bare：不跑 lifespan，保留 fixture 的 tmp DB
    yield c
    c.close()


def _assert_security_headers(resp) -> None:
    csp = resp.headers.get("Content-Security-Policy")
    assert csp, "缺少 Content-Security-Policy"
    # 结构性防护：禁止插件/外联对象、禁止被嵌套、禁止 base 劫持、禁止表单外发
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'self'" in csp
    # 只允许同源脚本与同源连接（外部脚本无法被注入加载）
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_health_has_security_headers(client):
    """公开端点也带头（中间件覆盖全部响应）。"""
    r = client.get("/health")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_unauthorized_response_has_security_headers(client):
    """401 由鉴权中间件直接返回 JSON，安全头中间件在更外层，不能漏。"""
    r = client.get("/projects")
    assert r.status_code == 401
    _assert_security_headers(r)


def test_authenticated_api_has_security_headers(client):
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "g"},
        headers={"Authorization": f"Bearer {SERVER_TOKEN}"},
    )
    assert r.status_code == 201, r.text
    _assert_security_headers(r)


def test_static_asset_has_security_headers(client):
    """前端 JS 走静态挂载，同样带头（避免只有 API 有 CSP 的假防护）。"""
    r = client.get("/static/app.core.js")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_index_page_has_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_csp_has_no_script_wildcard(client):
    """script-src 放宽成 * 是常见的"图省事"退化，测试锁死这一点。

    img-src 允许 https: 是有意的（报告里可能引用外部截图），但它只影响图片加载，
    不会带来脚本执行面。
    """
    csp = client.get("/health").headers["Content-Security-Policy"]
    assert "script-src *" not in csp
    assert "default-src *" not in csp
    assert "img-src 'self' data: blob: https:" in csp
