"""Android APK/XAPK shallow static-analysis preprocessor.

Mirrors the design of ``miniprogram.py``: a dependency-free, pure-Python
preprocessor that runs inside the (PyInstaller) server binary. It does the
SHALLOW pass only — enough to seed a Sharp Fact graph — while the heavy
decompilation (jadx / dex2jar / vineflower) is left to the agent inside the
worker container, which has those tools installed.

What the shallow pass extracts, without decompiling:
  * APK / XAPK container structure (entries, sizes, hashes)
  * Mobile framework fingerprint (Flutter / React Native / Xamarin / Cordova /
    native) from marker files and native libraries
  * Native library inventory across base + split APKs (lib/<abi>/*.so)
  * Plaintext strings scanned out of the DEX blobs: URLs, API-path candidates,
    suspected secrets — works even when Java class names are obfuscated
  * Package name / versions from AndroidManifest.xml (best-effort binary probe;
    left as "?" for the container agent when it cannot be read statically)

The three ``build_static_ai_*`` functions match the miniprogram naming so the
router layer is a straight analogue of ``routers/miniprogram.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# --- limits (mirror miniprogram.py conventions) ------------------------------
MAX_FILE_COUNT = 8000
MAX_APK_BYTES = 600 * 1024 * 1024
MAX_DEX_SCAN_BYTES = 40 * 1024 * 1024
MAX_XAPK_INNER_APKS = 24
MAX_AI_CONTEXT_STRINGS = 120
MAX_AI_CONTEXT_DOMAINS = 60
MAX_AI_CONTEXT_ENDPOINTS = 100
MAX_AI_CONTEXT_LIBS = 120
MAX_AI_CONTEXT_FILES = 120
MAX_AI_CONTEXT_SECRETS = 60

# --- APK-into-container injection ---------------------------------------------
# Fixed fact id carrying the server-side APK path so the dispatcher can inject
# the binary into the worker container before the agent decompiles it. The APK
# binary itself is NOT sent through the Fact graph (that only carries text);
# this fact records where the dispatcher can read the file from on the host.
APK_SOURCE_FACT_ID = "apk_source"
# Directory inside the worker container where the dispatcher drops the APK.
CONTAINER_TARGETS_DIR = "/home/kali/workspace/targets"
# Hard ceiling on the APK size the dispatcher will inject into a container.
MAX_INJECT_APK_BYTES = 500 * 1024 * 1024


def container_apk_path(filename: str) -> str:
    """Absolute path the APK will live at inside the worker container."""
    safe = Path(filename).name or "app.apk"
    return f"{CONTAINER_TARGETS_DIR}/{safe}"


def build_apk_source_fact(result: dict[str, Any], source_path: str) -> str:
    """Serialize the server-side APK location so the dispatcher can find and
    inject it. Stored as a JSON block inside a fact description."""
    filename = str(result.get("filename") or Path(source_path).name)
    package = result.get("package") or {}
    total_size = int(package.get("total_size") or 0)
    payload = {
        "kind": "apk_source",
        "host_path": str(Path(source_path).expanduser().resolve()),
        "filename": filename,
        "size": total_size,
        "container_path": container_apk_path(filename),
    }
    return (
        "APK 源文件定位信息（供 dispatcher 注入容器，勿删）：\n"
        + _json_block(payload)
    )


def parse_apk_source_fact(description: str) -> dict[str, Any] | None:
    """Extract the JSON payload from an apk_source fact description. Returns
    None if the description does not contain a valid payload."""
    if not description:
        return None
    start = description.find("{")
    end = description.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(description[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "apk_source":
        return None
    return payload

# --- regexes (same spirit as miniprogram.py) ---------------------------------
URL_RE = re.compile(rb"\b(?:https?|wss?)://[^\s\"'<>`\\)]{4,300}", re.IGNORECASE)
ENDPOINT_RE = re.compile(
    rb"(?<![\w:])/(?:api|v\d+|cgi|user|member|auth|login|logout|register|pay|order|"
    rb"upload|download|admin|open|client|service|app|account|token|oauth|sms|otp|profile)"
    rb"[A-Za-z0-9_./?=&:%+-]{0,200}",
    re.IGNORECASE,
)
# High-signal secret-ish tokens embedded in strings. Deliberately conservative.
SECRET_RE = re.compile(
    rb"(?i)(?:api[_-]?key|secret|token|passwd|password|access[_-]?key|"
    rb"appsecret|app[_-]?key|bearer|authorization|aws_access|private[_-]?key)"
    rb"[\"'`:=\s]{1,4}[A-Za-z0-9_\-./+]{8,120}"
)
PKG_NAME_RE = re.compile(rb"[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*){2,}")

# --- framework fingerprint markers -------------------------------------------
# entry-path substring -> (framework, marker label)
FRAMEWORK_MARKERS: list[tuple[str, str, str]] = [
    ("lib/", "libflutter.so", "Flutter"),
    ("", "libapp.so", "Flutter"),
    ("assets/flutter_assets/", "flutter_assets", "Flutter"),
    ("assets/", "index.android.bundle", "React Native"),
    ("lib/", "libreactnativejni.so", "React Native"),
    ("lib/", "libhermes.so", "React Native"),
    ("lib/", "libmonodroid.so", "Xamarin"),
    ("assemblies/", "assemblies", "Xamarin"),
    ("assets/", "www/cordova.js", "Cordova/Capacitor"),
    ("assets/", "capacitor.config.json", "Cordova/Capacitor"),
    ("lib/", "libil2cpp.so", "Unity (IL2CPP)"),
    ("assets/bin/Data/", "Managed", "Unity (Mono)"),
]

# Packer / commercial-shell fingerprints (by entry name; no DEX decode needed).
# Detection here is heuristic: hitting a marker means "likely packed — DEX is
# stub, real code loads at runtime", so the agent should plan on unpacking
# (panda-dex-dumper on device) before jadx.  Vendors map to well-known so names.
PACKER_MARKERS: list[tuple[str, str]] = [
    ("libjiagu", "360 加固 (libjiagu)"),
    ("libprotectclass", "爱加密 / SecNeo 类 (libprotectclass)"),
    ("libsecneo", "SecNeo 加固"),
    ("libdexhelper", "娜迦加固 (libdexhelper)"),
    ("libnesec", "娜迦加固 (libnesec)"),
    ("libexecmain", "腾讯乐固 (libexecmain)"),
    ("libexec", "通用加固 loader (libexec)"),
    ("libsgmain", "阿里聚安全 (libsgmain)"),
    ("libsgsecuritybody", "阿里聚安全 (libsgsecuritybody)"),
    ("libshell", "通用加固 shell (libshell)"),
    ("libDexHelper", "通用加固 (libDexHelper)"),
    ("libcev", "通用加固 (libcev)"),
    ("libapp_sec", "通用加固 (libapp_sec)"),
    ("assets/secData", "通用加固资源 (secData)"),
]
# 极小的主 classes.dex（< 60KB）常见于壳（真代码在运行时解密加载）。
PACKER_TINY_DEX_THRESHOLD = 60 * 1024

NATIVE_ABIS = ("armeabi-v7a", "arm64-v8a", "x86", "x86_64")


# --- small helpers (mirror miniprogram.py) -----------------------------------
def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _limit_rows(rows: list[Any], limit: int) -> list[Any]:
    return rows[: max(0, limit)]


def _truncate_text(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


# --- APK reading -------------------------------------------------------------
def _read_apk_bytes(path: Path) -> bytes:
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_file():
        raise ValueError(f"不是文件：{path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("文件为空")
    if size > MAX_APK_BYTES:
        raise ValueError(f"文件过大（>{MAX_APK_BYTES // (1024 * 1024)}MB），当前不支持")
    return path.read_bytes()


def _is_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _looks_like_xapk(zf: zipfile.ZipFile) -> bool:
    names = zf.namelist()
    inner_apks = [n for n in names if n.lower().endswith(".apk")]
    return len(inner_apks) >= 1 and any(n.lower() == "manifest.json" for n in names)


def _abi_of(entry_name: str) -> str:
    for abi in NATIVE_ABIS:
        if f"lib/{abi}/" in entry_name:
            return abi
    return ""


# --- core analysis of a single APK -------------------------------------------
def _analyze_single_apk(name: str, data: bytes) -> dict[str, Any]:
    """Shallow analysis of one APK's bytes. Returns a partial result dict."""
    result: dict[str, Any] = {
        "filename": name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "files": [],
        "native_libs": [],
        "frameworks": [],
        "packers": [],
        "dex_names": [],
        "notes": [],
    }
    if not _is_zip(data):
        result["notes"].append(f"{name}: 不是有效的 zip/apk 结构")
        return result

    files: list[dict[str, Any]] = []
    native_libs: list[dict[str, Any]] = []
    dex_blobs: list[tuple[str, bytes]] = []
    framework_hits: set[str] = set()
    packer_hits: set[str] = set()
    manifest_bytes: bytes | None = None

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        result["notes"].append(f"{name}: 无法解析 zip（{exc}）")
        return result

    with zf:
        infos = zf.infolist()[:MAX_FILE_COUNT]
        for info in infos:
            entry = info.filename
            lower = entry.lower()
            files.append({"path": entry, "size": info.file_size})

            # framework fingerprint
            for _prefix, marker, fw in FRAMEWORK_MARKERS:
                if marker.lower() in lower:
                    framework_hits.add(fw)

            # packer fingerprint (by entry name)
            if not packer_hits:
                for marker, vendor in PACKER_MARKERS:
                    if marker.lower() in lower:
                        packer_hits.add(vendor)
                        break

            # native libs
            if lower.endswith(".so") and "lib/" in lower:
                native_libs.append(
                    {
                        "path": entry,
                        "abi": _abi_of(entry),
                        "name": entry.rsplit("/", 1)[-1],
                        "size": info.file_size,
                    }
                )

            # dex blobs for string scan
            if lower.endswith(".dex"):
                result["dex_names"].append(entry)
                try:
                    blob = zf.read(info)
                    dex_blobs.append((entry, blob))
                    # 极小主 dex（<60KB）= 壳典型特征（真代码运行时解密加载）
                    if info.file_size < PACKER_TINY_DEX_THRESHOLD and lower.endswith("classes.dex"):
                        packer_hits.add("疑似加壳（classes.dex 极小）")
                except Exception as exc:  # noqa: BLE001 - keep scanning others
                    result["notes"].append(f"{entry}: 读取失败（{exc}）")

            # manifest (binary; best-effort probe only)
            if lower == "androidmanifest.xml" and manifest_bytes is None:
                try:
                    manifest_bytes = zf.read(info)
                except Exception:  # noqa: BLE001
                    manifest_bytes = None

    result["files"] = files
    result["native_libs"] = native_libs
    result["frameworks"] = sorted(framework_hits)
    result["packers"] = sorted(packer_hits)
    if packer_hits:
        result["notes"].append(
            "检测到疑似加固：" + "、".join(sorted(packer_hits))
            + "。jadx 反编译前需先脱壳（真机 panda-dex-dumper），否则只见壳入口。"
        )

    # string scan over dex blobs (bounded)
    urls: Counter[str] = Counter()
    endpoints: Counter[str] = Counter()
    secrets: list[str] = []
    scanned = 0
    for entry, blob in dex_blobs:
        if scanned >= MAX_DEX_SCAN_BYTES:
            result["notes"].append("DEX 字符串扫描已达上限，部分 dex 未扫描")
            break
        chunk = blob[: max(0, MAX_DEX_SCAN_BYTES - scanned)]
        scanned += len(chunk)
        for m in URL_RE.findall(chunk):
            urls[_decode(m)] += 1
        for m in ENDPOINT_RE.findall(chunk):
            endpoints[_decode(m)] += 1
        for m in SECRET_RE.findall(chunk):
            text = _decode(m)
            if len(secrets) < MAX_AI_CONTEXT_SECRETS * 2:
                secrets.append(_truncate_text(text, 160))

    result["urls"] = urls
    result["endpoints"] = endpoints
    result["secrets"] = secrets

    # Best-effort structured manifest summary from the AXML string pool:
    # package name / version / permissions / uses-features / component classes.
    # The string pool carries every literal, so this is far more reliable than
    # regex-fishing in binary bytes — yet still dependency-free.  Full XML
    # semantic decode (exported flags etc.) stays with the agent's apktool.
    summary = extract_manifest_summary(manifest_bytes)
    result["package_name"] = summary["package_name"]
    result["manifest"] = summary
    return result


