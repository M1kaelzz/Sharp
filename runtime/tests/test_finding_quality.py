"""结论可交付性评估测试（P0-1）。

真实授权渗透的交付物是报告，而报告的价值取决于结论能否复现——"只有标题和描述的
confirmed 漏洞"对外提交时等于不可验证的声称。本文件锁定评估规则与它的 API 暴露。

重点覆盖两类容易退化的地方：
1. **不能把"形同虚设的证据"算作齐备**（"见截图"、"如上"这种填了等于没填）；
2. **评分类产物不参与评估**（旗帜不需要复现四件套），避免误伤 scored 项目。
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.finding_quality import assess_finding, report_warning

SERVER_TOKEN = "server-token-xyz"

FULL_EVIDENCE = (
    "POST /api/user/update HTTP/1.1\nHost: target.example\nCookie: session=abc\n\nid=2\n\n"
    "HTTP/1.1 200 OK\nContent-Type: application/json\n\n{\"id\":2,\"email\":\"victim@example.com\"}"
)
FULL_REPRO = "1) 用账号 A 登录取得 session；2) 将请求中的 id 改为账号 B 的 id；3) 观察返回账号 B 的数据。"
FULL_IMPACT = "任意登录用户可读取他人资料，属于水平越权，影响全部注册用户。"


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


@pytest.fixture
def project(client) -> str:
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "g"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


# ── 评估规则 ──────────────────────────────────────────────────────────────────

def test_complete_finding():
    q = assess_finding(url="http://target.example/api/user", evidence=FULL_EVIDENCE,
                       reproduction=FULL_REPRO, impact=FULL_IMPACT)
    assert q.applicable and q.complete and q.score == q.total == 4
    assert q.missing == ()


def test_missing_items_are_listed_with_labels():
    q = assess_finding(url="http://t.example/x", evidence="", reproduction="", impact="")
    assert not q.complete
    assert set(q.missing) == {"evidence", "reproduction", "impact"}
    assert "原始请求/响应" in q.missing_labels


@pytest.mark.parametrize("filler", ["见截图", "如上", "TODO", "  ", "ok"])
def test_placeholder_evidence_does_not_count(filler):
    """填了但等于没填的内容不能算证据齐备。"""
    q = assess_finding(url="u", evidence=filler, reproduction=FULL_REPRO, impact=FULL_IMPACT)
    assert "evidence" in q.missing


def test_request_without_response_gets_guidance():
    q = assess_finding(url="u", evidence="GET /admin HTTP/1.1\nHost: t.example",
                       reproduction=FULL_REPRO, impact=FULL_IMPACT)
    assert any("未见响应" in n for n in q.notes)


def test_response_without_request_gets_guidance():
    q = assess_finding(url="u", evidence="HTTP/1.1 200 OK\nServer: nginx",
                       reproduction=FULL_REPRO, impact=FULL_IMPACT)
    assert any("未见原始请求" in n for n in q.notes)


def test_scored_artifact_is_not_assessed():
    """旗帜不需要复现证据——否则每个 flag 都会被标成"证据不足"。"""
    q = assess_finding(kind="flag", url="", evidence="flag{abc}", reproduction="", impact="")
    assert q.applicable is False and q.complete is True
    assert report_warning(q) == ""


def test_report_warning_text():
    q = assess_finding(url="u", evidence="", reproduction=FULL_REPRO, impact=FULL_IMPACT)
    warn = report_warning(q)
    assert "证据不足" in warn and "原始请求/响应" in warn
    assert report_warning(assess_finding(url="u", evidence=FULL_EVIDENCE,
                                        reproduction=FULL_REPRO, impact=FULL_IMPACT)) == ""


# ── API 暴露 ─────────────────────────────────────────────────────────────────

def _create(client, pid: str, **overrides) -> dict:
    body = {"fact_id": "origin", "title": "t", "severity": "high", "kind": "vuln"}
    body.update(overrides)
    r = client.post(f"/projects/{pid}/vulnerabilities", json=body, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()


def test_api_exposes_quality_for_incomplete_finding(client, project):
    v = _create(client, project, title="越权", description="描述")
    assert v["quality"]["applicable"] is True
    assert v["quality"]["complete"] is False
    assert v["quality"]["score"] == 0          # url 也没填 → 四项全缺
    assert "evidence" in v["quality"]["missing"]


def test_api_exposes_complete_quality(client, project):
    v = _create(client, project, title="越权", url="http://t.example/api",
                evidence=FULL_EVIDENCE, reproduction=FULL_REPRO, impact=FULL_IMPACT)
    assert v["quality"]["complete"] is True and v["quality"]["score"] == 4


def test_api_quality_survives_list_endpoint(client, project):
    _create(client, project, title="a")
    rows = client.get(f"/projects/{project}/vulnerabilities", headers=_h()).json()
    assert rows and rows[0]["quality"]["applicable"] is True


def test_api_flag_quality_not_applicable(client, project):
    v = _create(client, project, title="a-01 旗帜", kind="flag", score=100)
    assert v["quality"]["applicable"] is False
    assert v["quality"]["complete"] is True


def test_quality_is_derived_not_stored(client, project):
    """补全证据后完备度立即变化（说明是派生计算而非落库快照）。"""
    v = _create(client, project, title="越权")
    assert client.get(f"/projects/{project}/vulnerabilities/{v['id']}", headers=_h()).json()["quality"]["complete"] is False
    client.patch(f"/projects/{project}/vulnerabilities/{v['id']}",
                 json={"status": "confirmed"}, headers=_h())
    rows = client.get(f"/projects/{project}/vulnerabilities", headers=_h()).json()
    assert rows[0]["quality"]["complete"] is False  # 确认状态不改变证据完备度
