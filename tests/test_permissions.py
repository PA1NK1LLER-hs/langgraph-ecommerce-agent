"""RBAC 权限系统测试。"""

import sys
from unittest.mock import MagicMock

import pytest

# Mock bcrypt for model import tests
if "bcrypt" not in sys.modules:
    sys.modules["bcrypt"] = MagicMock()


# ═══════════════════════════════════════════════════
# 角色层级测试
# ═══════════════════════════════════════════════════


class TestRoleHierarchy:
    def test_admin_above_all(self):
        from auth.permissions import role_at_least
        assert role_at_least("admin", "admin") is True
        assert role_at_least("admin", "editor") is True
        assert role_at_least("admin", "viewer") is True

    def test_editor_above_viewer(self):
        from auth.permissions import role_at_least
        assert role_at_least("editor", "editor") is True
        assert role_at_least("editor", "viewer") is True
        assert role_at_least("editor", "admin") is False

    def test_viewer_only_viewer(self):
        from auth.permissions import role_at_least
        assert role_at_least("viewer", "viewer") is True
        assert role_at_least("viewer", "editor") is False
        assert role_at_least("viewer", "admin") is False

    def test_unknown_role_defaults_to_zero(self):
        from auth.permissions import role_at_least
        assert role_at_least("unknown", "viewer") is False
        assert role_at_least("viewer", "unknown") is True


# ═══════════════════════════════════════════════════
# 工具权限测试
# ═══════════════════════════════════════════════════


class TestToolPermissions:
    def test_admin_can_use_all_tools(self):
        from auth.permissions import can_use_tool
        for tool in ["execute_code", "rpa_browser_navigate", "mcp_playwright",
                      "mcp_docker", "mcp_write_file", "tool_search_knowledge",
                      "tool_forget_memory", "tool_index_knowledge", "unknown_tool"]:
            allowed, reason = can_use_tool(tool, "admin")
            assert allowed, f"Admin should be able to use {tool}: {reason}"

    def test_editor_can_use_high_risk_tools(self):
        from auth.permissions import can_use_tool
        allowed, _ = can_use_tool("execute_code", "editor")
        assert allowed

        allowed, _ = can_use_tool("mcp_write_file", "editor")
        assert allowed

        allowed, _ = can_use_tool("rpa_browser_click", "editor")
        assert allowed

        allowed, _ = can_use_tool("submit_rpa_update_track_table", "editor")
        assert allowed

    def test_editor_can_write_kb(self):
        from auth.permissions import can_use_tool
        allowed, _ = can_use_tool("tool_index_knowledge", "editor")
        assert allowed

    def test_editor_can_forget_memory(self):
        from auth.permissions import can_use_tool
        allowed, _ = can_use_tool("tool_forget_memory", "editor")
        assert allowed

    def test_viewer_cannot_use_high_risk_tools(self):
        from auth.permissions import can_use_tool
        for tool in ["execute_code", "mcp_write_file", "mcp_docker", "mcp_playwright",
                      "rpa_browser_navigate", "mcp_edit_file", "mcp_delete_files",
                      "submit_rpa_update_track_table"]:
            allowed, reason = can_use_tool(tool, "viewer")
            assert not allowed, f"Viewer should NOT be able to use {tool}"
            assert "观察者" in reason

    def test_viewer_cannot_write_kb(self):
        from auth.permissions import can_use_tool
        allowed, reason = can_use_tool("tool_index_knowledge", "viewer")
        assert not allowed
        assert "观察者" in reason

    def test_viewer_cannot_forget_memory(self):
        from auth.permissions import can_use_tool
        allowed, reason = can_use_tool("tool_forget_memory", "viewer")
        assert not allowed
        assert "观察者" in reason

    def test_viewer_can_search_and_read(self):
        from auth.permissions import can_use_tool
        allowed, _ = can_use_tool("tool_search_knowledge", "viewer")
        assert allowed

        allowed, _ = can_use_tool("tool_add_memory", "viewer")
        assert allowed

        allowed, _ = can_use_tool("tool_list_memories", "viewer")
        assert allowed

    def test_prefix_matching_for_rpa(self):
        from auth.permissions import can_use_tool
        # rpa_ prefix should match all RPA tools
        allowed, _ = can_use_tool("rpa_amazon_search", "editor")
        assert allowed

        allowed, reason = can_use_tool("rpa_amazon_search", "viewer")
        assert not allowed

    def test_unknown_tool_is_allowed_for_viewer(self):
        from auth.permissions import can_use_tool
        allowed, _ = can_use_tool("some_future_tool", "viewer")
        assert allowed