# --- domain aggregation ------------------------------------------------------
def _domains_from_urls(urls: Counter[str]) -> list[dict[str, Any]]:
    hosts: Counter[str] = Counter()
    scheme_of: dict[str, str] = {}
    for url, count in urls.items():
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        host = parsed.hostname or ""
        if not host:
            continue
        hosts[host] += count
        scheme_of.setdefault(host, parsed.scheme or "")
    return [
        {"host": host, "scheme": scheme_of.get(host, ""), "count": count}
        for host, count in hosts.most_common(MAX_AI_CONTEXT_DOMAINS)
    ]


# --- filesystem browsing (home-jailed) ---------------------------------------
APK_SUFFIXES = (".apk", ".xapk")
MAX_DIR_ENTRIES = 1000


def _home_root() -> Path:
    return Path.home().resolve()


def _resolve_under_home(rel_or_abs: str) -> Path:
    """Resolve a user-supplied path and enforce it stays within the home dir.

    Accepts either a path relative to home ("Downloads/apks") or an absolute
    path; either way the resolved target must be inside the home directory.
    Raises PermissionError on escape, FileNotFoundError if missing.
    """
    home = _home_root()
    raw = (rel_or_abs or "").strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = home / candidate
    resolved = candidate.resolve()
    if resolved != home and home not in resolved.parents:
        raise PermissionError("只能浏览用户主目录（~）以内的路径")
    return resolved


