from __future__ import annotations

import io
import hashlib
import json
import os
import platform
import posixpath
import re
import struct
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


TEXT_SUFFIXES = {
    ".js",
    ".json",
    ".wxml",
    ".wxss",
    ".wxs",
    ".css",
    ".html",
    ".htm",
    ".txt",
    ".map",
    ".xml",
    ".md",
}
MAX_FILE_COUNT = 5000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_SCAN_RESULTS = 500
MAX_SCAN_ENTRIES = 120_000
MAX_SCAN_DEPTH = 18
MAX_ANALYZE_PACKAGE_FILES = 80
MAX_UNPACK_FILES = 12000
MAX_UNPACK_FILE_BYTES = 30 * 1024 * 1024
MAX_AI_CONTEXT_FINDINGS = 80
MAX_AI_CONTEXT_DOMAINS = 50
MAX_AI_CONTEXT_ENDPOINTS = 80
MAX_AI_CONTEXT_APIS = 50
MAX_AI_CONTEXT_FILES = 80
WXAPKG_ENCRYPTED_MAGIC = b"V1MMWX"
WXAPKG_DECRYPT_OFFSET = 6
WXAPKG_DECRYPT_BLOCK_SIZE = 1024
WXAPKG_DECRYPT_SALT = b"saltiest"
WXAPKG_DECRYPT_IV = b"the iv: 16 bytes"
URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>`\\)]+", re.IGNORECASE)
ENDPOINT_RE = re.compile(
    r"(?<![\w:])/(?:api|v\d+|cgi|wx|user|member|auth|login|pay|order|upload|download|admin|open|client|service|mini|app)[A-Za-z0-9_./?=&:%+-]*",
    re.IGNORECASE,
)
WX_API_RE = re.compile(r"\bwx\.([A-Za-z_][A-Za-z0-9_]*)\b")
APP_ID_RE = re.compile(r"\bwx[a-f0-9]{16}\b", re.IGNORECASE)
WXAPKG_NAME_RE = re.compile(r"\.wxapkg(?:$|[._-])", re.IGNORECASE)
SECRET_KEY_RE = re.compile(
    r"(?i)\b(appsecret|access[_-]?token|refresh[_-]?token|session[_-]?key|"
    r"secret|token|api[_-]?key|openid|unionid|password|passwd)\b"
    r"\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
AI_ENDPOINT_KEYWORDS = (
    "login",
    "auth",
    "token",
    "session",
    "openid",
    "unionid",
    "user",
    "member",
    "phone",
    "mobile",
    "order",
    "pay",
    "payment",
    "upload",
    "download",
    "file",
    "admin",
    "coupon",
    "address",
    "idcard",
    "realname",
)


@dataclass(frozen=True)
class PackageFile:
    path: str
    offset: int
    size: int
    data: bytes | None = None
    unsafe_path: bool = False


def analyze_package(filename: str, data: bytes, *, app_id: str | None = None) -> dict[str, Any]:
    notes: list[str] = []
    package_format = "unknown"
    files: list[PackageFile] = []
    header: dict[str, Any] = {}

    if data.startswith(b"V1MMWX"):
        # 上传/内存路径：内置解密器可解 V1MMWX（微信个人号缓存加密格式），
        # 需调用方提供 AppID 作 PBKDF2 key（与微信缓存目录按 AppID 分组同源）。
        if app_id:
            try:
                data = _decrypt_wxapkg_data(app_id, data)
                notes.append(f'已使用 AppID {app_id} 解密 V1MMWX 包。')
                package_format = "wxapkg"
                files, header, parse_notes = _parse_wxapkg(data)
                notes.extend(parse_notes)
            except ValueError as exc:
                return {
                    "filename": filename,
                    "format": "encrypted_wxapkg",
                    "package": {"file_count": 0, "total_size": len(data), "index_length": 0, "body_length": 0},
                    "files": [],
                    "domains": [], "endpoints": [], "api_calls": [],
                    "findings": [{
                        "severity": "medium",
                        "title": '加密 wxapkg 解密失败',
                        "detail": f'{exc}。请确认 AppID 正确（微信缓存里按 AppID 分目录，或从 app.json 中提取）。',
                        "file": "", "line": None, "evidence": "V1MMWX header",
                    }],
                    "notes": [f'V1MMWX 解密失败：{exc}'] + notes,
                }
        else:
            return {
                "filename": filename,
                "format": "encrypted_wxapkg",
                "package": {
                    "file_count": 0,
                    "total_size": len(data),
                    "index_length": 0,
                    "body_length": 0,
                },
                "files": [],
                "domains": [], "endpoints": [], "api_calls": [],
                "findings": [{
                    "severity": "medium",
                    "title": '检测到加密 wxapkg',
                    "detail": '上传的 wxapkg 带 V1MMWX 加密头。可用"文件 + AppID"方式在工具内解密分析，或先用外部工具解密后上传。',
                    "file": "", "line": None, "evidence": "V1MMWX header",
                }],
                "notes": ['检测到 V1MMWX 加密包头。提供 AppID 后可直接解密分析。'],
            }

    if _looks_like_wxapkg(data):
        package_format = "wxapkg"
        files, header, parse_notes = _parse_wxapkg(data)
        notes.extend(parse_notes)
    elif zipfile.is_zipfile(io.BytesIO(data)):
        package_format = "zip"
        files, header, parse_notes = _parse_zip(data)
        notes.extend(parse_notes)
    else:
        notes.append('未识别为 wxapkg 或 zip，无法解析内部文件。')

    scanned = _scan_files(files)
    package_size = sum(item.size for item in files)
    package = {
        "file_count": len(files),
        "total_size": package_size or len(data),
        "index_length": header.get("index_length", 0),
        "body_length": header.get("body_length", 0),
    }

    return {
        "filename": filename,
        "format": package_format,
        "package": package,
        "files": [_file_to_dict(item) for item in files[:MAX_FILE_COUNT]],
        "domains": scanned["domains"],
        "endpoints": scanned["endpoints"],
        "api_calls": scanned["api_calls"],
        "findings": scanned["findings"],
        "app_profile": _parse_app_profile(files),
        "notes": notes,
    }


def scan_wechat_packages(limit: int = 300) -> dict[str, Any]:
    limit = max(1, min(int(limit), MAX_SCAN_RESULTS))
    host_platform = _host_platform()
    roots = _existing_wechat_roots()
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    entries_seen = 0
    truncated = False

    for root in roots:
        root_entries, root_truncated, root_seen = _scan_root_for_wxapkg(root, limit)
        entries_seen += root_seen
        truncated = truncated or root_truncated
        for item in root_entries:
            key = item.get("app_id") or item["path"]
            if key in seen:
                continue
            seen.add(key)
            packages.append(item)

    packages.sort(key=lambda item: item["mtime"], reverse=True)
    if len(packages) > limit:
        packages = packages[:limit]
        truncated = True

    return {
        "scanned_at": utcnow(),
        "platform": host_platform,
        "roots": [str(root) for root in roots],
        "default_output_dir": default_unpack_output_dir(),
        "packages": packages,
        "count": len(packages),
        "entries_seen": entries_seen,
        "truncated": truncated,
    }


def analyze_wechat_package_path(path: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not _path_is_allowed_wechat_package(target):
        raise PermissionError('只能分析微信缓存目录或 SHARP_WECHAT_WXAPKG_DIRS 指定目录下的小程序包')
    if target.is_dir():
        return analyze_package_directory(target)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    if not _is_wxapkg_filename(target.name) and target.suffix.lower() != ".zip":
        raise ValueError('只支持 wxapkg 或 zip 文件')
    size = target.stat().st_size
    if size > 80 * 1024 * 1024:
        raise ValueError('文件超过 80MB')
    encrypt_key = _infer_app_id_from_path(target)
    data, encrypted = _read_package_bytes(target, encrypt_key=encrypt_key, decrypt=True)
    result = analyze_package(target.name, data)
    if encrypted:
        result.setdefault("notes", []).insert(0, f'已使用 {encrypt_key} 解密微信缓存包。')
        result["format"] = "wxapkg"
    return result


def default_unpack_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.exists() else Path.home()
    return str(base / "sharp-wxapkg-output")


def unpack_wechat_package_path(
    path: str,
    output_dir: str | None = None,
    *,
    decrypt: bool = True,
    encrypt_key: str | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not _path_is_allowed_wechat_package(target):
        raise PermissionError('只能解包微信缓存目录或 SHARP_WECHAT_WXAPKG_DIRS 指定目录下的小程序包')
    if not target.exists():
        raise FileNotFoundError(str(target))

    destination = Path(output_dir or default_unpack_output_dir()).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise ValueError('输出路径必须是目录')
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f'无法创建输出目录：{exc}') from exc

    if target.is_dir():
        package_paths = _wxapkg_files_under_directory(target)
    else:
        if not _is_wxapkg_filename(target.name) and target.suffix.lower() != ".zip":
            raise ValueError('只支持 wxapkg 或 zip 文件')
        package_paths = [target]
    if not package_paths:
        raise ValueError('没有找到可解包的 wxapkg 文件')

    app_id = _normalize_encrypt_key(encrypt_key) or _infer_app_id_from_path(target)
    written: list[dict[str, Any]] = []
    warnings: list[str] = []
    encrypted_count = 0
    parsed_packages = 0
    total_files = 0
    used_paths: set[Path] = set()

    for package_path in package_paths:
        key_for_package = _normalize_encrypt_key(encrypt_key) or _infer_app_id_from_path(package_path) or app_id
        try:
            data, was_encrypted = _read_package_bytes(package_path, encrypt_key=key_for_package, decrypt=decrypt)
            if was_encrypted:
                encrypted_count += 1
            files, _, parse_notes = _parse_unpackable_package(package_path, data)
        except ValueError as exc:
            warnings.append(f"{package_path.name}: {exc}")
            continue
        warnings.extend(f"{package_path.name}: {note}" for note in parse_notes)
        parsed_packages += 1
        total_files += len(files)

        for item in files:
            if len(written) >= MAX_UNPACK_FILES:
                warnings.append(f'写出文件数超过 {MAX_UNPACK_FILES}，已停止继续解包。')
                break
            if item.data is None:
                warnings.append(f'{package_path.name}: {item.path} 内容不可读，已跳过。')
                continue
            if item.size > MAX_UNPACK_FILE_BYTES:
                warnings.append(f'{package_path.name}: {item.path} 超过 {MAX_UNPACK_FILE_BYTES // 1024 // 1024}MB，已跳过。')
                continue
            try:
                output_path = _safe_unpack_output_path(destination, item.path, used_paths)
            except ValueError as exc:
                warnings.append(f"{package_path.name}: {item.path} {exc}")
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(item.data)
            written.append(
                {
                    "path": str(output_path),
                    "relative_path": output_path.relative_to(destination).as_posix(),
                    "size": item.size,
                    "source_package": _relative_posix(target if target.is_dir() else package_path.parent, package_path),
                }
            )
        if len(written) >= MAX_UNPACK_FILES:
            break

    if parsed_packages == 0:
        detail = '；'.join(warnings[:5]) or '没有包被成功解析'
        raise ValueError(detail)

    return {
        "status": "ok",
        "source_path": str(target),
        "output_dir": str(destination),
        "app_id": app_id or "",
        "package_count": parsed_packages,
        "file_count": total_files,
        "written_count": len(written),
        "encrypted_count": encrypted_count,
        "files": written[:500],
        "warnings": warnings[:200],
    }


def build_static_ai_analysis_context(result: dict[str, Any], source_path: str) -> str:
    package = result.get("package") or {}
    app_id = str(package.get("app_id") or _first_app_id_from_result(result) or "")
    domains = [_compact_domain(item) for item in _limit_rows(result.get("domains") or [], MAX_AI_CONTEXT_DOMAINS)]
    endpoints = [_compact_endpoint(item) for item in _limit_rows(result.get("endpoints") or [], MAX_AI_CONTEXT_ENDPOINTS)]
    api_calls = [_compact_api_call(item) for item in _limit_rows(result.get("api_calls") or [], MAX_AI_CONTEXT_APIS)]
    findings = [_compact_finding(item) for item in _limit_rows(result.get("findings") or [], MAX_AI_CONTEXT_FINDINGS)]
    files = [_compact_file(item) for item in _limit_rows(result.get("files") or [], MAX_AI_CONTEXT_FILES)]
    keyword_endpoints = _keyword_endpoints(result.get("endpoints") or [])
    file_type_counts = Counter(str(item.get("type") or "file") for item in result.get("files") or [])

    summary = {
        "app_id": app_id,
        "source_path": source_path,
        "filename": result.get("filename", ""),
        "format": result.get("format", ""),
        "package": {
            "package_count": package.get("package_count", 1),
            "file_count": package.get("file_count", 0),
            "total_size": package.get("total_size", 0),
        },
        "counts": {
            "domains": len(result.get("domains") or []),
            "endpoints": len(result.get("endpoints") or []),
            "wx_api_calls": len(result.get("api_calls") or []),
            "findings": len(result.get("findings") or []),
            "files": len(result.get("files") or []),
        },
        "file_type_counts": dict(file_type_counts.most_common(30)),
    }

    # app.json 结构化画像：页面/分包/权限/插件 + 冷路径路由（tabBar 不可达）
    profile = result.get("app_profile") or {}
    route_block: list[str] = []
    if profile.get("has_app_json"):
        route_block = [
            '## 页面路由画像（来自 app.json）',
            f"- 页面总数: {profile.get('total_routes', 0)} | 冷路径(非 tabBar 可达): {profile.get('cold_route_count', 0)}",
        ]
        if profile.get("pages"):
            route_block.append(f"- 主包页面: {', '.join(profile['pages'][:80])}")
        for sp in profile.get("sub_packages") or []:
            route_block.append(f"- 分包[{sp.get('root','')}]: {', '.join((sp.get('pages') or [])[:60])}")
        if profile.get("permission"):
            route_block.append(f"- 声明的权限: {', '.join(profile['permission'][:30])}")
        if profile.get("plugins"):
            route_block.append(f"- 插件: {', '.join(profile['plugins'][:30])}")
        if profile.get("cold_routes"):
            route_block.append(
                f"- 冷路径路由(未在 tabBar，通常只经 navigateTo/分包可达，"
                f"更可能缺少鉴权): {', '.join(profile['cold_routes'][:40])}"
            )
        route_block += ["", "测试建议：冷路径页面往往缺少统一鉴权拦截，优先探测。", ""]

    return "\n".join(
        [
            '# 小程序 wxapkg 静态分析上下文',
            "",
            '该上下文来自 Sharp 内置 wxapkg 扫描、解密、解析和静态特征提取结果，用于后续授权安全分析。当前没有导入 HTTP 抓包流量；所有结论必须区分\'静态可疑风险\'和\'需要抓包/测试账号验证\'。',
            "",
            '## 概览',
            _json_block(summary),
            "",
            *route_block,
            '## 已发现风险点',
            _json_block(findings),
            "",
            '## 域名资产',
            _json_block(domains),
            "",
            '## 关键词命中接口候选',
            _json_block(keyword_endpoints),
            "",
            '## 接口与路径样本',
            _json_block(endpoints),
            "",
            '## wx API 调用统计',
            _json_block(api_calls),
            "",
            '## 文件样本',
            _json_block(files),
            "",
            '## 解析备注',
            _json_block(_limit_rows(result.get("notes") or [], 80)),
        ]
    )


def build_static_ai_intent_description(result: dict[str, Any]) -> str:
    package = result.get("package") or {}
    app_id = str(package.get("app_id") or _first_app_id_from_result(result) or '未知 AppID')
    return (
        f'基于小程序 {app_id} 的 wxapkg 静态分析上下文，生成一份面向授权测试的 AI 安全分析报告和后续测试清单。'
        '必须覆盖：1) 小程序资产画像与后端域名归类；2) 登录、鉴权、用户信息、订单、支付、上传、下载、文件、管理类接口的风险优先级；'
        '3) 已发现静态风险点的可信度判断；4) 需要抓包或测试账号验证的事项；5) 可执行的下一步测试任务清单。'
        '不要把静态特征直接判定为已确认漏洞；没有数据包时明确标注验证条件。'
    )


def build_static_ai_project_seed(result: dict[str, Any], source_path: str) -> dict[str, str]:
    package = result.get("package") or {}
    app_id = str(package.get("app_id") or _first_app_id_from_result(result) or "unknown")
    filename = str(result.get("filename") or app_id)
    return {
        "title": f'小程序静态安全分析 - {app_id}',
        "origin": (
            f'从本机微信缓存或上传包中解析小程序 {filename}。'
            f'来源路径：{source_path}。已完成 wxapkg 静态解析，包含域名、接口、wx API、文件列表和初步风险点。'
        ),
        "goal": (
            '基于 wxapkg 静态分析结果完成小程序安全分析，输出资产画像、高风险接口优先级、疑似风险说明、'
            '需要动态抓包验证的事项，以及后续授权渗透测试任务清单。'
        ),
    }


# --- HAR dynamic-traffic analysis (#18-24) -----------------------------------
MAX_HAR_ENTRIES = 2000
MAX_AI_CONTEXT_REQUESTS = 120
MAX_AI_CONTEXT_IDOR = 80

# Header names that typically carry an auth token / session identifier.
AUTH_HEADER_NAMES = (
    "authorization",
    "cookie",
    "x-auth-token",
    "x-token",
    "token",
    "access-token",
    "x-access-token",
    "x-session",
    "x-session-id",
    "session",
    "x-api-key",
    "apikey",
    "x-wx-openid",
    "x-user-id",
)

# Query/body param names that reference an object or identity — IDOR candidates.
IDOR_PARAM_RE = re.compile(
    r"(?i)^(?:.*[_\-.])?(id|uid|userid|user_id|memberid|member_id|orderid|order_id|"
    r"openid|unionid|accountid|account_id|customerid|shopid|store_id|fileid|file_id|"
    r"docid|recordid|pid|gid|tid|sid|serial)$"
)


def parse_har(data: bytes) -> dict[str, Any]:
    """Parse a HAR capture into a structured dynamic-analysis result.

    Extracts the request inventory (dedup by method+path), the auth mechanism
    in use (Bearer / Cookie / custom header), IDOR candidate parameters, and
    sensitive fields leaked in responses. Mirrors the shape of the static
    wxapkg analysis result so the same project-seeding path can consume it.
    """
    try:
        har = json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f'HAR 不是合法 JSON：{exc}') from exc

    log = har.get("log") if isinstance(har, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError('HAR 中没有 log.entries，无法解析流量')

    requests: list[dict[str, Any]] = []
    domain_counter: Counter[tuple[str, str]] = Counter()
    idor_candidates: list[dict[str, Any]] = []
    idor_seen: set[tuple[str, str]] = set()
    findings: list[dict[str, Any]] = []
    auth_counter: Counter[str] = Counter()
    request_seen: set[tuple[str, str]] = set()

    for entry in entries[:MAX_HAR_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        parsed = _parse_har_entry(entry)
        if parsed is None:
            continue
        url = parsed["url"]
        method = parsed["method"]
        host = parsed["host"]
        scheme = parsed["scheme"]
        path = parsed["path"]

        if host:
            domain_counter[(host, scheme)] += 1
        if scheme == "http" and host:
            findings.append(
                {
                    "severity": "medium",
                    "title": '接口通过明文 HTTP 传输',
                    "detail": '抓包中该接口使用明文 HTTP，token 和业务数据可被中间人截获或篡改。',
                    "file": f"{method} {path}",
                    "line": None,
                    "evidence": url,
                }
            )

        key = (method, _normalize_path(path))
        if key not in request_seen:
            request_seen.add(key)
            requests.append(
                {
                    "method": method,
                    "url": url,
                    "host": host,
                    "path": path,
                    "scheme": scheme,
                    "auth": parsed["auth_kind"],
                    "status": parsed["status"],
                    "req_params": parsed["param_names"],
                    "mime": parsed["resp_mime"],
                }
            )

        if parsed["auth_kind"]:
            auth_counter[parsed["auth_kind"]] += 1

        for cand in parsed["idor_params"]:
            ck = (cand["name"], _normalize_path(path))
            if ck in idor_seen:
                continue
            idor_seen.add(ck)
            idor_candidates.append(
                {
                    "param": cand["name"],
                    "value_sample": _truncate_text(cand["value"], 60),
                    "location": cand["location"],
                    "method": method,
                    "path": path,
                }
            )

        findings.extend(parsed["secret_findings"])

    domains = [
        {"host": host, "scheme": scheme, "count": count}
        for (host, scheme), count in domain_counter.most_common(MAX_AI_CONTEXT_DOMAINS)
    ]
    auth_summary = _summarize_auth(auth_counter, requests)

    return {
        "format": "har",
        "filename": "",
        "counts": {
            "entries": len(entries),
            "requests": len(requests),
            "domains": len(domains),
            "idor_candidates": len(idor_candidates),
            "findings": len(findings),
        },
        "auth": auth_summary,
        "domains": domains,
        "requests": requests,
        "idor_candidates": idor_candidates,
        "findings": findings,
    }


def _parse_har_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    request = entry.get("request")
    if not isinstance(request, dict):
        return None
    url = _clean_url(str(request.get("url") or ""))
    if not url:
        return None
    method = str(request.get("method") or "GET").upper()
    parsed_url = urlparse(url)
    host = parsed_url.hostname or ""
    scheme = parsed_url.scheme or ""
    path = parsed_url.path or "/"

    headers = _har_name_value(request.get("headers"))
    auth_kind = _detect_auth_kind(headers)

    param_names: list[str] = []
    idor_params: list[dict[str, Any]] = []
    for name, value in _har_name_value(request.get("queryString")):
        param_names.append(name)
        if IDOR_PARAM_RE.match(name):
            idor_params.append({"name": name, "value": value, "location": "query"})
    post = request.get("postData") if isinstance(request.get("postData"), dict) else None
    if post:
        for name, value in _har_name_value(post.get("params")):
            param_names.append(name)
            if IDOR_PARAM_RE.match(name):
                idor_params.append({"name": name, "value": value, "location": "body"})
        body_text = str(post.get("text") or "")
        if body_text:
            for name, value in _json_body_params(body_text):
                param_names.append(name)
                if IDOR_PARAM_RE.match(name):
                    idor_params.append({"name": name, "value": value, "location": "body-json"})

    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    status = int(response.get("status") or 0)
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    resp_mime = str(content.get("mimeType") or "")
    resp_text = str(content.get("text") or "")
    secret_findings = _scan_response_secrets(resp_text, f"{method} {path}")

    return {
        "url": url,
        "method": method,
        "host": host,
        "scheme": scheme,
        "path": path,
        "auth_kind": auth_kind,
        "status": status,
        "resp_mime": resp_mime,
        "param_names": sorted(set(param_names))[:40],
        "idor_params": idor_params,
        "secret_findings": secret_findings,
    }


def analyze_package_directory(directory: Path) -> dict[str, Any]:
    package_files = _wxapkg_files_under_directory(directory)
    if not package_files:
        raise ValueError('目录下没有找到 wxapkg 文件')
    combined_files: list[PackageFile] = []
    findings: list[dict[str, Any]] = []
    notes: list[str] = [f'目录分析：{directory}']
    total_size = 0
    analyzed_count = 0

    for package_path in package_files[:MAX_ANALYZE_PACKAGE_FILES]:
        encrypt_key = _infer_app_id_from_path(package_path)
        try:
            data, encrypted = _read_package_bytes(package_path, encrypt_key=encrypt_key, decrypt=True)
        except ValueError as exc:
            notes.append(f"{package_path.name}: {exc}")
            continue
        total_size += len(data)
        sub_result = analyze_package(package_path.name, data)
        analyzed_count += 1
        prefix = _relative_posix(directory, package_path.parent)
        if encrypted:
            notes.append(f'{package_path.name}: 已使用 {encrypt_key} 解密。')
        parsed_files: dict[str, PackageFile] = {}
        if _looks_like_wxapkg(data):
            try:
                for parsed_item in _parse_wxapkg(data)[0]:
                    parsed_files[parsed_item.path] = parsed_item
            except ValueError:
                parsed_files = {}
        for item in sub_result.get("files", []):
            inner_path = item.get("path", "")
            parsed_item = parsed_files.get(inner_path) or parsed_files.get("/" + str(inner_path).lstrip("/"))
            combined_files.append(
                PackageFile(
                    path=f"/{prefix}/{str(inner_path).lstrip('/')}".replace("//", "/"),
                    offset=int(item.get("offset") or 0),
                    size=int(item.get("size") or 0),
                    data=parsed_item.data if parsed_item else None,
                    unsafe_path=bool(item.get("unsafe_path")),
                )
            )
        for finding in sub_result.get("findings", []):
            finding = dict(finding)
            if finding.get("file"):
                finding["file"] = f"/{prefix}/{str(finding['file']).lstrip('/')}".replace("//", "/")
            findings.append(finding)
        notes.extend(sub_result.get("notes", []))

    if len(package_files) > MAX_ANALYZE_PACKAGE_FILES:
        notes.append(f'目录内 wxapkg 数量为 {len(package_files)}，本次只分析前 {MAX_ANALYZE_PACKAGE_FILES} 个。')

    scanned = _scan_files(combined_files)
    all_findings = findings + scanned["findings"]
    all_findings.sort(key=lambda item: {"high": 0, "medium": 1, "info": 2}.get(item.get("severity"), 3))
    app_id_match = APP_ID_RE.search(str(directory))
    return {
        "filename": directory.name,
        "format": "wxapkg_directory",
        "package": {
            "file_count": len(combined_files),
            "total_size": total_size,
            "index_length": 0,
            "body_length": 0,
            "package_count": analyzed_count,
            "app_id": app_id_match.group(0) if app_id_match else "",
        },
        "files": [_file_to_dict(item) for item in combined_files[:MAX_FILE_COUNT]],
        "domains": scanned["domains"],
        "endpoints": scanned["endpoints"],
        "api_calls": scanned["api_calls"],
        "findings": all_findings[:500],
        "app_profile": _parse_app_profile(combined_files),
        "notes": notes,
    }


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_like_wxapkg(data: bytes) -> bool:
    return len(data) >= 18 and data[0] == 0xBE and data[13] == 0xED


def _looks_encrypted_wxapkg(data: bytes) -> bool:
    return data.startswith(WXAPKG_ENCRYPTED_MAGIC)


def _read_package_bytes(path: Path, *, encrypt_key: str | None = None, decrypt: bool = True) -> tuple[bytes, bool]:
    data = path.read_bytes()
    if not _looks_encrypted_wxapkg(data):
        return data, False
    if not decrypt:
        raise ValueError('检测到加密 wxapkg，但当前请求未启用解密')
    key = _normalize_encrypt_key(encrypt_key) or _infer_app_id_from_path(path)
    if not key:
        raise ValueError('检测到加密 wxapkg，但无法从路径推断 AppID 解密 key')
    return _decrypt_wxapkg_data(key, data), True


def _decrypt_wxapkg_data(app_id: str, data: bytes) -> bytes:
    app_id = _normalize_encrypt_key(app_id) or ""
    if not app_id:
        raise ValueError('AppID 解密 key 不合法')
    required = WXAPKG_DECRYPT_OFFSET + WXAPKG_DECRYPT_BLOCK_SIZE
    if len(data) <= required:
        raise ValueError('加密 wxapkg 文件太小，无法解密')

    key = hashlib.pbkdf2_hmac("sha1", app_id.encode("utf-8"), WXAPKG_DECRYPT_SALT, 1000, dklen=32)
    cipher = Cipher(algorithms.AES(key), modes.CBC(WXAPKG_DECRYPT_IV))
    decryptor = cipher.decryptor()
    first_block = decryptor.update(data[WXAPKG_DECRYPT_OFFSET:required]) + decryptor.finalize()
    xor_key = app_id.encode("utf-8")[-2] if len(app_id) >= 2 else 0x66
    tail = bytes(byte ^ xor_key for byte in data[required:])
    decrypted = first_block[:1023] + tail
    if not _looks_like_wxapkg(decrypted):
        raise ValueError('wxapkg 解密失败：AppID key 不匹配或包结构不合法')
    return decrypted


def _parse_unpackable_package(package_path: Path, data: bytes) -> tuple[list[PackageFile], dict[str, Any], list[str]]:
    if _looks_like_wxapkg(data):
        return _parse_wxapkg(data)
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _parse_zip(data, read_all=True)
    raise ValueError('未识别为 wxapkg 或 zip')


def _parse_wxapkg(data: bytes) -> tuple[list[PackageFile], dict[str, Any], list[str]]:
    if len(data) < 18:
        raise ValueError('wxapkg 文件太小')

    first_mark = data[0]
    info1 = _u32(data, 1)
    index_length = _u32(data, 5)
    body_length = _u32(data, 9)
    last_mark = data[13]
    file_count = _u32(data, 14)
    if first_mark != 0xBE or last_mark != 0xED:
        raise ValueError('wxapkg 头部标记不正确')
    if file_count > MAX_FILE_COUNT:
        raise ValueError(f'wxapkg 文件数量过大：{file_count}')

    notes: list[str] = []
    cursor = 18
    index_end = cursor + index_length
    if index_end > len(data):
        notes.append('索引长度超过文件大小，已按实际可读范围解析。')
        index_end = len(data)

    files: list[PackageFile] = []
    for _ in range(file_count):
        if cursor + 12 > index_end:
            notes.append('索引区提前结束，部分文件未解析。')
            break
        name_len = _u32(data, cursor)
        cursor += 4
        if name_len > 4096 or cursor + name_len + 8 > index_end:
            notes.append('发现异常文件名长度，停止解析索引。')
            break
        raw_name = data[cursor : cursor + name_len]
        cursor += name_len
        offset = _u32(data, cursor)
        size = _u32(data, cursor + 4)
        cursor += 8

        path = raw_name.decode("utf-8", errors="replace")
        unsafe = _is_unsafe_package_path(path)
        content = None
        if offset + size <= len(data):
            content = data[offset : offset + size]
        else:
            notes.append(f'{path} 的偏移或长度超过包大小，已跳过内容扫描。')
        files.append(PackageFile(path=path, offset=offset, size=size, data=content, unsafe_path=unsafe))

    header = {
        "info1": info1,
        "index_length": index_length,
        "body_length": body_length,
        "file_count": file_count,
    }
    return files, header, notes


def _parse_zip(data: bytes, *, read_all: bool = False) -> tuple[list[PackageFile], dict[str, Any], list[str]]:
    files: list[PackageFile] = []
    notes: list[str] = ['按 zip 源码包解析，适合分析 wxapkg 外部工具还原后的结果。']
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_FILE_COUNT:
            raise ValueError(f'zip 文件数量过大：{len(infos)}')
        for info in infos:
            path = "/" + info.filename.lstrip("/")
            size = int(info.file_size)
            content = None
            if read_all and size <= MAX_UNPACK_FILE_BYTES:
                with archive.open(info) as fh:
                    content = fh.read(MAX_UNPACK_FILE_BYTES + 1)
            elif _is_text_path(path) and size <= MAX_TEXT_BYTES:
                with archive.open(info) as fh:
                    content = fh.read(MAX_TEXT_BYTES + 1)
            files.append(
                PackageFile(
                    path=path,
                    offset=0,
                    size=size,
                    data=content,
                    unsafe_path=_is_unsafe_package_path(path),
                )
            )
    return files, {"file_count": len(files)}, notes


_APP_JSON_NAMES = ("app.json", "/app.json", "app-config.json")


def _parse_app_profile(files: list[PackageFile]) -> dict[str, Any]:
    """Parse app.json into a structured route / permission / plugin profile.

    The generic scan treats .json files as plain text and regex-scans them; this
    extracts the *structure* (which pages exist, which are only reachable via
    subPackages / navigateTo rather than tabBar) so the AI knows the full attack
    surface — including cold paths that never show in the tab bar.
    """
    import json as _json

    app_text: str | None = None
    for item in files:
        if item.path in _APP_JSON_NAMES or item.path.rstrip("/").endswith("/app.json"):
            t = _decode_text_file(item)
            if t:
                app_text = t
                break
    profile: dict[str, Any] = {
        "has_app_json": app_text is not None,
        "pages": [],
        "sub_packages": [],
        "permission": [],
        "plugins": [],
        "total_routes": 0,
        "cold_route_count": 0,
    }
    if not app_text:
        return profile

    try:
        data = _json.loads(app_text)
    except ValueError:
        profile["parse_error"] = "app.json 不是合法 JSON"
        return profile
    if not isinstance(data, dict):
        return profile

    pages = data.get("pages") or []
    profile["pages"] = [str(p) for p in pages if isinstance(p, str)][:200]

    for sub in data.get("subPackages") or data.get("subpackages") or []:
        if not isinstance(sub, dict):
            continue
        root = str(sub.get("root", "")).strip("/")
        sub_pages = [str(p) for p in (sub.get("pages") or []) if isinstance(p, str)]
        profile["sub_packages"].append({"root": root, "pages": sub_pages})
        pages += [f"{root}/{p}" if root else p for p in sub_pages]

    # permission: { scope.userLocation: { desc: "..." } } → ["scope.userLocation: 说明"]
    for scope, cfg in (data.get("permission") or {}).items():
        desc = ""
        if isinstance(cfg, dict):
            desc = str(cfg.get("desc", ""))
        profile["permission"].append(f"{scope}" + (f": {desc}" if desc else ""))

    # plugins: { name: { version, provider } }
    for name, cfg in (data.get("plugins") or {}).items():
        provider = cfg.get("provider", "") if isinstance(cfg, dict) else ""
        profile["plugins"].append(f"{name} (provider: {provider})")

    profile["pages"] = list(dict.fromkeys(profile["pages"]))
    total = len(profile["pages"]) + sum(len(sp["pages"]) for sp in profile["sub_packages"])
    profile["total_routes"] = total
    # tabBar 页面可直达；其余算冷路径（只经 navigateTo / 分包可达）
    tab_pages = set()
    tabbar = data.get("tabBar") or {}
    for tl in (tabbar.get("list") or []) if isinstance(tabbar, dict) else []:
        if isinstance(tl, dict) and tl.get("pagePath"):
            tab_pages.add(str(tl["pagePath"]))
    all_routes = set(profile["pages"]) | {
        f"{sp['root']}/{p}" if sp["root"] else p
        for sp in profile["sub_packages"] for p in sp["pages"]
    }
    profile["cold_route_count"] = len(all_routes - tab_pages)
    profile["cold_routes"] = sorted(all_routes - tab_pages)[:100]
    return profile


def _scan_files(files: list[PackageFile]) -> dict[str, list[dict[str, Any]]]:
    domain_counter: Counter[tuple[str, str]] = Counter()
    endpoint_seen: set[tuple[str, str]] = set()
    endpoints: list[dict[str, Any]] = []
    api_counter: Counter[str] = Counter()
    api_files: defaultdict[str, set[str]] = defaultdict(set)
    findings: list[dict[str, Any]] = []

    for item in files:
        if item.unsafe_path:
            findings.append(
                {
                    "severity": "high",
                    "title": '包内存在危险文件路径',
                    "detail": '该路径包含绝对路径、上级目录或 Windows 分隔符。解包工具如果不做清理，可能造成任意文件写入。',
                    "file": item.path,
                    "line": None,
                    "evidence": item.path,
                }
            )

        text = _decode_text_file(item)
        if text is None:
            continue

        for match in URL_RE.finditer(text):
            url = _clean_url(match.group(0))
            parsed = urlparse(url)
            if not parsed.hostname:
                continue
            domain_counter[(parsed.hostname, parsed.scheme)] += 1
            key = (url, item.path)
            if key not in endpoint_seen:
                endpoint_seen.add(key)
                endpoints.append(
                    {
                        "url": url,
                        "host": parsed.hostname,
                        "scheme": parsed.scheme,
                        "file": item.path,
                        "line": _line_number(text, match.start()),
                    }
                )
            if parsed.scheme == "http":
                findings.append(
                    {
                        "severity": "medium",
                        "title": '使用明文 HTTP 接口',
                        "detail": '小程序请求明文 HTTP 接口会暴露请求内容，并且容易被中间人篡改。',
                        "file": item.path,
                        "line": _line_number(text, match.start()),
                        "evidence": url,
                    }
                )

        for match in ENDPOINT_RE.finditer(text):
            endpoint = match.group(0).rstrip(".,;")
            key = (endpoint, item.path)
            if key not in endpoint_seen and len(endpoint) >= 5:
                endpoint_seen.add(key)
                endpoints.append(
                    {
                        "url": endpoint,
                        "host": "",
                        "scheme": "relative",
                        "file": item.path,
                        "line": _line_number(text, match.start()),
                    }
                )

        for match in WX_API_RE.finditer(text):
            api = f"wx.{match.group(1)}"
            api_counter[api] += 1
            api_files[api].add(item.path)

        for match in APP_ID_RE.finditer(text):
            findings.append(
                {
                    "severity": "info",
                    "title": '发现小程序 AppID',
                    "detail": 'AppID 本身通常不是密钥，但可用于资产关联和接口归属判断。',
                    "file": item.path,
                    "line": _line_number(text, match.start()),
                    "evidence": match.group(0),
                }
            )

        for match in SECRET_KEY_RE.finditer(text):
            findings.append(
                {
                    "severity": "high",
                    "title": '疑似硬编码敏感凭据',
                    "detail": f'发现 {match.group(1)} 字段硬编码在前端包内，需要确认是否为真实凭据。',
                    "file": item.path,
                    "line": _line_number(text, match.start()),
                    "evidence": _mask_secret(match.group(2)),
                }
            )

        for match in PRIVATE_KEY_RE.finditer(text):
            findings.append(
                {
                    "severity": "high",
                    "title": '疑似私钥内容',
                    "detail": '前端包内不应包含私钥材料。',
                    "file": item.path,
                    "line": _line_number(text, match.start()),
                    "evidence": match.group(0),
                }
            )

        if re.search(r"\beval\s*\(", text):
            findings.append(
                {
                    "severity": "medium",
                    "title": '发现 eval 调用',
                    "detail": '动态执行代码会放大 XSS、供应链和反调试绕过风险。',
                    "file": item.path,
                    "line": None,
                    "evidence": "eval(...)",
                }
            )

    domains = [
        {"host": host, "scheme": scheme, "count": count}
        for (host, scheme), count in domain_counter.most_common(200)
    ]
    api_calls = [
        {"api": api, "count": count, "files": sorted(api_files[api])[:20]}
        for api, count in api_counter.most_common(200)
    ]
    endpoints.sort(key=lambda item: (item["host"] or "~", item["url"], item["file"]))
    findings.sort(key=lambda item: {"high": 0, "medium": 1, "info": 2}.get(item["severity"], 3))
    return {
        "domains": domains,
        "endpoints": endpoints[:500],
        "api_calls": api_calls,
        "findings": findings[:500],
    }


def _u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise ValueError('wxapkg 结构不完整')
    return struct.unpack(">L", data[offset : offset + 4])[0]


def _is_text_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES


def _decode_text_file(item: PackageFile) -> str | None:
    if item.data is None or not _is_text_path(item.path) or len(item.data) > MAX_TEXT_BYTES:
        return None
    if b"\x00" in item.data[:4096]:
        return None
    try:
        return item.data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return item.data.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            return None


def _is_unsafe_package_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if "\\" in path or "\x00" in path:
        return True
    if normalized.startswith("/") and len(parts) > 0:
        return False
    if any(part == ".." for part in parts):
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    cleaned = posixpath.normpath("/" + normalized.lstrip("/"))
    return cleaned.startswith("/../") or cleaned == "/.."


def _file_to_dict(item: PackageFile) -> dict[str, Any]:
    suffix = PurePosixPath(item.path).suffix.lower().lstrip(".") or "file"
    return {
        "path": item.path,
        "size": item.size,
        "offset": item.offset,
        "type": suffix,
        "text": _is_text_path(item.path),
        "unsafe_path": item.unsafe_path,
    }


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:'\")]}<>")


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_secret(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _limit_rows(rows: list[Any], limit: int) -> list[Any]:
    return rows[: max(0, limit)]


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _truncate_text(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _compact_domain(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": _truncate_text(item.get("host"), 180),
        "scheme": item.get("scheme", ""),
        "count": item.get("count", 0),
    }


def _compact_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": _truncate_text(item.get("url"), 260),
        "host": _truncate_text(item.get("host"), 180),
        "scheme": item.get("scheme", ""),
        "file": _truncate_text(item.get("file"), 180),
        "line": item.get("line"),
    }


def _compact_api_call(item: dict[str, Any]) -> dict[str, Any]:
    files = item.get("files") or []
    return {
        "api": item.get("api", ""),
        "count": item.get("count", 0),
        "files": [_truncate_text(path, 160) for path in files[:8]],
    }


def _compact_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": item.get("severity", ""),
        "title": _truncate_text(item.get("title"), 120),
        "detail": _truncate_text(item.get("detail"), 260),
        "file": _truncate_text(item.get("file"), 180),
        "line": item.get("line"),
        "evidence": _truncate_text(item.get("evidence"), 260),
    }


def _compact_file(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _truncate_text(item.get("path"), 220),
        "type": item.get("type", ""),
        "size": item.get("size", 0),
        "text": item.get("text", False),
        "unsafe_path": item.get("unsafe_path", False),
    }


def _first_app_id_from_result(result: dict[str, Any]) -> str | None:
    filename = str(result.get("filename") or "")
    match = APP_ID_RE.search(filename)
    if match:
        return match.group(0).lower()
    for item in result.get("findings") or []:
        evidence = str(item.get("evidence") or "")
        match = APP_ID_RE.search(evidence)
        if match:
            return match.group(0).lower()
    for item in result.get("files") or []:
        path = str(item.get("path") or "")
        match = APP_ID_RE.search(path)
        if match:
            return match.group(0).lower()
    return None


def _keyword_endpoints(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in endpoints:
        url = str(item.get("url") or "")
        file_path = str(item.get("file") or "")
        lowered = f"{url} {file_path}".lower()
        keyword = next((word for word in AI_ENDPOINT_KEYWORDS if word in lowered), "")
        if not keyword:
            continue
        key = (url, file_path)
        if key in seen:
            continue
        seen.add(key)
        selected.append(
            {
                "keyword": keyword,
                "url": _truncate_text(url, 260),
                "host": _truncate_text(item.get("host"), 180),
                "scheme": item.get("scheme", ""),
                "file": _truncate_text(file_path, 180),
                "line": item.get("line"),
            }
        )
        if len(selected) >= MAX_AI_CONTEXT_ENDPOINTS:
            break
    return selected


def _har_name_value(items: Any) -> list[tuple[str, str]]:
    """Normalize a HAR name/value array (headers/queryString/params)."""
    out: list[tuple[str, str]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append((name, str(item.get("value") or "")))
    return out


def _detect_auth_kind(headers: list[tuple[str, str]]) -> str:
    """Classify the auth mechanism from request headers (#24)."""
    for name, value in headers:
        lname = name.lower()
        if lname == "authorization":
            return "bearer" if value.lower().startswith("bearer ") else "authorization"
        if lname == "cookie" and value:
            return "cookie"
    for name, _ in headers:
        if name.lower() in AUTH_HEADER_NAMES:
            return f"header:{name.lower()}"
    return ""


def _summarize_auth(counter: Counter[str], requests: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(counter.values())
    primary = counter.most_common(1)[0][0] if counter else ""
    return {
        "detected": bool(counter),
        "primary": primary,
        "mechanisms": dict(counter.most_common()),
        "authenticated_requests": total,
        "total_requests": len(requests),
    }


def _normalize_path(path: str) -> str:
    """Collapse numeric path segments so /order/123 and /order/456 dedup together."""
    parts = []
    for seg in path.split("/"):
        parts.append("{id}" if seg.isdigit() and len(seg) >= 2 else seg)
    return "/".join(parts)


def _json_body_params(body_text: str) -> list[tuple[str, str]]:
    """Best-effort flatten of a JSON request body into (key, value) pairs."""
    try:
        obj = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if len(out) >= 60:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value)
                else:
                    out.append((str(key), str(value)))
        elif isinstance(node, list):
            for value in node[:20]:
                walk(value)

    walk(obj)
    return out


def _scan_response_secrets(resp_text: str, location: str) -> list[dict[str, Any]]:
    if not resp_text:
        return []
    text = resp_text[:MAX_TEXT_BYTES]
    findings: list[dict[str, Any]] = []
    for match in SECRET_KEY_RE.finditer(text):
        findings.append(
            {
                "severity": "high",
                "title": '响应中疑似泄露凭证/密钥',
                "detail": '抓包响应体中出现疑似 token/密钥/openid 等敏感字段，需确认是否越权可读或应脱敏。',
                "file": location,
                "line": None,
                "evidence": f"{match.group(1)}={_mask_secret(match.group(2))}",
            }
        )
        if len(findings) >= 20:
            break
    if PRIVATE_KEY_RE.search(text):
        findings.append(
            {
                "severity": "high",
                "title": '响应中包含私钥材料',
                "detail": '响应体出现 PRIVATE KEY 头，几乎可确定为密钥泄露。',
                "file": location,
                "line": None,
                "evidence": "-----BEGIN ... PRIVATE KEY-----",
            }
        )
    return findings


def build_dynamic_ai_analysis_context(
    result: dict[str, Any], source: str, *, has_static_context: bool = False
) -> str:
    auth = result.get("auth") or {}
    domains = [_compact_domain(item) for item in _limit_rows(result.get("domains") or [], MAX_AI_CONTEXT_DOMAINS)]
    requests = _limit_rows(result.get("requests") or [], MAX_AI_CONTEXT_REQUESTS)
    idor = _limit_rows(result.get("idor_candidates") or [], MAX_AI_CONTEXT_IDOR)
    findings = [_compact_finding(item) for item in _limit_rows(result.get("findings") or [], MAX_AI_CONTEXT_FINDINGS)]
    summary = {
        "source": source,
        "format": result.get("format", "har"),
        "counts": result.get("counts", {}),
        "auth": auth,
    }
    sections = [
        '# 小程序动态流量（HAR）分析上下文',
        "",
        '该上下文来自导入的 HTTP 抓包（HAR）。已提取接口清单、认证机制、IDOR 候选参数和响应敏感字段，用于授权动态安全测试。'
        '所有结论必须区分\'抓包中观察到的行为\'和\'需要用测试账号主动验证的越权/逻辑风险\'。',
        "",
        '## 概览',
        _json_block(summary),
        "",
        '## 认证机制',
        _json_block(auth),
        "",
        '## IDOR 越权候选参数（重点测试对象）',
        '以下参数引用了对象/身份 ID，是水平越权（IDOR）的首要测试点：换用另一账号的 token 或替换该参数值，对比响应差异。',
        _json_block(idor),
        "",
        '## 已发现风险点',
        _json_block(findings),
        "",
        '## 域名资产',
        _json_block(domains),
        "",
        '## 接口清单（method + path，已按数字段归并去重）',
        _json_block(requests),
    ]
    if has_static_context:
        sections += [
            "",
            '## ⚡ 静态+动态交叉分析指引（本项目已有 wxapkg 静态分析 fact）',
            '当前项目中同时存在静态分析上下文（wxapkg 代码提取）和动态流量上下文（本 HAR）。'
            '请在测试时主动交叉关联，不要独立测试：',
            '1. **用动态 token 验证静态高风险接口**：静态 fact 中标记的登录/支付/上传等接口，'
            '用本 HAR 中识别的 Bearer token 或 Cookie 发起真实请求，确认是否存在未授权访问或 IDOR。',
            '2. **补全冷路径**：静态 fact 中提取的接口路径如果在本动态 fact 的接口清单里缺失，'
            '说明这些是\'冷路径\'（用户未主动触发的功能）——主动构造请求测试，往往是越权漏洞高发区。',
            '3. **IDOR 候选参数对齐**：本 HAR 的 IDOR 候选参数（如 userId/orderId）可结合静态 fact '
            '中的参数命名规律（如混淆后的参数名）进行更全面的枚举测试。',
            '4. **凭证来源优先级**：如果本 HAR 中已有有效 token，优先写入 creds.env，'
            '不要重复从静态 fact 中寻找登录流程——静态分析已在先，现在用真实凭证直接测试业务逻辑。',
        ]
    return "\n".join(sections)


def build_dynamic_ai_intent_description(result: dict[str, Any]) -> str:
    auth = result.get("auth") or {}
    primary = auth.get("primary") or '未识别'
    return (
        f'基于导入的 HAR 动态流量上下文（主认证机制：{primary}），执行一份面向授权测试的业务逻辑安全分析。'
        '必须覆盖：1) 按接口清单梳理登录/鉴权/用户/订单/支付/上传等业务域；'
        '2) 对每个 IDOR 候选参数做水平越权对比测试（换用户 ID/token 比对响应）；'
        '3) 使用识别出的认证机制头做垂直越权测试（低权限 token 访问管理接口）；'
        '4) 对多步业务流程测试跳步绕过；5) 复核响应中疑似泄露的敏感字段。'
        '拿到 token 后先写入 /home/kali/workspace/creds.env 供复用；没有主动验证证据前，不要把抓包观察直接判定为已确认漏洞。'
    )


def build_dynamic_ai_project_seed(result: dict[str, Any], source: str) -> dict[str, str]:
    auth = result.get("auth") or {}
    counts = result.get("counts") or {}
    primary_host = ""
    domains = result.get("domains") or []
    if domains:
        primary_host = str(domains[0].get("host") or "")
    label = primary_host or "小程序动态流量"
    req_count = counts.get("requests", 0)
    auth_primary = auth.get("primary") or "未识别"
    idor_count = counts.get("idor_candidates", 0)
    return {
        "title": f"小程序动态安全分析 - {label}",
        "origin": (
            f"从抓包工具导出的 HAR 流量导入。来源：{source}。"
            f"共解析 {req_count} 个去重接口，主认证机制：{auth_primary}，"
            f"识别出 {idor_count} 个 IDOR 候选参数。"
        ),
        "goal": (
            '基于导入的动态流量完成小程序业务逻辑安全测试，重点覆盖水平越权(IDOR)、垂直越权、'
            '业务流程绕过和敏感数据泄露，输出已验证漏洞、复现证据和后续测试清单。'
        ),
    }


def _normalize_encrypt_key(value: str | None) -> str | None:
    if not value:
        return None
    match = APP_ID_RE.fullmatch(value.strip())
    return match.group(0).lower() if match else None


def _infer_app_id_from_path(path: Path) -> str | None:
    for part in [path.name, *(parent.name for parent in path.parents)]:
        match = APP_ID_RE.fullmatch(part)
        if match:
            return match.group(0).lower()
    match = APP_ID_RE.search(str(path))
    return match.group(0).lower() if match else None


def _safe_unpack_output_path(destination: Path, package_path: str, used_paths: set[Path]) -> Path:
    if "\x00" in package_path or "\\" in package_path:
        raise ValueError('包含不安全路径字符，已跳过。')
    raw_parts = [part for part in package_path.replace("\\", "/").split("/") if part]
    if any(part in ("..", ".") for part in raw_parts):
        raise ValueError('包含路径穿越片段，已跳过。')
    normalized = posixpath.normpath("/" + package_path.lstrip("/"))
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in ("..", ".") for part in parts):
        raise ValueError('包含路径穿越片段，已跳过。')
    if re.match(r"^[A-Za-z]:", package_path):
        raise ValueError('包含 Windows 绝对路径，已跳过。')

    candidate = (destination.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(destination)
    except ValueError as exc:
        raise ValueError('写出路径越过输出目录，已跳过。') from exc

    if candidate not in used_paths and not candidate.exists():
        used_paths.add(candidate)
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    parent = candidate.parent
    for index in range(1, 10000):
        renamed = (parent / f"{stem}-{index}{suffix}").resolve()
        try:
            renamed.relative_to(destination)
        except ValueError as exc:
            raise ValueError('写出路径越过输出目录，已跳过。') from exc
        if renamed not in used_paths and not renamed.exists():
            used_paths.add(renamed)
            return renamed
    raise ValueError('同名文件过多，已跳过。')


def _existing_wechat_roots() -> list[Path]:
    candidates = _platform_wechat_root_candidates()
    for raw in os.environ.get("SHARP_WECHAT_WXAPKG_DIRS", "").split(os.pathsep):
        text = raw.strip()
        if text:
            candidates.append(Path(text).expanduser())

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _host_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def _platform_wechat_root_candidates(system: str | None = None) -> list[Path]:
    host = _host_platform() if system is None else system.lower()
    if host in ("darwin", "macos"):
        return _macos_wechat_root_candidates()
    if host == "windows":
        return _windows_wechat_root_candidates()
    return _generic_wechat_root_candidates()


def _macos_wechat_root_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / "Library/Containers/com.tencent.xinWeChat/Data/.wxapplet/packages",
        home / "Library/Containers/com.tencent.xinWeChat/Data/.wxapplet/Applet",
        home / "Library/Containers/com.tencent.xinWeChat/Data/.wxapplet",
        home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium",
        home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
        home / "Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support",
        home / "Library/Containers/com.tencent.xinWeChat/Data/tmp",
        home / "Library/Containers/com.tencent.xinWeChat/Data",
        home / "Library/Application Support/com.tencent.xinWeChat",
        home / "Library/Application Support/Wechat",
        home / "Library/Application Support/WeChat",
    ]


def _windows_wechat_root_candidates() -> list[Path]:
    home = Path.home()
    user_profile = Path(os.environ.get("USERPROFILE") or str(home))
    documents = Path(os.environ.get("USERPROFILE") or str(home)) / "Documents"
    appdata = os.environ.get("APPDATA") or str(user_profile / "AppData/Roaming")
    local_appdata = os.environ.get("LOCALAPPDATA") or str(user_profile / "AppData/Local")
    candidates = [
        documents / "WeChat Files/Applet",
        documents / "WeChat Files",
        user_profile / "Documents/WeChat Files/Applet",
        user_profile / "Documents/WeChat Files",
        user_profile / "WeChat Files/Applet",
        user_profile / "WeChat Files",
    ]
    candidates.extend(
        [
            Path(appdata) / "Tencent/xwechat/radium",
            Path(appdata) / "Tencent/xwechat",
            Path(appdata) / "Tencent/WeChat/All Users/config",
            Path(appdata) / "Tencent/WeChat/All Users",
            Path(appdata) / "Tencent/WeChat/Applet",
            Path(appdata) / "Tencent/WeChat",
            Path(local_appdata) / "Tencent/xwechat/radium",
            Path(local_appdata) / "Tencent/xwechat",
            Path(local_appdata) / "Tencent/WeChat/Applet",
            Path(local_appdata) / "Tencent/WeChat",
        ]
    )
    return candidates


def _generic_wechat_root_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / "Documents/WeChat Files/Applet",
        home / "Documents/WeChat Files",
        home / ".config/wechat",
        home / ".local/share/wechat",
    ]


def _scan_root_for_wxapkg(root: Path, limit: int) -> tuple[list[dict[str, Any]], bool, int]:
    file_items: list[dict[str, Any]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    entries_seen = 0
    truncated = False

    while stack:
        directory, depth = stack.pop()
        if depth > MAX_SCAN_DEPTH:
            continue
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > MAX_SCAN_ENTRIES:
                        truncated = True
                        return _group_scan_items(file_items, root, limit), truncated, entries_seen
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if _directory_is_probably_relevant(entry.name, depth):
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if not _is_wxapkg_filename(entry.name):
                            continue
                        path = Path(entry.path).resolve()
                        stat = path.stat()
                        if stat.st_size <= 0:
                            continue
                        file_items.append(_package_scan_item(path, stat.st_size, stat.st_mtime, root))
                    except (OSError, ValueError):
                        continue
        except (OSError, PermissionError):
            continue

    return _group_scan_items(file_items, root, limit), truncated, entries_seen


def _directory_is_probably_relevant(name: str, depth: int) -> bool:
    if depth < 5:
        return True
    lowered = name.lower()
    if APP_ID_RE.fullmatch(name):
        return True
    if lowered.isdigit():
        return True
    return any(
        token in lowered
        for token in ("wx", "app", "applet", "pkg", "package", "packages", "mini", "wechat", "xwechat", "radium", "xweb", "service", "cache", "tmp")
    )


def _is_wxapkg_filename(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(".wxapkg") or bool(WXAPKG_NAME_RE.search(lowered))


def _package_scan_item(path: Path, size: int, mtime: float, root: Path) -> dict[str, Any]:
    path_text = str(path)
    app_id_match = APP_ID_RE.search(path_text)
    return {
        "path": path_text,
        "name": path.name,
        "size": size,
        "mtime": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mtime_ts": mtime,
        "app_id": app_id_match.group(0) if app_id_match else "",
        "root": str(root),
        "kind": "file",
        "package_count": 1,
    }


def _group_scan_items(items: list[dict[str, Any]], root: Path, limit: int) -> list[dict[str, Any]]:
    grouped: dict[Path, dict[str, Any]] = {}
    singles: list[dict[str, Any]] = []
    for item in items:
        path = Path(item["path"])
        app_dir = _nearest_app_id_directory(path)
        if app_dir is None:
            singles.append(item)
            continue
        app_id = app_dir.name
        current = grouped.get(app_dir)
        if current is None:
            current = {
                "path": str(app_dir),
                "name": app_id,
                "size": 0,
                "mtime": item["mtime"],
                "mtime_ts": item["mtime_ts"],
                "app_id": app_id,
                "root": str(root),
                "kind": "directory",
                "package_count": 0,
            }
            grouped[app_dir] = current
        current["size"] += int(item.get("size") or 0)
        current["package_count"] += 1
        if float(item.get("mtime_ts") or 0) > float(current.get("mtime_ts") or 0):
            current["mtime_ts"] = item["mtime_ts"]
            current["mtime"] = item["mtime"]

    results = list(grouped.values()) + singles
    results.sort(key=lambda item: item.get("mtime", ""), reverse=True)
    for item in results:
        item.pop("mtime_ts", None)
    return results[: max(limit * 2, limit)]


def _nearest_app_id_directory(path: Path) -> Path | None:
    for parent in [path.parent, *path.parents]:
        if APP_ID_RE.fullmatch(parent.name):
            return parent
    return None


def _wxapkg_files_under_directory(directory: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [name for name in dirnames if not name.endswith("_output") and not name.startswith(".")]
        for filename in filenames:
            if _is_wxapkg_filename(filename):
                path = Path(root) / filename
                try:
                    if path.stat().st_size > 0:
                        files.append(path)
                except OSError:
                    continue
    files.sort(key=lambda item: (item.name != "__APP__.wxapkg", str(item)))
    return files


def _relative_posix(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def _read_inner_file_from_package(package_path: Path, inner_path: str) -> bytes | None:
    try:
        data = package_path.read_bytes()
        if _looks_like_wxapkg(data):
            files, _, _ = _parse_wxapkg(data)
        elif zipfile.is_zipfile(io.BytesIO(data)):
            files, _, _ = _parse_zip(data)
        else:
            return None
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    normalized = "/" + str(inner_path).lstrip("/")
    for item in files:
        if item.path == normalized or item.path == inner_path:
            return item.data
    return None


def _path_is_allowed_wechat_package(path: Path) -> bool:
    if not path.exists():
        return False
    roots = _existing_wechat_roots()
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
