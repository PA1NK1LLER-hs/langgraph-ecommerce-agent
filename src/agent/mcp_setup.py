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
_failed: set[str] = set()  # 本会话内连接失败的服务，避免每次意图都重试（15s 超时）

# 意图 → MCP 服务映射（仅懒加载部分）
INTENT_MCP_MAP: dict[str, list[str]] = {
    "rpa": ["RPA"],
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
    headers: dict[str, str] | None = None,
    register: bool = True,
) -> int:
    """连接一个 MCP Server 并（可选）注册其全部工具。

    register=False：只连接、把 importer 放入 _mcp_importers 供内部调用
    （如 RPA 调度器经 call_tool 推任务），但工具不注册进 app_context——
    LLM / /api/tools 看不到这些工具。连接状态照常记录。
    """
    import asyncio as _aio

    if name in _connected:
        return 0

    if name in _failed:
        logger.info("%s MCP: previously failed this session, skipped", name)
        return 0

    if url:
        config = StreamableHttpTransportConfig(url=url, headers=headers)
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
        _failed.add(name)
        return 0
    except BaseException as exc:
        logger.warning("%s MCP: connection failed (%s: %s), skipped", name, type(exc).__name__, exc)
        _failed.add(name)
        return 0
    tools = await importer.import_tools()
    if register:
        register_mcp_tools(tools)
    _mcp_importers[name] = importer
    _connected.add(name)
    logger.info(
        "%s MCP: %d tools loaded (transport=%s%s)",
        name, len(tools), config.type, "" if register else ", 未注册给 LLM",
    )
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


def _rpa_url():
    return os.getenv("MCP_RPA_URL")


def _rpa_headers() -> dict[str, str] | None:
    """远程部署鉴权：RPA MCP server 设置 RPA_MCP_TOKEN 后需携带 X-RPA-Token 头。"""
    token = os.getenv("RPA_MCP_TOKEN")
    if token:
        return {"X-RPA-Token": token}
    return None


async def setup_rpa_mcp() -> int:
    """连接独立 RPA MCP server（批量任务始终独立进程执行）。

    RPA 与 agent 永远解耦（客户端电脑上不跑 RPA）：
      - 设置 MCP_RPA_URL → Streamable HTTP 跨机/容器连接（独立部署 + --reload 热更新），
        配合 RPA_MCP_TOKEN 自动携带 X-RPA-Token 鉴权头；
      - 未设置 → 本机 stdio 子进程（`python -m skills.rpa.mcp_server --transport stdio`），
        同样是独立进程，批量任务不在 agent 进程内跑。
    两种模式 agent 侧代码一致，执行统一走 `_mcp_importers["RPA"].call_tool()`。

    连接成功但**不注册** mcp_rpa_* 工具给 LLM（register=False）：agent 侧只暴露
    submit_rpa_* 提交工具（审批拦提交，立即返回 job_id），任务由后台调度器
    经本连接推给 executor，杜绝聊天回合被 5~15 分钟阻塞。importer 仍保留在
    `_mcp_importers["RPA"]` 供调度器调用。

    连接成功后置位 rpa_mcp_connected 并失效 skill 工具缓存；连接失败本会话
    不再重试（此时调度器会把任务标记失败）。
    """
    import sys

    url = _rpa_url()
    if url:
        count = await _connect_and_register(
            name="RPA", url=url,
            command=None, args=None,
            headers=_rpa_headers(),
            register=False,
        )
    else:
        count = await _connect_and_register(
            name="RPA", url=None,
            command=sys.executable,
            args=["-m", "skills.rpa.mcp_server", "--transport", "stdio"],
            headers=None,
            register=False,
        )
    if count > 0:
        from app_context import get_app_context
        ctx = get_app_context()
        ctx.set_rpa_mcp_connected(True)
        ctx.set_skill_tools_cache(None)
        logger.info("RPA MCP 已连接：executor 就绪（%d tools，未注册给 LLM，由调度器调用）", count)
    return count


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
    "RPA": setup_rpa_mcp,
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
    """Agent 退出时清理所有 MCP 连接。

    同时清空已注册的 MCP 工具（app_context._mcp_tools）：
    register_mcp_tools 是追加语义，若不清空，shutdown 后重连（测试里
    Filesystem 每个用例独立 loop 各自连接）会把同一批 mcp_* 工具重复注册，
    造成 get_all_tools() 里同名工具翻倍。
    """
    from app_context import get_app_context

    for importer in _mcp_importers.values():
        try:
            await importer.disconnect()
        except BaseException:
            pass
    _mcp_importers.clear()
    _connected.clear()
    ctx = get_app_context()
    ctx.clear_mcp_tools()
    ctx.set_skill_tools_cache(None)
    logger.info("All MCP connections closed")
