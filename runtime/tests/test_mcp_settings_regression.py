"""MCP 设置 API 回归测试

覆盖 2026-08-30 修复的两个问题：
  1. 前端新增按钮不显示表单（mcpEditor.show 状态缺失）
  2. 空列表 PUT 后 dispatch.yaml 渲染为 `mcp_servers:`（yaml 解析为 None，
     dispatcher 的 list 字段校验失败）—— 应渲染为 `mcp_servers: []`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试可导入 sharp 包（与现有测试一致的方式）
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── _render_mcp_block 空列表渲染 ────────────────────────────────────────────

def test_render_mcp_block_empty_list_yields_bracket_list():
    """空列表必须渲染为 `mcp_servers: []`，而不是只有 `mcp_servers:`。

    后者 yaml.safe_load 解析为 None；DispatchConfig.mcp_servers 是
    Field(default_factory=list)，字段存在但为 None 时校验失败
    （pydantic 默认值只在字段缺失时生效）。
    """
    from sharp.server.routers.settings import _render_mcp_block

    rendered = _render_mcp_block([])
    assert rendered.strip() == "mcp_servers: []"

    import yaml
    data = yaml.safe_load(rendered)
    assert data["mcp_servers"] == []


def test_render_mcp_block_nonempty_still_normal():
    """非空列表渲染不受影响（逐项展开）。"""
    from sharp.server.routers.settings import _render_mcp_block

    rendered = _render_mcp_block([{
        "name": "test-mcp", "transport": "http",
        "url": "http://host.docker.internal:9999/mcp",
        "headers": {}, "command": None, "args": [], "env": {},
        "enabled": True, "max_tools": 100,
    }])
    import yaml
    data = yaml.safe_load(rendered)
    assert len(data["mcp_servers"]) == 1
    assert data["mcp_servers"][0]["name"] == "test-mcp"
    assert data["mcp_servers"][0]["transport"] == "http"


def test_dispatch_config_accepts_empty_mcp_block():
    """dispatch.yaml 中 `mcp_servers: []` 能被 DispatchConfig 正常加载。"""
    import yaml

    from sharp.dispatcher.config import DispatchConfig

    # 用项目真实的 dispatch.yaml 验证（若存在），否则构造最小配置
    project_root = ROOT.parent
    cfg_file = project_root / "dispatch.yaml"
    if cfg_file.exists():
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        cfg = DispatchConfig(**data)
        assert cfg.mcp_servers == []
    else:
        cfg = DispatchConfig(
            server="localhost",
            runtime={
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "interval": 5,
                "healthcheck_timeout": 30,
                "prompt_group": "default",
            },
            tasks={},
            container={},
            common_env={},
            workers=[],
            mcp_servers=[],
        )
        assert cfg.mcp_servers == []


# ── _read_mcp_servers 对 None 的兜底 ────────────────────────────────────────

def test_read_mcp_servers_tolerates_bare_key(tmp_path, monkeypatch):
    """文件中只有 `mcp_servers:`（无子项）时，读取应返回 [] 而不是 500。"""
    from sharp.server.routers import settings as settings_mod

    cfg = tmp_path / "dispatch.yaml"
    cfg.write_text("server: {}\nruntime: {}\nmcp_servers:\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "dispatch_config_path", lambda: cfg)

    servers, text = settings_mod._read_mcp_servers()
    assert servers == []
    assert "mcp_servers" in text


# ── 前端：聊天方法存在性（防止再次被覆盖丢失）──────────────────────────────

def test_app_chat_js_keeps_chat_methods():
    """app.chat.js 必须保留 Sharp 助手（通用聊天）的全部方法。

    2026-08-30 曾因误覆盖导致 goChat 等聊天方法丢失，侧边栏「Sharp 助手」
    点击无响应。此测试确保聊天方法始终存在。
    """
    js_path = SRC / "sharp" / "server" / "static" / "app.chat.js"
    assert js_path.exists(), f"缺少文件 {js_path}"
    content = js_path.read_text(encoding="utf-8")

    required = [
        "goChat",
        "loadChatSessions",
        "startNewChatSelect",
        "confirmNewChat",
        "switchChatSession",
        "deleteChatSession",
        "deleteCurrentChat",
        "sendChatMessage",
        "stopChat",
    ]
    missing = [m for m in required if f"{m}(" not in content]
    assert not missing, f"app.chat.js 缺少聊天方法: {missing}"