"""Frontend state-integrity guard (batch: settings buttons + vuln charts move;
extended 2026-10-20: nested paths + index.html coverage).

Alpine 模板表达式求值失败有两种模式，都会让 UI 静默损坏：

1. **根标识符未声明**：``x-text`` 渲染为空——设置页「修改密码 / 保存 MCP」
   按钮变空白灰块的根因（pwLoading / mcpSaving 从未声明，历史还踩过
   usageSummary / graphActionMenu）。
2. **嵌套键缺失**（本次扩展纳入守护）：``miniProgram.packages.length``、
   ``android.browser.loaded``、``mcpEditor.form.name`` 这类读取会抛 TypeError /
   ReferenceError。所有视图常驻 DOM（x-show 只藏不求值豁免）→ 每次页面加载
   刷一屏控制台报错；同类问题还会让 JS 侧崩溃（secretsForm.X.trim() 在保存时
   抛异常且发生在 try 块外 → 保存静默失败）。

守护来源：

- declared = app.js ``const obj = {...}`` 字面量（对象值递归解析嵌套键）
  + 各模块 ``Object.assign(obj, {...})``（含节式方法）+ ``obj.x = ...`` 名
  + ``sharp*`` window 助手。解析器对字符串/注释（// 与 /* */）感知——
  代码里 `//` 注释中的花括号/撇号不能让括号匹配错位。
- scanned  = 强求值属性（x-text / x-show / x-model / x-html / :disabled /
  :value / :title），扫描 ``views/*.html`` **与 index.html**（内联模板里
  toast / yamlPreview* 曾漏网）。
- checked  = (a) 根标识符必须已声明；(b) 链 ``a.b[.c...]`` 中，凡 ``a``（及
  逐段解析出的对象字面量）有已知键集，每一段都必须存在于声明键集里。
- whitelisted = x-for 循环变量、单字符根（x/y/svg 坐标与箭头函数参数）、
  JS 关键字/内建（说明理由后加入 KEYWORDS）。

突变门（差分证明）：摘除 ``pwLoading``（根场景）或改名 ``android.browser``
（嵌套场景）都必须让本文件失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "sharp" / "server" / "static"
APP_JS = STATIC / "app.js"

INDENT4 = r"^ {4}"

KEYWORDS = {
    "true", "false", "null", "undefined", "in", "of", "if", "else", "return",
    "new", "typeof", "instanceof", "this", "window", "document", "event",
    "Math", "String", "Number", "Boolean", "JSON", "Date", "Array", "Object",
    "Intl", "console", "Alpine", "fetch", "parseInt", "parseFloat",
    "encodeURIComponent", "decodeURIComponent", "isNaN", "length",
    "toUpperCase", "toLowerCase", "toFixed", "trim", "replace", "split",
    "join", "slice", "splice", "includes", "startsWith", "endsWith",
    "indexOf", "find", "findIndex", "filter", "map", "reduce", "forEach",
    "some", "every", "sort", "concat", "push", "pop", "shift", "unshift",
    "keys", "values", "entries", "max", "min", "abs", "round", "floor",
    "ceil", "pow", "sqrt", "now", "getTime", "padStart", "padEnd",
    "localeCompare", "match", "matchAll", "search", "test", "charAt", "at",
    "$event", "$refs", "$el", "$store", "$data", "$watch", "$dispatch",
    "$nextTick", "$root", "items", "href", "action",
    # Words appearing only inside template-literal labels (not state refs).
    "AI", "APK", "XAPK", "DEX", "CTF", "IDOR", "MCP", "Prompt", "Dispatcher",
    "Worker", "Sharp", "ACTIVE", "STANDBY", "all", "at_home", "a_",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "PI_API_KEY", "PI_BASE_URL", "PI_MODEL",
    "SHARP_MODEL", "SHARP_BASE_URL", "SHARP_ANTHROPIC_AUTH_TOKEN",
    "web", "zip", "wxapkg", "xapk", "scored", "pentest", "miniprogram",
}

STRONG_ATTR = re.compile(
    r'(?:x-text|x-show|x-model(?:\.\w+)?|x-html|:disabled|:value|:title)="([^"]+)"'
)
_KEY = re.compile(r"([A-Za-z_$][\w$]*)\s*([:(])")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── 字符串/注释感知的扫描原语 ──────────────────────────────────────────────

def _skip_string(text: str, i: int, limit: int) -> int:
    """i 指向开引号（' " `）；返回闭合引号之后的位置。"""
    if text[i] == "`":
        return _skip_template(text, i, limit)
    quote = text[i]
    j = i + 1
    while j < limit:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return limit


