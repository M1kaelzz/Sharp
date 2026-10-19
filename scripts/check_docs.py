#!/usr/bin/env python3
"""文档一致性检查（P2-B）：把"文档与代码不脱节"从口号变成可跑的检查。

**为什么需要**：`docs/ARCHITECTURE.md` §12 写着"文档与代码不脱节"，但那条约定完全靠人记住。
实际后果有两类，都是**肉眼很难发现**的：

1. **归属/编号漂移**：追加新小节时"找末尾，不找归属"——于是出现过 `## 11. 测试与工程` 下面挂着
   `### 10.1`（编号重复且与父节不符）、`10.4` 排在 `10.3` 前面、`§2` 块里 2.9/2.8 插在 2.2 前面。
   人眼看着"有编号、挺整齐"，机器一眼就看出不一致。
2. **声明与代码不一致**：§9.3 的模块树写着每个 `app*.js` 的 `（N 方法）` 与 `（N 行）`，
   而这些数字随每次前端改动漂移 —— 没人会记得同步它们。

检查三类问题：

- **A 编号完整性**：编号重复 / 子节编号与父节不符 / 同一父节内编号乱序
- **B 交叉引用**：`§N.M`（支持 `ARCHITECTURE §9.4` 这种文件限定写法）必须能解析到真实章节
- **C 声明与代码一致**：模块树里的方法数/行数声明 vs 实际文件

退出码非 0 表示有问题（可挂 CI，与 `scripts/check_methods.py` 并列）。

用法::

    python3 scripts/check_docs.py            # 检查并打印报告
    python3 scripts/check_docs.py -v         # 附带每项明细
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATIC = ROOT / "runtime" / "src" / "sharp" / "server" / "static"

# 文档文件名 → 用于识别 "ARCHITECTURE §9.4" 这类文件限定引用
DOC_FILES = {
    "ARCHITECTURE": DOCS / "ARCHITECTURE.md",
    "USAGE": DOCS / "USAGE.md",
    "CHANGELOG": DOCS / "CHANGELOG.md",
}

# 编号规则与 scripts/check_methods.py 的 extract_defined_methods 保持一致 ——
# 两处口径不同会得出"到底几个方法"的两套答案，那比不检查更糟。
_METHOD_RE = re.compile(r"^\s*(?:async\s+)?(\w+)\s*\(([^{}\n]*(?:\{\}[^{}\n]*)*)\)\s*\{")
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "typeof", "void",
    "delete", "new", "do", "else", "try", "finally", "throw", "break", "continue",
    "class", "extends", "super", "import", "export", "default", "from", "as", "static",
    "get", "set", "of", "in", "instanceof", "await", "yield", "this",
}

_HEADING_RE = re.compile(r"^(#{2,4})\s+((?:\d+(?:\.\d+)*)[\.\s、][^\n]*)$", re.M)
# 模块树里的声明：`app.core.js   # 说明（70 方法）` / `（236 行）`
_MODULE_CLAIM_RE = re.compile(r"(app[\w.\-]*\.js|index\.html)\b[^\n(（]*[（(](\d+)\s*(方法|行)[）)]")


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def method_count(js_path: Path) -> int:
    """单个模块的方法数（口径同 check_methods.py）。"""
    seen: set[str] = set()
    for line in _load(js_path).splitlines():
        m = _METHOD_RE.match(line)
        if m and m.group(1) not in _JS_KEYWORDS:
            seen.add(m.group(1))
    return len(seen)


def headings(text: str) -> list[tuple[int, str, str]]:
    """→ [(行号, 级别标记, 编号+标题)]，只取带编号的标题。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{2,4})\s+((?:\d+(?:\.\d+)*)[\.\s、][^\n]*)$", line)
        if m:
            out.append((i, m.group(1), m.group(2).strip()))
    return out


def section_numbers(text: str) -> set[str]:
    nums = set()
    for _, _, title in headings(text):
        m = re.match(r"(\d+(?:\.\d+)*)", title)
        if m:
            nums.add(m.group(1))
    return nums


# ── A 编号完整性 ───────────────────────────────────────────────────────────

def check_numbering(doc_name: str, text: str, verbose: bool) -> list[str]:
    problems: list[str] = []
    hs = headings(text)

    nums = [re.match(r"(\d+(?:\.\d+)*)", t).group(1) for _, _, t in hs]
    for num, count in Counter(nums).items():
        if count > 1:
            lines = [str(ln) for ln, _, t in hs if t.startswith(num + ".") or t.startswith(num + " ")]
            problems.append(f"{doc_name}: 章节号 §{num} 重复 {count} 次（行 {', '.join(lines)}）")

    # 子节编号必须与最近的上层父节同前缀
    parent_stack: list[tuple[str, int]] = []  # (编号, 级别)
    for line_no, level, title in hs:
        num = re.match(r"(\d+(?:\.\d+)*)", title).group(1)
        depth = len(title.split(".")[0])
        parts = level.count("#")
        while parent_stack and parent_stack[-1][1] >= parts:
            parent_stack.pop()
        if parts > 2 and num.count(".") >= 1:
            major = num.split(".")[0]
            enclosing = next((p for p in reversed(parent_stack) if p[1] == 2), None)
            if enclosing and enclosing[0] != major:
                problems.append(
                    f"{doc_name}:{line_no}: 子节 §{num}（{title[:28]}…）挂在 §{enclosing[0]} 之下，"
                    f"编号前缀不符（应为 {enclosing[0]}.x）"
                )
        parent_stack.append((num, parts))
        if verbose and not problems:
            pass

    # 同一父节内编号应递增（CHANGELOG 是"最新置顶"，豁免）
    if doc_name != "CHANGELOG":
        by_parent: dict[str, list[tuple[int, str]]] = defaultdict(list)
        current_major = ""
        for line_no, level, title in hs:
            num = re.match(r"(\d+(?:\.\d+)*)", title).group(1)
            if len(level) == 2:
                current_major = num
                continue
            by_parent[current_major].append((line_no, num))
        for major, items in by_parent.items():
            seq = [n for _, n in items]
            if seq != sorted(seq, key=lambda s: [int(x) for x in s.split(".")]):
                problems.append(
                    f"{doc_name}: §{major} 的子节顺序为 {seq}，不是编号递增"
                    "（文档约定：ARCHITECTURE 是「静态真相」，按编号排序便于查阅）"
                )
    return problems


