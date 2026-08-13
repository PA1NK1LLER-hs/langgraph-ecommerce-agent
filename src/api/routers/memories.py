"""用户记忆路由。"""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import get_current_user
from ..models import User as UserModel
from auth.permissions import role_at_least
from context import add_memory, search_memory, list_memories, forget_memory

router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryAddRequest(BaseModel):
    content: str = Field(..., min_length=1)
    category: str = Field(default="general")


class MemoryDeleteRequest(BaseModel):
    query: str = Field(..., min_length=1)


@router.get("")
async def api_list(user: Annotated[UserModel, Depends(get_current_user)]):
    # asyncio.to_thread 避免同步 Mem0 调用阻塞事件循环
    return await asyncio.to_thread(list_memories, str(user.id))


@router.post("")
async def api_add(req: MemoryAddRequest, user: Annotated[UserModel, Depends(get_current_user)]):
    return await asyncio.to_thread(
        add_memory, str(user.id), content=req.content, category=req.category
    )


@router.delete("")
async def api_delete(req: MemoryDeleteRequest, user: Annotated[UserModel, Depends(get_current_user)]):
    if not role_at_least(user.role, "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"观察者不能删除记忆（需编辑者以上权限）",
        )
    return await asyncio.to_thread(forget_memory, str(user.id), query=req.query)
