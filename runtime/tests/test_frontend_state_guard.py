"""Frontend state-integrity guard (batch: settings buttons + vuln charts move).

Alpine templates evaluate expressions against the root component returned by
``sharpApp()``. A state field used in a template but never DECLARED in
``app.js`` (nor mounted by an ``app.*.js`` module via Object.assign(obj, ...))
fails at expression evaluation: ``:disabled`` may survive via short-circuit,
but ``x-text`` renders EMPTY — the exact bug that made the 修改密码 / 保存 MCP
buttons blank gray blocks (pwLoading / mcpSaving were never declared).

This guard statically cross-checks every view template against the declared
state, so a future "template references undeclared state" fails CI instead of
shipping an invisible button:

- declared = app.js top-level obj field/method names + module Object.assign
  member names + obj.xxx assignments + sharp* window helpers.
- scanned  = identifiers in STRONG-evaluation attrs only (x-text / x-show /
  x-model / x-html / :disabled / :value / :title) — :class/:style literals
  (tailwind word noise) are deliberately not covered.
- whitelisted = x-for loop vars, JS keywords/globals/builtins.

Mutant gate (differential): removing pwLoading from app.js must fail here.
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
    "localeCompare", "match", "matchAll", "search", "test", "charAt",
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _declared_names(app_js_text: str | None = None) -> set[str]:
    app_js = app_js_text if app_js_text is not None else _read(APP_JS)
    declared: set[str] = set()
    obj_start = app_js.index("const obj = {")
    obj_end = app_js.index("\n  };", obj_start)
    body = app_js[obj_start:obj_end]
    declared |= set(re.findall(INDENT4 + r"([A-Za-z_$][\w$]*)\s*:", body, re.M))
    declared |= set(
        re.findall(INDENT4 + r"(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", body, re.M)
    )
    for f in sorted(STATIC.glob("app.*.js")):
        txt = _read(f)
        for assign in re.finditer(r"Object\.assign\(obj,\s*\{", txt):
            depth = 1
            i = assign.end()
            while i < len(txt) and depth:
                if txt[i] == "{":
                    depth += 1
                elif txt[i] == "}":
                    depth -= 1
                i += 1
            module_body = txt[assign.end(): i - 1]
            declared |= set(
                re.findall(
                    INDENT4 + r"(?:async\s+)?([A-Za-z_$][\w$]*)\s*[(:]",
                    module_body, re.M,
                )
            )
        declared |= set(re.findall(r"\bobj\.([A-Za-z_$][\w$]*)\s*=", txt))
    for f in sorted(STATIC.glob("*.js")):
        declared |= set(re.findall(r"^function (sharp\w+)", _read(f), re.M))
    return declared


def _template_identifiers() -> dict[str, set[str]]:
    uses: dict[str, set[str]] = {}
    attr = re.compile(
        r'(?:x-text|x-show|x-model(?:\.\w+)?|x-html|:disabled|:value|:title)="([^"]+)"'
    )
    for f in sorted((STATIC / "views").glob("*.html")):
        txt = _read(f)
        loop_vars: set[str] = set()
        for m in re.finditer(r'x-for="(?:let\s+)?(?:\(\s*([^)]+)\)|([A-Za-z_$][\w$]*))', txt):
            for part in (m.group(1) or m.group(2) or "").split(","):
                name = part.split(":")[-1].strip()
                if name:
                    loop_vars.add(name)
        for m in attr.finditer(txt):
            expr = m.group(1)
            # Strip quoted/template-literal CONTENT first: 'tool_guide' and
            # `text ${label}` contribute label-string words, not state refs.
            code_only = re.sub(r"'[^']*'", "", expr)
            code_only = re.sub(r'"[^"]*"', "", code_only)
            code_only = re.sub(r"`[^`]*`", "", code_only)
            for name in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)", code_only):
                if name in loop_vars or name in KEYWORDS:
                    continue
                if len(name) <= 1:  # single letters from x/y/svg coords noise
                    continue
                uses.setdefault(name, set()).add(f.name)
    return uses


def test_settings_button_states_declared():
    """The three historical blanks must stay declared (regression pin)."""
    src = _read(APP_JS)
    for field in ("pwLoading", "pwError", "mcpSaving"):
        assert re.search(INDENT4 + re.escape(field) + r"\s*:", src, re.M), (
            f"app.js must declare {field} — templates x-text it; "
            "undeclared = blank button text (the settings-page bug)"
        )


def test_no_undeclared_state_in_templates():
    declared = _declared_names()
    uses = _template_identifiers()
    problems = []
    for name, files in sorted(uses.items()):
        if name not in declared:
            problems.append(f"{name} (used in {sorted(files)})")
    assert not problems, (
        "模板引用了未在 app.js / 模块挂载中声明的状态——x-text 求值会失败、按钮/文本"
        "渲染为空白（设置页按钮空白 bug 的根因）。补声明，或在 KEYWORDS 白名单说明"
        "理由：\n  - " + "\n  - ".join(problems)
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


def test_mutant_removing_pwloading_declaration_fails():
    """Guard self-check: strip pwLoading from an app.js snapshot → the
    integrity checks must fail (differential proof, no runtime patching)."""
    src = _read(APP_JS)
    mutated = re.sub(INDENT4 + r"pwLoading: false,.*\n", "", src, flags=re.M)
    assert mutated != src, "mutation did not apply"

    # pin check must fail on the mutated snapshot
    with pytest.raises(AssertionError):
        for field in ("pwLoading", "pwError", "mcpSaving"):
            assert re.search(
                INDENT4 + re.escape(field) + r"\s*:", mutated, re.M
            ), f"app.js must declare {field}"

    # and the template scan must flag pwLoading as undeclared now
    declared = _declared_names(app_js_text=mutated)
    uses = _template_identifiers()
    missing = [n for n in uses if n not in declared and n not in KEYWORDS]
    assert "pwLoading" in missing, "template scan must catch the stripped declaration"