# ── B 交叉引用 ─────────────────────────────────────────────────────────────

def _qualifier_before(text: str, pos: int, window: int = 48) -> str | None:
    """在 § 之前的一小段文本里找文档名。

    只认"紧邻"不够：CHANGELOG 里常见写法是 `ARCHITECTURE 新增 §2.9`、
    `USAGE §7.3（…）` —— 前者文档名与 § 之间有别的字，紧邻匹配会漏判，
    于是引用被当成"指向 CHANGELOG"而全量误报。
    """
    segment = text[max(0, pos - window):pos]
    found = re.findall(r"(ARCHITECTURE|USAGE|CHANGELOG)", segment)
    return found[-1] if found else None


def check_references(doc_name: str, text: str, known: dict[str, set[str]]) -> list[str]:
    """校验 `§N.M` 能否解析到真实章节。

    归属策略（不同文档性质不同，不能一刀切）：

    - **ARCHITECTURE / USAGE**：是"真相文档"，自身有编号章节 —— 未限定的 `§x` 必须在本文件里存在
    - **CHANGELOG**：是**变更日志**，自身没有编号章节，里面的 `§x` 天然是**指向另两份文档的指针**
      → 先看前面是否写了文件名；没写就要求"在任一文档中存在"（历史上默认指 ARCHITECTURE），
      两边都不在才算悬空引用
    """
    problems: list[str] = []
    pools = {name: nums for name, nums in known.items() if nums}
    for m in re.finditer(r"§\s*(\d+(?:\.\d+)*)", text):
        num = m.group(1)
        qualifier = _qualifier_before(text, m.start())
        if qualifier:
            pool = known.get(qualifier) or set()
            if num not in pool:
                problems.append(f"{doc_name}: 引用 §{num}（指向 {qualifier}）在目标文档中不存在")
            continue
        if doc_name == "CHANGELOG":
            if not any(num in pool for pool in pools.values()):
                problems.append(f"{doc_name}: 引用 §{num} 在 ARCHITECTURE / USAGE 中都不存在")
            continue
        if num not in known.get(doc_name, set()):
            problems.append(f"{doc_name}: 引用 §{num} 在本文件中不存在")
    return problems


# ── C 声明与代码一致 ───────────────────────────────────────────────────────

def check_version_claim(text: str) -> list[str]:
    """§12 规则 4 的另一半：ARCHITECTURE 里声明的"当前 `v=N`"必须与 index.html 实际一致。

    此前只检查了方法数，版本号没人管 —— 实测已经漂到 `v=64`（实际早已 70+）。
    两个数字都不难同步，难的是"记得同步"，所以交给机器。
    """
    m = re.search(r"当前\s*`v=(\d+)`", text)
    if not m:
        return []
    claimed = int(m.group(1))
    index = STATIC / "index.html"
    if not index.exists():
        return []
    found = {int(v) for v in re.findall(r"\?v=(\d+)", _load(index))}
    if not found:
        return []
    actual = max(found)
    if claimed != actual:
        return [f"ARCHITECTURE: 声明前端版本号为 v={claimed}，index.html 实际是 v={actual}"]
    return []


def check_code_claims(text: str) -> list[str]:
    problems: list[str] = []
    for m in _MODULE_CLAIM_RE.finditer(text):
        target, claimed, unit = m.group(1), int(m.group(2)), m.group(3)
        path = STATIC / target if target.startswith(("app", "index")) else None
        if path is None or not path.exists():
            continue
        if unit == "方法":
            actual = method_count(path)
        else:
            actual = len(_load(path).splitlines())
        if actual != claimed:
            problems.append(
                f"ARCHITECTURE: 模块树声明 {target} 为 {claimed} {unit}，实际 {actual} {unit}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    texts = {name: _load(path) for name, path in DOC_FILES.items()}
    known = {name: section_numbers(text) for name, text in texts.items()}

    problems: list[str] = []
    for name, text in texts.items():
        problems += check_numbering(name, text, args.verbose)
        problems += check_references(name, text, known)
    problems += check_code_claims(texts["ARCHITECTURE"])
    problems += check_version_claim(texts["ARCHITECTURE"])

    print("=" * 62)
    print("  Sharp 文档一致性检查")
    print("=" * 62)
    for name, text in texts.items():
        heads = headings(text)
        refs = len(re.findall(r"§\s*\d", text))
        print(f"  {name + '.md':<18} 编号章节 {len(heads):>3} 个 | §引用 {refs:>3} 处")
    print()

    if problems:
        print(f"  ✗ 发现 {len(problems)} 处不一致：\n")
        for item in problems:
            print(f"    • {item}")
        print()
        return 1
    print("  ✓ 编号完整、引用可解析、模块树声明与代码一致。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
