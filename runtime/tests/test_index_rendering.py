"""index.html 拆分后的渲染契约测试（P2）。

`index.html` 从 4918 行单文件拆成「骨架 + static/views/*.html」后，`/` 由服务端
拼回同一个 HTML。这件事的风险全在"拼不回"上——所以测试锁三件事：

1. **等价性**：正常渲染必须包含全部 11 个视图块与 12 个脚本标签；
2. **不残留指令**：`@include` 若漏展开，浏览器会把注释原样显示（页面"少了半个视图"）；
3. **安全与故障可见性**：片段路径不能穿越出 static/；片段缺失要 500 报错，
   而不是静默返回半截页面（白屏是最难排查的失败模式）。
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    from sharp.server import db

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)
    yield c
    c.close()


VIEW_MARKERS = {
    "dashboard": "view === 'dashboard'",
    "vulns": "view === 'vulns'",
    "list": "view === 'list'",
    "graph": "view === 'graph'",
    "reports": "view === 'reports'",
    "approvals": "view === 'approvals'",
    "app-analysis": "view === 'app-analysis'",
    "chat": "view === 'chat'",
    "newproject": "view === 'newproject'",
    "settings": "view === 'settings'",
    "dispatcher": "view === 'dispatcher'",
}


def test_index_renders_without_leftover_include(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "@include" not in r.text, "include 指令未展开 —— 页面会缺整块视图"


@pytest.mark.parametrize("name,marker", sorted(VIEW_MARKERS.items()))
def test_every_view_block_is_present(client, name, marker):
    assert marker in client.get("/").text, f"{name} 视图块没有拼进来"


def test_all_script_tags_present(client):
    html = client.get("/").text
    for module in ("app.core.js", "app.projects.js", "app.graph.js", "app.board.js",
                   "app.timeline.js", "app.replay.js", "app.project-detail.js",
                   "app.intents.js", "app.approvals.js", "app.analyzers.js",
                   "app.chat.js", "app.js"):
        assert f'/static/{module}' in html, f"缺少脚本 {module}"


def test_shell_markers_present(client):
    """骨架职责（侧栏 / 模态框 / Toast / Alpine 根）不能因为拆分而丢。"""
    html = client.get("/").text
    assert 'x-data="sharpApp()"' in html
    assert "id=\"cy\"" in html          # 图谱画布容器
    assert "Toast" in html
    assert html.count("<script") >= 12


def test_rendered_page_is_substantial(client):
    """拆分后仍应是完整页面（防"只拼出骨架"这类低级错误）。"""
    html = client.get("/").text
    assert len(html) > 250_000, f"渲染结果只有 {len(html)} 字符，疑似片段没拼进来"
    assert html.count("\n") > 4000


def test_render_is_cached_until_files_change(client):
    """mtime 缓存：同一状态连续两次渲染返回同一对象内容（不重复读盘）。"""
    from sharp.server import app as app_module

    first = app_module.render_index_html()
    second = app_module.render_index_html()
    assert first == second
    assert app_module._index_cache["html"][1] == first


# ── 安全与故障可见性 ──────────────────────────────────────────────────────────

def _render_with(tmp_path, monkeypatch, skeleton: str):
    from sharp.server import app as app_module

    static = tmp_path / "static"
    (static / "views").mkdir(parents=True)
    (static / "index.html").write_text(skeleton, encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", static)
    monkeypatch.setattr(app_module, "_index_cache", {}, raising=False)
    return app_module.render_index_html


def test_missing_view_raises_instead_of_half_page(tmp_path, monkeypatch):
    from fastapi import HTTPException

    render = _render_with(tmp_path, monkeypatch, "<!-- @include views/nope.html -->")
    with pytest.raises(HTTPException) as exc:
        render()
    assert exc.value.status_code == 500
    assert "missing view include" in exc.value.detail


def test_include_cannot_escape_static_dir(tmp_path, monkeypatch):
    """`@include ../../etc/passwd` 之类的路径必须被拒（目录穿越）。"""
    from fastapi import HTTPException

    outside = tmp_path / "secret.html"
    outside.write_text("SECRET", encoding="utf-8")
    render = _render_with(tmp_path, monkeypatch, "<!-- @include ../secret.html -->")
    with pytest.raises(HTTPException) as exc:
        render()
    assert exc.value.status_code == 500
    assert "invalid view include" in exc.value.detail


def test_include_accepts_nested_view_path(tmp_path, monkeypatch):
    """正常片段能拼进来（include 指令按设计独占一行）。"""
    render = _render_with(tmp_path, monkeypatch, "A\n<!-- @include views/x.html -->\nB")
    (tmp_path / "static" / "views" / "x.html").write_text("MID", encoding="utf-8")
    assert render() == "A\nMID\nB"


def test_include_regex_does_not_eat_blank_line(tmp_path, monkeypatch):
    """回归：正则曾用 `\\s*$` 吞掉后续空行，导致拼接结果与原文差 11 行。"""
    render = _render_with(tmp_path, monkeypatch, "A\n<!-- @include views/x.html -->\n\nB")
    (tmp_path / "static" / "views" / "x.html").write_text("MID\n", encoding="utf-8")
    assert render() == "A\nMID\n\nB"
    assert re.search(r"@include", render()) is None


# ── 侧栏标签行的一致性 ──────────────────────────────────────────────────────

def test_sidebar_tab_counts_hide_at_zero(client):
    """标签栏里每个计数都必须在为 0 时隐藏 —— 否则该标签会与邻居长得不一样。

    真实反馈：`线索 0` 显示在 `详情` 与 `日志` 之间，看起来格式不一致。
    根因不只是"多了一个 0"：侧栏默认宽 320px、6 个标签各 `flex-1`，
    减去 `px-3` 后内容宽约 29px —— `详情`(24px) 放得下，`线索 0`(约 33px) 放不下，
    于是**折成两行**：该标签比邻居高、选中下划线也更低。

    不变量：标签栏里凡是 `x-text` 计数的地方，都必须带 `x-show` 守卫。
    阶段 / 假设 一直是这样，线索 漏了 —— 这条测试把口径钉住。
    """
    html = client.get("/").text
    # 标签行以 `<!-- Detail -->` 注释结束（即详情面板开始处）
    tab_bar = html[html.index("sideTab = 'detail'"): html.index("<!-- Detail -->")]
    assert "sideTab = 'hints'" in tab_bar, "标签栏结构变了，断言需要更新"

    # 找出标签栏内所有计数 span
    counts = re.findall(r'<span[^>]*x-text="[^"]*"[^>]*>', tab_bar)
    counts += re.findall(r'<span[^>]*x-show="[^"]*"[^>]*x-text="[^"]*"[^>]*>', tab_bar)
    assert counts, "标签栏里应能找到计数 span"
    missing = [c for c in counts if "x-show" not in c]
    assert not missing, f"这些计数在 0 时不会隐藏，会让标签与邻居不一致：{missing}"


def test_hints_tab_label_is_plain_text(client):
    """线索标签的文字与 详情 / 日志 同为纯文字，计数只在非空时追加。"""
    html = client.get("/").text
    tab_bar = html[html.index("sideTab = 'detail'"): html.index("<!-- Detail -->")]
    hints_block = tab_bar[tab_bar.index("sideTab = 'hints'"):]
    hints_block = hints_block[:hints_block.index("</button>")]
    assert re.search(r">\s*线索\s*<", hints_block), "线索标签本身应是纯文字"
    assert 'x-show="project.hints.length"' in hints_block


# ── 「规划」标签：阶段 + 假设合并 ───────────────────────────────────────────

def test_sidebar_merges_stages_and_hypotheses_into_plan(client):
    """阶段与假设合并为一个「规划」标签。

    原因不是"少一个更好看"：侧栏默认 320px、标签行每个 `flex-1` 只剩约 29px 内容宽，
    6 个标签时任何带计数的标签都会折成两行（`线索 0` 就是这么暴露的）。
    合并后 5 个标签，且两者语义本就同源 —— 都是"AI 规划器产出的推进结构"。
    """
    html = client.get("/").text
    tab_bar = html[html.index("sideTab = 'detail'"): html.index("<!-- Detail -->")]
    values = re.findall(r"sideTab = '([a-z]+)'", tab_bar)
    assert values == ["detail", "hints", "log", "plan", "live"], values
    assert "stages" not in values and "hypotheses" not in values


def test_plan_panel_contains_both_sections(client):
    """合并后两段内容都要在（不能因为精简标签把功能弄丢）。"""
    html = client.get("/").text
    assert "阶段目标（Sub Goals）" in html
    assert "未验证假设（Hypotheses）" in html
    # 两段共用一个滚动容器：同一 sideTab 值在两个 div 上只出现一次（wrapper）
    plan_panels = re.findall(r'x-show="sideTab === \'plan\'"', html)
    assert len(plan_panels) == 1, f"规划面板应是单个容器，实得 {len(plan_panels)}"


def test_plan_tab_count_guards_zero(client):
    """「规划」的计数同样要在为 0 时隐藏（与线索/阶段/假设同一约定）。"""
    html = client.get("/").text
    tab_bar = html[html.index("sideTab = 'detail'"): html.index("<!-- Detail -->")]
    assert 'x-show="planTabCount()"' in tab_bar


# ── 目标空间详情（P2-A）────────────────────────────────────────────────────

def test_asset_space_entry_and_modal_present(client):
    """资产中心要有"空间详情"入口，以及承载它的弹窗。

    入口只在**选中资产时**出现（`x-show="projectAssetFilter"`）—— 与"清除资产筛选"同一行，
    在需要它的那一刻才出现。
    """
    html = client.get("/").text
    assert 'openAssetSpace()' in html, "缺空间详情入口"
    assert 'x-show="assetSpaceOpen"' in html, "缺空间详情弹窗"
    # 四段内容都要在：概览 / 接口台账 / 历史发现 / 沉淀知识
    for section in ("目标空间", "接口台账（跨项目共享）", "历史发现（跨项目）", "已沉淀知识（目标键维度）"):
        assert section in html, f"弹窗缺「{section}」段"


def test_asset_space_entry_is_gated_on_selection(client):
    """入口不该常驻：没选资产时点了没意义，所以与筛选状态绑定。"""
    html = client.get("/").text
    idx = html.index("openAssetSpace()")
    window = html[max(0, idx - 400):idx]
    assert 'x-show="projectAssetFilter"' in window, "入口应只在选中资产时显示"
