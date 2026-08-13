"""用户记忆管理 — Mem0 后端（L3 语义记忆）+ 情景记忆（L2）。

三层记忆架构：
  L1: 工作记忆（当前对话窗口，~10 条消息）
  L2: 情景记忆（跨对话的关键事件摘要 — 本模块 episodic 函数）
  L3: 语义记忆（Mem0 向量库 — 持久化偏好/事实/背景）

用户隔离：通过 Python contextvars 实现请求/任务级别的用户上下文隔离。
每个 WebSocket 连接 / asyncio Task 独立持有自己的 user_id，多用户并发安全。
"""

import logging
from contextvars import ContextVar
from typing import Any

from .episodic import get_episodic_store
from .memory_store import get_memory_store

logger = logging.getLogger(__name__)

# ── 请求级用户上下文隔离 ──
# ContextVar 在每个 asyncio Task 中自动隔离，确保多用户并发时互不干扰。
# 同步代码（CLI main）中也能正常工作 — 同步上下文等价于一个独立的 Task。
_current_user_var: ContextVar[str] = ContextVar("current_user", default="default_user")


def set_current_user(user_id: str) -> None:
    """设置当前请求上下文的用户 ID（ContextVar 隔离，多用户安全）。

    在 WebSocket handler 中调用后，该连接内所有后续的 Agent 工具调用
    都能通过 get_current_user() 获取到正确的用户 ID。
    """
    _current_user_var.set(user_id)


def get_current_user() -> str:
    """获取当前请求上下文的用户 ID（从 ContextVar 读取，多用户安全）。"""
    return _current_user_var.get()


# ---------------------------------------------------------------------------
# 新 API — Agent 工具直接调用
# ---------------------------------------------------------------------------


