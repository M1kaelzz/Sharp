"""跨项目知识复用（P0-A′/C/D）。

**为什么这一批是重做的而不是新加的**：旧实现"看起来"完整 —— 有写入端、有读取端
（`inject_knowledge_hints` 在建项时把知识转成 hint）、有测试。但实测 proj_002：

1. origin 事实是一句**自然语言**（`目标地址：https://…example.com/…\\n授权范围：仅主域`），
   旧解析器只认"以 scheme 开头的 URL / 裸域名" → 键为 `None` → 注入函数立刻 return []
   → **知识注入一次都没执行过**；
2. 写入端的键是垃圾（`flag.txt`、`Next.js`、`scan.py`…54 个键里真域名 3 个）→ 即使执行也匹配不上；
3. 只按 root_domain 一个维度匹配 → "Peplink 上的经验"永远到不了另一个 Peplink 目标；
4. 只在建项时注入一次 → 项目跑到一半新积累的知识对本项目不可见。

这个文件把这四条各自钉住。
"""

from __future__ import annotations

import sqlite3

import pytest

from sharp.server import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as c:
        yield c


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sharp.server.app import app

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", "server-token-xyz")
    db.configure(tmp_path / "sharp.db")
    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer server-token-xyz"}


def _project(client, title: str, origin: str) -> str:
    resp = client.post(
        "/projects",
        json={"title": title, "origin": origin, "goal": "g"},
        headers=_h(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _extract(client, project_id: str, description: str):
    """走真实写入路径：服务端从项目的 origin 事实解析键，再抽取知识。"""
    resp = client.post(
        "/knowledge/extract",
        json={"project_id": project_id, "description": description},
        headers=_h(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── A1：真实 origin 是句子 ──────────────────────────────────────────────────

def test_sentence_origin_produces_a_key():
    from sharp.server.routers.knowledge import _extract_root_domain

    sentence = (
        "目标地址：https://app.fh.example.com/cgi-bin/MANGA/index.cgi\n"
        "授权范围：仅主域"
    )
    assert _extract_root_domain(sentence) == "domain:example.com"


def test_ip_origin_does_not_become_a_fake_domain():
    from sharp.server.routers.knowledge import _extract_root_domain

    assert _extract_root_domain("目标：10.0.100.58，仅内网") == "ip:10.0.100.58"


# ── A2：写入端不再产生垃圾键 ────────────────────────────────────────────────

def test_extract_stores_under_canonical_key(client):
    pid = _project(client, "t", "目标地址：https://a.fh.example.com/x\n授权范围：仅主域")
    _extract(client, pid, "发现接口 http://10.0.172.234:8080/admin 可未授权访问")

    # 按**项目 origin 解析出的键**归档（而不是描述里出现的 IP）：下一个目标是
    # 同一个 example.com 部署时，它按 origin 算出的键正是这个，才能匹配上。
    rows = client.get("/knowledge/domain:example.com", headers=_h()).json()
    assert rows, "知识应挂在 origin 解析出的规范键下"
    assert all(r["root_domain"] == "domain:example.com" for r in rows)
    assert any("10.0.172.234:8080" in r["content"] for r in rows), "描述里的接口要作为内容存进来"


def test_unusable_key_stores_nothing(client):
    """归属不明的结论宁可不存 —— 垃圾键比没数据更糟（永远匹配不上还占位置）。"""
    pid = _project(client, "t", "本地路径：/Users/x/app.apk")
    resp = client.post(
        "/knowledge/extract",
        json={"project_id": pid, "description": "生成了 rescan.py 与 flag.txt"},
        headers=_h(),
    )
    assert resp.status_code == 200
    assert resp.json()["extracted_count"] == 0


# ── D：dead_end 一等 kind ──────────────────────────────────────────────────

def test_dead_end_is_its_own_kind(client):
    pid = _project(client, "t", "https://app.example.com")
    resp = client.post(
        "/knowledge/extract",
        json={
            "project_id": pid,
            "description": "https://app.example.com/cgi-bin/MANGA/index.cgi 的注入测试：命令注入与 SSTI 均不适用。",
        },
        headers=_h(),
    )
    assert resp.status_code == 200
    rows = client.get("/knowledge/domain:example.com", headers=_h()).json()
    kinds = {r["kind"] for r in rows}
    assert "dead_end" in kinds, f"否定结论必须单独成类，实际 {kinds}"


def test_positive_result_is_not_marked_dead_end(client):
    pid = _project(client, "t", "https://app.example.com")
    client.post(
        "/knowledge/extract",
        json={"project_id": pid, "description": "https://app.example.com/api/v1/users 返回 200，未授权可读。"},
        headers=_h(),
    )
    rows = client.get("/knowledge/domain:example.com", headers=_h()).json()
    assert rows
    assert all(r["kind"] != "dead_end" for r in rows), "正向结论不能被误标为不通"


# ── C：产品维度（跨产品复用）────────────────────────────────────────────────

def test_product_match_reaches_a_different_target(client):
    """同一个产品换个域名部署 —— 只靠目标键一条都过不来，这正是要修的。"""
    # 刻意用**不同根域**：否则两台设备键相同，命中的是"同目标"分支，测不到跨产品
    pid_a = _project(client, "A", "https://router.acme-corp.example/")
    client.put(f"/projects/{pid_a}/product", json={"product": "Peplink MANGA"}, headers=_h())
    client.post(
        "/knowledge/extract",
        json={
            "project_id": pid_a,
            "description": "https://router.acme-corp.example/cgi-bin/MANGA/index.cgi 的默认口令测试不适用，该型号已移除该路径。",
        },
        headers=_h(),
    )

    # 另一台设备、另一个根域，但同一产品
    pid_b = _project(client, "B", "https://router.other-brand.example/")
    client.put(f"/projects/{pid_b}/product", json={"product": "Peplink MANGA"}, headers=_h())

    data = client.get(f"/projects/{pid_b}/knowledge", headers=_h()).json()
    assert data["key"] == "domain:other-brand.example"
    assert data["same_target"] == [], "根域不同，不该有同目标知识"
    assert data["same_product"], "同产品经验必须能跨根域过来"
    assert data["count"] >= 1
    assert data["product_block"], "同产品经验必须出现在给 worker 的块里"
    assert "不要重复尝试" in data["product_block"]


def test_unlabelled_product_does_not_match_anything(client):
    """空产品名是"未标注"，不是"同一个产品"—— 否则所有未标注行会互相匹配。"""
    pid_a = _project(client, "A", "https://a.example.com/")
    client.post(
        "/knowledge/extract",
        json={"project_id": pid_a, "description": "https://a.example.com/admin 未授权可访问"},
        headers=_h(),
    )
    pid_b = _project(client, "B", "https://totally-different.example.org/")
    data = client.get(f"/projects/{pid_b}/knowledge", headers=_h()).json()
    assert data["count"] == 0
    assert data["product_block"] == ""


def test_same_target_knowledge_is_separated_from_same_product(client):
    pid = _project(client, "A", "https://a.example.com/")
    client.post(
        "/knowledge/extract",
        json={"project_id": pid, "description": "https://a.example.com/api 未授权可读"},
        headers=_h(),
    )
    data = client.get(f"/projects/{pid}/knowledge", headers=_h()).json()
    assert data["same_target"], "同目标知识必须单独一组"
    assert all(r["root_domain"] == "domain:example.com" for r in data["same_target"])


def test_product_is_not_overwritten_by_a_later_guess(client):
    pid = _project(client, "A", "https://a.example.com/")
    client.put(f"/projects/{pid}/product", json={"product": "Peplink MANGA"}, headers=_h())
    resp = client.put(
        f"/projects/{pid}/product", json={"product": "某轮猜测的名字"}, headers=_h()
    )
    assert resp.json()["updated"] is False
    assert resp.json()["product"] == "Peplink MANGA"
    # 人工显式改则允许
    resp = client.put(
        f"/projects/{pid}/product",
        json={"product": "Peplink MANGA v8", "force": True},
        headers=_h(),
    )
    assert resp.json()["updated"] is True


def test_product_length_is_bounded(client):
    pid = _project(client, "A", "https://a.example.com/")
    resp = client.put(f"/projects/{pid}/product", json={"product": "x" * 65}, headers=_h())
    assert resp.status_code == 422


def test_project_meta_exposes_product(client):
    pid = _project(client, "A", "https://a.example.com/")
    client.put(f"/projects/{pid}/product", json={"product": "Peplink MANGA"}, headers=_h())
    meta = client.get(f"/projects/{pid}", headers=_h()).json()["project"]
    assert meta["product"] == "Peplink MANGA"


# ── A3：建项时注入（hint）与运行中读取一致 ──────────────────────────────────

def test_creation_time_hint_injection_works_for_same_target(client):
    pid_a = _project(client, "A", "https://a.example.com/")
    client.post(
        "/knowledge/extract",
        json={"project_id": pid_a, "description": "https://a.example.com/debug 未授权可读"},
        headers=_h(),
    )
    pid_b = _project(client, "B", "https://a.example.com/")
    detail = client.get(f"/projects/{pid_b}", headers=_h()).json()
    joined = str(detail.get("hints") or [])
    assert "知识库" in joined, "建项时应把同目标知识作为 hint 注入"


# ── dispatcher 侧：产品提取与知识块 ─────────────────────────────────────────

def test_extract_product_accepts_both_spellings():
    from sharp.dispatcher.tasks.explore import _extract_product

    assert _extract_product({"data": {"product": "Peplink MANGA"}}) == "Peplink MANGA"
    assert _extract_product({"target_product": "Azurite"}) == "Azurite"
    assert _extract_product({"data": {}}) is None
    assert _extract_product({"data": {"product": "   "}}) is None


def test_extract_product_is_bounded():
    from sharp.dispatcher.tasks.explore import _extract_product

    assert len(_extract_product({"product": "x" * 200})) == 64


def test_knowledge_block_says_when_empty():
    """空知识必须明说，而不是留白 —— 留白会让模型以为被省略了。"""
    from sharp.dispatcher.tasks.reason import _knowledge_block

    class _Client:
        def fetch_knowledge(self, project_id):
            return None

    text = _knowledge_block(_Client(), "p1")
    assert "无历史知识" in text


def test_knowledge_block_marks_same_product_as_lower_confidence():
    from sharp.dispatcher.tasks.reason import _knowledge_block

    class _Client:
        def fetch_knowledge(self, project_id):
            return {
                "key": "domain:b.example",
                "product": "Peplink MANGA",
                "same_target": [],
                "same_product": [{"kind": "dead_end", "title": "t", "content": "c"}],
                "count": 1,
                "block": "",
                "product_block": "已知不通的路径（同产品 Peplink MANGA）—— **不要重复尝试**，除非有新证据：\n  - t: c",
            }

    text = _knowledge_block(_Client(), "p1")
    assert "不要重复尝试" in text
    assert "可信度低于同目标" in text, "跨产品知识必须标注为待复核，不能当既成事实"


def test_reason_prompt_has_knowledge_placeholder():
    from sharp.dispatcher.prompting import load_prompt

    text = load_prompt("default", "reason.md")
    assert "{knowledge}" in text, "reason 提示词必须给知识留位置"
    assert "must not be tried again" in text, "死胡同的禁止语义务必须写进提示词"


@pytest.mark.parametrize("name", ["explore.md", "bootstrap.md"])
def test_output_examples_carry_product_and_env_facts(name):
    """字段必须出现在**输出示例**里。

    #4 的教训：规则写在散文里、示例里没有字段时，模型不会主动输出 ——
    "机制在纸面上通过、在现场为空"。所以这里断言的是示例 JSON 里真的有这两个键。
    """
    import json
    import re

    from sharp.dispatcher.prompting import load_prompt

    text = load_prompt("default", name)
    examples = re.findall(r'\{"accepted": true.*?\}\}', text, re.S)
    assert examples, f"{name} 里找不到输出示例"
    blob = " ".join(examples)
    assert '"product"' in blob, f"{name} 的输出示例缺 product 字段"
    assert '"env_facts"' in blob, f"{name} 的输出示例缺 env_facts 字段"


# ── 注入内容的可读性（活体验证时发现的真实问题）────────────────────────────

def test_fingerprint_is_a_short_value_not_a_paragraph():
    """实测：未加界时指纹标题里塞进了 2000 字符的探测叙述，注入提示词等于投毒。"""
    from sharp.server.routers.knowledge import _bounded_value

    blob = "nginx；(2) 在 web 根与 /cgi-bin/MANGA/ 下探测常见敏感/备份文件均返回 404，无泄露"
    assert _bounded_value(blob) == "nginx"
    assert len(_bounded_value("x" * 500)) == 80
    assert _bounded_value("   ") == ""


def test_injected_block_clips_long_content(client):
    pid = _project(client, "t", "https://app.example.com/")
    # dead_end 条目的 content 是整段描述 —— 正是活体里塞进 2000 字符的那种
    client.post(
        "/knowledge/extract",
        json={
            "project_id": pid,
            "description": "https://app.example.com/api 的注入测试不适用。" + "细节" * 500,
        },
        headers=_h(),
    )
    data = client.get(f"/projects/{pid}/knowledge", headers=_h()).json()
    assert data["block"]
    for line in data["block"].splitlines():
        assert len(line) < 600, "单条知识必须被截断，否则会挤掉提示词预算"
    assert "完整内容见知识库" in data["block"]


def test_new_dead_end_markers_cover_probe_failure_wording():
    from sharp.server.routers.knowledge import _looks_like_dead_end

    assert _looks_like_dead_end("全部 404，无配置文件或备份泄露")
    assert _looks_like_dead_end("该参数对注入均无响应")
    assert not _looks_like_dead_end("/api/v1/users 未授权可读，返回用户列表")


# ── 凭据抽取不落假值（2026-09-16，跑分实测） ──────────────────────────────────

@pytest.mark.parametrize(
    "value",
    [
        "secret=PLACEHOLDER_FLAG1",  # 源码里的 flag 占位符
        "api_key=dk_live_a1b2c3d4e5f6g7h8i9j0",  # 键盘序占位
        "password=0/true/null/",  # 正则粘连产物，根本不是单个值
        "token=CHANGEME",
        "password={{db_password}}",
        "token=<your_token>",
        "password=1panel_password",  # 值本身就是"密钥变量名"
        "token=db_token",
    ],
)
def test_synthetic_credential_values_are_recognized(value):
    from sharp.server.routers.knowledge import _looks_synthetic_credential

    assert _looks_synthetic_credential(value), f"{value} 是合成值，不该进凭据知识"


@pytest.mark.parametrize(
    "value",
    [
        "password=Admin@123",
        "token=internal_admin_token_2024",  # 可疑，但可能就是题目埋的硬编码密钥 → 放过
        "TOKEN=f35c2d92-4524-4ad1-8ffa-fc45498d4906",
        "api_key=dk_live_9f3a2b1c8d7e6f5a",  # 看着假但没有占位特征 → 不误杀
    ],
)
def test_real_looking_credentials_survive(value):
    """宁可留可疑的，也不误杀题目故意埋的硬编码密钥。

    边界画在"明确写着占位/模板变量"上（见上一条参数表），而不是"看起来像假的"。
    """
    from sharp.server.routers.knowledge import _looks_synthetic_credential

    assert not _looks_synthetic_credential(value), f"{value} 可能真能用，不该被过滤"


def test_extract_drops_synthetic_credentials_but_keeps_real_ones(client):
    """端到端：一条引用了源码占位符的结论，只应沉淀出真凭据。"""
    pid = _project(client, "t", "目标地址：https://app.fh.example.com/x")

    _extract(
        client,
        pid,
        "读 /var/www/html/config.php 发现硬编码凭据 password=Sup3rS3cret2024，"
        "同文件另有 api_key=dk_live_a1b2c3d4e5f6g7h8i9j0 与 secret=PLACEHOLDER_FLAG1（均为占位）",
    )

    stored = [
        row["content"]
        for row in client.get("/knowledge", headers=_h()).json()
        if row["kind"] == "credential"
    ]
    joined = "\n".join(stored)

    assert any("Sup3rS3cret2024" in item for item in stored), "真凭据必须留下"
    assert "PLACEHOLDER_FLAG1" not in joined
    assert "a1b2c3d4e5f6" not in joined


def test_password_with_special_chars_is_not_truncated(client):
    """带 `@` 的口令必须整条存下来 —— 截断后的凭据是**错**的，比没有更糟。"""
    pid = _project(client, "t", "目标地址：https://app.fh.example.com/x")

    _extract(client, pid, "后台默认口令为 password=Admin@123，登录后可见管理面板")

    stored = [
        row["content"]
        for row in client.get("/knowledge", headers=_h()).json()
        if row["kind"] == "credential"
    ]

    assert stored == ["password=Admin@123"], stored
