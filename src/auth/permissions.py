"""RBAC 权限系统 — 角色级访问控制。

角色层级:
  admin  — 全权限：可执行任意工具、管理知识库、管理用户、跳过审批
  editor — 读写：可执行非破坏性工具、上传/管理知识库、需审批高风险操作
  viewer — 只读：只能对话和检索，不能修改知识库或执行高风险工具

工具权限：
  - 高风险工具（execute_code, RPA, Docker, 文件删除/写入）: admin/editor
  - 知识库写入（index_knowledge）: editor+
  - 知识库读取（search_knowledge）: viewer+
  - 用户记忆修改（forget_memory）: editor+

审批规则：
  - admin:  可跳过所有审批
  - editor: 低/中风险跳过，高风险需审批
  - viewer: 所有写操作都需审批

注意：本模块只包含纯函数，不依赖 FastAPI/数据库。
FastAPI 权限依赖（require_admin / require_editor）位于 `api.deps`，
经 `auth/__init__.py` 兼容再导出。
"""

from __future__ import annotations

import logging
from enum import Enum

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 角色定义
# ---------------------------------------------------------------------------


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


ROLE_HIERARCHY: dict[str, int] = {
    Role.ADMIN: 3,
    Role.EDITOR: 2,
    Role.VIEWER: 1,
}


def role_at_least(user_role: str, required: str) -> bool:
    """检查用户角色是否达到指定级别。"""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required, 0)


# ---------------------------------------------------------------------------
# 工具权限定义
# ---------------------------------------------------------------------------


# 高风险工具 — 仅 admin/editor 可调用
HIGH_RISK_TOOLS: set[str] = {
    "execute_code",
    "rpa_",
    "mcp_playwright",
    "mcp_docker",
    "mcp_write_file",
    "mcp_edit_file",
    "mcp_delete_files",
    "mcp_move_file",
}

# 知识库写操作 — editor+ 可调用
KB_WRITE_TOOLS: set[str] = {
    "tool_index_knowledge",
}

# 记忆写操作 — editor+ 可调用
MEMORY_WRITE_TOOLS: set[str] = {
    "tool_forget_memory",
}

# admin 专属工具
ADMIN_ONLY_TOOLS: set[str] = set()  # 预留：用户管理、系统配置等


def can_use_tool(tool_name: str, user_role: str) -> tuple[bool, str]:
    """检查用户是否可以使用指定工具。

    Args:
        tool_name: 工具名称。
        user_role: 用户角色。

    Returns:
        (是否允许, 拒绝原因) — 允许时原因为空字符串。
    """
    # admin 可使用所有工具
    if user_role == Role.ADMIN:
        return True, ""

    # admin 专属工具
    if tool_name in ADMIN_ONLY_TOOLS:
        return False, f"工具 '{tool_name}' 仅限管理员使用"

    # 高风险工具 — 需 editor+
    for prefix in HIGH_RISK_TOOLS:
        if tool_name.startswith(prefix) or tool_name == prefix:
            if user_role == Role.VIEWER:
                return False, f"观察者不能使用高风险工具 '{tool_name}'（需编辑者以上权限）"
            return True, ""

    # 知识库写入
    if tool_name in KB_WRITE_TOOLS:
        if user_role == Role.VIEWER:
            return False, "观察者不能修改知识库（需编辑者以上权限）"
        return True, ""

    # 记忆写入
    if tool_name in MEMORY_WRITE_TOOLS:
        if user_role == Role.VIEWER:
            return False, "观察者不能删除记忆（需编辑者以上权限）"
        return True, ""

    # 其他工具 — viewer+ 可调用
    return True, ""


def role_can_skip_approval(user_role: str, risk_level: str) -> bool:
    """判断用户角色是否可以跳过指定风险级别的审批。

    Args:
        user_role: 用户角色。
        risk_level: 工具风险级别 — "low" / "medium" / "high"。

    Returns:
        True 表示可跳过审批。
    """
    if user_role == Role.ADMIN:
        return True
    if user_role == Role.EDITOR:
        return risk_level in ("low", "medium")
    # viewer — 任何写操作都需审批
    return risk_level == "low"


def get_user_role(user) -> str:
    """从用户对象提取角色字符串。"""
    if hasattr(user, "role"):
        return user.role
    return Role.VIEWER


# ---------------------------------------------------------------------------
# FastAPI 权限依赖
# ---------------------------------------------------------------------------
# B4：FastAPI 依赖（require_admin / require_editor）已移至 api.deps，
# 本模块只保留纯函数，不再反向依赖 Web 层。兼容导入见 auth/__init__.py。


async def check_tool_permission(tool_name: str, user_role: str) -> None:
    """检查工具权限，无权限时抛出 HTTPException。"""
    allowed, reason = can_use_tool(tool_name, user_role)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=reason,
        )
