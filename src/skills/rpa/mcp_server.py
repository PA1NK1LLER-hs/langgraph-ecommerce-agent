# -*- coding: utf-8 -*-
"""RPA 独立 MCP Server — 把 RPA 批量任务暴露为独立 MCP 服务，与 agent 进程解耦。

RPA 始终以独立进程执行（客户端电脑上不跑 RPA）：本机 stdio 子进程（agent 按
"rpa" 意图懒连接）或跨机 Streamable HTTP（MCP_RPA_URL + RPA_MCP_TOKEN 鉴权）。
agent 进程内不注册 RPA 工具，统一经 mcp_rpa_* 调用本服务。任务在本服务进程
跑，执行日志写入本服务日志；实时回流 agent 前端为后续 Phase 2（跨进程日志转发）。
不引入 agent.core 全栈依赖，因此可作为独立 Windows 常驻服务部署：
紫鸟 + 本服务同机运行，agent 经 MCP 远程调用。

基于 MCP SDK 2.0.0 的 `MCPServer` 高层 API（`add_tool` / `run_*_async`）。

传输：
  - stdio           同机子进程模式（`python -m skills.rpa.mcp_server --transport stdio`）
  - streamable-http 跨机/容器模式（MCP 2025-03-26 标准，含有状态会话管理）

鉴权：
  设置 RPA_MCP_TOKEN 环境变量后，所有 HTTP 请求需携带 `X-RPA-Token` 头，
  否则返回 401。未设置则放行（仅限本机/内网）。

启动：
  python -m skills.rpa.mcp_server --transport streamable-http --host 0.0.0.0 --port 8911
  开发热更新：加 --reload（uvicorn 文件监听，改代码秒级重启）
"""

import argparse
import asyncio
import contextlib
import inspect
import io
import logging
import os
import threading
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from pydantic import Field

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# 独立部署时不经过 src/config.py（agent 进程才 import 它），这里自行加载项目根 .env，
# 必须在 import skills.rpa.* 之前执行 —— common/config.py 在模块加载期读取环境变量。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_PROJECT_ROOT / ".env")

from skills.rpa.adapters import get_rpa_batch_tools

logger = logging.getLogger(__name__)

# 工具运行期间的 stdout 重定向锁（见 _langchain_tool_to_callable._invoke_sync）
_STDOUT_REDIRECT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# LangChain @tool → MCPServer 可注册 callable 转换
# ---------------------------------------------------------------------------


def _build_params(lc_tool: Any) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    """从 args_schema 重建参数列表 + 注解（保留每个参数的 description）。

    RPA 工具用 `Field(description=...)` 声明参数说明，但 `MCPServer.add_tool`
    只认带类型注解的 callable。这里把 args_schema 的每个字段还原成
    `Annotated[type, Field(description=...)]` 注解，交给 `func_metadata`
    生成 JSON Schema，从而让 LLM 侧仍能看到每个参数的语义。
    """
    args_schema = getattr(lc_tool, "args_schema", None)
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    if args_schema is not None and getattr(args_schema, "model_fields", None):
        for field_name, field_info in args_schema.model_fields.items():
            ann = field_info.annotation
            if field_info.description:
                ann = Annotated[(ann, Field(description=field_info.description))]
            annotations[field_name] = ann
            # RPA 工具的 args_schema 只用 `default=`，不用 default_factory，
            # 因此 field_info.default 对非必填字段即真实默认值。
            default = inspect.Parameter.empty if field_info.is_required() else field_info.default
            params.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=ann,
                )
            )

    return params, annotations


