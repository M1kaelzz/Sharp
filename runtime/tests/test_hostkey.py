"""主机键规范化：知识库与资产台账共用的唯一口径。

反例集全部取自**线上库里真实出现过的键**（54 个垃圾键中的代表），不是编造的样例。
根因是旧判定 `"." in host and len(host.split(".")[-1]) >= 2` —— ".txt/.php/.py/.js"
都满足了，于是文件名被当成域名，166 行数据里真域名只有 3 个。
"""

from __future__ import annotations

import pytest

from sharp.server.hostkey import (
    PREFIX_DOMAIN,
    PREFIX_HOST,
    PREFIX_IP,
    canonical_key,
    is_plausible_domain,
    key_from_text,
    key_from_sources,
    split_key,
)


# ── 从 URL / 主机的规范化 ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://example.com:8443/path", "domain:example.com"),
    ("http://Example.COM/", "domain:example.com"),
    ("sub.api.example.com", "domain:example.com"),
    ("example.com", "domain:example.com"),
    ("10.0.172.234", "ip:10.0.172.234"),
    ("http://10.0.100.58:8080/x", "ip:10.0.100.58"),
    ("internal-api", "host:internal-api"),
    ("http://internal-api:5000/api", "host:internal-api"),
    ("azurite", "host:azurite"),
])
def test_canonical_key_accepts(raw, expected):
    assert canonical_key(raw) == expected


# 全部来自线上库的真实垃圾键
@pytest.mark.parametrize("junk", [
    "flag.txt", "wordlist2.txt", "rescan.py", "Next.js", "proxy.php", "database.host",
    "rce.py", "T.class", "cm-api.js", "index.php", "results.json", "pr14734.diff",
    "login.php", "err.log", "php.ini", "f1-exploits.md", "admin-credentials.txt",
    "flag1.html", "now.json", "index.cgi", "web.log", "xxx.jvm", "lang.Runtime",
])
def test_canonical_key_rejects_real_world_junk(junk):
    assert canonical_key(junk) is None, f"{junk} 曾被当成域名，必须拒绝"


@pytest.mark.parametrize("apiish", [
    "User.Read", "security.HugeSecurityManager", "process.execSync",
    "LoadImage.image", "webtools.securedLoginId", "constructor.constructor",
])
def test_api_method_names_are_not_hosts(apiish):
    """驼峰大写是干净信号：真主机名在这些结论里一律小写。"""
    assert canonical_key(apiish) is None


@pytest.mark.parametrize("junk", [
    "2886991874", "", "   ", "1", "12:30", "None", "a", "-bad-", "10.0", "1.2.3",
    "/Users/x/app.apk", "本地路径无 host", "localhost",
])
def test_canonical_key_rejects_non_hosts(junk):
    assert canonical_key(junk) is None, f"{junk!r} 不该被当成主机"


def test_partial_versions_do_not_become_domains():
    """`1.2.3:8080` 若不拦尾段纯数字，会算出 domain:2.3 这种无意义键。"""
    assert canonical_key("1.2.3:8080") is None
    assert canonical_key("10.0") is None


# ── 从自由文本提取 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("发现接口 https://api.example.com/v1/login 无鉴权", "domain:example.com"),
    ("后端域名 m.example.com 已确认", "domain:example.com"),
    ("服务监听 10.0.172.234:8080", "ip:10.0.172.234"),
    ("容器内 http://internal-api:5000/debug/config 可访问", "host:internal-api"),
    ("目标 10.0.100.58 已连通", "ip:10.0.100.58"),
])
def test_key_from_text_extracts(text, expected):
    assert key_from_text(text) == expected


@pytest.mark.parametrize("text", [
    "本地路径无 host",
    "执行 sqlmap 扫描完成，未发现注入",
    "凭证为 internal_admin_token_2024",
    "工具产物 rescan.py 已生成",
    "database.host 这个配置项未设置",
    "",
    None,
])
def test_key_from_text_finds_nothing(text):
    assert key_from_text(text) is None


def test_bare_word_is_not_a_host():
    """散文里的普通单词不能被当主机（必须带 scheme/端口才算）。"""
    assert key_from_text("internal-api 服务存在") is None
    assert key_from_text("internal-api:5000 服务存在") == "host:internal-api"


# ── 多来源兜底 ──────────────────────────────────────────────────────────────

def test_key_from_sources_prefers_explicit_host_over_text():
    key = key_from_sources("https://example.com", "无关文本")
    assert key == "domain:example.com"


def test_key_from_sources_falls_back_to_text():
    assert key_from_sources("", None, "host: 10.0.172.234:80") == "ip:10.0.172.234"


