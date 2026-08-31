"""对话线程路由 — 对话历史管理。

消息实际存储在 LangGraph checkpoint (PostgreSQL) 中，本模块仅管理线程元数据
（thread_id ↔ user 映射 + 标题），作为索引层复用已有 checkpoint 基础设施。
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import User as UserModel, Thread as ThreadModel
from config import display_model_name
from agent.utils import content_to_text

logger = logging.getLogger("api.threads")
router = APIRouter(prefix="/api/threads", tags=["threads"])


# ── Schema ──


class ThreadResponse(BaseModel):
    id: int
    thread_id: str
    title: str
    created_at: str
    updated_at: str


class ThreadListResponse(BaseModel):
    threads: list[ThreadResponse]
    count: int


class CreateThreadRequest(BaseModel):
    title: str = Field(default="新对话", max_length=200)


class ThreadMessagesResponse(BaseModel):
    thread_id: str
    title: str
    messages: list[dict]


# ── 列表 ──


@router.get("", response_model=ThreadListResponse)
async def list_threads(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
):
    """列出当前用户的所有对话线程（按更新时间倒序）。"""
    result = await db.execute(
        select(ThreadModel)
        .where(ThreadModel.user_id == user.id)
        .order_by(ThreadModel.updated_at.desc())
        .limit(limit),
    )
    threads = result.scalars().all()
    return ThreadListResponse(
        threads=[
            ThreadResponse(
                id=t.id,
                thread_id=t.thread_id,
                title=t.title,
                created_at=str(t.created_at),
                updated_at=str(t.updated_at),
            )
            for t in threads
        ],
        count=len(threads),
    )


# ── 创建 ──


@router.post("", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED)
async def create_thread(
    req: CreateThreadRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建新对话线程，返回 thread_id 供 WebSocket 连接使用。"""
    thread_id = str(uuid.uuid4())
    thread = ThreadModel(thread_id=thread_id, user_id=user.id, title=req.title)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return ThreadResponse(
        id=thread.id,
        thread_id=thread.thread_id,
        title=thread.title,
        created_at=str(thread.created_at),
        updated_at=str(thread.updated_at),
    )


# ── 详情（读取消息历史）──