def _langchain_tool_to_callable(lc_tool: Any):
    """把单个 LangChain @tool 包装成 async callable，供 MCPServer.add_tool 注册。

    关键点：
      - 通过 __signature__ / __annotations__ 让 inspect.signature 读到
        与 args_schema 一致的参数与 description；
      - async 包装内用 asyncio.to_thread 调用同步的 tool.invoke（RPA 耗时
        长），避免阻塞事件循环；
      - 捕获异常并返回 {"status": "error", ...}，与 RPA 工具自身约定一致，
        而不是把协议级 isError 抛给 agent。
    """
    name = lc_tool.name
    description = lc_tool.description or ""
    params, annotations = _build_params(lc_tool)

    def _invoke_sync(kwargs: dict, buf: io.StringIO):
        """同步执行工具，并把工具自身的 print() 重定向到 buf。

        stdio 传输下 stdout 就是 JSON-RPC 协议通道，任务脚本里任何 print
        都会污染传输流（轻则乱码丢结果，重则 MCP 会话报错）。重定向后
        print 落到 buf，由 _invoke 写进日志；协议通道保持干净。
        用全局锁串行化重定向，避免并发工具调用时 sys.stdout 被互相抢占。
        """
        with _STDOUT_REDIRECT_LOCK:
            with contextlib.redirect_stdout(buf):
                if hasattr(lc_tool, "invoke"):
                    return lc_tool.invoke(kwargs)
                # 兜底：旧式普通 callable（理论上不会出现）
                return lc_tool(**kwargs)

    async def _invoke(**kwargs):
        buf = io.StringIO()
        try:
            result = await asyncio.to_thread(_invoke_sync, kwargs, buf)
            captured = buf.getvalue().strip()
            if captured:
                logger.info("RPA工具[%s] stdout:\n%s", name, captured)
            if hasattr(result, "content"):
                return str(result.content)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("RPA MCP tool '%s' failed", name)
            return {"status": "error", "message": str(exc)}

    _invoke.__name__ = name
    _invoke.__qualname__ = name
    _invoke.__doc__ = description
    _invoke.__annotations__ = annotations
    _invoke.__signature__ = inspect.Signature(
        parameters=params, return_annotation=inspect.Signature.empty
    )
    return _invoke


# ---------------------------------------------------------------------------
# Server 工厂
# ---------------------------------------------------------------------------


def create_rpa_mcp_server() -> MCPServer:
    """创建暴露 RPA 批量端到端任务的 MCPServer（远程部署用）。

    本地部署不连 RPA MCP，批量任务固定 agent 进程内执行（日志实时回流）；
    本 server 仅用于 RPA 与 agent 分机部署时承载批量任务。
    每次调用重新获取工具列表，因此配合 --reload 后新增/修改的工具会立即反映。
    """
    server = MCPServer(
        "langgraph-rpa",
        title="LangGraph RPA MCP Server",
        description="紫鸟浏览器 RPA 批量自动化任务",
    )

    for t in get_rpa_batch_tools():
        try:
            fn = _langchain_tool_to_callable(t)
            server.add_tool(fn, name=t.name, description=t.description, structured_output=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to register tool '%s' to MCP: %s", getattr(t, "name", "?"), exc)

    return server


# ---------------------------------------------------------------------------
# stdio 传输入口
# ---------------------------------------------------------------------------


async def run_stdio_server() -> None:
    """启动 stdio MCP Server（阻塞，处理 stdin/stdout JSON-RPC）。"""
    server = create_rpa_mcp_server()
    await server.run_stdio_async()


# ---------------------------------------------------------------------------
# Streamable HTTP 传输入口（有状态会话管理）
# ---------------------------------------------------------------------------


class _TokenAuthMiddleware:
    """可选 token 鉴权：设置 RPA_MCP_TOKEN 后强制校验 X-RPA-Token 头。"""

    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.token:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if headers.get("x-rpa-token") != self.token:
                response = json_bytes({"status": "error", "message": "unauthorized"})
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(response)).encode())],
                })
                await send({"type": "http.response.body", "body": response})
                return
        await self.app(scope, receive, send)


def json_bytes(obj: Any) -> bytes:
    import json

    return json.dumps(obj, ensure_ascii=False).encode()


def build_http_app(token: str | None = None) -> Any:
    """构建 Streamable HTTP ASGI app（含可选鉴权）。

    关闭 DNS rebinding 保护：本服务可能绑定 0.0.0.0 供跨机/容器调用，
    保留默认 localhost-only 校验会把远程 Host 头判为 421。
    """
    server = create_rpa_mcp_server()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=False,
        stateless_http=False,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if token:
        app.add_middleware(_TokenAuthMiddleware, token=token)
    return app


def run_streamable_http_server(host: str, port: int, token: str | None, reload: bool = False) -> None:
    """启动 Streamable HTTP MCP Server（uvicorn）。"""
    try:
        import uvicorn
    except ImportError:
        logger.error("Streamable HTTP server requires 'uvicorn'. Install with: pip install uvicorn")
        raise

    app = build_http_app(token=token)
    logger.info("RPA MCP server (streamable-http) on http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", reload=reload, reload_dirs=["src/skills/rpa"])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="RPA standalone MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8911)
    parser.add_argument("--reload", action="store_true", help="uvicorn 文件监听热更新（仅 streamable-http）")
    args = parser.parse_args()

    token = os.getenv("RPA_MCP_TOKEN") or None

    if args.transport == "stdio":
        asyncio.run(run_stdio_server())
    else:
        run_streamable_http_server(args.host, args.port, token, reload=args.reload)


if __name__ == "__main__":
    main()
