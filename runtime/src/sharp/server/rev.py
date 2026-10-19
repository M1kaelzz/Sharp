"""进程代码指纹 —— 用来发现"跑着的进程 ≠ 磁盘上的代码"。

**为什么需要它**：本项目已经两次被这个坑咬到。

1. 批次 P1-5：旧 server 实例占着端口 15 小时 44 分，新实例启动失败，但 **dispatcher 起来了**
   → 于是出现"新表已建、新端点 404"的怪现象（表是 dispatcher 迁移建的，server 还是旧代码）。
2. 批次 P0-补：改完 #5 校验后实测 `POST /projects/{id}/complete` 返回 **403** 而不是预期的 **422**，
   一度以为鉴权挡路，实际是 **server 进程 09:09 启动、代码 10:06 才落盘** —— 测的是旧进程。

`GET /` 上那个 `v=70` 是**手写常量**，改代码时不一定跟着动，所以它证明不了任何事。这里的做法是：

- 进程导入本模块时算一次指纹（`CODE_REV`）—— 代表**当前进程实际加载的代码**
- 请求时再算一次磁盘指纹（`disk_rev`）—— 代表**磁盘现状**
- 两者不一致 ⇒ `stale=True`：进程是旧的，改动没生效

指纹取「相对路径 : 内容 SHA-256」的汇总 SHA-256 前 12 位，覆盖 `sharp/` 下所有 `.py`
与 `server/static/` 下的 `.html/.js/.css`（前端改动同样要能发现）。

**为什么用内容哈希而不是 mtime**：mtime 会把"改了又改回来""`git checkout` 同一份内容"
判成变更 —— 那正是这个机制最该避免的假警报（喊狼来了几次就没人信了）。
内容哈希完全没有这个问题，代价是每次要读一遍源文件：实测 121 个文件 / 4.8 MB ≈ **5 ms**，
对只在启动和健康检查时调用的路径完全可接受。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Sequence

# sharp/ 包根目录：覆盖 server 与 dispatcher 两侧的 Python 代码
_PKG_ROOT = Path(__file__).resolve().parent.parent

# 参与指纹的文件类型（默认"全部"= Python + 前端）
_SUFFIXES: frozenset[str] = frozenset({".py", ".html", ".js", ".css"})

# `stale` 的判据**只看 Python**：只有 Python 代码是"进程启动时加载、改了必须重启"的。
# 前端资源（views/*.html、app*.js、样式）由静态服务按请求从磁盘读取（include 走 mtime 缓存），
# **改完即时生效、不需要重启** —— 把它们算进 stale 会制造假警报，而假警报喊几次就没人信了
# （这条原则在本文件里已经写过一次：见"为什么用内容哈希而不是 mtime"）。
_PY_SUFFIXES: frozenset[str] = frozenset({".py"})
_ASSET_SUFFIXES: frozenset[str] = frozenset({".html", ".js", ".css"})

# 这些目录里的东西不是"代码"，且会随运行不断变化
_SKIP_DIR_PARTS: frozenset[str] = frozenset({"__pycache__", ".git", "node_modules", ".venv"})


def compute_rev(
    roots: Sequence[Path] | None = None,
    *,
    suffixes: Iterable[str] | None = None,
) -> str:
    """算一组目录的代码指纹（12 位十六进制）。

    `roots` 默认就是 sharp 包根；测试可传入临时目录。
    """
    root_list = [Path(r) for r in roots] if roots is not None else [_PKG_ROOT]
    wanted = frozenset(suffixes) if suffixes is not None else _SUFFIXES

    entries: list[str] = []
    for root in root_list:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_PARTS for part in path.parts):
                continue
            if path.suffix not in wanted:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue  # 竞态下文件可能刚好消失；指纹算不进去即可
            entries.append(f"{path.relative_to(root)}:{hashlib.sha256(blob).hexdigest()}")

    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return digest[:12]


# 进程启动（模块导入）时的指纹 = 这个进程实际加载的 Python 代码
CODE_REV: str = compute_rev(suffixes=_PY_SUFFIXES)

# 前端资源指纹（信息项，不参与 stale 判定）
ASSETS_REV: str = compute_rev(suffixes=_ASSET_SUFFIXES)


def disk_rev() -> str:
    """磁盘当前的 **Python** 指纹（与 CODE_REV 同口径，可直接比较）。"""
    return compute_rev(suffixes=_PY_SUFFIXES)


def is_stale() -> bool:
    """进程加载的 Python 代码是否已落后于磁盘（即"改了没重启，必须重启"）。"""
    return CODE_REV != disk_rev()


def rev_info() -> dict[str, object]:
    """给 /health 用的完整指纹信息。

    - `code_rev` / `disk_rev` / `stale`：**Python 代码**，`stale=True` 表示必须重启
    - `assets_rev` / `assets_disk_rev` / `assets_changed`：前端资源，**仅信息** ——
      改了会变，但不需要重启（静态服务即时生效）
    """
    current = disk_rev()
    assets_now = compute_rev(suffixes=_ASSET_SUFFIXES)
    return {
        "code_rev": CODE_REV,
        "disk_rev": current,
        "stale": CODE_REV != current,
        "assets_rev": ASSETS_REV,
        "assets_disk_rev": assets_now,
        "assets_changed": ASSETS_REV != assets_now,
    }
