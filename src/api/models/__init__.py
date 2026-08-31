"""用户模型 & 对话线程模型 & RPA 任务队列。"""
from datetime import datetime

import bcrypt
from sqlalchemy import String, DateTime, Integer, ForeignKey, func, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base

# 角色常量
USER_ROLE_ADMIN = "admin"
USER_ROLE_EDITOR = "editor"
USER_ROLE_VIEWER = "viewer"
VALID_ROLES = {USER_ROLE_ADMIN, USER_ROLE_EDITOR, USER_ROLE_VIEWER}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=USER_ROLE_VIEWER, server_default=USER_ROLE_VIEWER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    threads: Mapped[list["Thread"]] = relationship("Thread", back_populates="user", cascade="all, delete-orphan")

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return bcrypt.checkpw(plain.encode(), hashed.encode())

    @property
    def is_admin(self) -> bool:
        return self.role == USER_ROLE_ADMIN

    @property
    def is_editor(self) -> bool:
        return self.role in (USER_ROLE_ADMIN, USER_ROLE_EDITOR)

    @property
    def is_viewer(self) -> bool:
        return self.role in (USER_ROLE_ADMIN, USER_ROLE_EDITOR, USER_ROLE_VIEWER)


class Thread(Base):
    """对话线程元数据 — 索引表，实际消息存储在 LangGraph checkpoint 中。

    每个 Thread 关联一个 LangGraph thread_id，用户通过侧边栏切换对话时
    加载对应 checkpoint 中的消息历史。
    """

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User", back_populates="threads")


class RpaJob(Base):
    """RPA 任务队列（DB 当队列）— 提交即入队，后台调度器按序认领执行。

    状态流转：queued → running → done / failed。一次认领一个（调度器 + FOR
    UPDATE SKIP LOCKED），多台 executor 未来可共享此表而不重复取任务。
    """

    __tablename__ = "rpa_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued", index=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
