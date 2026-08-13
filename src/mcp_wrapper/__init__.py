"""MCP 模块 — Model Context Protocol 服务端与客户端。

提供：
- MCP Server：将 Agent 工具暴露为标准 MCP 工具，供 Claude Desktop 等客户端调用
- MCP Client：导入外部 MCP 服务器工具，集成到 Agent 的工具列表中
"""

from .server import create_mcp_server, run_stdio_server, run_sse_server
from .client import (
    import_mcp_tools,
    MCPToolImporter,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
)

__all__ = [
    "create_mcp_server",
    "run_stdio_server",
    "run_sse_server",
    "import_mcp_tools",
    "MCPToolImporter",
    "StdioTransportConfig",
    "StreamableHttpTransportConfig",
]