def _skip_template(text: str, i: int, limit: int) -> int:
    """i 指向模板字面量反引号；正确处理 ${...}（可含嵌套模板，如
    `` `a ${x.map(v => `<b>${v}</b>`)}` ``）。"""
    j = i + 1
    while j < limit:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            return j + 1
        if ch == "$" and j + 1 < limit and text[j + 1] == "{":
            j = _match_pair(text, j + 1) + 1
            continue
        j += 1
    return limit


def _skip_regex(text: str, i: int, limit: int) -> int | None:
    """text[i] == '/'；若是正则字面量则返回其后位置，否则 None（除法/注释除外）。

    用「前一个有效字符」启发式判别：运算符/开括号之后是正则，标识符/闭括号之后
    是除法。不做这一步的话，正则里的引号/反引号（如 /['"`]/ 类字符类）会被误判
    为字符串起点，导致括号匹配跳跃数千字符。
    """
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j >= 0 and (text[j].isalnum() or text[j] in "_$)]}"):
        return None
    k = i + 1
    in_class = False
    while k < limit:
        ch = text[k]
        if ch == "\\":
            k += 2
            continue
        if ch == "\n":
            return None  # 未闭合 → 不当作正则
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return k + 1
        k += 1
    return None


def _advance(text: str, i: int, limit: int) -> int:
    """从 i 跳过一段非结构文本（字符串/注释/正则）；不适用时原样返回 i。"""
    ch = text[i]
    if ch in "'\"`":
        return _skip_string(text, i, limit)
    if ch == "/":
        if i + 1 < limit and text[i + 1] == "/":
            nl = text.find("\n", i)
            return limit if nl == -1 else nl + 1
        if i + 1 < limit and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            return limit if close == -1 else close + 2
        r = _skip_regex(text, i, limit)
        if r is not None:
            return r
    return i


def _skip_junk(text: str, i: int, limit: int) -> int:
    """跳过空白与注释（// 行注释、/* */ 块注释）。"""
    while i < limit:
        ch = text[i]
        if ch in " \t\n\r":
            i += 1
            continue
        if ch == "/" and i + 1 < limit and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = limit if nl == -1 else nl + 1
            continue
        if ch == "/" and i + 1 < limit and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = limit if close == -1 else close + 2
            continue
        break
    return i


def _match_pair(text: str, open_idx: int) -> int:
    """open_idx 指向 { ( [ 之一；返回配对闭合符的索引（字符串/注释/正则感知）。"""
    pairs = {"{": "}", "(": ")", "[": "]"}
    open_ch = text[open_idx]
    close_ch = pairs[open_ch]
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        nxt = _advance(text, i, n)
        if nxt != i:
            i = nxt
            continue
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced pair in declaration")


def _skip_value(text: str, i: int, limit: int) -> int:
    """i 指向非对象字面量的值起点；跳到本层 ',' 之后（或 limit）。"""
    depth = 0
    while i < limit:
        nxt = _advance(text, i, limit)
        if nxt != i:
            i = nxt
            continue
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0:
            return i + 1
        i += 1
    return limit


