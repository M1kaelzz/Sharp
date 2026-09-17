"""交互式 Android 动态调试 —— 每个动态会话绑定一个常驻 worker 容器。

与 android_chat 的无状态 LLM 问答不同，动态会话在容器里跑 claude-code
（自带 bash + adb/frida），逐轮通过 `claude --session-id` / `-r` 续接，
让 AI 能真实连接 root 手机、hook、看输出、改脚本再 hook。

架构位置：本模块在 **server 进程**里，直接用 Docker SDK 管容器（与 dispatcher
进程的 ContainerManager 相互独立、互不干扰）。server 跑 `./sharp web` 时本就有
Docker 访问权，chat 也早已直接 shell 出 node 子进程，所以这里直接用 SDK 一致。

生命周期：会话首条消息时 ensure 一个名为 sharp-android-dyn-<sid> 的容器
（`sleep infinity`），空闲超过 IDLE_TIMEOUT 由 reaper 回收；不开动态会话时零占用。
"""

from __future__ import annotations

import logging
import os
import threading
import time

import docker
from docker.errors import APIError, DockerException, NotFound

LOG = logging.getLogger(__name__)

# 动态会话容器名前缀（与 dispatcher 的 sharp-dispatch-* / sharp-startup-* 区分开）。
DYN_PREFIX = "sharp-android-dyn-"

# 空闲多久后回收容器（秒）。每轮消息刷新 last_used。
IDLE_TIMEOUT_SECONDS = 30 * 60

# claude-code 在容器里的工作目录（镜像里已 git init 好、放了 AGENTS.md/CLAUDE.md）。
WORKSPACE = "/home/kali/workspace"


def _worker_image() -> str:
    """动态会话用哪个镜像。默认与 dispatch.yaml 的 worker 镜像一致。
    可用 SHARP_WORKER_IMAGE 环境变量覆盖（如自建镜像）。"""
    return os.environ.get("SHARP_WORKER_IMAGE", "sharp-worker:latest")


def _adb_server_socket() -> str:
    """宿主 adb server 的 TCP 地址，注入容器供 adb 客户端连真机。
    与 dispatch.yaml 的 ADB_SERVER_SOCKET 同义；缺省用 Docker Desktop 的固定网关。"""
    return os.environ.get("ADB_SERVER_SOCKET", "tcp:host.docker.internal:5037")


def _container_env() -> dict[str, str]:
    """注入容器的环境变量：claude-code 调模型要的 Anthropic 三件套 + adb socket。
    Anthropic 配置实时从 secrets 文件读，和 App 对话/设置页保持一致。"""
    from sharp.server.android_chat import get_llm_config

    cfg = get_llm_config()
    env = {
        "ANTHROPIC_AUTH_TOKEN": cfg.get("auth_token", ""),
        "ANTHROPIC_BASE_URL": cfg.get("base_url", ""),
        "ANTHROPIC_MODEL": cfg.get("model", ""),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "ADB_SERVER_SOCKET": _adb_server_socket(),
    }
    # 不注入空的 ADB_SERVER_SOCKET —— 空值会让容器内 adb 报错。
    if not env["ADB_SERVER_SOCKET"]:
        env.pop("ADB_SERVER_SOCKET")
    return env


