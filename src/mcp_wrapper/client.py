"""MCP Client — 导入外部 MCP 服务器工具集成到 Agent。

用法示例:
    from mcp_wrapper.client import MCPToolImporter, StdioTransportConfig

    # stdio 子进程
    config = StdioTransportConfig(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/path"])

    async with MCPToolImporter(config) as importer:
        tools = await importer.import_tools()

    # Streamable HTTP 远程 (MCP 2025-03-26)
    from mcp_wrapper.client import StreamableHttpTransportConfig
    config = StreamableHttpTransportConfig(url="http://localhost:8931/mcp")
    async with MCPToolImporter(config) as importer:
        tools = await importer.import_tools()
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union, List

from langchain_core.tools import tool
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport 配置
# ---------------------------------------------------------------------------


@dataclass
class StdioTransportConfig:
    """stdio 子进程传输配置。

    command: 可执行文件路径或命令名（如 "npx", "python", "uvx"）
    args:    命令行参数列表
    env:     子进程环境变量（None = 继承当前进程）
    cwd:     子进程工作目录
    """

    type: Literal["stdio"] = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None


@dataclass
class StreamableHttpTransportConfig:
    """Streamable HTTP 传输配置（MCP 2025-03-26）。

    url:     MCP Server 的 Streamable HTTP 端点 URL（如 /mcp）
    headers: 附加 HTTP 请求头（如 {"X-RPA-Token": "..."}），用于远程部署鉴权
    """

    type: Literal["streamable_http"] = "streamable_http"
    url: str = ""
    headers: dict[str, str] | None = None


TransportConfig = StdioTransportConfig | StreamableHttpTransportConfig


def _create_transport(config: TransportConfig):
    """根据配置创建对应的传输上下文管理器。"""
    if config.type == "stdio":
        params = StdioServerParameters(
            command=config.command,
            args=config.args or [],
            env=config.env,
            cwd=config.cwd,
        )
        return stdio_client(params)
    elif config.type == "streamable_http":
        from contextlib import asynccontextmanager

        from mcp.client.streamable_http import streamable_http_client

        # 远程部署鉴权：SDK 允许注入自定义 http_client（带自定义头），
        # 例如 RPA MCP server 设置 RPA_MCP_TOKEN 后需携带 X-RPA-Token 头。
        http_client = None
        if config.headers:
            import httpx2

            http_client = httpx2.AsyncClient(headers=config.headers)

        cm = streamable_http_client(url=config.url, http_client=http_client)

        @asynccontextmanager
        async def _adapt():
            # MCP SDK 2.0.0 的 streamable_http_client 只 yield (read_stream, write_stream)，
            # 不再返回 get_session_id。
            try:
                async with cm as (read, write):
                    yield read, write
            finally:
                if http_client is not None:
                    await http_client.aclose()

        return _adapt()
    raise ValueError(f"Unknown transport type: {config.type}")


# ---------------------------------------------------------------------------
# JSON Schema → Pydantic 递归转换
# ---------------------------------------------------------------------------


def _json_schema_to_pydantic(
    name: str,
    schema: dict[str, Any] | None,
    defs: dict[str, Any] | None = None,
) -> type | None:
    """将 JSON Schema 递归转换为 Pydantic 类型/模型。

    支持: $ref, $defs, anyOf/oneOf (→Union), allOf, enum (→Literal),
          嵌套 object, array+items, 基础标量类型。

    Returns:
        Pydantic BaseModel 子类（object）、Python 类型（标量/数组）、None（转换失败）。
    """
    if not schema:
        return None

    defs = defs or schema.get("$defs", {})

    # ── $ref ──
    if "$ref" in schema:
        ref_path = schema["$ref"]
        if ref_path.startswith("#/$defs/") or ref_path.startswith("#/definitions/"):
            ref_name = ref_path.rsplit("/", 1)[-1]
            resolved = defs.get(ref_name)
            if resolved:
                return _json_schema_to_pydantic(ref_name, resolved, defs)
        return None

    # ── anyOf / oneOf → Union ──
    for key in ("anyOf", "oneOf"):
        if key in schema:
            sub_types: list[type] = []
            for i, sub in enumerate(schema[key]):
                converted = _json_schema_to_pydantic(f"{name}_opt{i}", sub, defs)
                if converted is not None:
                    sub_types.append(converted)
            if not sub_types:
                return str
            if len(sub_types) == 1:
                return Optional[sub_types[0]]
            return Union[tuple(sub_types)]  # type: ignore

    # ── allOf → 合并字段 ──
    if "allOf" in schema:
        from pydantic import create_model

        merged_fields: dict[str, Any] = {}
        for i, sub in enumerate(schema["allOf"]):
            converted = _json_schema_to_pydantic(f"{name}_part{i}", sub, defs)
            if converted is not None and hasattr(converted, "model_fields"):
                for fname, finfo in converted.model_fields.items():
                    merged_fields[fname] = (finfo.annotation, finfo.default)
        if merged_fields:
            return create_model(name, **merged_fields)
        return None

    schema_type = schema.get("type", "string")

    # ── 标量类型 ──
    type_map: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "null": type(None),
    }
    if schema_type in type_map:
        base_type = type_map[schema_type]
        if "enum" in schema:
            try:
                # Literal 要求至少 2 个值
                enum_values = tuple(schema["enum"])
                if len(enum_values) >= 1:
                    return Literal[enum_values]  # type: ignore
            except Exception:
                pass
            return base_type
        return base_type

    # ── array ──
    if schema_type == "array":
        items = schema.get("items", {})
        item_type = _json_schema_to_pydantic(f"{name}_item", items, defs) if items else str
        return List[item_type if item_type is not None else str]  # type: ignore

    # ── object → Pydantic BaseModel ──
    if schema_type == "object":
        from pydantic import create_model, Field as PydanticField

        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields: dict[str, tuple[type, Any]] = {}

        for prop_name, prop_schema in properties.items():
            prop_type = _json_schema_to_pydantic(f"{name}_{prop_name}", prop_schema, defs)
            if prop_type is None:
                prop_type = str
            default = prop_schema.get("default", ...)
            description = prop_schema.get("description", "")
            if prop_name in required:
                if description:
                    fields[prop_name] = (prop_type, PydanticField(description=description))
                else:
                    fields[prop_name] = (prop_type, ...)
            else:
                if default is not ...:
                    if description:
                        fields[prop_name] = (prop_type, PydanticField(default=default, description=description))
                    else:
                        fields[prop_name] = (prop_type, default)
                else:
                    if description:
                        fields[prop_name] = (Optional[prop_type], PydanticField(default=None, description=description))
                    else:
                        fields[prop_name] = (Optional[prop_type], None)

        if not fields:
            return None
        return create_model(name, **fields)

    return str


# ---------------------------------------------------------------------------
# MCPToolImporter — 有状态连接管理
# ---------------------------------------------------------------------------


class MCPToolImporter:
    """连接外部 MCP Server 并导入其工具为 LangChain 兼容格式。

    支持 stdio 子进程和 SSE/HTTP 两种传输。

    使用 async context manager 管理连接生命周期:

        config = StdioTransportConfig(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/path"])
        async with MCPToolImporter(config) as importer:
            tools = await importer.import_tools()
            result = await tools[0].func(query="hello")  # session 有效

    # 或者手动管理:
        importer = MCPToolImporter(config)
        await importer.connect()
        tools = await importer.import_tools()
        ...
        await importer.disconnect()
    """

    def __init__(self, config: TransportConfig):
        self._config = config
        self._transport_ctx = None
        self._read_stream = None
        self._write_stream = None
        self._session: ClientSession | None = None
        self._connected = False

    # ── 上下文管理器 ──

    async def __aenter__(self) -> "MCPToolImporter":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    # ── 连接管理 ──

    async def connect(self) -> None:
        """建立传输通道并初始化 MCP session（idempotent）。"""
        if self._session is not None:
            return

        transport_cm = _create_transport(self._config)
        self._transport_ctx = transport_cm
        try:
            self._read_stream, self._write_stream = await transport_cm.__aenter__()
            self._session = ClientSession(self._read_stream, self._write_stream)
            await self._session.__aenter__()
            await self._session.initialize()
        except BaseException:
            # 半建立的连接需清理，否则悬挂的 async generator 会在 GC 时触发
            # anyio「Attempted to exit cancel scope」噪音（Python 3.14 asyncio）。
            await self.disconnect()
            raise
        self._connected = True
        logger.info("MCP session connected (%s)", self._config.type)

    async def disconnect(self) -> None:
        """关闭 session 和传输通道（idempotent）。"""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except BaseException:
                pass
            self._session = None
        if self._transport_ctx is not None:
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except BaseException:
                pass
            self._transport_ctx = None
            self._read_stream = None
            self._write_stream = None
        self._connected = False
        logger.info("MCP session disconnected")

    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    # ── 工具操作 ──

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出远程 MCP Server 的所有工具定义。"""
        if not self._session:
            raise RuntimeError("MCPToolImporter not connected. Call connect() first.")
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.input_schema}
            for t in result.tools
        ]

    async def import_tools(self) -> list:
        """导入远程工具，返回 LangChain @tool 兼容函数列表。

        Returns:
            LangChain @tool 函数列表（以 `mcp_` 为前缀命名）。
            工具函数持有此 importer 的引用，调用时实时检查 session 状态。
        """
        if not self._session:
            raise RuntimeError("MCPToolImporter not connected. Call connect() first.")
        mcp_tools = await self._session.list_tools()
        return [_build_lc_tool(t, self) for t in mcp_tools.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """直接调用远程工具（不通过 LangChain 包装）。"""
        if not self._session:
            raise RuntimeError("MCPToolImporter not connected.")
        return await self._session.call_tool(name, arguments=arguments)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


async def import_mcp_tools(
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
) -> list:
    """便捷函数：连接 MCP Server 并导入工具。

    至少提供 command（stdio）或 url（SSE）之一。

    Args:
        command: stdio 传输 — 可执行文件路径或命令名。
        args:    stdio 传输 — 命令行参数列表。
        url:     SSE 传输 — MCP Server 端点 URL。
        headers: SSE 传输 — 自定义 HTTP 头。

    Returns:
        LangChain @tool 函数列表，调用方可直接用于 Agent 工具绑定。

    Note:
        返回的工具持有内部 importer 引用以保持连接存活。
        如需精确控制生命周期，请直接使用 MCPToolImporter + async with。
    """
    if url:
        config: TransportConfig = StreamableHttpTransportConfig(url=url)
    elif command:
        config = StdioTransportConfig(command=command, args=args or [])
    else:
        raise ValueError("Either 'command' (stdio) or 'url' (SSE) must be provided.")

    importer = MCPToolImporter(config)
    await importer.connect()
    tools = await importer.import_tools()

    # 给工具打标记以保持 importer 引用存活
    for t in tools:
        t._mcp_importer = importer

    return tools


# ---------------------------------------------------------------------------
# 内部: MCP Tool → LangChain @tool 适配
# ---------------------------------------------------------------------------


def _build_lc_tool(mcp_tool: Any, importer: MCPToolImporter) -> Any:
    """将 MCP Tool 转换为 LangChain @tool 兼容函数。

    关键设计：闭包捕获 importer 而非 session，调用时通过 importer._session 获取当前连接。
    这避免了 session 生命周期 bug — importer 保持连接存活。
    """
    args_model = _json_schema_to_pydantic(mcp_tool.name, mcp_tool.input_schema or {})

    async def _call_tool(**kwargs: Any) -> dict[str, Any]:
        session = importer._session
        if session is None:
            return {"status": "error", "message": f"MCP connection closed for tool '{mcp_tool.name}'"}
        try:
            result = await session.call_tool(mcp_tool.name, arguments=kwargs)
            if result.content:
                return {
                    "status": "success",
                    "results": [c.text if hasattr(c, "text") else str(c) for c in result.content],
                }
            return {"status": "success", "results": []}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    tool_name = f"mcp_{mcp_tool.name}"
    if args_model:
        tool_func = tool(tool_name, description=mcp_tool.description or "", args_schema=args_model)(_call_tool)
    else:
        tool_func = tool(tool_name, description=mcp_tool.description or "")(_call_tool)
    return tool_func
