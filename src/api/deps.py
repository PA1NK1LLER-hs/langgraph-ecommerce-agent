"""JWT 工具函数和 FastAPI 依赖注入。"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .models import User

logger = logging.getLogger(__name__)

# B2：不在导入期崩溃。未配置时生成临时密钥并警告 —— 应用（含 /api/health）仍可启动，
# 代价是服务重启后旧 token 全部失效；生产环境必须显式配置。
_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_urlsafe(32)
    logger.warning(
        "JWT_SECRET_KEY 未设置！已生成临时随机密钥（服务重启后所有 token 将失效）。"
        "生产环境请通过环境变量提供至少 32 字符的随机密钥。"
    )
SECRET_KEY = _SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

security_scheme = HTTPBearer(auto_error=False)


def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI 依赖：从 Authorization header 中解析 JWT 并返回 User。"""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供认证令牌")
    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌格式错误")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """FastAPI 依赖：可选的用户认证（不强制要求登录）。"""
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    if payload is None:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    result = await db.execute(select(User).where(User.id == int(user_id)))
    return result.scalar_one_or_none()


async def get_user_by_token(token: str, db: AsyncSession) -> User | None:
    """用 JWT 字符串解析用户（WebSocket 首条消息认证路径）。"""
    payload = decode_token(token)
    if payload is None:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    result = await db.execute(select(User).where(User.id == uid))
    return result.scalar_one_or_none()


async def get_ws_user(websocket: WebSocket, db: AsyncSession) -> User | None:
    """WebSocket 认证：从查询参数 token 中解析 JWT（向后兼容旧客户端）。"""
    token = websocket.query_params.get("token", "")
    if not token:
        return None
    return await get_user_by_token(token, db)


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """依赖：要求 admin 角色（B4：权限依赖归属 api 层，auth 层只保留纯函数）。"""
    from auth.permissions import role_at_least, get_user_role, Role  # 函数内导入避免 auth/__init__ 循环
    user_role = get_user_role(user)
    if not role_at_least(user_role, Role.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"需要管理员权限（当前: {user_role}）",
        )
    return user


async def require_editor(user: Annotated[User, Depends(get_current_user)]) -> User:
    """依赖：要求 editor+ 角色。"""
    from auth.permissions import role_at_least, get_user_role, Role
    user_role = get_user_role(user)
    if not role_at_least(user_role, Role.EDITOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"需要编辑者以上权限（当前: {user_role}）",
        )
    return user
