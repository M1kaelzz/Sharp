"""文档一致性（P2-B）：把"文档与代码不脱节"变成每次 pytest 都会跑的事。

`docs/ARCHITECTURE.md` §12 一直写着这条约定，但完全靠人记住。实测漂移有两类，
都是**肉眼很难发现**的：

1. **归属/编号漂移**：追加小节时"找末尾，不找归属" —— 于是出现过
   `## 11. 测试与工程` 下面挂着 `### 10.1`（编号重复且与父节不符）、`10.4` 排在 `10.3` 前、
   §2 块里 `2.9/2.8` 插在 `2.2` 之前。人眼看"有编号、挺整齐"，机器一眼看出不一致。
2. **声明与代码不一致**：§9.3 模块树写着每个 `app*.js` 的 `（N 方法）`/`（N 行）`，
   随每次前端改动漂移 —— 没人会记得同步。首次运行就查出 8 个模块的方法数全过时。

这些断言只在**确实漂移**时失败，失败信息直接给出位置与"应为多少"，修起来是一行的事。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs.py"


def _checker():
    spec = importlib.util.spec_from_file_location("check_docs", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def docs():
    mod = _checker()
    texts = {name: mod._load(path) for name, path in mod.DOC_FILES.items()}
    known = {name: mod.section_numbers(text) for name, text in texts.items()}
    return mod, texts, known


def test_no_documentation_drift(docs):
    """编号完整、§ 引用可解析、模块树声明与代码一致 —— 一处不一致就失败。"""
    mod, texts, known = docs
    problems: list[str] = []
    for name, text in texts.items():
        problems += mod.check_numbering(name, text, verbose=False)
        problems += mod.check_references(name, text, known)
    problems += mod.check_code_claims(texts["ARCHITECTURE"])
    assert not problems, (
        "文档与代码/自身约定不一致（跑 `python3 scripts/check_docs.py` 看完整报告）：\n  - "
        + "\n  - ".join(problems)
    )


def test_module_tree_claims_are_verified_against_real_files(docs):
    """反向确认：检查器**确实**在读真实文件，而不是空转通过。

    做法：故意把某个模块的方法数声明改错，检查器必须报出来。若它不报，
    说明这条护栏是假的（我们不接受"永远绿"的检查）。
    """
    mod, texts, _known = docs
    original = texts["ARCHITECTURE"]
    import re

    m = re.search(r"(├── app\.core\.js\s+# [^\n（]*?)（\d+ 方法", original)
    assert m, "模块树结构变了，断言需要更新"
    broken = original[: m.start()] + f"{m.group(1)}（9999 方法" + original[m.end():]
    problems = mod.check_code_claims(broken)
    assert any("app.core.js" in p for p in problems), "改了声明却检查不出来 → 护栏是假的"
