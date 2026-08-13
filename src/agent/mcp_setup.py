"""MCP 外部工具启动模块 — 按需连接，减少启动内存占用。

启动时只连接 Filesystem + SearXNG（高频使用），
其余 MCP 服务按意图懒加载：首次需要时才连接子进程。

传输方式：
- 设置 MCP_*_URL 环境变量 → Streamable HTTP 远程连接（Docker 模式）
- 未设置 → stdio 本地子进程（npx 模式）
"""

import logging
import os
from pathlib import Path

from mcp_wrapper.client import MCPToolImporter, StreamableHttpTransportConfig, StdioTransportConfig
from agent.core import register_mcp_tools

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_mcp_importers: dict[str, MCPToolImporter] = {}
_connected: set[str] = set()

# 意图 → MCP 服务映射（仅懒加载部分）
INTENT_MCP_MAP: dict[str, list[str]] = {
    "rpa": ["Playwright"],
    "time": ["Time"],
    "complex": ["SequentialThinking", "Docker"],
}


async def _connect_and_register(
    name: str,
    *,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """连接一个 MCP Server 并注册其全部工具。"""
    import asyncio as _aio

    if name in _connected:
        return 0

    if url:
        config = StreamableHttpTransportConfig(url=url)
        logger.info("%s MCP: connecting via Streamable HTTP at %s", name, url)
    elif command:
        config = StdioTransportConfig(command=command, args=args or [], env=env)
    else:
        logger.info("%s MCP: no URL or command configured, skipped", name)
        return 0

    importer = MCPToolImporter(config)
    try:
        await _aio.wait_for(importer.connect(), timeout=15)
    except _aio.TimeoutError:
        logger.warning("%s MCP: connection timed out (15s), skipped", name)
        return 0
    except BaseException as exc:
        logger.warning("%s MCP: connection failed (%s: %s), skipped", type(exc).__name__, exc)
        return 0
    tools = await importer.import_tools()
    register_mcp_tools(tools)
    _mcp_importers[name] = importer
    _connected.add(name)
    logger.info("%s MCP: %d tools loaded (transport=%s)", name, len(tools), config.type)
    return len(tools)


# ── 各 MCP Server 连接函数 ──


def _playwright_url():
    return os.getenv("MCP_PLAYWRIGHT_URL")


async def setup_playwright_mcp() -> int:
    url = _playwright_url()
    return await _connect_and_register(
        name="Playwright", url=url,
        command="npx", args=["-y", "@playwright/mcp"],
    )


def _filesystem_url():
    return os.getenv("MCP_FILESYSTEM_URL")


async def setup_filesystem_mcp() -> int:
    url = _filesystem_url()
    data_dir = str((_PROJECT_ROOT / "data").resolve())
    workspace_dir = str((_PROJECT_ROOT / "workspace").resolve())
    os.makedirs(workspace_dir, exist_ok=True)
    return await _connect_and_register(
        name="Filesystem", url=url,
        command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", data_dir, workspace_dir],
    )


async def setup_sequentialthinking_mcp() -> int:
    url = os.getenv("MCP_SEQUENTIALTHINKING_URL")
    return await _connect_and_register(
        name="SequentialThinking", url=url,
        command="npx", args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
    )


async def setup_time_mcp() -> int:
    url = os.getenv("MCP_TIME_URL")
    return await _connect_and_register(
        name="Time", url=url,
        command="python", args=["-m", "mcp_server_time"],
    )


async def setup_docker_mcp() -> int:
    url = os.getenv("MCP_DOCKER_URL")
    return await _connect_and_register(
        name="Docker", url=url,
        command="npx", args=["-y", "@paretools/docker"],
    )


async def setup_searxng_mcp() -> int:
    url = os.getenv("MCP_SEARXNG_URL")
    searxng_instance = os.getenv("SEARXNG_INSTANCE_URL", "http://localhost:8088")
    return await _connect_and_register(
        name="SearXNG", url=url,
        command="npx", args=["-y", "@kassol/mcp-searxng"],
        env={"SEARXNG_URL": searxng_instance},
    )


# 所有 MCP 服务的设置函数注册表
_MCP_REGISTRY: dict[str, callable] = {  # type: ignore
    "Playwright": setup_playwright_mcp,
    "Filesystem": setup_filesystem_mcp,
    "SequentialThinking": setup_sequentialthinking_mcp,
    "Time": setup_time_mcp,
    "Docker": setup_docker_mcp,
    "SearXNG": setup_searxng_mcp,
}

# 启动时始终连接的基础服务（高频使用）
_ESSENTIAL_MCP = ["Filesystem", "SearXNG"]


async def setup_essential_mcp_tools() -> None:
    """Agent 启动时调用 — 只连接高频使用的基础 MCP 服务。"""
    for name in _ESSENTIAL_MCP:
        setup_fn = _MCP_REGISTRY.get(name)
        if not setup_fn:
            continue
        try:
            count = await setup_fn()
            print(f"  [MCP] {name}: {count} tools loaded")
        except BaseException as exc:
            print(f"  [MCP] {name}: SKIPPED ({exc})")
            logger.warning("MCP %s setup failed: %s", name, exc)


async def ensure_mcp_for_intent(intent: str) -> None:
    """按意图懒加载对应的 MCP 服务。已经连接的服务会跳过。"""
    names = INTENT_MCP_MAP.get(intent, [])
    for name in names:
        if name in _connected:
            continue
        setup_fn = _MCP_REGISTRY.get(name)
        if not setup_fn:
            continue
        try:
            count = await setup_fn()
            print(f"  [MCP] {name}: {count} tools loaded (lazy, intent={intent})")
        except BaseException as exc:
            print(f"  [MCP] {name}: SKIPPED ({exc})")
            logger.warning("MCP %s lazy setup failed: %s", name, exc)


# 兼容旧调用方
setup_external_mcp_tools = setup_essential_mcp_tools


async def shutdown_mcp_tools() -> None:
    """Agent 退出时清理所有 MCP 连接。"""
    for importer in _mcp_importers.values():
        try:
            await importer.disconnect()
        except BaseException:
            pass
    _mcp_importers.clear()
    _connected.clear()
    logger.info("All MCP connections closed")