@router.get("/{thread_id}", response_model=ThreadMessagesResponse)
async def get_thread(
    thread_id: str,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """获取对话线程的消息历史（从 LangGraph checkpoint 加载）。"""
    # 验证线程属于当前用户
    result = await db.execute(
        select(ThreadModel).where(
            ThreadModel.thread_id == thread_id,
            ThreadModel.user_id == user.id,
        ),
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    # 从 LangGraph checkpoint 加载消息
    messages = await _load_messages_from_checkpoint(thread_id)
    return ThreadMessagesResponse(
        thread_id=thread.thread_id,
        title=thread.title,
        messages=messages,
    )


# ── 删除 ──


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: str,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """删除对话线程（同时删除 LangGraph checkpoint 中的消息）。"""
    result = await db.execute(
        select(ThreadModel).where(
            ThreadModel.thread_id == thread_id,
            ThreadModel.user_id == user.id,
        ),
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    await db.delete(thread)
    await db.commit()

    # 异步清理 LangGraph checkpoint（失败不影响删除结果）
    try:
        from agent.graph import get_checkpoint_agent
        agent = await get_checkpoint_agent()
        if agent and hasattr(agent, "checkpointer"):
            config = {"configurable": {"thread_id": thread_id}}
            # 尝试删除 checkpoint（各 checkpointer 实现不同，忽略不支持的情况）
            try:
                checkpointer = agent.checkpointer
                if hasattr(checkpointer, "adelete_thread"):
                    await checkpointer.adelete_thread(config)
            except Exception:
                logger.debug("Checkpoint cleanup failed for thread %s", thread_id[:8], exc_info=True)
    except Exception:
        logger.debug("Failed to clean up checkpoint for thread %s", thread_id, exc_info=True)

    return None


# ── 更新标题 ──


class UpdateTitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.patch("/{thread_id}", response_model=ThreadResponse)
async def update_thread_title(
    thread_id: str,
    req: UpdateTitleRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """更新对话标题。"""
    result = await db.execute(
        select(ThreadModel).where(
            ThreadModel.thread_id == thread_id,
            ThreadModel.user_id == user.id,
        ),
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    thread.title = req.title
    thread.updated_at = datetime.now()
    await db.commit()
    await db.refresh(thread)
    return ThreadResponse(
        id=thread.id,
        thread_id=thread.thread_id,
        title=thread.title,
        created_at=str(thread.created_at),
        updated_at=str(thread.updated_at),
    )


# ── 内部工具函数 ──


async def ensure_thread(user_id: int, thread_id: str, title: str = "新对话") -> None:
    """确保线程元数据存在（WebSocket 连接时调用）。幂等操作。"""
    from ..database import async_session as _session_factory
    if _session_factory is None:
        logger.warning("ensure_thread: database unavailable, thread %s not saved", thread_id[:8])
        return
    async with _session_factory() as db:
        existing = await db.execute(
            select(ThreadModel).where(ThreadModel.thread_id == thread_id),
        )
        if existing.scalar_one_or_none() is None:
            db.add(ThreadModel(thread_id=thread_id, user_id=user_id, title=title))
            await db.commit()


async def auto_title_thread(thread_id: str, first_message: str) -> None:
    """用首条用户消息的前 30 字自动设置对话标题。

    在 WebSocket 首条消息时调用；失败时记录 WARNING 日志便于排查。
    """
    from ..database import async_session as _session_factory
    if _session_factory is None:
        logger.warning("auto_title_thread: database unavailable (POSTGRES_URL not set)")
        return
    title = first_message.replace("\n", " ").strip()[:30]
    if not title:
        return
    try:
        async with _session_factory() as db:
            result = await db.execute(
                select(ThreadModel).where(ThreadModel.thread_id == thread_id),
            )
            thread = result.scalar_one_or_none()
            if thread is not None:
                thread.title = title
                thread.updated_at = datetime.now()
                await db.commit()
                logger.info("Thread %s auto-titled: %s", thread_id[:8], title)
            else:
                logger.warning("auto_title_thread: thread %s not found in DB", thread_id[:8])
    except Exception:
        logger.exception("auto_title_thread failed for thread %s", thread_id[:8])


async def _load_messages_from_checkpoint(thread_id: str) -> list[dict]:
    """从 LangGraph checkpoint 加载消息历史，序列化为前端友好格式。

    使用 get_checkpoint_agent()：优先复用 _agent_cache 中任意实例（共享同一
    SQLite checkpoint 文件，可跨实例读取）；缓存为空时构建默认实例，
    重启后历史仍可从磁盘 checkpoint 恢复。
    """
    from agent.graph import get_checkpoint_agent

    safe: list[dict] = []
    try:
        agent = await get_checkpoint_agent()
        if agent is None:
            logger.debug("No agent available, cannot load checkpoint for %s", thread_id)
            return safe
        config = {"configurable": {"thread_id": thread_id}}
        state = await agent.aget_state(config)
        if state is None or state.values is None:
            logger.debug("No checkpoint state for thread %s", thread_id)
            return safe

        raw_messages = state.values.get("messages", [])
        for m in raw_messages:
            msg_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else "unknown")

            if msg_type == "human":
                content = getattr(m, "content", "") if hasattr(m, "content") else m.get("content", "")
                # 多模态消息（list）只取文本块，避免 base64 图片块泄入历史（见 content_to_text）
                safe.append({"role": "user", "content": content_to_text(content)})
            elif msg_type == "ai":
                content = getattr(m, "content", "") if hasattr(m, "content") else m.get("content", "")
                has_tool_calls = bool(getattr(m, "tool_calls", None) if hasattr(m, "tool_calls") else False)
                additional = getattr(m, "additional_kwargs", {}) if hasattr(m, "additional_kwargs") else {}
                entry: dict = {"role": "assistant", "content": str(content) if content else ""}
                if additional.get("_model_used"):
                    # 对外展示真实模型名（内部键 flash/pro → .env 配置的模型 ID）
                    entry["model"] = display_model_name(additional["_model_used"])
                if has_tool_calls:
                    tool_calls = getattr(m, "tool_calls", []) or []
                    entry["tool_calls"] = [
                        {"name": tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?"),
                         "args": str(tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}))[:200]}
                        for tc in tool_calls
                    ]
                safe.append(entry)
            elif msg_type == "tool":
                name = getattr(m, "name", "?") if hasattr(m, "name") else m.get("name", "?")
                content = getattr(m, "content", "") if hasattr(m, "content") else m.get("content", "")
                is_error = isinstance(content, dict) and content.get("status") == "error"
                safe.append({
                    "role": "tool",
                    "content": str(name),
                    "tool_result": str(content)[:300],
                    "error": bool(is_error),
                })
    except Exception:
        logger.debug("Failed to load messages for thread %s", thread_id, exc_info=True)

    return safe