def add_memory(
    user_id: str,
    content: str,
    category: str = "general",
) -> dict[str, Any]:
    """存入一条自然语言记忆。Mem0 内部用 LLM 提取事实、去重合并。

    例: add_memory("u1", "用户叫何山，在巧逗逗做运营，喜欢简洁回复")
    """
    store = get_memory_store()
    try:
        result = store.add(content, user_id=user_id, category=category)
        return {"status": "success", "result": result}
    except Exception as exc:
        logger.exception("add_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc)}


def search_memory(
    user_id: str,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """语义搜索记忆。query 为自然语言描述。

    例: search_memory("u1", "用户是谁，在哪工作")
    """
    store = get_memory_store()
    try:
        results = store.search(query, user_id=user_id, limit=limit)
        return {"status": "success", "results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("search_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "results": []}


def list_memories(
    user_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    """列出用户全部记忆。"""
    store = get_memory_store()
    try:
        results = store.get_all(user_id=user_id, limit=limit)
        return {"status": "success", "results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("list_memories failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "results": []}


def forget_memory(
    user_id: str,
    query: str,
) -> dict[str, Any]:
    """语义搜索后删除匹配的记忆。

    例: forget_memory("u1", "用户叫何山")
    """
    store = get_memory_store()
    try:
        deleted = store.search_then_delete(query, user_id=user_id)
        return {"status": "success", "deleted": deleted}
    except Exception as exc:
        logger.exception("forget_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "deleted": 0}


# ---------------------------------------------------------------------------
# L2 情景记忆 API — 关键事件/决策/任务完成记录
# ---------------------------------------------------------------------------


def save_episodic_memory(
    user_id: str,
    summary: str,
    *,
    importance: float = 0.6,
    tags: list[str] | None = None,
    category: str = "general",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存一条情景记忆（L2）。

    用于记录对后续对话有价值的"事件"：
    - 用户做出了重要决策
    - 完成了某个复杂任务
    - 工具调用产生了重要结果
    - 用户表达了明确的偏好

    Args:
        user_id: 用户 ID。
        summary: 事件摘要（一句话，<200 字符）。
        importance: 重要性 0-1（0.3=低, 0.6=中, 0.9=高，默认 0.6）。
        tags: 标签，如 ["decision", "deployment"]。
        category: 分类 — task_completion / user_decision / tool_result / preference / general。
        metadata: 附加元数据。

    Returns:
        {"status": "success", "episode": {...}} 或 {"status": "error", ...}
    """
    store = get_episodic_store()
    try:
        episode = store.add(
            user_id, summary,
            importance=importance,
            tags=tags,
            category=category,
            metadata=metadata,
        )
        return {"status": "success", "episode": episode}
    except Exception as exc:
        logger.exception("save_episodic_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc)}


def search_episodic_memory(
    user_id: str,
    query: str = "",
    *,
    tags: list[str] | None = None,
    category: str | None = None,
    min_importance: float = 0.0,
    limit: int = 20,
) -> dict[str, Any]:
    """搜索情景记忆（L2）。

    Args:
        user_id: 用户 ID。
        query: 关键词搜索（在摘要和标签中匹配）。
        tags: 按标签过滤（交集）。
        category: 按分类过滤。
        min_importance: 最低重要性阈值。
        limit: 返回条数上限。

    Returns:
        {"status": "success", "results": [...], "count": N}
    """
    store = get_episodic_store()
    try:
        results = store.search(
            user_id, query,
            tags=tags,
            category=category,
            min_importance=min_importance,
            limit=limit,
        )
        return {"status": "success", "results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("search_episodic_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "results": []}


def get_recent_episodes(
    user_id: str,
    limit: int = 10,
) -> dict[str, Any]:
    """获取最近的 N 条情景记忆。

    Args:
        user_id: 用户 ID。
        limit: 返回条数上限。

    Returns:
        {"status": "success", "results": [...], "count": N}
    """
    store = get_episodic_store()
    try:
        results = store.get_recent(user_id, limit=limit)
        return {"status": "success", "results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("get_recent_episodes failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "results": []}


def delete_episodic_memory(user_id: str, episode_id: str) -> dict[str, Any]:
    """删除指定 ID 的情景记忆。"""
    store = get_episodic_store()
    try:
        ok = store.delete(user_id, episode_id)
        return {"status": "success", "deleted": ok}
    except Exception as exc:
        logger.exception("delete_episodic_memory failed for user %s", user_id)
        return {"status": "error", "message": str(exc), "deleted": False}


# ---------------------------------------------------------------------------
# 组合上下文 API — Agent 工具直接调用的统一入口
# ---------------------------------------------------------------------------


def get_conversation_context(
    user_id: str,
    query: str = "",
    *,
    episodic_limit: int = 10,
    semantic_limit: int = 5,
) -> dict[str, Any]:
    """获取当前用户的完整对话上下文。

    组合 L2 情景记忆 + L3 语义记忆，返回 Agent 可直接注入的上下文文本。

    Args:
        user_id: 用户 ID。
        query: 当前用户查询（用于针对性检索，可选）。
        episodic_limit: L2 情景记忆返回上限。
        semantic_limit: L3 语义记忆返回上限。

    Returns:
        {
            "status": "success",
            "episodic": [最近的情景记忆列表],
            "semantic": [语义记忆搜索结果],
            "context_text": "格式化的上下文字符串",
        }
    """
    # L2: 获取最近的情景记忆
    recent_eps = get_recent_episodes(user_id, limit=episodic_limit)
    episodic_list = recent_eps.get("results", [])

    # L3: 语义搜索
    search_q = query or "用户 偏好 背景 决策"
    sem_result = search_memory(user_id, search_q, limit=semantic_limit)
    semantic_list = sem_result.get("results", [])

    # 构建格式化的上下文字符串
    parts = []

    if episodic_list:
        parts.append("## 近期情景记忆（关键事件）")
        for ep in episodic_list:
            imp = ep.get("importance", 0)
            imp_label = "⭐" if imp >= 0.9 else ("●" if imp >= 0.6 else "○")
            parts.append(f"- {imp_label} [{ep.get('category', '')}] {ep.get('summary', '')}")
            tags = ep.get("tags", [])
            if tags:
                parts.append(f"  标签: {', '.join(tags)}")

    if semantic_list:
        parts.append("## 长期记忆（偏好/背景/事实）")
        for m in semantic_list:
            text = m.get("memory", "")[:200]
            if text:
                parts.append(f"- {text}")

    context_text = "\n".join(parts) if parts else "(无相关记忆)"

    return {
        "status": "success",
        "episodic": episodic_list,
        "semantic": semantic_list,
        "context_text": context_text,
    }


# ---------------------------------------------------------------------------
# 兼容 API — main.py 启动加载
# ---------------------------------------------------------------------------


def load_user_context(user_id: str) -> dict[str, Any]:
    """加载用户全部记忆（替代旧的 preferences.md 读取）。"""
    result = list_memories(user_id)
    memories = result.get("results", [])
    return {"memories": memories, "count": len(memories)}


def format_context_summary(user_context: dict[str, Any]) -> str:
    """格式化用户记忆摘要（用于 CLI 启动展示）。"""
    memories = user_context.get("memories", [])
    count = user_context.get("count", len(memories))
    if count == 0:
        return "(无记忆)"

    preview = []
    for m in memories[:5]:
        text = m.get("memory", "")[:80]
        if text:
            preview.append(f"- {text}")
    summary = "\n".join(preview)
    return f"已加载 {count} 条记忆:\n{summary}" if preview else f"已加载 {count} 条记忆"
