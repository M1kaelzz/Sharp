from contextlib import asynccontextmanager
import asyncio
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sharp import __version__
from sharp.server import db
from sharp.server.routers import auth, android, android_chat, approvals, assets, baseline, chat, coverage, dispatcher, export, facts, hints, hypotheses, intents, knowledge, maintenance, miniprogram, projects, reports, settings, sub_goals, vulnerabilities

STATIC_DIR = Path(__file__).parent / "static"

# Paths exempt from authentication
_AUTH_EXEMPT = {"/", "/health", "/auth/status", "/auth/setup", "/auth/login", "/auth/logout"}
_AUTH_EXEMPT_PREFIXES = ("/static/",)

# 安全响应头（P0 安全修复）。CSP 取舍说明：
# Alpine.js 用 Function 构造器求值模板表达式、Tailwind runtime 会注入 <style>，
# 因此 script-src 必须放行 'unsafe-inline' 'unsafe-eval' —— 这版 CSP 的防护重点是
# 「禁止加载外部脚本 / 禁止被嵌套 / 禁止表单外发 / 禁止 object 与 base 劫持」；
# 对 inline 注入的防护主要靠前端 DOMPurify 白名单（static/app.core.js 的 renderMd）。
# 若将来把 Tailwind/Alpine 换成构建期产物，应改用 nonce 并去掉 unsafe-*。
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # HSTS：即使用户通过反向代理暴露到公网，也能防止 MITM 降级攻击。
    # 本工具设计为本地运行，但加上此头零成本且能在被意外暴露时提供防护。
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    # Startup DB housekeeping: gated wal-checkpoint + VACUUM while the process
    # still owns the DB alone (batch DB-health). Best-effort, never blocks boot
    # beyond the (normally instant) pass.
    from sharp.server.db_maintenance import run_startup_maintenance
    from pathlib import Path as _P

    try:
        run_startup_maintenance(_P(db.current_path()))
    except Exception:
        logging.getLogger(__name__).warning("startup db maintenance failed", exc_info=True)
    from sharp.server.events import set_event_loop
    set_event_loop(asyncio.get_event_loop())
    yield
    # Reap interactive Android dynamic-debug containers on shutdown so they don't
    # leak across server restarts. Best-effort: never block/raise on shutdown.
    try:
        from sharp.server.android_dynamic import get_manager
        n = get_manager().reap_all()
        if n:
            logging.getLogger(__name__).info("reaped %d android dynamic container(s) on shutdown", n)
    except Exception:
        pass


app = FastAPI(
    title="Sharp",
    description="Sharp - Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect all API routes with JWT. Auth and static paths are exempt.

    The dispatcher (a trusted backend process, not a browser) authenticates with
    a shared server token from the SHARP_SERVER_TOKEN env var instead of a JWT.
    """
    path = request.url.path
    if path in _AUTH_EXEMPT or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    from sharp.server.auth import extract_token, get_jwt_key, verify_token, server_token
    token = extract_token(request)
    if token:
        srv = server_token()
        if srv and token == srv:
            return await call_next(request)
        if verify_token(token, get_jwt_key()):
            return await call_next(request)
    return JSONResponse({"detail": "未授权，请先登录"}, status_code=401)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """给所有响应补安全头（含 401 与静态资源）。

    注册在 auth_middleware 之后 = 处于更外层，保证鉴权失败的 JSON 响应也带 CSP。
    """
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(dispatcher.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(approvals.router)
app.include_router(approvals.emergency_router)
app.include_router(export.router)
app.include_router(reports.router)
app.include_router(miniprogram.router)
app.include_router(android.router)
app.include_router(android_chat.router)
app.include_router(chat.router)
app.include_router(vulnerabilities.router)
app.include_router(knowledge.router)
app.include_router(facts.router)
app.include_router(assets.router)
app.include_router(sub_goals.router)
app.include_router(baseline.router)
app.include_router(hypotheses.router)
app.include_router(coverage.router)
app.include_router(maintenance.router)


# ── index.html 骨架 + 视图片段（P2 拆分）──────────────────────────────────────
# index.html 只保留骨架（head / 侧栏 / 模态框 / 脚本），11 个视图块拆到 static/views/，
# 由这里的 include 在响应时拼回**完全相同**的 HTML（拆分前后有逐字节 diff 验证）。
_VIEWS_INCLUDE_RE = re.compile(r"^[ \t]*<!--\s*@include\s+([\w./-]+)\s*-->[ \t]*$", re.M)
_index_cache: dict[str, tuple[float, str]] = {}


def render_index_html() -> str:
    """把 index.html 里的 @include 指令替换成 views/ 片段内容。

    - 片段路径限制在 static/ 内（防目录穿越）
    - 片段缺失时给出明确 500，而不是返回半截页面（白屏最难查）
    - 按 mtime 缓存拼接结果：改片段立即生效，运行期不再重复读盘
    """
    skeleton = STATIC_DIR / "index.html"
    views_dir = STATIC_DIR / "views"
    stamps = [skeleton.stat().st_mtime]
    if views_dir.is_dir():
        stamps += [p.stat().st_mtime for p in views_dir.glob("*.html")]
    stamp = max(stamps)
    cached = _index_cache.get("html")
    if cached is not None and cached[0] == stamp:
        return cached[1]

    root = STATIC_DIR.resolve()

    def _replace(match: "re.Match[str]") -> str:
        rel = match.group(1)
        path = (STATIC_DIR / rel).resolve()
        if path != root and root not in path.parents:
            raise HTTPException(status_code=500, detail=f"invalid view include: {rel}")
        if not path.is_file():
            raise HTTPException(status_code=500, detail=f"missing view include: {rel}")
        return path.read_text(encoding="utf-8").rstrip("\n")

    html = _VIEWS_INCLUDE_RE.sub(_replace, skeleton.read_text(encoding="utf-8"))
    _index_cache["html"] = (stamp, html)
    return html


@app.get("/", include_in_schema=False)
def index():
    return HTMLResponse(render_index_html())


@app.get("/health", include_in_schema=False)
def health():
    """Unauthenticated readiness probe used by the launcher's wait_for_server.

    除 `status` 外还回报**代码指纹**（`code_rev` = 本进程启动时加载的代码，
    `disk_rev` = 磁盘现状，`stale` = 两者不一致即"改了代码没重启"）。
    见 `sharp.server.rev` —— 这个字段存在的唯一目的是让"测了旧进程"当场暴露。
    """
    from sharp.server.rev import rev_info

    return {"status": "ok", **rev_info()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