def list_directory(rel_path: str = "") -> dict[str, Any]:
    """List sub-directories and APK/XAPK files under a home-relative path."""
    home = _home_root()
    target = _resolve_under_home(rel_path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise ValueError(f"不是目录：{target}")

    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    truncated = False
    count = 0
    try:
        entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except PermissionError as exc:
        raise PermissionError(f"无权读取目录：{target}") from exc

    for entry in entries:
        if count >= MAX_DIR_ENTRIES:
            truncated = True
            break
        name = entry.name
        if name.startswith("."):
            continue  # skip hidden entries
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            dirs.append({"name": name, "rel": str(entry.resolve().relative_to(home))})
            count += 1
        elif entry.suffix.lower() in APK_SUFFIXES:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            files.append(
                {
                    "name": name,
                    "rel": str(entry.resolve().relative_to(home)),
                    "abs": str(entry.resolve()),
                    "size": size,
                    "suffix": entry.suffix.lower().lstrip("."),
                }
            )
            count += 1

    rel_here = "" if target == home else str(target.relative_to(home))
    # breadcrumb segments from home down to target
    crumbs: list[dict[str, str]] = [{"name": "~", "rel": ""}]
    if rel_here:
        acc = Path()
        for seg in Path(rel_here).parts:
            acc = acc / seg
            crumbs.append({"name": seg, "rel": str(acc)})
    parent_rel = "" if not rel_here else str(Path(rel_here).parent) if str(Path(rel_here).parent) != "." else ""

    return {
        "home": str(home),
        "rel": rel_here,
        "abs": str(target),
        "parent_rel": parent_rel,
        "at_home": target == home,
        "crumbs": crumbs,
        "dirs": dirs,
        "files": files,
        "truncated": truncated,
    }


# --- public entrypoints ------------------------------------------------------
def analyze_apk_path(path_str: str) -> dict[str, Any]:
    """Analyze an APK or XAPK file on disk. Returns a merged result dict."""
    # Jail the user-supplied path to the home dir, same as the directory
    # browser (list_directory). Without this an authenticated user could point
    # the analyzer at any host file (e.g. /etc/passwd) for an arbitrary read.
    path = _resolve_under_home(path_str)
    data = _read_apk_bytes(path)

    parts: list[dict[str, Any]] = []
    fmt = "apk"
    notes: list[str] = []

    if _is_zip(data):
        try:
            outer = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"无法解析文件为 zip/apk：{exc}") from exc
        with outer:
            if _looks_like_xapk(outer):
                fmt = "xapk"
                inner_apks = [n for n in outer.namelist() if n.lower().endswith(".apk")]
                notes.append(f"XAPK 包含 {len(inner_apks)} 个 APK：{', '.join(inner_apks[:8])}")
                for inner in inner_apks[:MAX_XAPK_INNER_APKS]:
                    try:
                        parts.append(_analyze_single_apk(inner, outer.read(inner)))
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"{inner}: 分析失败（{exc}）")
            else:
                parts.append(_analyze_single_apk(path.name, data))
    else:
        raise ValueError("文件不是有效的 APK/XAPK（缺少 zip 头）")

    return _merge_parts(path, fmt, parts, notes)


