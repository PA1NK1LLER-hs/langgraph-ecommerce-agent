"""认证相关 Pydantic schemas。"""
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, description="用户名")
    password: str = Field(..., min_length=6, max_length=128, description="密码")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT access token")
    token_type: str = "bearer"
    username: str = Field(..., description="用户名")
    role: str = Field(default="viewer", description="用户角色")


class UserResponse(BaseModel):
    id: int
    username: str
    role: str = "viewer"
    created_at: str | None = None
