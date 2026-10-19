"""App 渗透测试对话式 AI 接口。

直接调用 LLM API（绕过 dispatcher/worker 路径），支持流式输出。
Node.js subprocess 用于绕过部分网关的 TLS/JA3 指纹过滤。
配置实时从 secrets 文件读取，设置页改完即生效，无需重启。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

ANTHROPIC_VERSION = "2023-06-01"
MAX_HISTORY_MESSAGES = 30   # 保留最近 N 条，避免 context 爆炸

# Node.js 流式调用脚本 — 复用 healthcheck 的 fetch 方式绕过 TLS 指纹过滤。
# Security: the request body (which carries the whole chat history) is read
# from **stdin** and the auth token from **process.env**, NOT from argv —
# argv is visible to any process on the machine via `ps`, and a single argv
# argument is capped at 128KB (MAX_ARG_STRLEN) on Linux, so a long session
# pushed through argv would both leak credentials and fail with E2BIG.
_NODE_STREAM_SCRIPT = r"""
const [url] = process.argv.slice(1);
const headers = { 'content-type': 'application/json' };
if (process.env.SHARP_AUTH_TOKEN) headers['authorization'] = `Bearer ${process.env.SHARP_AUTH_TOKEN}`;
if (process.env.ANTHROPIC_VERSION) headers['anthropic-version'] = process.env.ANTHROPIC_VERSION;
let body = '';
process.stdin.setEncoding('utf8');
for await (const chunk of process.stdin) body += chunk;
(async () => {
  let r;
  try { r = await fetch(url, { method: 'POST', headers, body }); }
  catch (e) { process.stderr.write(String(e)); process.exit(1); }
  if (!r.ok) {
    const t = await r.text();
    process.stderr.write(`HTTP_ERROR ${r.status}\n${t}\n`);
    process.exit(1);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    process.stdout.write(dec.decode(value, { stream: true }));
  }
  process.exit(0);
})();
""".strip()


# Secrets 文件 mtime 缓存：避免每条聊天消息都重新读取并解析文件
_secrets_cache: dict[str, object] = {"mtime": None, "vals": {}}


def _read_secrets_cached() -> dict[str, str]:
    """读取 secrets 文件，按 mtime 缓存。文件未变时直接返回缓存结果。"""
    from sharp.secrets import read_secrets_file, resolve_secrets_file
    try:
        path = resolve_secrets_file()
        mtime = path.stat().st_mtime if path.exists() else 0
    except Exception:
        mtime = 0
    if _secrets_cache["mtime"] == mtime and mtime:
        return _secrets_cache["vals"]  # type: ignore[return-value]
    vals = read_secrets_file()
    _secrets_cache["mtime"] = mtime
    _secrets_cache["vals"] = vals
    return vals


def get_llm_config() -> dict[str, str]:
    """从 secrets 文件读取配置，按 mtime 缓存避免重复文件 I/O。设置页保存后立即生效。"""
    def pick(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_vals.get(key, default)

    file_vals = _read_secrets_cached()
    base_url = pick("SHARP_BASE_URL", "https://api.anthropic.com").rstrip("/")
    return {
        "auth_token": pick("SHARP_ANTHROPIC_AUTH_TOKEN"),
        "base_url": base_url,
        "model": pick("SHARP_MODEL", "claude-3-5-sonnet-20241022"),
    }


def build_system_prompt(apk_context: str) -> str:
    return (
        "你是一名专业的 Android 应用安全测试工程师，擅长逆向工程、动态插桩和移动端渗透测试。\n"
        "你的任务是协助用户对以下 APK 进行安全测试，可以生成 Frida 脚本、objection 命令、"
        "脱壳方案、绕过脚本，以及任何渗透测试所需的技术输出。\n"
        "回复时使用中文，代码块用对应语言标注。\n\n"
        "## 目标 APK 静态分析结果\n\n"
        f"{apk_context}"
    )


_TOOL_GUIDE_KNOWLEDGE = (
    "你是一名犀利的工具向导，熟悉 Sharp 的每个功能，用中文简洁、准确地讲解它怎么用。\n"
    "这是权威说明，只依据它回答，不要编造没有提到的功能或路径。术语要讲清，"
    "主动说明用途、操作流程、适用场景、注意事项，以及如何配合其他功能。\n\n"
    "## Sharp 是什么\n"
    "Sharp 是一个基于证据—行动图（Evidence-Action Graph）的自动化渗透测试引擎。它把一次测试"
    "拆成多个项目，每个项目由 AI 驱动地确认证据、提出行动、并在隔离容器里执行，最终产出"
    "报告。核心工作流是：证据 → 行动 → 执行 → 新证据 循环迭代。\n\n"
    "## 核心概念\n"
    "- 项目：一次渗透测试任务，包含目标、证据、行动、线索。\n"
    "- 证据 Fact：测试中确认的事实性结论（如开放端口、服务版本、漏洞特征）。初始有 origin（起点）/goal（验收标准）。\n"
    "- 行动 Intent：基于证据提出的待执行动作，交给 AI worker 去容器里跑。\n"
    "- 线索 Hint：人工给的补充方向，能引导 AI 调整探索方向。\n"
    "- Worker：负责执行任务的 AI 后端（claudecode / codex 等），由调度器分配。\n\n"
    "## 主要功能入口（对应左侧导航）\n"
    "1. 项目列表：查看所有项目，新建项目（填标题/来源/目标/线索），开项目工作台看图 / 行动板 / 证据链三视图，"
    "跟踪进度、派发深度分析、生成工程化报告。\n"
    "2. App 分析：上传或填本机路径分析 APK/XAPK，做浅层静态扫描（框架、域名、接口、密钥、"
    "native 库），可一键派发到 AI 做深度反编译。\n"
    "3. 小程序分析：上传/扫描小程序包（wxapkg）做静态分析，导入 HAR 流量做动态业务分析。\n"
    "4. AI 对话：独立通用对话，选「工具向导」可问 Sharp 怎么用；选「通用助手」做其他问答。"
    "还能把对话里的发现写回项目证据图（写入线索）。\n"
    "5. 调度器：查看 worker/任务状态，重启调度，看实时运行和跳过原因。\n"
    "6. 设置：改模型/超时/报告指令，配置 API Token 等秘钥。\n\n"
    "## 常用操作\n"
    "- 新建项目 → 目标填 origin/goal → 保存后调度器自动 bootstrap 并开始探索。\n"
    "- 项目卡在停滞时，加一条新线索会触发 AI 重新评估方向。\n"
    "- App 分析完点「发送到 AI 深度分析（后台）」会新建项目并派发，可在项目列表跟踪。\n"
    "- 报告：项目完成后可生成工程化测试报告，或导出 YAML。\n"
    "注意：目标是渗透测试工具，只可用于你已获得明确书面授权的系统。"
)


def build_generic_system_prompt(role: str = "assistant") -> str:
    """System prompt for a generic AI chat session (no APK). `role` lets the
    caller set the persona; defaults to a neutral assistant which is also the
    base for the tool-guide mode."""
    if role == "tool_guide":
        return _TOOL_GUIDE_KNOWLEDGE
    return (
        "你是一个通用的中文 AI 助手，回答要简洁、准确、直接。\n"
        "如果对方问的是本工具（Sharp）的用法，尽量把功能和操作步骤说清楚。"
    )


async def stream_llm(
    messages: list[dict],
    system_prompt: str,
    config: dict[str, str],
) -> AsyncIterator[str]:
    """异步生成器：流式 yield AI 回复的文本 delta。"""
    payload = json.dumps({
        "model": config["model"],
        "max_tokens": 4096,
        "stream": True,
        "system": system_prompt,
        "messages": messages,
    }, ensure_ascii=False)

    base_url = config["base_url"]
    # 部分网关端点不带 /v1，其他通常带；统一补上
    if not base_url.endswith("/v1"):
        endpoint = f"{base_url}/v1/messages"
    else:
        endpoint = f"{base_url}/messages"

    proc = await asyncio.create_subprocess_exec(
        "node", "-e", _NODE_STREAM_SCRIPT,
        endpoint,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "SHARP_AUTH_TOKEN": config["auth_token"],
            "ANTHROPIC_VERSION": ANTHROPIC_VERSION,
        },
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    # Payload goes over stdin, not argv (see _NODE_STREAM_SCRIPT header).
    proc.stdin.write(payload.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    buffer = b""
    # Idle timeout on each read: an LLM endpoint that goes silent (no data for
    # STREAM_IDLE_TIMEOUT) must not leave this coroutine and the node child
    # process hanging forever. Streaming responses normally emit chunks
    # regularly, so this is an idle bound, not a total-run bound.
    STREAM_IDLE_TIMEOUT = 60.0
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=STREAM_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(f"LLM 流式响应超过 {STREAM_IDLE_TIMEOUT:.0f}s 无数据，已中断")
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # content_block_delta 携带文本
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield text
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    # 检查 stderr 是否有错误
    stderr_data = b""
    if proc.stderr:
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    if proc.returncode != 0 and stderr_data:
        err = stderr_data.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"LLM 调用失败: {err}")


def new_session_id() -> str:
    return f"acs_{uuid.uuid4().hex[:16]}"


def utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()