def _merge_parts(
    path: Path, fmt: str, parts: list[dict[str, Any]], notes: list[str]
) -> dict[str, Any]:
    merged_urls: Counter[str] = Counter()
    merged_endpoints: Counter[str] = Counter()
    merged_secrets: list[str] = []
    all_libs: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    frameworks: set[str] = set()
    dex_names: list[str] = []
    package_name = ""
    total_size = 0

    for part in parts:
        merged_urls.update(part.get("urls") or Counter())
        merged_endpoints.update(part.get("endpoints") or Counter())
        merged_secrets.extend(part.get("secrets") or [])
        all_libs.extend(part.get("native_libs") or [])
        all_files.extend(part.get("files") or [])
        frameworks.update(part.get("frameworks") or [])
        dex_names.extend(part.get("dex_names") or [])
        total_size += int(part.get("size") or 0)
        notes.extend(part.get("notes") or [])
        if not package_name and part.get("package_name"):
            package_name = part["package_name"]

    if not frameworks:
        frameworks = {"Native (Java/Kotlin)"}

    # dedupe libs by (name, abi)
    seen_libs: set[tuple[str, str]] = set()
    unique_libs: list[dict[str, Any]] = []
    for lib in all_libs:
        key = (lib.get("name", ""), lib.get("abi", ""))
        if key in seen_libs:
            continue
        seen_libs.add(key)
        unique_libs.append(lib)

    abis = sorted({lib.get("abi", "") for lib in all_libs if lib.get("abi")})

    return {
        "filename": path.name,
        "source_path": str(path),
        "format": fmt,
        "package": {
            "package_name": package_name or "?",
            "apk_count": len(parts),
            "file_count": len(all_files),
            "total_size": total_size,
            "abis": abis,
            "dex_count": len(dex_names),
        },
        "frameworks": sorted(frameworks),
        "native_libs": unique_libs,
        "urls": [{"url": u, "count": c} for u, c in merged_urls.most_common(MAX_AI_CONTEXT_STRINGS)],
        "domains": _domains_from_urls(merged_urls),
        "endpoints": [
            {"path": p, "count": c} for p, c in merged_endpoints.most_common(MAX_AI_CONTEXT_ENDPOINTS)
        ],
        "secrets": merged_secrets[:MAX_AI_CONTEXT_SECRETS],
        "files": all_files,
        "dex_names": dex_names,
        "notes": notes,
    }


