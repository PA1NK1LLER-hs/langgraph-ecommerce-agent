"""MCP Server — 将 Agent 工具暴露为 MCP 标准工具。

启动方式:
    python -m src.mcp_wrapper.server        # stdio 传输（Claude Desktop 集成）
    python -c "import asyncio; from mcp_wrapper.server import run_sse_server; asyncio.run(run_sse_server())"  # SSE 传输

Claude Desktop 配置示例（claude_desktop_config.json）:
    {
      "mcpServers": {
        "langgraph-agent": {
          "command": "python",
          "args": ["-m", "src.mcp_wrapper.server"],
          "cwd": "/path/to/langgraph-agent"
        }
      }
    }
"""

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from agent.core import get_all_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangChain @tool → MCP Tool schema 转换
# ---------------------------------------------------------------------------


def _pydantic_to_json_schema(model: type) -> dict[str, Any]:
    """将 Pydantic v2 args_schema 转为 JSON Schema dict。"""
    try:
        schema = model.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        return schema
    except Exception:
        return {"type": "object", "properties": {}}


def _langchain_tool_to_mcp(lc_tool: Any) -> Tool:
    """将单个 LangChain @tool 转为 MCP Tool 定义。"""
    if hasattr(lc_tool, "args_schema") and lc_tool.args_schema:
        input_schema = _pydantic_to_json_schema(lc_tool.args_schema)
    else:
        input_schema = {"type": "object", "properties": {}}

    description = (lc_tool.description or "").split("\n")[0][:1024]

    return Tool(
        name=lc_tool.name,
        description=description,
        inputSchema=input_schema,
    )


# ---------------------------------------------------------------------------
# Server 工厂
# ---------------------------------------------------------------------------


def create_mcp_server() -> Server:
    """创建 MCP Server 实例，注册所有 Agent 工具。

    list_tools 和 call_tool handler 在每次调用时重新获取工具列表，
    因此运行时新增的工具会立即可见。
    """
    server = Server("langgraph-agent")

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        all_tools = get_all_tools()
        mcp_tools: list[Tool] = []
        for t in all_tools:
            try:
                mcp_tools.append(_langchain_tool_to_mcp(t))
            except Exception as exc:
                logger.warning("Failed to convert tool '%s' to MCP: %s", t.name, exc)
        return mcp_tools

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        all_tools = get_all_tools()
        tool_map = {t.name: t for t in all_tools}
        tool_func = tool_map.get(name)

        if tool_func is None:
            return [TextContent(
                type="text",
                text=json.dumps(
                    {"status": "error", "message": f"Unknown tool: {name}"},
                    ensure_ascii=False,
                ),
            )]

        try:
            # 将 sync invoke 调度到线程池，避免阻塞事件循环
            if hasattr(tool_func, "invoke"):
                result = await asyncio.to_thread(tool_func.invoke, arguments)
            else:
                result = await asyncio.to_thread(tool_func, **arguments)

            if hasattr(result, "content"):
                result_text = str(result.content)
            elif isinstance(result, dict):
                result_text = json.dumps(result, ensure_ascii=False, default=str)
            else:
                result_text = str(result)
            return [TextContent(type="text", text=result_text)]
        except Exception as exc:
            logger.exception("MCP tool '%s' failed", name)
            return [TextContent(
                type="text",
                text=json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False),
            )]

    return server


# ---------------------------------------------------------------------------
# 传输入口
# ---------------------------------------------------------------------------


async def run_stdio_server() -> None:
    """启动 stdio MCP Server（阻塞，处理 stdin/stdout JSON-RPC 消息）。"""
    server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def run_sse_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    sse_endpoint: str = "/sse",
    messages_endpoint: str = "/messages/",
) -> None:
    """启动 SSE/HTTP MCP Server。

    Args:
        host:             监听地址。
        port:             监听端口。
        sse_endpoint:     SSE 连接端点路径。
        messages_endpoint: JSON-RPC 消息端点路径（必须以 / 结尾）。
    """
    from mcp.server.sse import SseServerTransport

    server = create_mcp_server()
    sse_transport = SseServerTransport(messages_endpoint)

    async def handle_sse(request: Any) -> None:
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    try:
        from starlette.applications import Starlette
        from starlette.routing import Route
    except ImportError:
        logger.error(
            "SSE server requires 'starlette' package. Install with: pip install starlette uvicorn"
        )
        raise

    app = Starlette(
        routes=[
            Route(sse_endpoint, endpoint=handle_sse),
            Route(
                messages_endpoint + "{path:path}",
                endpoint=sse_transport.handle_post_message,
                methods=["POST"],
            ),
        ]
    )

    try:
        import uvicorn
    except ImportError:
        logger.error(
            "SSE server requires 'uvicorn' package. Install with: pip install uvicorn"
        )
        raise

    logger.info("MCP SSE server starting on %s:%s", host, port)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    uvicorn_server = uvicorn.Server(config)
    await uvicorn_server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_stdio_server())
