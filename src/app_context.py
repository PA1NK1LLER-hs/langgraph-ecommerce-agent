"""应用全局上下文 — 统一管理 Agent 实例和工具注册表。

注意：用户 ID 隔离已迁移至 context.manager 的 ContextVar 机制，
每个 asyncio Task 独立持有自己的 user_id，不再通过 AppContext 管理。

替代模块级的分散全局变量，提供：
- 线程安全的单例访问
- Agent 实例及其生命周期管理
- MCP 工具注册表
- Skill 工具缓存管理
- 测试时的 reset 能力
"""

from __future__ import annotations

import threading
from typing import Any

from config import POSTGRES_URL


class AppContext:
    """应用全局上下文 — 线程安全的单例。

    管理所有需要全局共享的可变状态：
    - agent 实例（LangGraph compiled graph）
    - agent context（用于缓存一致性检查）
    - MCP 工具列表
    - Skill 工具缓存
    - Checkpointer 关闭回调

    注意：用户 ID 不再由此类管理，改用 context.manager._current_user_var（ContextVar）。
    """

    def __init__(self):
        # ── Agent ──
        self._agent: Any = None
        self._agent_context: str | None = None
        self._checkpointer_closer: Any = None  # callable to close old checkpointer

        # ── MCP 工具 ──
        self._mcp_tools: list = []

        # ── Skill 工具缓存 ──
        self._skill_tools_cache: list | None = None

        # ── RPA 是否由独立 MCP server 提供 ──
        # True 时 skills 层跳过进程内 RPA 工具（避免与 mcp_rpa_* 重复）。
        self._rpa_mcp_connected: bool = False

        # ── 线程安全 ──
        self._lock = threading.Lock()

    # ── Agent 管理 ──

    def get_agent(self):
        """获取当前缓存的 Agent 实例。"""
        return self._agent

    def set_agent(self, agent, context: str = "", checkpointer_closer: Any = None):
        """设置 Agent 实例并记录其 context 和 checkpointer 清理回调。"""
        with self._lock:
            self._agent = agent
            self._agent_context = context
            self._checkpointer_closer = checkpointer_closer

    def get_agent_context(self) -> str | None:
        """获取当前 agent 的 context summary。"""
        return self._agent_context

    async def close_checkpointer(self) -> None:
        """安全关闭当前 agent 的 checkpointer（释放数据库连接池）。"""
        closer = None
        with self._lock:
            closer = self._checkpointer_closer
            self._checkpointer_closer = None
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass

    # ── MCP 工具注册 ──

    def register_mcp_tools(self, tools: list) -> None:
        with self._lock:
            self._mcp_tools.extend(tools)

    def clear_mcp_tools(self) -> None:
        """清空已注册的 MCP 工具（MCP 全量断开时调用，避免重连重复注册）。"""
        with self._lock:
            self._mcp_tools.clear()

    def get_mcp_tools(self) -> list:
        with self._lock:
            return list(self._mcp_tools)

    # ── Skill 工具缓存 ──

    def get_skill_tools_cache(self) -> list | None:
        return self._skill_tools_cache

    def set_skill_tools_cache(self, tools: list) -> None:
        with self._lock:
            self._skill_tools_cache = tools

    # ── RPA MCP 连接状态 ──

    def set_rpa_mcp_connected(self, connected: bool) -> None:
        with self._lock:
            self._rpa_mcp_connected = connected

    def is_rpa_mcp_connected(self) -> bool:
        return self._rpa_mcp_connected

    # ── Reset（测试用）──

    def reset(self) -> None:
        """重置所有状态（仅用于测试）。"""
        with self._lock:
            self._agent = None
            self._agent_context = None
            self._checkpointer_closer = None
            self._mcp_tools.clear()
            self._skill_tools_cache = None
            self._rpa_mcp_connected = False


# ── 模块级单例 ──

_ctx: AppContext | None = None
_ctx_lock = threading.Lock()


def get_app_context() -> AppContext:
    """获取 AppContext 单例（线程安全）。"""
    global _ctx
    if _ctx is None:
        with _ctx_lock:
            if _ctx is None:
                _ctx = AppContext()
    return _ctx
