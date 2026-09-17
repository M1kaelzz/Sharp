#!/usr/bin/env python3
"""
前端方法完整性检查脚本

扫描 index.html 中所有 Alpine 事件绑定(@click, @mouseenter, x-html, :class 等)
调用的方法名，与所有 app.*.js 模块中定义的方法名做交叉对比，
找出"模板引用了但 JS 中未定义"的方法——这类缺失会导致 Alpine 静默崩溃、整页不渲染。

用法：
    python scripts/check_methods.py              # 项目根目录运行
    python scripts/check_methods.py --verbose     # 显示详细匹配信息

退出码：
    0 — 无缺失方法
    1 — 发现缺失方法（CI 中可据此阻断）
"""

import re
import sys
import os
from pathlib import Path
from collections import defaultdict

# ── 配置 ──────────────────────────────────────────────────────────────────
STATIC_DIR = Path("runtime/src/sharp/server/static")
INDEX_HTML = STATIC_DIR / "index.html"
VIEWS_DIR = STATIC_DIR / "views"


def html_files() -> list[Path]:
    """所有含 Alpine 绑定的 HTML：骨架 index.html + 拆出去的视图片段。

    P2 把 index.html 里的 11 个视图块拆到 views/ 后，只扫骨架会**漏掉整块视图**
    的绑定（假阴性），所以这里必须一并扫描。
    """
    files = [INDEX_HTML]
    if VIEWS_DIR.is_dir():
        files += sorted(VIEWS_DIR.glob("*.html"))
    return files
JS_GLOB = "app*.js"

# Alpine 事件指令前缀，扫描这些属性中调用的方法名
ALPINE_DIRECTIVES = [
    r"@click", r"@dblclick", r"@mouseenter", r"@mouseleave", r"@mouseover",
    r"@mousedown", r"@mouseup", r"@mousemove", r"@focus", r"@blur",
    r"@keydown", r"@keyup", r"@keypress", r"@submit", r"@change",
    r"@input", r"@contextmenu", r"@wheel",
    r"x-on:\w+",
]
# 属性绑定中也可能调用方法（:class="foo()", x-text="bar()", x-html="baz()" 等）
ALPINE_BINDINGS = [r"x-html", r"x-text", r"x-show", r"x-model", r":class", r":style",
                   r":disabled", r":value", r":src", r":href", r":title", r":selected",
                   r":readonly", r":checked", r":multiple"]

# 浏览器原生 / Alpine 内置，不算"缺失"
BUILTINS = {
    # JS 内置
    "confirm", "alert", "prompt", "parseInt", "parseFloat", "isNaN", "isFinite",
    "JSON", "String", "Number", "Boolean", "Array", "Object", "Math", "Date",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "console", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    # Alpine 内置魔法属性
    "$el", "$refs", "$event", "$store", "$watch", "$dispatch", "$nextTick",
    "$data", "$root", "$id",
    # 常见 HTML 属性赋值中的变量（非方法调用）
    "true", "false", "null", "undefined",
}

# JS Array/String/Object 原型方法——在 Alpine 表达式中通过 .method() 调用
# 这些不是自定义方法，不应报为缺失
PROTOTYPE_METHODS = {
    "filter", "find", "findIndex", "includes", "join", "push", "reduce",
    "slice", "some", "splice", "toUpperCase", "toLowerCase", "trim",
    "map", "forEach", "sort", "reverse", "concat", "indexOf", "lastIndexOf",
    "split", "replace", "replaceAll", "match", "search", "startsWith",
    "endsWith", "substring", "substr", "charAt", "charCodeAt", "padStart",
    "padEnd", "repeat", "keys", "values", "entries", "assign", "freeze",
    "from", "isArray", "of", "flat", "flatMap", "fill", "at", "pop", "shift",
    "unshift", "toString", "valueOf", "toLocaleString", "toFixed",
    "toPrecision", "toExponential", "hasOwnProperty", "isPrototypeOf",
    "propertyIsEnumerable", "constructor",
}


def extract_method_calls_from_html(html: str, html_name: str = "index.html") -> dict[str, list[str]]:
    """从 HTML 中提取所有 Alpine 指令里调用的方法名。

    返回 {method_name: [出现位置描述, ...]}
    """
    calls: dict[str, list[str]] = defaultdict(list)

    # 构建匹配所有 Alpine 属性值的正则
    # Alpine allows modifier chains after the directive/base, e.g.
    #   @click.self="fn()"   @keydown.escape.window="fn()"
    #   @submit.prevent="fn()"   x-model.debounce.500ms="field"
    # A bare "@click=" pattern would silently miss every modifier-bound call
    # (false negative: a missing method would go unreported). The modifier
    # segment accepts word chars, digits (".500ms"), ":" and "-".
    attr_patterns = []
    for prefix in ALPINE_DIRECTIVES + ALPINE_BINDINGS:
        attr_patterns.append(rf'(?:{prefix})(?:\.[\w:-]+)*="([^"]*)"')

    combined = re.compile("|".join(attr_patterns))

    # 同时记录行号
    for line_no, line in enumerate(html.splitlines(), 1):
        for m in combined.finditer(line):
            # 多个捕获组中找到实际匹配的那个（非 None）
            expr = None
            for g in m.groups():
                if g is not None:
                    expr = g
                    break
            if expr is None:
                continue
            # 从表达式中提取方法调用：identifier followed by (
            # 只匹配独立函数调用（不以 . 开头），排除原型方法
            # (?<![$\w.]) 确保 .filter() 不会被匹配
            for call_match in re.finditer(r"(?<![$\w.])([a-zA-Z_]\w*)\s*\(", expr):
                name = call_match.group(1)
                if name in BUILTINS or name in PROTOTYPE_METHODS:
                    continue
                # 排除 JS 关键字（if/for/while 等在三元表达式中可能出现）
                if name in {"if", "for", "while", "switch", "catch", "return",
                            "function", "typeof", "void", "delete", "new",
                            "do", "else", "try", "finally", "throw", "break",
                            "continue", "class", "extends", "super", "import",
                            "export", "default", "from", "as", "static",
                            "get", "set", "of", "in", "instanceof", "await",
                            "yield"}:
                    continue
                calls[name].append(f"{html_name}:{line_no}")

    return calls


