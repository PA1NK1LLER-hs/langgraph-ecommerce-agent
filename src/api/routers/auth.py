"""认证路由 — 注册、登录、用户信息。"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import User as UserModel
from ..schemas import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from ..deps import create_access_token, get_current_user
from ..rate_limit import check_auth_rate, get_client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _check_rate_limit(request: Request) -> None:
    """检查认证端点速率限制（Redis 优先 / 内存回退），超限时抛出 429。"""
    # B7：复用 rate_limit 的统一 IP 提取策略（不信任客户端可伪造的 X-Forwarded-For）
    ip = get_client_ip(request)
    allowed, retry_after = await check_auth_rate(ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁，请 {retry_after:.0f} 秒后重试",
            headers={"Retry-After": str(int(retry_after))},
        )


@router.post("/register", response_model=TokenResponse)
async def register(request: Request, req: RegisterRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    """注册新用户。用户名唯一，密码 bcrypt 哈希存储。"""
    await _check_rate_limit(request)
    existing = await db.execute(select(UserModel).where(UserModel.username == req.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    user = UserModel(username=req.username, password_hash=UserModel.hash_password(req.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username, role=user.role)


@router.post("/login", response_model=TokenResponse)
async def login(request: Request, req: LoginRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    """用户登录。验证密码后返回 JWT token。"""
    await _check_rate_limit(request)
    result = await db.execute(select(UserModel).where(UserModel.username == req.username))
    user = result.scalar_one_or_none()

    if user is None or not UserModel.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username, role=user.role)


@router.get("/me", response_model=UserResponse)
async def me(user: Annotated[UserModel, Depends(get_current_user)]):
    """获取当前登录用户信息。需要 Bearer token。"""
    return UserResponse(id=user.id, username=user.username, role=user.role, created_at=str(user.created_at))