# --- AI context builders (mirror miniprogram.py naming) ----------------------
def build_static_ai_analysis_context(result: dict[str, Any], source_path: str) -> str:
    package = result.get("package") or {}
    frameworks = result.get("frameworks") or []
    file_type_counts: Counter[str] = Counter()
    for f in result.get("files") or []:
        suffix = str(f.get("path", "")).rsplit(".", 1)
        file_type_counts[("." + suffix[-1]).lower() if len(suffix) == 2 else "(no-ext)"] += 1

    summary = {
        "package_name": package.get("package_name", "?"),
        "source_path": source_path,
        "filename": result.get("filename", ""),
        "format": result.get("format", ""),
        "frameworks": frameworks,
        "packers": result.get("packers") or [],
        "package": {
            "apk_count": package.get("apk_count", 1),
            "file_count": package.get("file_count", 0),
            "total_size": package.get("total_size", 0),
            "abis": package.get("abis", []),
            "dex_count": package.get("dex_count", 0),
        },
        "counts": {
            "domains": len(result.get("domains") or []),
            "endpoints": len(result.get("endpoints") or []),
            "urls": len(result.get("urls") or []),
            "native_libs": len(result.get("native_libs") or []),
            "suspected_secrets": len(result.get("secrets") or []),
            "files": len(result.get("files") or []),
        },
        "file_type_counts": dict(file_type_counts.most_common(30)),
    }

    framework_note = (
        "该应用疑似为 " + " / ".join(frameworks) + " 应用。"
        "对 Flutter / React Native / Xamarin / Unity 应用，Java 反编译价值有限，"
        "真正的业务逻辑在 libapp.so / index.android.bundle / assemblies 等处，"
        "需改用对应工具（如 blutter、hermes-dec、strings 分析 .so）。"
        if frameworks and frameworks != ["Native (Java/Kotlin)"]
        else "该应用疑似为原生 Java/Kotlin 应用，可用容器内 jadx / dex2jar / vineflower 反编译深挖。"
    )
    packers = result.get("packers") or []
    packer_note = ""
    if packers:
        packer_note = (
            "\n\n检测到疑似加固壳（" + "、".join(packers) + "）："
            "jadx / dex2jar 直接反编译只能看到壳入口（classes.dex 是 stub），"
            "真正业务代码在运行时解密加载。务必先脱壳：真机 root + panda-dex-dumper"
            "（adb push /data/local/tmp → 待 App 启动壳解密后 dump → pull 回 jadx），"
            "或对壳厂商选用对应脱壳工具。不要对着 stub dex 浪费时间。"
        )

    return "\n".join(
        [
            "# 安卓 APK 静态分析上下文（浅层）",
            "",
            "该上下文来自 Sharp server 侧对 APK/XAPK 的浅层静态扫描（容器结构、框架指纹、"
            "native 库清单、DEX 明文字符串提取），未做反编译。深度反编译、调用链追踪和完整"
            "接口提取需由容器内 agent 用 jadx / dex2jar / vineflower 完成。所有结论必须区分"
            "“静态可疑”和“需人工进一步验证（抓包 / 脱壳 / 真机 frida hook，均需额外环境与授权）”。",
            "",
            "## 框架判定",
            framework_note,
            "",
            "## 加固检测",
            packer_note.strip() if packers else "未检测到已知加固壳特征。",
            "",
            "## 概览",
            _json_block(summary),
            "",
            "## 疑似敏感字符串（需人工确认，可能为误报）",
            _json_block(_limit_rows(result.get("secrets") or [], MAX_AI_CONTEXT_SECRETS)),
            "",
            "## 域名资产",
            _json_block(_limit_rows(result.get("domains") or [], MAX_AI_CONTEXT_DOMAINS)),
            "",
            "## 接口与路径候选（DEX 明文）",
            _json_block(_limit_rows(result.get("endpoints") or [], MAX_AI_CONTEXT_ENDPOINTS)),
            "",
            "## URL 样本",
            _json_block(_limit_rows(result.get("urls") or [], MAX_AI_CONTEXT_STRINGS)),
            "",
            "## Native 库清单",
            _json_block(_limit_rows(result.get("native_libs") or [], MAX_AI_CONTEXT_LIBS)),
            "",
            "## 文件样本",
            _json_block(_limit_rows(result.get("files") or [], MAX_AI_CONTEXT_FILES)),
            "",
            "## 解析备注",
            _json_block(_limit_rows(result.get("notes") or [], 80)),
        ]
    )