def extract_defined_methods(js_files: list[Path]) -> set[str]:
    """从所有 JS 模块中提取已定义的方法名。

    使用逐行扫描策略，匹配对象方法简写：
      methodName() {          — 对象方法
      async methodName() {    — 异步方法
      methodName(arg) {       — 带参数方法

    逐行扫描可避免多行贪婪匹配问题（如 Object.assign(obj, { ... })）。
    """
    defined = set()
    # 匹配行首（可选缩进 + 可选 async）方法名(参数) {
    # 参数部分允许 {} （默认参数如 options = {}），但不允许换行
    method_re = re.compile(
        r"^\s*(?:async\s+)?(\w+)\s*\(([^{}\n]*(?:\{\}[^{}\n]*)*)\)\s*\{"
    )

    JS_KEYWORDS = {
        "if", "for", "while", "switch", "catch", "return",
        "function", "typeof", "void", "delete", "new",
        "do", "else", "try", "finally", "throw", "break",
        "continue", "class", "extends", "super", "import",
        "export", "default", "from", "as", "static",
        "get", "set", "of", "in", "instanceof", "await",
        "yield", "this",
    }

    for jsf in js_files:
        for line in jsf.read_text().splitlines():
            m = method_re.match(line)
            if m:
                name = m.group(1)
                if name not in JS_KEYWORDS:
                    defined.add(name)

    return defined


def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    html_paths = html_files()
    if not INDEX_HTML.exists():
        print(f"错误：找不到 {INDEX_HTML}")
        print("请确保在项目根目录下运行此脚本。")
        sys.exit(2)

    js_files = sorted(STATIC_DIR.glob(JS_GLOB))
    if not js_files:
        print(f"错误：在 {STATIC_DIR} 下未找到 {JS_GLOB} 文件")
        sys.exit(2)

    html_calls: dict[str, list[str]] = {}
    for path in html_paths:
        rel = path.relative_to(STATIC_DIR).as_posix()
        for name, refs in extract_method_calls_from_html(path.read_text(), rel).items():
            html_calls.setdefault(name, []).extend(refs)
    defined = extract_defined_methods(js_files)

    # 找出 HTML 中调用了但 JS 中未定义的方法
    missing = sorted(
        name for name in html_calls
        if name not in defined
        and name not in BUILTINS
        # 排除明显不是方法名的（纯状态变量在 :class/x-show 中被引用）
        # 但保留可能是方法的——宁可误报不可漏报
    )

    # 过滤：有些"方法名"实际是状态变量在三元中被当函数调用（误匹配）
    # 检查是否在 JS 中以属性形式存在（stateVar: value）
    state_vars = set()
    for jsf in js_files:
        content = jsf.read_text()
        for m in re.finditer(r"(\w+)\s*:\s*[^{]", content):
            state_vars.add(m.group(1))

    # 最终缺失列表：排除实际是状态变量的
    true_missing = [m for m in missing if m not in state_vars]

    # ── 输出报告 ──────────────────────────────────────────────────────
    print("=" * 60)
    print("  Sharp 前端方法完整性检查")
    print("=" * 60)
    print()
    print(f"  HTML 文件:  {len(html_paths)} 个（index.html + {max(0, len(html_paths) - 1)} 个视图片段）")
    print(f"  JS  模块:   {len(js_files)} 个")
    print(f"  已定义方法: {len(defined)} 个")
    print(f"  HTML 调用:  {len(html_calls)} 个不同的名称")
    print()

    if js_files:
        print("  JS 模块清单:")
        for jsf in js_files:
            print(f"    • {jsf.name}")
    print()

    if not true_missing:
        print("  ✓ 全部通过，无缺失方法。")
        print()
        sys.exit(0)
    else:
        print(f"  ✗ 发现 {len(true_missing)} 个缺失方法：")
        print()
        for name in true_missing:
            locations = html_calls[name]
            loc_str = ", ".join(locations[:5])
            if len(locations) > 5:
                loc_str += f" ... (+{len(locations) - 5} 处)"
            print(f"    ❌ {name}()")
            print(f"       引用位置: {loc_str}")
            print()
        print("  这些方法在 index.html 中被调用但未在任何 JS 模块中定义。")
        print("  Alpine.js 会静默崩溃，导致整页不渲染。")
        print("  请在对应的 app.*.js 模块中补齐方法定义。")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
