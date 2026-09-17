"""主机归属规范化 —— 知识库与资产台账共用的唯一口径。

**为什么需要单独一个模块**：此前有两份各写各的启发式：

- `routers/knowledge.py::_is_plausible_domain` —— 判定条件是
  `"." in host and len(host.split(".")[-1]) >= 2`，即"点后面 ≥2 个字符"。
  于是 `.txt / .php / .py / .js / .json / .diff / .class` 全被当成合法 TLD：
  实际库里 54 个键中真域名只有 3 个（`flag.txt ×18`、`wordlist2.txt ×12`、
  `rescan.py ×8`、`Next.js ×7` …）。
- `services.py::_is_plausible_domain` / `extract_web_asset_ref` —— 注释自称
  "Mirrors the knowledge base's host normalization"，其实**并不 mirror**：
  URL 分支放行单标签主机与裸 IP，裸分支却拒绝，于是 `asset_ref` 里出现
  `2886991874` 这种无从解释的值。

两份不一致的口径意味着"同一件事有两套真相"。这里统一成一处。

**设计取舍：为什么不用"真实 TLD 表"**。看起来最"正确"的做法是内嵌一份 TLD 列表，
但列表永远不全，而且大量 ccTLD 与文件扩展名撞车（`.py` 是巴拉圭、`.md` 是摩尔多瓦、
`.sh` 是圣赫勒拿、`.rs`/`.pl`/`.ml`/`.st`/`.so` …）。在渗透结论这种文本里，
`rescan.py` 是**文件**的概率远高于"巴拉圭域名"。所以这里反过来做：

1. **严格语法校验**：每个 label 必须 `[a-z0-9]` 开头结尾、中间可含 `-`，长度 1~63，总长 ≤253；
2. **文件扩展名黑名单**：TLD 命中任何常见代码/文件扩展名 → 判定为文件而不是域名。

精确优先于召回：**宁可某个稀有 ccTLD 目标存不进来，也不要让垃圾键污染匹配** ——
垃圾键的危害不是"多一条无用数据"，而是让真正的复用永远匹配不上。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable

# ── 键的类型前缀 ────────────────────────────────────────────────────────────
# 统一带前缀，避免"这是个 IP 还是个域名"的歧义；也便于匹配时按类型加权。
PREFIX_DOMAIN = "domain"
PREFIX_IP = "ip"
PREFIX_HOST = "host"        # 单标签主机名，如 internal-api / azurite
QUARANTINE = "unattributed"  # 无法归属的历史数据：保留可审计，但永不参与匹配

# ── 文件扩展名黑名单 ────────────────────────────────────────────────────────
# 命中的 TLD 一律判为"这是文件，不是域名"。含大量真实 ccTLD（py/md/sh/rs/pl/ml/st/so…），
# 这是刻意的：在安全结论文本里它们几乎总是文件后缀。
_FILE_LIKE_TLDS: frozenset[str] = frozenset({
    # 代码与脚本
    "py", "pyc", "pyo", "js", "mjs", "cjs", "ts", "tsx", "jsx", "java", "class", "jar",
    "go", "rs", "rb", "php", "phtml", "pl", "pm", "lua", "sh", "bash", "zsh", "ps1",
    "bat", "cmd", "c", "h", "cc", "cpp", "cxx", "hpp", "cs", "swift", "kt", "kts",
    "m", "mm", "scala", "clj", "ex", "exs", "erl", "hs", "jl", "r", "dart", "vue",
    "svelte", "elm", "groovy", "gradle", "cmake", "make", "mk", "am",
    # 配置与数据
    "json", "xml", "yml", "yaml", "toml", "ini", "cfg", "conf", "env", "lock", "properties",
    "csv", "tsv", "sql", "db", "sqlite", "sqlite3", "dump", "bak", "backup", "old", "orig",
    "tmp", "temp", "swp", "swo", "log", "out", "err", "diff", "patch", "rej", "orig",
    # 文档与标记
    "txt", "md", "markdown", "rst", "adoc", "tex", "pdf", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "odt", "rtf", "epub", "csv",
    # 网页与模板
    "html", "htm", "xhtml", "css", "scss", "sass", "less", "styl", "ejs", "hbs", "pug",
    "jade", "twig", "jinja", "j2", "tpl", "mustache", "liquid", "haml", "slim",
    # 二进制与归档
    "exe", "dll", "so", "dylib", "o", "a", "lib", "obj", "bin", "dat", "elf", "out",
    "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "deb", "rpm", "apk", "ipa",
    "dmg", "iso", "img", "jar", "war", "ear", "whl", "egg", "gem", "nupkg",
    # 媒体
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff", "psd",
    "mp3", "mp4", "avi", "mov", "mkv", "wav", "flac", "ogg", "webm", "woff", "woff2",
    "ttf", "eot", "otf",
    # 证书与密钥
    "pem", "crt", "cer", "key", "pub", "p12", "pfx", "jks", "keystore", "csr",
    # 词典/结果等常见产物名（渗透场景高频）
    "txt", "lst", "list", "wordlist", "dict", "rules", "md5", "sha1", "sha256",
    # 早期遗漏、实测被当成域名的两个
    "cgi", "jvm",
})

# 语言/框架标识符的尾段 —— 这些**不是** TLD，但语法上长得像域名尾段。
# 全部来自线上库实测被误判的真实键：`database.host`、`xxx.jvm`、`lang.runtime`、
# `constructor.constructor`、`p.text`、`session.upload`、`LoadImage.image`、`User.Read`。
# 不拦的话它们会稳定地污染匹配（凡是 `X.host` 形状的配置项都会被折成一个假域名）。
_PLACEHOLDER_TLDS: frozenset[str] = frozenset({
    "host", "runtime", "constructor", "text", "upload", "image", "read", "exec",
    "type", "value", "data", "object", "string", "number", "boolean", "function",
    "method", "property", "param", "arg", "field", "option", "config", "setting",
    "handler", "callback", "worker", "thread", "process", "module", "package",
})

# 这些单标签词不是主机名，是扫描/工具产物的噪音
_JUNK_SINGLE_LABELS: frozenset[str] = frozenset({
    "localhost", "true", "false", "none", "null", "nil", "n/a", "na", "todo", "tbd",
    "example", "test", "unknown", "undefined", "nan", "inf",
})

# 常见**真实** TLD。只在"散文里的裸 token"这条低置信路径上要求命中它：
# 带 scheme / 带端口的上下文已经是强证据，不需要这层。
# 目的：拦住 `database.host`、`lang.runtime`、`xxx.jvm`、`p.text` 这类
# "长得像域名但其实不是"的占位符与 API 字段名。
_COMMON_REAL_TLDS: frozenset[str] = frozenset({
    "com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name", "pro",
    "io", "co", "ai", "dev", "app", "cloud", "tech", "online", "site", "website",
    "space", "store", "shop", "blog", "xyz", "top", "club", "live", "life", "world",
    "agency", "solutions", "systems", "services", "software", "network", "digital",
    "group", "team", "company", "center", "plus", "one", "run", "page", "link",
    "me", "tv", "cc", "fm", "am", "gg", "to", "sh", "ly",
    "cn", "com.cn", "net.cn", "org.cn", "gov.cn", "hk", "tw", "jp", "kr", "sg", "my",
    "th", "vn", "ph", "id", "in", "pk", "bd", "lk", "np", "au", "nz", "uk", "co.uk",
    "de", "fr", "nl", "be", "ch", "at", "it", "es", "pt", "se", "no", "dk", "fi",
    "pl", "cz", "sk", "hu", "ro", "bg", "gr", "tr", "ru", "ua", "by", "kz", "il",
    "ae", "sa", "qa", "eg", "za", "ng", "ke", "ma", "br", "ar", "cl", "mx", "pe",
    "ca", "us", "eu",
})

_LABEL_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\Z")

# URL-ish：scheme://host[:port]/...
# 终止字符里**必须**包含中文/全角标点：中文行文里 URL 常紧跟 `）`、`，`、`。`、`】`，
# 不排除它们就会把标点吞进主机名 → 语法校验失败 → 这个 URL 被丢弃，
# 于是**更靠后的 URL 反而胜出**（实测：`平台（https://tsecbench.zc.tencent.com）…` 的
# 平台域名被丢掉，资产键错成了后面出现的预检 IP）。这是真实踩到的坑，不是理论问题。
_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.\-]*://([^\s/?#\"'<>,;\)\]（）【】《》「」『』〈〉，。；：！？、…—～·“”‘’|\\^`]+)"
)
# host:port（裸形式，如 10.0.172.234:8080 / internal-api:5000）
_HOSTPORT_RE = re.compile(r"\b([a-z0-9][a-z0-9.\-]*):(\d{1,5})\b")
# 裸 IPv4（无 scheme 无端口），如 "10.0.172.234 已开放 8080"
_BARE_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# 裸域名（无 scheme），如 api.example.com
_BARE_DOMAIN_RE = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b")


def _strip_host(raw: str) -> str | None:
    """从 URL/host[:port]/path 里取出纯主机名（小写、去端口与路径）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if "://" in text:
        text = text.split("://", 1)[1]
    # 去 userinfo
    if "@" in text.split("/", 1)[0]:
        text = text.split("@", 1)[1]
    # 去路径/查询/片段
    for sep in ("/", "?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    text = text.strip().strip("[]").strip()
    if not text:
        return None
    # 去端口（IPv6 已在上面 strip 掉方括号，这里只处理 host:port）
    if text.count(":") == 1:
        head, _, tail = text.partition(":")
        if tail.isdigit():
            text = head
    return text.lower() or None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_file_like(host: str) -> bool:
    """`rescan.py` / `flag.txt` / `Next.js` —— 是文件，不是主机。"""
    tld = host.rsplit(".", 1)[-1] if "." in host else host
    tld = tld.lower()
    return tld in _FILE_LIKE_TLDS or tld in _PLACEHOLDER_TLDS


def _valid_syntax(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    if host.startswith("-") or host.endswith("-"):
        return False
    labels = host.rstrip(".").split(".")
    if not labels or any(not _LABEL_RE.match(lbl) for lbl in labels):
        return False
    # 单标签、纯数字、或看起来像版本号/计数器的东西不是主机
    if len(labels) == 1 and (host.isdigit() or len(host) < 2):
        return False
    # 末段纯数字 → 是版本号/计数/残缺 IP（`1.2.3`、`10.0`、`1.2.3.4.5`），不是域名。
    # 不拦的话 `1.2.3:8080` 会被折算成 `domain:2.3`。
    if labels[-1].isdigit():
        return False
    return True


def canonical_key(value: str | None) -> str | None:
    """把 URL 或裸主机折算成规范键：`domain:example.com` / `ip:1.2.3.4` / `host:internal-api`。

    适用场景是**调用方已断言这是主机**（如项目 origin 事实、资产来源）。
    在散文里找主机请用 `key_from_text()` —— 它另有一层置信度约束。

    返回 None 表示"这里没有可归属的主机"（句子、文件路径、纯数字等）。
    """
    raw = (value or "").strip()
    host = _strip_host(raw)
    if not host:
        return None
    # 无 scheme 的裸 token 带大写 → 是 API 方法名 / 类型名 / 标识符，不是主机名。
    # 实证样例：`User.Read`、`security.HugeSecurityManager`、`process.execSync`、
    # `LoadImage.image`。真主机名在这些结论里一律小写，这个信号很干净。
    if "://" not in raw and any(ch.isupper() for ch in raw):
        return None
    if _is_ip(host):
        return f"{PREFIX_IP}:{host}"
    if not _valid_syntax(host):
        return None
    if _is_file_like(host):
        return None
    labels = host.rstrip(".").split(".")
    if len(labels) == 1:
        if host in _JUNK_SINGLE_LABELS:
            return None
        return f"{PREFIX_HOST}:{host}"
    # 多标签：取后两段作为可复用归属（co.uk 这类不完美，但渗透目标绝大多数成立）
    root = ".".join(labels[-2:])
    if _is_file_like(root):
        return None
    return f"{PREFIX_DOMAIN}:{root}"


def key_from_text(text: str | None) -> str | None:
    """在自由文本里找第一个**可信**主机并折算成键。

    分两档置信度：

    - **高（带 scheme 或带端口）**：`http://internal-api:5000`、`10.0.172.234:8080`
      —— 上下文本身就是证据，走 `canonical_key` 的宽松规则；
    - **低（散文里的裸 token）**：`api.example.com 已确认` —— 额外要求 TLD 命中
      `_COMMON_REAL_TLDS`，且不含大写。否则 `database.host`、`lang.runtime` 这类
      占位符会被当成域名（这正是旧口径造出 54 个垃圾键的路径之一）。

    纯裸单词（`internal-api` 不带端口）**不算** —— 否则散文里的普通词会被当成主机。
    """
    body = text or ""
    if not body:
        return None

    # 与 `host_from_text` 同一条规矩：**按文本位置**取最早可用者，
    # 而不是"哪个模式先命中就用哪个"（否则紧随全角标点的靠前 URL 会被丢掉、
    # 让后面无关的主机变成目标键 —— 实测踩过）。
    best: tuple[int, str] | None = None
    for pattern in (_URL_RE, _HOSTPORT_RE, _BARE_IP_RE):
        for match in pattern.finditer(body):
            key = canonical_key(match.group(0))
            if key:
                if best is None or match.start() < best[0]:
                    best = (match.start(), key)
                break
    if best:
        return best[1]

    for match in _BARE_DOMAIN_RE.finditer(body):
        token = match.group(0)
        if any(ch.isupper() for ch in token):
            continue
        if token.count(".") < 1:
            continue
        tld = token.rsplit(".", 1)[-1].lower()
        if tld not in _COMMON_REAL_TLDS:
            continue
        key = canonical_key(token)
        if key and key.startswith(PREFIX_DOMAIN):
            return key
    return None


def key_from_sources(*candidates: str | None) -> str | None:
    """按优先级尝试多个来源，返回第一个能折算出的键（与 `key_from_text` 不同，
    这里对裸主机也接受 —— 因为调用方给的本来就是"主机"而不是散文）。"""
    for item in candidates:
        if not item:
            continue
        key = canonical_key(item)
        if key:
            return key
    for item in candidates:
        if not item:
            continue
        key = key_from_text(item)
        if key:
            return key
    return None


def split_key(key: str) -> tuple[str, str]:
    """`domain:example.com` → ("domain", "example.com")，便于按类型加权匹配。"""
    kind, _, value = (key or "").partition(":")
    return (kind or "", value)


def _is_usable_host(host: str, *, raw: str) -> bool:
    """一个提取出来的主机是否可用（IP / 语法合法 / 不是文件 / 不是标识符）。"""
    if _is_ip(host):
        return True
    if not _valid_syntax(host):
        return False
    if _is_file_like(host):
        return False
    if len(host.split(".")) == 1 and host in _JUNK_SINGLE_LABELS:
        return False
    # 无 scheme 的裸 token 带大写 → 标识符/方法名，不是主机（同上文 canonical_key 的理由）
    if "://" not in raw and any(ch.isupper() for ch in raw):
        return False
    return True


def _earliest_usable(body: str, patterns: tuple) -> tuple[int, str] | None:
    """在多个模式里取**位置最靠前**的可用主机。

    为什么必须按位置：先前是"按模式逐个试"，于是只要靠后的模式先命中就赢了 ——
    实测 `平台（https://tsecbench.zc.tencent.com）…VPN: http://10.0.100.58` 这种文本里，
    平台域名因紧跟全角括号被判不可用，结果**后面那个无关 IP 成了目标键**。
    "文本里第一个主机"就该是位置上的第一个。
    """
    best: tuple[int, str] | None = None
    for pattern in patterns:
        for match in pattern.finditer(body):
            raw = match.group(0)
            host = _strip_host(raw)
            if host and _is_usable_host(host, raw=raw):
                if best is None or match.start() < best[0]:
                    best = (match.start(), host)
                break  # 同一模式内后续命中只会更靠后
    return best


def host_from_text(text: str | None) -> str | None:
    """在自由文本里找第一个可信主机，返回**完整主机名**（不做根域归并）。

    与 `key_from_text` 的区别只在最后一步：那个返回**归并后的复用键**
    （`sub.example.com` → `domain:example.com`），这个返回**完整主机**
    （`sub.example.com`）—— 资产台账要的是"到底是哪台机器"，
    知识库要的是"哪些目标可以互相复用经验"。两个问题不同，不能共用一个输出。
    """
    body = text or ""
    if not body:
        return None

    found = _earliest_usable(body, (_URL_RE, _HOSTPORT_RE, _BARE_IP_RE))
    if found:
        return found[1]

    for match in _BARE_DOMAIN_RE.finditer(body):
        token = match.group(0)
        if any(ch.isupper() for ch in token):
            continue
        if token.rsplit(".", 1)[-1].lower() not in _COMMON_REAL_TLDS:
            continue
        host = _strip_host(token)
        if host and _is_usable_host(host, raw=token):
            return host
    return None


def asset_ref(value: str | None) -> str | None:
    """资产台账用的规范化：从 URL / 主机 / **句子** 里取出完整主机名。

    存在的理由：真实 origin 事实是句子（`目标地址：https://…\\n授权范围：仅主域`），
    而旧实现只认"以 scheme 开头的 URL 或裸域名"→ 句子算出空串 →
    `asset_ref` 为空 → 资产台账与覆盖提示对这些项目静默失效。
    """
    text = (value or "").strip()
    if not text:
        return None
    direct = _strip_host(text)
    if direct and _is_usable_host(direct, raw=text):
        return direct
    return host_from_text(text)


def is_plausible_domain(host: str) -> bool:
    """保留的兼容入口：判定一个裸字符串是否是可信域名（不含 IP / 单标签 / 文件）。"""
    key = canonical_key(host)
    return bool(key and key.startswith(PREFIX_DOMAIN))


def iter_keys(values: Iterable[str | None]) -> list[str]:
    """批量折算，跳过无法归属的项。"""
    out: list[str] = []
    for value in values:
        key = canonical_key(value)
        if key:
            out.append(key)
    return out