def build_static_ai_intent_description(result: dict[str, Any]) -> str:
    package = result.get("package") or {}
    pkg = str(package.get("package_name") or "未知包名")
    frameworks = result.get("frameworks") or ["Native (Java/Kotlin)"]
    fw_text = " / ".join(frameworks)
    return (
        f"基于安卓应用 {pkg}（框架：{fw_text}）的 APK 浅层静态分析上下文，"
        "在容器内用 jadx / dex2jar / vineflower 完成深度反编译与安全分析，生成面向授权测试的报告和后续测试清单。"
        "必须覆盖：1) 应用资产画像、包名/组件（Activity/Service/Receiver/Provider）与后端域名归类；"
        "2) 登录、鉴权、用户信息、订单、支付、上传、下载、token/oauth、管理类接口的风险优先级；"
        "3) 硬编码密钥/证书/配置等静态风险点的可信度判断；"
        "4) 需要脱壳（panda-dex-dumper）、动态插桩（frida）或抓包验证的事项，明确标注为"
        "“需人工在真机/模拟器上进一步验证”（这些能力需要额外环境与授权，本工具当前不自动执行）；"
        "5) 可执行的下一步测试任务清单。"
        "不要把静态特征直接判定为已确认漏洞；无运行时证据时明确标注验证条件。"
    )


