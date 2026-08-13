"""LangGraph Agent API — FastAPI 模块化入口。

启动:
    python -m src.api.server
    uvicorn src.api.server:app --host 0.0.0.0 --port 8080 --ws wsproto --reload

架构:
    routers/auth.py       — 注册、登录、用户信息
    routers/chat.py       — WebSocket 流式对话
    routers/tools.py      — 工具/技能列表
    routers/knowledge.py  — 知识库检索
    routers/memories.py   — 用户记忆 CRUD
    models/               — SQLAlchemy 模型 (User)
    schemas/              — Pydantic 请求/响应模型
    deps.py               — JWT 工具 + 依赖注入
    database.py           — 异步 SQLAlchemy 引擎
"""

import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response

# 引导路径
SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from config import LLM_API_KEY
from context import set_current_user, load_user_context
from agent.graph import get_agent
from agent.mcp_setup import setup_essential_mcp_tools, shutdown_mcp_tools
from agent.utils import build_memory_injection

# ── 请求级日志追踪 ──
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIDFilter(logging.Filter):
    """将当前请求 ID 注入每条日志记录，确保 format 中 %(request_id)s 可用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _request_id_var.get()
        return True


# ── 日志 ──
LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"
# force=True 确保 basicConfig 覆盖已有 handler 的格式（uvicorn 可能先初始化）
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)

# 将 filter 同时添加到 root logger 和所有现有 handler
root_logger = logging.getLogger()
root_logger.addFilter(RequestIDFilter())
for h in root_logger.handlers:
    h.addFilter(RequestIDFilter())

for noisy in ("lightrag", "Qdrant", "neo4j", "httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
logger = logging.getLogger("api")

# ── 数据库初始化 ──
_agent_ready = False
_db_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent_ready, _db_ready

    # ── 启动 ──
    if not LLM_API_KEY:
        logger.error("LLM_API_KEY not set — API will not function")
    else:
        set_current_user(os.getenv("AGENT_USER_ID", "default_user"))

        try:
            logger.info("Connecting MCP servers...")
            await setup_essential_mcp_tools()
        except BaseException as exc:
            # BaseException 而非 Exception：Python 3.14+ 中 CancelledError 不再继承 Exception
            logger.warning("MCP setup failed: %s", exc)

        try:
            from api.database import init_db
            await init_db()
            _db_ready = True
            logger.info("Database initialized")
        except Exception as exc:
            logger.warning("Database unavailable (%s) — auth disabled", exc)

        try:
            ctx = load_user_context(os.getenv("AGENT_USER_ID", "default_user"))
            injection = build_memory_injection(ctx.get("memories", []))
            await get_agent(context_summary=injection)
            _agent_ready = True
            logger.info("Agent ready")
        except Exception as exc:
            logger.exception("Agent startup failed: %s", exc)

    yield

    # ── 关闭 ──
    await shutdown_mcp_tools()
    logger.info("API shutdown complete")


# ═══════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════

app = FastAPI(
    title="LangGraph Agent",
    description="通用 AI 助手 — FastAPI + Vue 3",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 全局 API 速率限制中间件 ──
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from api.rate_limit import api_limiter, get_client_ip, check_api_rate


class RateLimitMiddleware(BaseHTTPMiddleware):
    """对所有 /api/* 请求应用速率限制。

    豁免：WebSocket 端点（已有自己的 ws_limiter）、health 端点。
    """

    async def dispatch(self, request, call_next):
        # 仅限制 API 端点
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/health":
            return await call_next(request)

        # WebSocket upgrade 请求由 chat.py 自行限流
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        ip = get_client_ip(request)
        allowed, retry_after = await check_api_rate(ip)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"请求过于频繁，请 {retry_after:.0f} 秒后重试",
                    "retry_after": int(retry_after),
                },
                headers={"Retry-After": str(int(retry_after))},
            )

        return await call_next(request)


app.add_middleware(RateLimitMiddleware)

# ── 注册路由 ──
from api.routers import auth, chat, tools, knowledge, memories, threads, approval

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(tools.router)
app.include_router(knowledge.router)
app.include_router(memories.router)
app.include_router(threads.router)
app.include_router(approval.router)


# ── 统一异常处理（B3）──
# 三种错误风格（HTTPException 裸 detail / 422 校验数组 / 500 异常字符串）统一为：
# {"detail": <人话>, "request_id": <追踪ID>}，内部细节只进日志不泄漏给客户端。
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """统一 HTTPException 响应结构，保留 Retry-After 等自定义头。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": _request_id_var.get()},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """请求参数校验失败：返回简化 422，字段级细节仅记录日志。"""
    logger.warning(
        "Request validation failed on %s %s: %s",
        request.method, request.url.path, exc.errors()[:3],
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数有误", "request_id": _request_id_var.get()},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常：完整堆栈进日志，客户端只收到通用 500。"""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试", "request_id": _request_id_var.get()},
    )


# ── 健康检查 (无需认证) ──
import asyncio as _asyncio


async def _check_qdrant() -> bool:
    """探测 Qdrant 是否可达（3s 超时）。"""
    try:
        from rag.indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()
        if indexer._rag is not None:
            return True
        await _asyncio.wait_for(indexer._ensure_initialized_async(), timeout=3.0)
        return indexer._rag is not None
    except Exception:
        return False


async def _check_mem0() -> bool:
    """探测 Mem0 是否可达（3s 超时）。"""
    try:
        from context.memory_store import get_memory_store
        store = get_memory_store()
        client = await _asyncio.wait_for(
            _asyncio.to_thread(lambda: store._client if hasattr(store, "_client") else None),
            timeout=3.0,
        )
        return client is not None
    except Exception:
        return False


@app.get("/api/health")
async def health():
    # 并行探测所有服务
    qdrant_ok, mem0_ok = await _asyncio.gather(
        _check_qdrant(), _check_mem0(),
    )
    checks = {
        "agent": _agent_ready,
        "database": _db_ready,
        "qdrant": qdrant_ok,
        "mem0": mem0_ok,
    }
    all_ok = all(checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
    }


# ── 请求 ID 中间件 ──

class RequestIDMiddleware:
    """纯 ASGI 中间件：为每个 HTTP/WebSocket 请求注入唯一 request_id。

    通过 ContextVar 隔离，多用户并发时日志可精确区分来源。
    X-Request-ID 响应头也方便前端对接。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        # 优先使用客户端传入的 X-Request-ID，否则生成新的
        headers = dict(scope.get("headers", []))
        req_id = headers.get(b"x-request-id", b"").decode() or str(uuid.uuid4())[:8]
        _request_id_var.set(req_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-request-id", req_id.encode()))
                message["headers"] = headers_list
            await send(message)

        try:
            return await self.app(scope, receive, send_with_id)
        finally:
            _request_id_var.set("-")  # 请求结束后恢复默认值


# 注册为纯 ASGI 中间件（需在 SPAMiddleware 之前以确保 API 日志也带 ID）
app.add_middleware(RequestIDMiddleware)

# ── 前端 SPA 中间件 (纯 ASGI — 避免 anyio 兼容问题) ──
import os as _os

DIST_DIR = str((PROJECT_ROOT / "frontend" / "dist").resolve())
DIST_INDEX = _os.path.join(DIST_DIR, "index.html")

# 预加载 index.html 内容
# 注意：index.html 的 hash 引用会随构建变化，需要支持热更新
_INDEX_HTML = None
_INDEX_HTML_MTIME = 0

def _get_index_html() -> str | None:
    """读取 index.html，如果文件已更新则重新加载。"""
    global _INDEX_HTML, _INDEX_HTML_MTIME
    if not _os.path.exists(DIST_INDEX):
        return None
    mtime = _os.path.getmtime(DIST_INDEX)
    if _INDEX_HTML is None or mtime != _INDEX_HTML_MTIME:
        with open(DIST_INDEX, encoding="utf-8") as _f:
            _INDEX_HTML = _f.read()
        _INDEX_HTML_MTIME = mtime
    return _INDEX_HTML


class SPAMiddleware:
    """纯 ASGI 中间件：API 请求透传，其他路径优先返回静态文件，否则返回 index.html。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "/")

        # API / WebSocket 走正常路由
        if path.startswith("/api/") or path.startswith("/ws"):
            return await self.app(scope, receive, send)

        # 静态文件
        safe = path.lstrip("/").replace("\\", "/")
        file_path = _os.path.normpath(_os.path.join(DIST_DIR, safe))

        if file_path.startswith(DIST_DIR) and _os.path.isfile(file_path):
            # 手动构建 ASGI 文件响应
            content_type = "text/javascript" if file_path.endswith(".js") else \
                          "text/css" if file_path.endswith(".css") else \
                          "image/svg+xml" if file_path.endswith(".svg") else \
                          "text/html; charset=utf-8"
            try:
                with open(file_path, "rb") as f:
                    body = f.read()
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", content_type.encode()),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"public, max-age=3600"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
            except Exception:
                pass

        # SPA fallback: 返回 index.html（自动检测文件更新）
        index_html = _get_index_html()
        if index_html:
            body = index_html.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


# 注册为纯 ASGI 中间件（不是 @app.middleware）
app.add_middleware(SPAMiddleware)


# ═══════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════

def main():
    print("=" * 55)
    print("  LangGraph Agent API Server")
    print("  http://localhost:8080     — 前端页面")
    print("  http://localhost:8080/docs — Swagger UI")
    print("  ws://localhost:8080/ws/chat — WebSocket")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info", ws="wsproto")


if __name__ == "__main__":
    main()
