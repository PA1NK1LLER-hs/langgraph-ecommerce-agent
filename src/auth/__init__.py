from .permissions import (
    can_use_tool,
    check_tool_permission,
    role_can_skip_approval,
    role_at_least,
    HIGH_RISK_TOOLS,
    get_user_role,
    Role,
)

# B4：FastAPI 权限依赖已移至 api 层，这里仅做兼容再导出
from api.deps import require_admin, require_editor

__all__ = [
    "require_admin",
    "require_editor",
    "can_use_tool",
    "check_tool_permission",
    "role_can_skip_approval",
    "role_at_least",
    "HIGH_RISK_TOOLS",
    "get_user_role",
    "Role",
]
