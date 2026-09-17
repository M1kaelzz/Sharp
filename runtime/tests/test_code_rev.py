"""代码指纹：让"跑着的进程 ≠ 磁盘代码"当场暴露。

两个真实事故促成了它：
①P1-5：旧 server 占端口导致新实例没起来，但 dispatcher 起来了 → "新表已建、新端点 404"；
②P0-补：改完校验后实测拿到 403 而非 422，实际是进程比代码早启动 1 小时。
`GET /` 上的 `v=70` 是手写常量，证明不了任何事，所以这里用 mtime+size 算真实指纹。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sharp.server import rev as rev_mod


def _make_tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("y = 2\n", encoding="utf-8")


def test_rev_is_stable_for_unchanged_tree(tmp_path):
    _make_tree(tmp_path)
    assert rev_mod.compute_rev([tmp_path]) == rev_mod.compute_rev([tmp_path])


def test_rev_changes_when_a_file_changes(tmp_path):
    _make_tree(tmp_path)
    before = rev_mod.compute_rev([tmp_path])
    time.sleep(0.01)
    (tmp_path / "pkg" / "a.py").write_text("x = 999\n", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path]) != before, "改了内容必须换指纹，否则 stale 检测失效"


def test_rev_ignores_unrelated_suffixes(tmp_path):
    _make_tree(tmp_path)
    before = rev_mod.compute_rev([tmp_path])
    (tmp_path / "pkg" / "notes.txt").write_text("junk", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path]) == before, "txt 不参与指纹（否则日志/临时文件会误报）"


def test_rev_covers_frontend_assets(tmp_path):
    _make_tree(tmp_path)
    before = rev_mod.compute_rev([tmp_path])
    (tmp_path / "pkg" / "app.js").write_text("// changed\n", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path]) != before, "前端改动也要能被发现"


def test_rev_skips_pycache(tmp_path):
    _make_tree(tmp_path)
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00\x01")
    before = rev_mod.compute_rev([tmp_path])
    os.utime(tmp_path / "pkg" / "__pycache__" / "a.cpython-312.pyc", None)
    assert rev_mod.compute_rev([tmp_path]) == before, "__pycache__ 随运行变化，必须排除"


def test_rev_missing_root_is_tolerated(tmp_path):
    assert rev_mod.compute_rev([tmp_path / "nope"])  # 不抛异常，返回一个确定值


def test_rev_is_short_hex(tmp_path):
    _make_tree(tmp_path)
    value = rev_mod.compute_rev([tmp_path])
    assert len(value) == 12 and all(c in "0123456789abcdef" for c in value)


def test_process_rev_is_computed_at_import():
    # 进程级指纹必须是一个具体的值（代表"本进程加载的代码"）
    assert len(rev_mod.CODE_REV) == 12


def test_rev_info_shape():
    """指纹信息含两组：Python（决定是否必须重启）与前端资源（仅信息）。"""
    info = rev_mod.rev_info()
    assert {"code_rev", "disk_rev", "stale"} <= set(info)
    assert info["code_rev"] == rev_mod.CODE_REV
    assert info["stale"] is (info["code_rev"] != info["disk_rev"])


# ── /health 集成（与 test_security_headers.py 相同的装配方式）─────────────────

@pytest.fixture
def env_db(tmp_path, monkeypatch):
    from sharp.server import db

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", "server-token-xyz")
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient

    from sharp.server.app import app

    c = TestClient(app)  # bare：不跑 lifespan，保留 fixture 的 tmp DB
    yield c
    c.close()


def test_health_reports_rev(client):
    """未鉴权 /health 也要带指纹——部署后第一眼就能看出 server 是不是旧进程。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert len(body["code_rev"]) == 12
    assert "stale" in body


def test_rev_uses_content_not_mtime(tmp_path):
    """改了又改回来（或 git checkout 回同一份内容）不该报 stale——假警报喊几次就没人信了。"""
    _make_tree(tmp_path)
    before = rev_mod.compute_rev([tmp_path])
    original = (tmp_path / "pkg" / "a.py").read_text(encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("x = 999\n", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path]) != before
    (tmp_path / "pkg" / "a.py").write_text(original, encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path]) == before, "内容回到原样就必须回到原指纹"


def test_touch_without_content_change_keeps_rev(tmp_path):
    _make_tree(tmp_path)
    before = rev_mod.compute_rev([tmp_path])
    os.utime(tmp_path / "pkg" / "b.py", None)  # 只碰 mtime
    assert rev_mod.compute_rev([tmp_path]) == before


# ── stale 只看 Python：前端改动即时生效，不该报警 ──────────────────────────

def test_py_change_marks_stale_but_asset_change_does_not(tmp_path):
    """只有 Python 代码需要"改完重启"。

    前端资源（views/*.html、app*.js）由静态服务按请求从磁盘读，include 走 mtime 缓存 ——
    改完**即时生效**。最初把 .html/.js 也算进 stale，结果改一个模板就报
    `stale:true`（实测发生），这正是本文件反复强调要避免的**假警报**。
    """
    _make_tree(tmp_path)
    py_before = rev_mod.compute_rev([tmp_path], suffixes=rev_mod._PY_SUFFIXES)
    asset_before = rev_mod.compute_rev([tmp_path], suffixes=rev_mod._ASSET_SUFFIXES)

    (tmp_path / "pkg" / "app.js").write_text("// changed\n", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path], suffixes=rev_mod._PY_SUFFIXES) == py_before, \
        "前端改动不该让 stale 判据变化"
    assert rev_mod.compute_rev([tmp_path], suffixes=rev_mod._ASSET_SUFFIXES) != asset_before, \
        "但前端指纹自己要变（信息项）"

    (tmp_path / "pkg" / "a.py").write_text("x = 999\n", encoding="utf-8")
    assert rev_mod.compute_rev([tmp_path], suffixes=rev_mod._PY_SUFFIXES) != py_before, \
        "Python 改动必须让 stale 判据变化"


def test_rev_info_separates_restart_from_live(client=None):
    info = rev_mod.rev_info()
    assert set(info) == {
        "code_rev", "disk_rev", "stale", "assets_rev", "assets_disk_rev", "assets_changed",
    }
    assert info["stale"] is (info["code_rev"] != info["disk_rev"])
    assert info["assets_changed"] is (info["assets_rev"] != info["assets_disk_rev"])


def test_health_exposes_asset_fingerprint():
    """未鉴权 /health 也要能区分"必须重启"与"前端已更新、无需重启"。"""
    from fastapi.testclient import TestClient

    from sharp.server import db as _db
    from sharp.server.app import app

    body = TestClient(app).get("/health").json()
    assert "assets_changed" in body
    assert "assets_rev" in body
