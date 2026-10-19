"""
session_manager.py — Sharp 多账号 HTTP 会话管理工具
预置路径：/home/kali/tools/session_manager.py

快速开始：
    import sys; sys.path.insert(0, '/home/kali/tools')
    from session_manager import SessionManager

    sm = SessionManager.from_env()           # 读 creds.env 里的 TOKEN / USER_ID
    sm.add('bob', token='BOB_TOKEN', user_id='bob_id')

    # 对比两个账号访问同一资源（IDOR检测）
    result = sm.compare('GET', 'https://target/api/order/123')
    sm.print_compare(result)

    # 批量扫描 ID 范围（水平越权）
    hits = sm.idor_scan('GET', 'https://target/api/order/{id}', range(1, 500))
    sm.print_hits(hits)

    # 并发竞态测试（优惠券/库存）
    r = sm.race('POST', 'https://target/api/coupon/use', data={'couponId': 'ABC'})
    print(r['status_counts'])

stdlib only — no external dependencies.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """One named HTTP session (token/cookie/custom-header based)."""

    name: str
    token: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    user_id: str | None = None

    def auth_headers(self) -> dict[str, str]:
        """Return all auth-related headers for this session."""
        h = dict(self.headers)
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Multi-account HTTP session manager for privilege escalation testing."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ── factory ────────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, path: str = "/home/kali/workspace/creds.env") -> "SessionManager":
        """Load the primary session from a creds.env file (KEY=VALUE format)."""
        sm = cls()
        env: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        env[k.strip()] = v.strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        token = env.get("TOKEN") or env.get("ACCESS_TOKEN") or env.get("JWT")
        user_id = env.get("USER_ID") or env.get("UID")
        cookie_str = env.get("COOKIE") or env.get("SESSION_COOKIE")
        cookies: dict[str, str] = {}
        if cookie_str:
            for part in cookie_str.split(";"):
                if "=" in part:
                    ck, _, cv = part.strip().partition("=")
                    cookies[ck] = cv
        sm.add("primary", token=token, cookies=cookies, user_id=user_id)
        return sm

    # ── session management ─────────────────────────────────────────────────────

    def add(
        self,
        name: str,
        *,
        token: str | None = None,
        cookies: dict[str, str] | str | None = None,
        headers: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> "SessionManager":
        """Register a named session. Returns self for chaining."""
        if isinstance(cookies, str):
            parsed: dict[str, str] = {}
            for part in cookies.split(";"):
                if "=" in part:
                    k, _, v = part.strip().partition("=")
                    parsed[k] = v
            cookies = parsed
        with self._lock:
            self._sessions[name] = Session(
                name=name,
                token=token,
                cookies=cookies or {},
                headers=headers or {},
                user_id=user_id,
            )
        return self

    def names(self) -> list[str]:
        """List all registered session names."""
        return list(self._sessions.keys())

    # ── request ────────────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        url: str,
        session: str = "primary",
        *,
        data: bytes | dict[str, Any] | str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        """Issue a request with the named session; return a result dict.

        Result keys: status, headers, body (parsed JSON or str), url, session, ok.
        """
        import urllib.request
        import urllib.error

        sess = self._sessions.get(session)
        if sess is None:
            raise KeyError(f"Unknown session '{session}'. Available: {self.names()}")

        hdrs: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        hdrs.update(sess.auth_headers())
        if extra_headers:
            hdrs.update(extra_headers)

        body: bytes | None = None
        if isinstance(data, dict):
            body = json.dumps(data).encode()
        elif isinstance(data, str):
            body = data.encode()
        elif isinstance(data, bytes):
            body = data

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    parsed_body: Any = json.loads(raw)
                except Exception:
                    parsed_body = raw.decode("utf-8", errors="replace")
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": parsed_body,
                    "url": url,
                    "session": session,
                    "ok": True,
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed_body = json.loads(raw)
            except Exception:
                parsed_body = raw.decode("utf-8", errors="replace")
            return {
                "status": exc.code,
                "headers": dict(exc.headers),
                "body": parsed_body,
                "url": url,
                "session": session,
                "ok": False,
            }
        except urllib.error.URLError as exc:
            return {"status": 0, "error": str(exc), "url": url, "session": session, "ok": False}

    # ── testing patterns ───────────────────────────────────────────────────────

    def compare(
        self,
        method: str,
        url: str,
        *sessions: str,
        data: bytes | dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Compare responses from multiple sessions for the same request.

        Typical IDOR check: use account A's token, request account B's resource ID.
        If status == 200 and the body contains B's data → confirmed horizontal escalation.
        """
        if not sessions:
            sessions = tuple(self.names())
        results: dict[str, Any] = {}
        for s in sessions:
            results[s] = self.request(method, url, session=s, data=data)
        statuses = {s: r["status"] for s, r in results.items()}
        first = next(iter(statuses.values()), None)
        differs = [s for s, st in statuses.items() if st != first]
        return {"results": results, "statuses": statuses, "differs": differs}

    def idor_scan(
        self,
        method: str,
        url_template: str,
        ids: Any,
        session: str = "primary",
        *,
        workers: int = 10,
        stop_on_hit: bool = False,
        data_template: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Scan a range of IDs to detect IDOR hits (HTTP 200 responses).

        url_template : use {id} as the placeholder, e.g.
                       'https://host/api/order/{id}'
        data_template: optional POST body with {id} in values, e.g.
                       {'userId': '{id}'}
        Returns list of hit results sorted by resource_id.
        """
        hits: list[dict[str, Any]] = []
        found = threading.Event()

        def _probe(resource_id: Any) -> dict[str, Any] | None:
            if stop_on_hit and found.is_set():
                return None
            url = url_template.replace("{id}", str(resource_id))
            body: dict[str, Any] | None = None
            if data_template:
                body = {
                    k: v.replace("{id}", str(resource_id)) if isinstance(v, str) else v
                    for k, v in data_template.items()
                }
            result = self.request(method, url, session=session, data=body,
                                  extra_headers=extra_headers)
            result["resource_id"] = resource_id
            if result["status"] == 200:
                if stop_on_hit:
                    found.set()
                return result
            return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe, rid): rid for rid in ids}
            for fut in as_completed(futures):
                res = fut.result()
                if res is not None:
                    hits.append(res)
        hits.sort(key=lambda r: r.get("resource_id", 0))
        return hits

    def race(
        self,
        method: str,
        url: str,
        session: str = "primary",
        *,
        concurrency: int = 50,
        data: bytes | dict[str, Any] | str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send concurrent requests to probe race conditions (coupons, quotas, etc.)."""
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(self.request, method, url, session,
                            data=data, extra_headers=extra_headers)
                for _ in range(concurrency)
            ]
            for fut in as_completed(futures):
                results.append(fut.result())
        status_counts = dict(Counter(r["status"] for r in results))
        return {"total": len(results), "status_counts": status_counts, "results": results}

    # ── output helpers ─────────────────────────────────────────────────────────

    def print_compare(self, result: dict[str, Any]) -> None:
        """Pretty-print a compare() result."""
        for sess, r in result["results"].items():
            preview = str(r.get("body", ""))[:160]
            marker = " ⚠" if sess in result["differs"] else "  "
            print(f"{marker}[{sess:12s}] HTTP {r['status']} | {preview}")
        if result["differs"]:
            print(f"  !! status differs between sessions: {result['statuses']}")

    def print_hits(self, hits: list[dict[str, Any]], *, show_body: bool = True) -> None:
        """Pretty-print idor_scan() hits."""
        for h in hits:
            rid = h.get("resource_id", "?")
            preview = str(h.get("body", ""))[:160] if show_body else ""
            print(f"  HIT id={rid} | {preview}")
        print(f"  Total hits: {len(hits)}")
