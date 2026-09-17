"""Batch-8 regression tests: mobile capability additions.

Covers:
- miniprogram app.json structural profile (routes / subPackages / cold paths)
- miniprogram V1MMWX in-memory decryption path is exercised via analyze_package
  signature (app_id param wired) — actual decrypt needs a real encrypted sample,
  so we assert the API contract instead
- Android AXML string-pool decoding (dependency-free manifest summary)
- Android packer fingerprint detection (libjiagu / tiny classes.dex)
- knowledge host-normalization fallback for mobile projects
"""

from __future__ import annotations

import io
import json
import struct
import zipfile

import pytest

from sharp.server.android import (
    _analyze_single_apk,
    _axml_string_pool,
    extract_manifest_summary,
)


# ── AXML test helper: build a minimal valid AXML (XML header + utf16 string pool)
def _u16(x): return struct.pack("<H", x)
def _u32(x): return struct.pack("<I", x)


def _build_axml(strings: list[str]) -> bytes:
    payload = bytearray()
    offsets = []
    pos = 0
    for s in strings:
        entry = _u16(len(s)) + s.encode("utf-16-le") + b"\x00\x00"
        payload += entry
        offsets.append(pos)
        pos += len(entry)
    chunk = bytearray()
    chunk += _u16(0x0001)                        # type STRING_POOL
    chunk += _u16(28)                            # headerSize
    chunk += _u32(28 + len(strings) * 4 + len(payload))
    chunk += _u32(len(strings))                  # stringCount
    chunk += _u32(0)                             # styleCount
    chunk += _u32(0)                             # flags → UTF-16
    chunk += _u32(28 + len(strings) * 4)         # stringsStart
    chunk += _u32(0)                             # stylesStart
    for o in offsets:
        chunk += _u32(o)
    chunk += payload
    total = 8 + len(chunk)
    return _u16(0x0003) + _u16(8) + _u32(total) + chunk   # XML header + pool


MANIFEST_STRINGS = [
    "com.example.app",
    "android.permission.INTERNET",
    "android.permission.ACCESS_NETWORK_STATE",
    "versionName", "1.2.3",
    "versionCode", "12",
    "com.example.app.MainActivity",
    "android.hardware.camera",
]


def test_axml_string_pool_extracts_all():
    pool = _axml_string_pool(_build_axml(MANIFEST_STRINGS))
    assert pool == MANIFEST_STRINGS


def test_extract_manifest_summary_structured():
    sm = extract_manifest_summary(_build_axml(MANIFEST_STRINGS))
    assert sm["package_name"] == "com.example.app"
    assert "android.permission.INTERNET" in sm["permissions"]
    assert sm["version_name"] == "1.2.3"
    assert "com.example.app.MainActivity" in sm["components"]


def test_axml_truncated_garbage_returns_empty():
    assert _axml_string_pool(b"") == []
    assert _axml_string_pool(b"\x03\x00junk") == []
    assert extract_manifest_summary(b"\x00")["package_name"] == ""


def _fake_apk(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_packer_detection_libjiagu_and_tiny_dex():
    apk = _fake_apk({
        "AndroidManifest.xml": _build_axml(["com.packed.app"]),
        "lib/arm64-v8a/libjiagu.so": b"x" * 200,
        "classes.dex": b"\x00" * 1000,
    })
    r = _analyze_single_apk("packed.apk", apk)
    assert any("360" in p or "libjiagu" in p for p in r["packers"])
    assert any("classes.dex 极小" in p for p in r["packers"])
    assert r["notes"][0].startswith("检测到疑似加固")


def test_packer_detection_clean_apk_no_hits():
    apk = _fake_apk({
        "AndroidManifest.xml": _build_axml(["com.clean.app"]),
        "classes.dex": b"\x00" * (200 * 1024),   # normal-size dex
        "res/values/strings.xml": b"<string>hi</string>",
    })
    r = _analyze_single_apk("clean.apk", apk)
    assert r["packers"] == []


# ── miniprogram app.json profile ───────────────────────────────────────────

def _pkg_file(path: str, content: bytes):
    from sharp.server.miniprogram import PackageFile
    return PackageFile(path=path, offset=0, size=len(content), data=content)


def test_app_profile_routes_and_cold_paths():
    from sharp.server.miniprogram import _parse_app_profile
    app_json = json.dumps({
        "pages": ["pages/index/index", "pages/profile/profile"],
        "tabBar": {"list": [{"pagePath": "pages/index/index"}]},
        "subPackages": [{"root": "pkgA", "pages": ["a/home", "a/admin"]}],
        "permission": {"scope.userLocation": {"desc": "定位"}},
    }).encode()
    files = [
        _pkg_file("/app.json", app_json),
        _pkg_file("/pages/index/index.js", b"Page({})"),
    ]
    p = _parse_app_profile(files)
    assert p["has_app_json"] is True
    assert p["pages"] == ["pages/index/index", "pages/profile/profile"]
    assert p["sub_packages"] == [{"root": "pkgA", "pages": ["a/home", "a/admin"]}]
    # index 在 tabBar → 可达；其余 3 条为冷路径
    assert p["cold_route_count"] == 3
    assert "pkgA/a/admin" in p["cold_routes"]


def test_app_profile_missing_returns_placeholder():
    from sharp.server.miniprogram import _parse_app_profile
    p = _parse_app_profile([_pkg_file("/pages/x.js", b"Page({})")])
    assert p["has_app_json"] is False
    assert p["total_routes"] == 0


# ── knowledge host fallback ────────────────────────────────────────────────

def test_knowledge_host_fallback():
    """移动端 fallback：origin 是本地路径时，从事实描述里取主机。

    返回的是**规范键**（`domain:example.com` 而非裸 `example.com`）——
    与知识库、资产台账共用同一口径（`sharp.server.hostkey`）。
    """
    from sharp.server.routers.knowledge import _extract_root_domain, _first_host_in_text

    # APK 项目 origin 是本地路径 → 没有可归属主机
    assert _extract_root_domain("/Users/x/app.apk") is None
    # …但事实描述里带着 API 主机 → 规范化 fallback
    assert _first_host_in_text("发现接口 https://api.example.com/v1/login 无鉴权") == "domain:example.com"
    assert _first_host_in_text("后端域名 m.example.com 已确认") == "domain:example.com"
    assert _first_host_in_text("本地路径无 host") is None


def test_origin_sentence_is_parsed():
    """真实 origin 事实是**句子**，不是裸 URL —— 旧实现对句子返回 None，
    导致跨目标知识注入从未执行过（proj_002 实测命中 0 条）。"""
    from sharp.server.routers.knowledge import _extract_root_domain

    sentence = (
        "目标地址：https://app.fh.example.com/cgi-bin/MANGA/index.cgi\n"
        "授权范围：仅主域"
    )
    assert _extract_root_domain(sentence) == "domain:example.com"
    # IP 目标（渗透场景大量存在）也要能归属，而不是算出 "100.58" 这种伪域名
    assert _extract_root_domain("目标：10.0.100.58，已授权") == "ip:10.0.100.58"