def build_static_ai_project_seed(result: dict[str, Any], source_path: str) -> dict[str, str]:
    package = result.get("package") or {}
    pkg = str(package.get("package_name") or "unknown")
    filename = str(result.get("filename") or pkg)
    fmt = str(result.get("format") or "apk").upper()
    in_container = container_apk_path(filename)
    return {
        "title": f"安卓 App 静态安全分析 - {pkg}",
        "origin": (
            f"从上传或指定路径解析安卓安装包 {filename}（{fmt}）。"
            f"已完成 server 侧浅层静态扫描，包含框架指纹、native 库、DEX 明文域名/接口/"
            f"疑似密钥和文件清单。APK 二进制已由 dispatcher 注入到当前 worker 容器：{in_container}"
            "（可直接读取，无需再等待或搜索文件）。"
        ),
        "goal": (
            f"在容器内对已就位的 APK（{in_container}）用 jadx / dex2jar / vineflower 完成深度反编译"
            "与安全分析，输出资产画像、组件与接口清单、高风险接口优先级、硬编码密钥等静态风险说明、"
            "需要脱壳 / frida / 抓包进一步验证（需人工在真机上做，本工具不自动执行）的事项，"
            "以及后续授权渗透测试任务清单。"
        ),
    }


# --- AXML string-pool decoding (dependency-free) ------------------------------
# AndroidManifest.xml inside an APK is binary AXML.  Its string pool is a plain
# chunk holding every literal (package name, permission names, attribute names,
# component names) as UTF-8/UTF-16.  We do NOT need to walk the full XML tree
# for a shallow pass — reading the string pool already gives a reliable picture
# of package/version/permissions/components without a third-party AXML parser
# (keeps the PyInstaller server binary dependency-free).  Real component-graph
# decoding is left to the agent's apktool inside the worker container.