class DynamicSessionManager:
    """管理动态会话容器：创建/复用/回收，供 stream 端点 exec claude 进去。

    单例，server 进程内共享。用一把锁保证并发消息不会重复建同名容器。
    """

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # 记录每个容器最后使用时间，供 reaper 判断空闲。
        self._last_used: dict[str, float] = {}
        self._reaper_started = False
        self._reaper_guard = threading.Lock()

    # ── Docker client（惰性连接，Docker 不可用时给清晰报错）─────────────────────
    def _docker(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as exc:
                raise RuntimeError(f"无法连接 Docker：{exc}") from exc
        return self._client

    def container_name(self, session_id: str) -> str:
        return f"{DYN_PREFIX}{session_id.replace('/', '-')}"

    def _lock_for(self, name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock

    def ensure_running(self, session_id: str) -> str:
        """确保会话容器在跑，返回容器名。已在跑则复用；不存在则创建。"""
        name = self.container_name(session_id)
        with self._lock_for(name):
            self._ensure_running_locked(name)
        self._last_used[name] = time.time()
        self._ensure_reaper()
        return name

    def _ensure_running_locked(self, name: str) -> None:
        client = self._docker()
        state = self._state(name)
        if state == "running":
            return
        if state is not None:
            # 存在但没在跑 —— 起它。
            try:
                client.containers.get(name).start()
                return
            except DockerException as exc:
                if self._state(name) == "running":
                    return
                raise RuntimeError(f"启动动态会话容器失败 {name}: {exc}") from exc
        # 不存在 —— 创建。
        LOG.info("creating android dynamic container=%s image=%s", name, _worker_image())
        try:
            client.containers.run(
                _worker_image(),
                ["sleep", "infinity"],
                detach=True,
                name=name,
                environment=_container_env(),
                network_mode="bridge",
                # Docker Desktop 上 host.docker.internal 自动可解析；原生 Linux 需要
                # 显式映射到网关，这里一并加上，两个平台都能连宿主 adb server。
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
        except APIError as exc:
            if self._is_name_conflict(exc):
                # 并发下别的线程刚建好 —— 复用。
                if self._state(name) == "running":
                    return
                try:
                    client.containers.get(name).start()
                    return
                except DockerException:
                    pass
            raise RuntimeError(f"创建动态会话容器失败 {name}: {exc}") from exc
        except DockerException as exc:
            raise RuntimeError(f"创建动态会话容器失败 {name}: {exc}") from exc

    def exec_claude_stream(
        self,
        name: str,
        prompt: str,
        claude_session_id: str,
        *,
        first_turn: bool,
    ):
        """在容器里跑一轮 claude，流式 yield stdout 文本块（同步生成器）。

        首轮用 `claude --session-id <sid>` 建会话，之后用 `claude -r <sid>` 续接，
        这样 AI 记得上一轮连了哪台设备、hook 了什么、上次输出是什么。
        `-p` 无头模式；`--dangerously-skip-permissions` 让它直接跑 adb/frida。

        调用方（SSE 端点）负责把 yield 的文本转成 data 事件推给浏览器，并在结束后
        把完整回复落库。异常以 RuntimeError 抛出，调用方转成 error 事件。
        """
        client = self._docker()
        try:
            container = client.containers.get(name)
        except DockerException as exc:
            raise RuntimeError(f"动态会话容器不可用 {name}: {exc}") from exc

        if first_turn:
            argv = [
                "claude", "--session-id", claude_session_id,
                "--dangerously-skip-permissions", "-p", "--", prompt,
            ]
        else:
            argv = [
                "claude", "-r", claude_session_id,
                "--dangerously-skip-permissions", "-p", "--", prompt,
            ]

        api = container.client.api
        try:
            exec_info = api.exec_create(
                container.id, argv,
                stdout=True, stderr=True, stdin=False, tty=False,
                workdir=WORKSPACE,
            )
            exec_id = exec_info["Id"]
            stream = api.exec_start(exec_id, detach=False, tty=False, stream=True, demux=True)
        except DockerException as exc:
            raise RuntimeError(f"启动 claude 失败: {exc}") from exc

        stderr_tail: list[str] = []
        try:
            for chunk in stream:
                stdout, stderr = self._split_demux(chunk)
                if stdout:
                    yield stdout.decode("utf-8", errors="replace")
                if stderr:
                    # stderr 不直接给用户，留尾巴用于失败诊断。
                    stderr_tail.append(stderr.decode("utf-8", errors="replace"))
                    if len(stderr_tail) > 20:
                        stderr_tail.pop(0)
        except DockerException as exc:
            raise RuntimeError(f"读取 claude 输出失败: {exc}") from exc

        # 检查退出码：非 0 且没有任何 stdout 时，把 stderr 尾巴作为错误抛出。
        try:
            info = api.exec_inspect(exec_id)
            rc = info.get("ExitCode")
        except DockerException:
            rc = None
        if rc not in (0, None) and stderr_tail:
            raise RuntimeError("claude 执行出错：" + "".join(stderr_tail).strip()[-800:])

    @staticmethod
    def _split_demux(chunk) -> tuple[bytes | None, bytes | None]:
        """demux=True 时 docker 每帧是 (stdout, stderr) 元组；兼容非元组回退。"""
        if isinstance(chunk, tuple) and len(chunk) == 2:
            return chunk[0], chunk[1]
        return chunk, None

    def touch(self, name: str) -> None:
        """刷新容器最后使用时间（每轮消息调用），避免使用中被 reaper 回收。"""
        self._last_used[name] = time.time()

    def remove(self, session_id: str) -> None:
        """显式销毁会话容器（关闭动态会话 / New Chat 时调用）。"""
        name = self.container_name(session_id)
        self._remove_by_name(name)

    def _remove_by_name(self, name: str) -> None:
        self._last_used.pop(name, None)
        client = self._docker()
        try:
            client.containers.get(name).remove(force=True)
        except NotFound:
            return
        except DockerException as exc:
            LOG.warning("failed to remove dynamic container=%s error=%s", name, exc)

    def _state(self, name: str) -> str | None:
        client = self._docker()
        try:
            c = client.containers.get(name)
        except NotFound:
            return None
        except DockerException as exc:
            raise RuntimeError(f"inspect 动态会话容器失败 {name}: {exc}") from exc
        try:
            c.reload()
        except DockerException:
            return None
        state = c.attrs.get("State", {}).get("Status")
        return str(state) if state else None

    @staticmethod
    def _is_name_conflict(exc: APIError) -> bool:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        explanation = str(getattr(exc, "explanation", "") or exc)
        return status == 409 or "is already in use" in explanation

    # ── 空闲回收 ────────────────────────────────────────────────────────────────
    def _ensure_reaper(self) -> None:
        with self._reaper_guard:
            if self._reaper_started:
                return
            self._reaper_started = True
        t = threading.Thread(target=self._reaper_loop, name="android-dyn-reaper", daemon=True)
        t.start()

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.time()
            stale = [n for n, ts in list(self._last_used.items())
                     if now - ts > IDLE_TIMEOUT_SECONDS]
            for name in stale:
                LOG.info("reaping idle android dynamic container=%s", name)
                self._remove_by_name(name)

    def reap_all(self) -> int:
        """回收所有动态会话容器（server 关停时调用）。返回回收数量。"""
        client = self._docker()
        try:
            containers = client.containers.list(all=True)
        except DockerException as exc:
            LOG.warning("failed to list dynamic containers for reap error=%s", exc)
            return 0
        count = 0
        for c in containers:
            if c.name.startswith(DYN_PREFIX):
                self._remove_by_name(c.name)
                count += 1
        return count


# server 进程内单例。
_manager: DynamicSessionManager | None = None
_manager_guard = threading.Lock()


def get_manager() -> DynamicSessionManager:
    global _manager
    if _manager is None:
        with _manager_guard:
            if _manager is None:
                _manager = DynamicSessionManager()
    return _manager