def test_key_from_sources_returns_none_when_hopeless():
    assert key_from_sources("本地路径", "无 host") is None


def test_split_key():
    assert split_key("domain:example.com") == ("domain", "example.com")
    assert split_key("ip:10.0.0.1") == ("ip", "10.0.0.1")


def test_is_plausible_domain_only_domains():
    assert is_plausible_domain("example.com")
    assert not is_plausible_domain("10.0.0.1")
    assert not is_plausible_domain("azurite")
    assert not is_plausible_domain("flag.txt")


# ── 资产台账口径：完整主机（不归并根域）────────────────────────────────────

from sharp.server.hostkey import asset_ref, host_from_text  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("目标地址：http://host.docker.internal:8099/\n授权范围：仅本机靶机", "host.docker.internal"),
    ("https://sub.example.com:8443/x", "sub.example.com"),
    ("服务在 10.0.172.234:8080", "10.0.172.234"),
    ("http://internal-api:5000/api", "internal-api"),
])
def test_asset_ref_keeps_full_host(text, expected):
    """资产台账要的是"哪台机器"，所以**不**做根域归并（那是知识键的语义）。"""
    assert asset_ref(text) == expected


@pytest.mark.parametrize("junk", ["本地路径 /Users/x/app.apk", "rescan.py", "", None, "flag.txt"])
def test_asset_ref_rejects_junk(junk):
    assert asset_ref(junk) is None


def test_host_from_text_differs_from_key_from_text():
    """同一段文本：资产口径给完整主机，知识口径给归并后的复用键。"""
    text = "https://sub.example.com/x"
    assert host_from_text(text) == "sub.example.com"
    assert key_from_text(text) == "domain:example.com"


def test_asset_ref_handles_sentence_origin():
    """这正是实测抓到的缺陷：旧实现只认 URL/裸域名，句子 origin 算出空串，
    资产台账与覆盖提示对这些项目静默失效。"""
    from sharp.server.services import extract_web_asset_ref

    sentence = "目标地址：http://host.docker.internal:8099/\n授权范围：仅本机自建靶机"
    assert extract_web_asset_ref(sentence) == "host.docker.internal"
    assert extract_web_asset_ref("http://plain.example.com/a") == "plain.example.com"
    assert extract_web_asset_ref("rescan.py") == ""


# ── 真实事故回归：中文标点 + 匹配顺序 ───────────────────────────────────────

def test_url_followed_by_cjk_punctuation_is_not_swallowed():
    """URL 紧跟中文标点时必须照常解析出来。

    中文行文里 `）`、`，`、`。`、`】` 紧贴 URL 是常态；旧正则把它们吞进主机名 →
    语法校验失败 → 这个 URL 被丢弃。这不是理论问题：本次跑分项目就因此把**资产键**
    算成了 origin 里后面出现的预检 IP，而不是平台域名。
    """
    for text, want in (
        ("平台（https://a.example.com）后面还有字", "a.example.com"),
        ("平台 https://b.example.com，然后继续", "b.example.com"),
        ("平台 https://c.example.com。句子结束", "c.example.com"),
        ("平台【https://d.example.com】结束", "d.example.com"),
        ("平台 https://e.example.com、继续", "e.example.com"),
    ):
        assert host_from_text(text) == want, text


def test_first_host_is_positionally_first_not_pattern_first():
    """**位置最靠前**的主机胜出 —— 而不是"哪个模式先命中"。

    真实 origin 文本（本次 TSec 项目）：平台域名在前、预检 IP 在后。
    旧实现按模式顺序试，靠后的 IP 反而赢了，于是目标键/资产键全错。
    """
    origin = """靶场环境：TSec Benchmark（https://tsecbench.zc.tencent.com）
VPN 已连通并已验证：GET http://10.0.100.58 → {"client_ip":"10.254.0.65","status":"ok"}"""
    assert host_from_text(origin) == "tsecbench.zc.tencent.com"
    assert key_from_text(origin) == "domain:tencent.com"
    from sharp.server.services import extract_web_asset_ref

    assert extract_web_asset_ref(origin) == "tsecbench.zc.tencent.com"


def test_earlier_bare_domain_still_loses_to_later_url():
    """**分档不变**：带 scheme 的 URL 仍优先于散文里的裸域名（置信度不同）。

    位置序只在**同一档内**生效 —— 否则一句"顺便提一下 foo.com"会压过后面真正的目标 URL。
    """
    text = "先记一句 foo.com，真正的目标是 https://bar.example.com/"
    assert host_from_text(text) == "bar.example.com"