def _axml_string_pool(data: bytes) -> list[str]:
    """Return every string in an AXML binary string pool (UTF-8 + UTF-16).

    AXML layout: a chunk header whose first field is chunk type 0x0003
    (XML), then chunk type 0x0001 (string pool).  We only parse the string
    pool header + entries; safe on truncated/garbage input (returns []).
    """
    if len(data) < 8:
        return []
    # xml chunk header layout: type(2) headerSize(2) chunkSize(4)
    type_ = int.from_bytes(data[0:2], "little")
    if type_ != 0x0003:
        return []
    header_size = int.from_bytes(data[2:4], "little") if len(data) >= 4 else 8
    pos = max(header_size, 8)
    if pos + 28 > len(data):
        return []
    # string pool chunk header layout: type(2) headerSize(2) chunkSize(4)
    # stringCount(4) styleCount(4) flags(4) stringsStart(4) stylesStart(4)
    pool_type = int.from_bytes(data[pos:pos+2], "little")
    if pool_type != 0x0001:
        return []
    pool_header = int.from_bytes(data[pos+2:pos+4], "little")
    if pool_header < 28:
        pool_header = 28
    string_count = int.from_bytes(data[pos+8:pos+12], "little")
    # style_count = int.from_bytes(data[pos+12:pos+16], 'little')
    flags = int.from_bytes(data[pos+16:pos+20], "little")
    strings_start = int.from_bytes(data[pos+20:pos+24], "little")
    if string_count > 200_000:      # sanity
        return []
    utf8 = bool(flags & 0x100)
    offsets_pos = pos + pool_header
    if offsets_pos + string_count * 4 > len(data):
        return []
    strings_base = pos + strings_start
    out: list[str] = []
    for i in range(string_count):
        off = int.from_bytes(data[offsets_pos + i*4: offsets_pos + i*4 + 4], "little")
        p = strings_base + off
        if p >= len(data):
            continue
        try:
            if utf8:
                # utf8: length (1-2 bytes) then chars (1-2 bytes), then bytes
                skip = 1
                if p + skip < len(data) and data[p] & 0x80:
                    skip = 2
                clen = data[p] & 0x7F
                if skip == 2:
                    clen = ((data[p] & 0x7F) << 8) | data[p+1]
                p2 = p + skip
                if p2 + 1 > len(data):
                    continue
                # byte length (may differ from char length for multibyte)
                if data[p2] & 0x80:
                    bskip = 2
                    blen = ((data[p2] & 0x7F) << 8) | data[p2+1]
                else:
                    bskip = 1
                    blen = data[p2]
                p3 = p2 + bskip
                raw = data[p3: p3 + blen]
                sval = raw.decode("utf-8", errors="replace")
            else:
                # utf16: uint16 char count then UTF-16LE bytes
                if p + 2 > len(data):
                    continue
                char_count = int.from_bytes(data[p:p+2], "little")
                raw = data[p+2: p+2 + char_count*2]
                sval = raw.decode("utf-16-le", errors="replace")
            out.append(sval)
        except Exception:  # noqa: BLE001 - malformed pool entry is not fatal
            continue
    return out


def extract_manifest_summary(manifest_bytes: bytes | None) -> dict[str, Any]:
    """Best-effort structured manifest summary from the AXML string pool.

    Returns keys that are None/[] when the pool cannot be read (caller keeps
    the old best-effort behaviour and leaves detail to the container agent).
    """
    summary: dict[str, Any] = {
        "package_name": "",
        "version_name": "",
        "version_code": "",
        "permissions": [],
        "uses_features": [],
        "components": [],   # activity/service/receiver/provider names (dotted)
        "debuggable": None,
        "allow_backup": None,
        "cleartext": None,
    }
    if not manifest_bytes or len(manifest_bytes) < 8:
        return summary
    strings = _axml_string_pool(manifest_bytes)
    if not strings:
        return summary
    # A dotted candidate whose parent dirs look like a package (best effort).
    pkg_candidates = [s for s in strings if s.count(".") >= 2
                      and not s.startswith(("android.", "androidx.", "java.", "kotlin.", "com.android", "http", "https", "/"))]
    # permission names: android.permission.* / android.Manifest.permission-like
    perms = sorted({s for s in strings if s.startswith("android.permission.")})
    summary["permissions"] = perms
    feats = sorted({s for s in strings if s.startswith("android.hardware.")})
    summary["uses_features"] = feats
    summary["package_name"] = pkg_candidates[0] if pkg_candidates else ""
    # attribute values adjacent to common keys (dump each meaningful pair)
    for idx, s in enumerate(strings):
        if s == "versionName" and idx + 1 < len(strings) and not summary["version_name"]:
            nxt = strings[idx+1]
            if len(nxt) < 60:
                summary["version_name"] = nxt
        if s == "versionCode" and idx + 1 < len(strings) and not summary["version_code"]:
            nxt = strings[idx+1]
            if len(nxt) < 20:
                summary["version_code"] = nxt
        if s == "debuggable" and summary["debuggable"] is None:
            # string pool rarely stores booleans as strings; look for the
            # application attribute list we can't fully parse — keep None.
            pass
        if s == "allowBackup" and summary["allow_backup"] is None:
            pass
    # components: strings that look like Java classes after the package
    if summary["package_name"]:
        pfx = summary["package_name"] + "."
        summary["components"] = sorted({s for s in strings
                                        if s.startswith(pfx) and len(s) > len(pfx)
                                        and "$" not in s})[:300]
    return summary

