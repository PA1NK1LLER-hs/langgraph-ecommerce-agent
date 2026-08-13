"""审批路由 — Human-in-the-Loop 的 REST API。

提供挂起审批查询和审批决策端点。
通过 LangGraph 的 Command(resume=...) 机制恢复被 interrupt 暂停的图执行。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from langgraph.types import Command

from ..database import get_db
from ..deps import get_current_user
from ..models import User as UserModel, Thread as ThreadModel

logger = logging.getLogger("api.approval")
router = APIRouter(prefix="/api/approvals", tags=["approval"])


# ── Schema ──


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|deny)$", description="审批决定：approve 或 deny")


class ApprovalStatusResponse(BaseModel):
    thread_id: str
    has_pending: bool
    pending_calls: list[dict] = Field(default_factory=list, description="待审批的工具调用列表")


# ── 端点 ──


@router.get("/{thread_id}", response_model=ApprovalStatusResponse)
async def get_approval_status(
    thread_id: str,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """查询对话线程是否有挂起的审批请求。

    从 LangGraph checkpoint 中读取当前中断状态。
    """
    # 验证线程归属
    result = await db.execute(
        select(ThreadModel).where(
            ThreadModel.thread_id == thread_id,
            ThreadModel.user_id == user.id,
        ),
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    # 从 checkpoint 读取中断状态（B10 修复：使用共享 checkpoint 的缓存 agent）
    from agent.graph import get_checkpoint_agent
    agent = await get_checkpoint_agent()
    if agent is None:
        return ApprovalStatusResponse(thread_id=thread_id, has_pending=False)

    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = await agent.aget_state(config)
        if state is None:
            return ApprovalStatusResponse(thread_id=thread_id, has_pending=False)

        # LangGraph 在中断时会在 state 中设置 __interrupt__ 信息
        interrupts = getattr(state, "interrupts", []) or []
        pending_calls = []
        for interrupt_item in interrupts:
            value = getattr(interrupt_item, "value", None) or interrupt_item
            if isinstance(value, dict) and value.get("type") == "approval_required":
                pending_calls.extend(value.get("calls", []))

        return ApprovalStatusResponse(
            thread_id=thread_id,
            has_pending=len(pending_calls) > 0,
            pending_calls=pending_calls,
        )
    except Exception:
        logger.debug("Failed to read checkpoint state for thread %s", thread_id, exc_info=True)
        return ApprovalStatusResponse(thread_id=thread_id, has_pending=False)


@router.post("/{thread_id}")
async def decide_approval(
    thread_id: str,
    req: ApprovalDecisionRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """处理审批决定（批准或拒绝）。

    使用 LangGraph Command(resume=...) 恢复被 interrupt 暂停的图执行。
    """
    # 验证线程归属
    result = await db.execute(
        select(ThreadModel).where(
            ThreadModel.thread_id == thread_id,
            ThreadModel.user_id == user.id,
        ),
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    from agent.graph import get_checkpoint_agent
    agent = await get_checkpoint_agent()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Agent 未就绪")

    # 检查是否有挂起的审批
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = await agent.aget_state(config)
        interrupts = getattr(state, "interrupts", []) or []
        if not interrupts:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="无待确认的操作（审批可能已过期或已被处理）",
            )
        # 验证中断类型为审批类型，避免误恢复其他类型的中断
        has_approval = False
        for interrupt_item in interrupts:
            value = getattr(interrupt_item, "value", None) or interrupt_item
            if isinstance(value, dict) and value.get("type") == "approval_required":
                has_approval = True
                break
        if not has_approval:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="当前中断不是审批类型，无法通过此接口恢复",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Approval state check failed for thread %s: %s", thread_id[:8], exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="审批状态检查失败，请重试",
        )

    resume_value = req.decision  # "approve" or "deny"
    logger.info("Approval decision for thread %s: %s", thread_id[:8], resume_value)

    # 设置用户上下文，确保记忆工具使用正确的用户
    from context.manager import set_current_user
    set_current_user(str(user.id))

    try:
        # 使用 Command(resume=...) 恢复被中断的图执行
        # 注意：astream 会继续从 interrupt 点流式输出
        # 前端通过 WebSocket 接收后续事件
        async for _chunk in agent.astream(
            Command(resume=resume_value),
            config=config,
            stream_mode=["updates", "messages"],
        ):
            pass  # 事件通过已有的 WebSocket 连接推送
    except Exception as exc:
        logger.exception("Failed to resume agent for thread %s", thread_id[:8])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"恢复执行失败: {str(exc)[:200]}",
        )

    return {
        "status": "ok",
        "decision": resume_value,
        "thread_id": thread_id,
    }
