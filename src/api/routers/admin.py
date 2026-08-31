"""管理员路由 — 用户角色管理。

新用户注册默认 viewer（见 models.User.role 的 server_default），此前没有任何
提权入口，KB 管理（上传/删除/重建，要求 editor+）对运营侧是「死门」——除了手工
改库的账号外全员 403。这里提供 admin 专属的角色管理端点，由管理员把 viewer 提升
为 editor/admin。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import require_admin
from ..models import User as UserModel, VALID_ROLES, USER_ROLE_ADMIN

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RoleUpdateRequest(BaseModel):
    role: str = Field(..., description="目标角色：admin / editor / viewer")


@router.get("/users")
async def api_list_users(
    _admin: Annotated[UserModel, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """列出所有用户（admin 专属，供角色管理界面选择目标用户）。"""
    result = await db.execute(select(UserModel).order_by(UserModel.id))
    users = result.scalars().all()
    return {
        "status": "success",
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "created_at": str(u.created_at),
            }
            for u in users
        ],
    }


@router.put("/users/{user_id}/role")
async def api_update_user_role(
    user_id: int,
    req: RoleUpdateRequest,
    admin: Annotated[UserModel, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """设置用户角色（admin 专属）。防止删除最后一个 admin。"""
    if req.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法角色 '{req.role}'（可选：{', '.join(sorted(VALID_ROLES))}）",
        )

    target = await db.get(UserModel, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    # 防止管理员把自己降级，导致系统无人可管
    if target.id == admin.id and req.role != USER_ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能降低自己的管理员权限",
        )

    # 防止删除最后一个 admin
    if target.role == USER_ROLE_ADMIN and req.role != USER_ROLE_ADMIN:
        admin_count = (
            await db.execute(
                select(func.count())
                .select_from(UserModel)
                .where(UserModel.role == USER_ROLE_ADMIN)
            )
        ).scalar_one()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="系统必须保留至少一个管理员",
            )

    old_role = target.role
    target.role = req.role
    await db.commit()
    return {
        "status": "success",
        "user_id": user_id,
        "username": target.username,
        "role": req.role,
        "previous_role": old_role,
    }