def _scan_object(text: str, brace: int) -> dict[str, dict | None]:
    """扫描 '{' 起始的对象字面量 → {键: 子字面量树 或 None}。

    None = 标量/数组/调用（无法继续下钻）；节式方法也记 None。
    """
    close = _match_pair(text, brace)
    tree: dict[str, dict | None] = {}
    i = brace + 1
    while i < close:
        i = _skip_junk(text, i, close)
        if i >= close:
            break
        m = _KEY.match(text, i)
        if not m:
            nxt = _advance(text, i, close)
            i = nxt if nxt != i else i + 1
            continue
        name, sep = m.group(1), m.group(2)
        if sep == "(":
            # 节式方法：跳过参数括号 + 函数体
            paren = _match_pair(text, m.end() - 1)
            k = _skip_junk(text, paren + 1, close)
            if k < close and text[k] == "{":
                k = _match_pair(text, k)
            tree.setdefault(name, None)
            i = k + 1
            continue
        j = _skip_junk(text, m.end(), close)
        if j < close and text[j] == "{":
            tree[name] = _scan_object(text, j)
            i = _match_pair(text, j) + 1
        else:
            tree[name] = None
            i = _skip_value(text, j, close)
    return tree


# ── 声明合并 ──────────────────────────────────────────────────────────────

def _declared_tree(app_js_text: str | None = None) -> dict[str, dict | None]:
    """合并全部声明来源 → {名字: 嵌套键树 或 None}。"""
    src = app_js_text if app_js_text is not None else _read(APP_JS)
    start = src.index("const obj = {") + len("const obj = ")
    tree = _scan_object(src, start)
    for f in sorted(STATIC.glob("app.*.js")):
        txt = _read(f)
        for m in re.finditer(r"Object\.assign\(obj,\s*\{", txt):
            for k, v in _scan_object(txt, m.end() - 1).items():
                tree[k] = v
        tree |= {m.group(1): None for m in re.finditer(r"\bobj\.([A-Za-z_$][\w$]*)\s*=", txt)}
    for f in sorted(STATIC.glob("*.js")):
        tree |= {m.group(1): None for m in re.finditer(r"^function (sharp\w+)", _read(f), re.M)}
    return tree


def _declared_names(app_js_text: str | None = None) -> set[str]:
    return set(_declared_tree(app_js_text))


# ── 模板扫描 ──────────────────────────────────────────────────────────────

def _template_chains() -> dict[str, set[str]]:
    """{表达式中的标识符或链: 出现文件集合}。"""
    uses: dict[str, set[str]] = {}
    files = sorted((STATIC / "views").glob("*.html")) + [STATIC / "index.html"]
    for f in files:
        txt = _read(f)
        loop_vars: set[str] = set()
        for m in re.finditer(r'x-for="(?:let\s+)?(?:\(\s*([^)]+)\)|([A-Za-z_$][\w$]*))', txt):
            for part in (m.group(1) or m.group(2) or "").split(","):
                name = part.split(":")[-1].strip()
                if name:
                    loop_vars.add(name)
        for m in STRONG_ATTR.finditer(txt):
            expr = m.group(1)
            # Strip quoted/template-literal CONTENT first: words inside label
            # strings are not state refs.
            code_only = re.sub(r"'[^']*'", "", expr)
            code_only = re.sub(r'"[^"]*"', "", code_only)
            code_only = re.sub(r"`[^`]*`", "", code_only)
            for chain in re.findall(
                r"(?<![.\w$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)", code_only
            ):
                parts = chain.split(".")
                if parts[0] in loop_vars or parts[0] in KEYWORDS:
                    continue
                if len(chain) <= 1:  # single letters from x/y/svg coords noise
                    continue
                uses.setdefault(chain, set()).add(f.name)
    return uses


def _scan_problems(tree: dict[str, dict | None]) -> list[str]:
    problems = []
    for chain, files in sorted(_template_chains().items()):
        parts = chain.split(".")
        root = parts[0]
        if root in KEYWORDS or len(root) <= 1:
            continue
        if root not in tree:
            problems.append(f"{chain} (used in {sorted(files)}) — 根未声明")
            continue
        cur = tree[root]
        walked = root
        for seg in parts[1:]:
            walked += f".{seg}"
            if cur is None:
                break  # 标量/数组/方法返回值：无法静态继续校验
            if seg not in cur:
                problems.append(
                    f"{chain} (used in {sorted(files)}) — 嵌套键 {walked} 未声明"
                )
                break
            cur = cur[seg]
    return problems


# ── 测试 ──────────────────────────────────────────────────────────────────