# ═══════════════════════════════════════════════════
# 审批跳过规则测试
# ═══════════════════════════════════════════════════


class TestApprovalSkip:
    def test_admin_skips_all(self):
        from auth.permissions import role_can_skip_approval
        assert role_can_skip_approval("admin", "high") is True
        assert role_can_skip_approval("admin", "medium") is True
        assert role_can_skip_approval("admin", "low") is True

    def test_editor_skips_low_and_medium(self):
        from auth.permissions import role_can_skip_approval
        assert role_can_skip_approval("editor", "low") is True
        assert role_can_skip_approval("editor", "medium") is True
        assert role_can_skip_approval("editor", "high") is False

    def test_viewer_only_skips_low(self):
        from auth.permissions import role_can_skip_approval
        assert role_can_skip_approval("viewer", "low") is True
        assert role_can_skip_approval("viewer", "medium") is False
        assert role_can_skip_approval("viewer", "high") is False


# ═══════════════════════════════════════════════════
# get_user_role 测试
# ═══════════════════════════════════════════════════


class TestGetUserRole:
    def test_from_object_with_role(self):
        from auth.permissions import get_user_role

        class FakeUser:
            role = "admin"

        assert get_user_role(FakeUser()) == "admin"

    def test_from_object_without_role(self):
        from auth.permissions import get_user_role

        class FakeUser:
            pass

        assert get_user_role(FakeUser()) == "viewer"  # default

    def test_from_dict(self):
        from auth.permissions import get_user_role
        # get_user_role 只适用于有 role 属性的对象，dict 需先取 .get("role")
        # 此处验证 hasattr 检查逻辑
        assert get_user_role(type("Fake", (), {"role": "editor"})()) == "editor"


# ═══════════════════════════════════════════════════
# 用户模型测试
# ═══════════════════════════════════════════════════


class TestUserModelRoles:
    @pytest.fixture(autouse=True)
    def _ensure_deps(self):
        """确保 bcrypt 和 sqlalchemy 可用。"""
        pytest.importorskip("bcrypt")
        pytest.importorskip("sqlalchemy")

    def test_role_constants(self):
        from api.models import USER_ROLE_ADMIN, USER_ROLE_EDITOR, USER_ROLE_VIEWER, VALID_ROLES
        assert USER_ROLE_ADMIN == "admin"
        assert USER_ROLE_EDITOR == "editor"
        assert USER_ROLE_VIEWER == "viewer"
        assert "admin" in VALID_ROLES
        assert "editor" in VALID_ROLES
        assert "viewer" in VALID_ROLES

    def test_user_is_admin(self):
        from api.models import User
        user = User(username="admin_test", role="admin")
        assert user.is_admin is True
        assert user.is_editor is True

    def test_user_is_editor(self):
        from api.models import User
        user = User(username="editor_test", role="editor")
        assert user.is_admin is False
        assert user.is_editor is True

    def test_user_is_viewer(self):
        from api.models import User
        user = User(username="viewer_test", role="viewer")
        assert user.is_admin is False
        assert user.is_editor is False


# ═══════════════════════════════════════════════════
# Token schema 测试
# ═══════════════════════════════════════════════════


class TestTokenSchema:
    def test_token_response_includes_role(self):
        from api.schemas import TokenResponse
        resp = TokenResponse(access_token="test", username="user")
        assert resp.role == "viewer"  # default

    def test_user_response_includes_role(self):
        from api.schemas import UserResponse
        resp = UserResponse(id=1, username="user", role="admin")
        assert resp.role == "admin"
        assert resp.username == "user"