def test_settings_button_states_declared():
    """The three historical blanks must stay declared (regression pin)."""
    src = _read(APP_JS)
    for field in ("pwLoading", "pwError", "mcpSaving"):
        assert re.search(INDENT4 + re.escape(field) + r"\s*:", src, re.M), (
            f"app.js must declare {field} — templates x-text it; "
            "undeclared = blank button text (the settings-page bug)"
        )


def test_no_undeclared_state_in_templates():
    """全量扫描（含 index.html 与嵌套键）：不允许任何未声明引用。"""
    problems = _scan_problems(_declared_tree())
    assert not problems, (
        "模板引用了未声明（或嵌套键缺失）的状态——x-text 求值会失败、按钮/文本渲染为"
        "空白，嵌套缺失还会每次加载刷控制台报错、JS 侧 .trim() 之类直接抛异常。"
        "补声明，或在 KEYWORDS 白名单说明理由：\n  - " + "\n  - ".join(problems)
    )


def test_vuln_charts_live_in_vulns_view_not_dashboard():
    """The severity donut + trend block moved HOME to the vulns module; the
    dashboard keeps only the stat cards (density decision: one chart band per
    page — cost band on dashboard, vuln charts in the vulns module)."""
    vulns = _read(STATIC / "views" / "vulns.html")
    dash = _read(STATIC / "views" / "dashboard.html")
    assert "漏洞严重度分布" in vulns and "vulnDonutSvg()" in vulns
    assert "漏洞发现趋势" in vulns and "vulnTrendSvg()" in vulns
    assert "漏洞严重度分布" not in dash, "chart block must not stay duplicated on dashboard"
    assert "vulnDonutSvg()" not in dash
    assert "确认漏洞" in dash  # overview stat card stays
    assert "待验证" in vulns   # status strip is vulns-module-only


def test_dashboard_usage_summary_reads_are_null_safe():
    """usageSummary 是唯一以 null 为初值的状态根，且成本带用 x-show（子节点
    恒求值）——所有读取必须走 ?. 或短路，否则每次加载报 TypeError。"""
    dash = _read(STATIC / "views" / "dashboard.html")
    problems = []
    for m in STRONG_ATTR.finditer(dash):
        expr = m.group(1)
        if "usageSummary" not in expr:
            continue
        bare = re.search(r"(?<!\?)usageSummary\.", expr)
        if bare and not re.search(r"usageSummary\s*(&&|\|\|)", expr):
            problems.append(expr)
    assert not problems, (
        "成本带内的 usageSummary 读取必须用 ?.（或在 x-show 里短路）；"
        "裸读会在 usageSummary=null 时抛 TypeError：\n  - " + "\n  - ".join(problems)
    )


def test_mutant_removing_pwloading_declaration_fails():
    """Guard self-check (root case): strip pwLoading from an app.js snapshot →
    the integrity checks must fail (differential proof, no runtime patching)."""
    src = _read(APP_JS)
    mutated = re.sub(INDENT4 + r"pwLoading: false,.*\n", "", src, flags=re.M)
    assert mutated != src, "mutation did not apply"

    with pytest.raises(AssertionError):
        for field in ("pwLoading", "pwError", "mcpSaving"):
            assert re.search(
                INDENT4 + re.escape(field) + r"\s*:", mutated, re.M
            ), f"app.js must declare {field}"

    problems = _scan_problems(_declared_tree(app_js_text=mutated))
    assert any(p.startswith("pwLoading") for p in problems), (
        "template scan must catch the stripped pwLoading declaration"
    )


def test_mutant_renaming_android_browser_fails():
    """Guard self-check (nested case): rename the ``browser`` key inside the
    android declaration → template refs like android.browser.loaded must be
    flagged. 差分证明嵌套校验真实生效。"""
    src = _read(APP_JS)
    mutated = src.replace("browser: {", "browserRenamed: {", 1)
    assert mutated != src, "mutation did not apply"

    problems = _scan_problems(_declared_tree(app_js_text=mutated))
    assert any(p.startswith("android.browser") for p in problems), (
        "nested scan must catch the renamed android.browser declaration"
    )