"""Phase 2 测试 — Human-in-the-Loop 审批模块。

测试 classify_tool_risk、build_approval_message、build_approval_payload 等纯函数。
"""

import pytest
from agent.approval import (
    classify_tool_risk,
    build_approval_message,
    build_approval_payload,
    get_approval_mode,
    check_command_policy,
    HIGH_RISK_TOOLS,
)


# ═══════════════════════════════════════════════════
# classify_tool_risk
# ═══════════════════════════════════════════════════

class TestClassifyToolRisk:
    """测试风险分类逻辑。"""

    def test_execute_code_is_high_risk(self):
        needs, level, reason = classify_tool_risk("execute_code")
        assert needs is True
        assert level == "high"

    def test_rpa_tool_is_high_risk(self):
        needs, level, reason = classify_tool_risk("rpa_start_browser")
        assert needs is True
        assert level == "high"

    def test_mcp_write_file_is_medium_risk(self):
        needs, level, reason = classify_tool_risk("mcp_write_file")
        assert needs is True
        assert level == "medium"

    def test_mcp_delete_files_is_high_risk(self):
        needs, level, reason = classify_tool_risk("mcp_delete_files")
        assert needs is True
        assert level == "high"

    def test_mcp_search_files_is_low_risk(self):
        needs, level, reason = classify_tool_risk("mcp_search_files")
        assert needs is False
        assert level == "low"

    def test_mcp_edit_file_is_medium_risk(self):
        needs, level, reason = classify_tool_risk("mcp_edit_file")
        assert needs is True
        assert level == "medium"

    def test_mcp_move_file_is_medium_risk(self):
        needs, level, reason = classify_tool_risk("mcp_move_file")
        assert needs is True
        assert level == "medium"

    def test_tool_forget_memory_is_medium_risk(self):
        needs, level, reason = classify_tool_risk("tool_forget_memory")
        assert needs is True
        assert level == "medium"

    def test_tool_index_knowledge_is_medium_risk(self):
        needs, level, reason = classify_tool_risk("tool_index_knowledge")
        assert needs is True
        assert level == "medium"

    def test_docker_tool_is_high_risk(self):
        needs, level, reason = classify_tool_risk("mcp_docker_restart_container")
        assert needs is True
        assert level == "high"

    def test_unknown_tool_is_low_risk_in_standard_mode(self):
        needs, level, reason = classify_tool_risk("tool_search_knowledge")
        assert needs is False
        assert level == "low"

    def test_rpa_prefix_match(self):
        """rpa_ 前缀匹配所有浏览器自动化工具。"""
        needs, _, _ = classify_tool_risk("rpa_navigate")
        assert needs is True

        needs2, _, _ = classify_tool_risk("rpa_click_element")
        assert needs2 is True

    def test_amazon_prefix_match(self):
        needs, _, _ = classify_tool_risk("amazon_list_products")
        assert needs is True

    def test_submit_rpa_is_high_risk(self):
        """submit_rpa_* 提交任务 = 高风险（拦提交，可能影响生产店铺数据）。"""
        needs, level, reason = classify_tool_risk("submit_rpa_update_track_table")
        assert needs is True
        assert level == "high"
        assert reason

    # ── 审批模式 ──

    def test_off_mode_never_requires_approval(self):
        needs, level, reason = classify_tool_risk("execute_code", mode="off")
        assert needs is False
        assert level == "low"

    def test_standard_mode_default(self):
        """standard 模式（默认）：不在注册表的工具自动放行。"""
        needs, level, reason = classify_tool_risk("tool_search_knowledge", mode="standard")
        assert needs is False

    def test_strict_mode_requires_approval_for_unknown(self):
        """strict 模式：不在注册表的工具默认需要审批。"""
        needs, level, reason = classify_tool_risk("some_unknown_tool", mode="strict")
        assert needs is True
        assert level == "medium"

    def test_strict_mode_low_risk_still_allowed(self):
        """strict 模式下，显式标记为 low 的工具仍然放行。"""
        needs, level, reason = classify_tool_risk("mcp_search_files", mode="strict")
        assert needs is False

    def test_empty_tool_name(self):
        needs, level, reason = classify_tool_risk("")
        assert needs is False


# ═══════════════════════════════════════════════════
# build_approval_message
# ═══════════════════════════════════════════════════

class TestBuildApprovalMessage:
    """测试审批消息构建。"""

    def test_high_risk_message_contains_emoji(self):
        msg = build_approval_message("execute_code", {"code": "print(1)"})
        assert "执行代码" in msg
        assert "execute_code" in msg
        assert "print(1)" in msg
        assert "HIGH" in msg

    def test_medium_risk_message(self):
        msg = build_approval_message("mcp_write_file", {"path": "/tmp/x.txt"})
        assert "写入文件" in msg
        assert "MEDIUM" in msg

    def test_low_risk_returns_empty(self):
        msg = build_approval_message("tool_search_knowledge", {"query": "x"})
        assert msg == ""

    def test_none_args(self):
        msg = build_approval_message("execute_code", None)
        assert "(无参数)" in msg

    def test_long_args_truncated(self):
        msg = build_approval_message("execute_code", {"code": "x" * 500})
        assert len(msg) < 600  # args truncated at 300 chars

    def test_unknown_tool_in_standard_mode_returns_empty(self):
        msg = build_approval_message("unknown_tool", {})
        assert msg == ""


# ═══════════════════════════════════════════════════
# build_approval_payload
# ═══════════════════════════════════════════════════

class TestBuildApprovalPayload:
    """测试审批 payload 构建。"""

    def test_single_risky_call(self):
        calls = [{"id": "tc1", "name": "execute_code", "args": {"code": "print(1)"}}]
        payload = build_approval_payload(calls)
        assert payload["type"] == "approval_required"
        assert payload["total_risky"] == 1
        assert len(payload["calls"]) == 1
        assert payload["calls"][0]["risk_level"] == "high"

    def test_mixed_risk_calls(self):
        calls = [
            {"id": "tc1", "name": "execute_code", "args": {}},
            {"id": "tc2", "name": "mcp_write_file", "args": {}},
        ]
        payload = build_approval_payload(calls)
        assert payload["total_risky"] == 2
        assert payload["calls"][0]["risk_level"] == "high"
        assert payload["calls"][1]["risk_level"] == "medium"

    def test_summary_counts_high_and_medium(self):
        calls = [
            {"id": "tc1", "name": "execute_code", "args": {}},
            {"id": "tc2", "name": "mcp_write_file", "args": {}},
        ]
        payload = build_approval_payload(calls)
        assert "1 个高风险操作" in payload["message"]
        assert "1 个中风险操作" in payload["message"]

    def test_empty_calls(self):
        payload = build_approval_payload([])
        assert payload["total_risky"] == 0


# ═══════════════════════════════════════════════════
# get_approval_mode
# ═══════════════════════════════════════════════════

class TestGetApprovalMode:
    """测试审批模式读取。"""

    def test_default_is_standard(self, monkeypatch):
        monkeypatch.delenv("APPROVAL_MODE", raising=False)
        assert get_approval_mode() == "standard"

    def test_off(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_MODE", "off")
        assert get_approval_mode() == "off"

    def test_strict(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_MODE", "strict")
        assert get_approval_mode() == "strict"

    def test_invalid_falls_back_to_standard(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_MODE", "invalid_value")
        assert get_approval_mode() == "standard"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_MODE", "OFF")
        assert get_approval_mode() == "off"


# ═══════════════════════════════════════════════════
# check_command_policy（命令级策略，借鉴 Codex execpolicy）
# ═══════════════════════════════════════════════════

class TestCheckCommandPolicy:
    def test_forbidden_command(self):
        is_forbidden, reason = check_command_policy({"code": "rm -rf /tmp"})
        assert is_forbidden is True
        assert "禁止" in reason

    def test_allowed_command(self):
        is_forbidden, _ = check_command_policy({"code": "ls -la"})
        assert is_forbidden is False

    def test_multiline_any_forbidden(self):
        code = "ls -la\nrm -rf /\n"
        is_forbidden, _ = check_command_policy({"code": code})
        assert is_forbidden is True

    def test_script_key(self):
        is_forbidden, _ = check_command_policy({"script": "sudo reboot"})
        assert is_forbidden is True

    def test_non_dict_args(self):
        assert check_command_policy(None) == (False, "")
        assert check_command_policy("not a dict") == (False, "")

    def test_no_command_field(self):
        assert check_command_policy({"foo": "bar"}) == (False, "")


# ═══════════════════════════════════════════════════
# HIGH_RISK_TOOLS 注册表格式
# ═══════════════════════════════════════════════════

class TestHighRiskToolsRegistry:
    """测试高风险工具注册表格式。"""

    def test_all_entries_are_tuples(self):
        for entry in HIGH_RISK_TOOLS:
            assert isinstance(entry, tuple), f"Expected tuple, got {type(entry)}: {entry}"
            assert len(entry) == 3, f"Expected (prefix, level, reason), got: {entry}"

    def test_all_levels_are_valid(self):
        valid_levels = {"low", "medium", "high"}
        for prefix, level, reason in HIGH_RISK_TOOLS:
            assert level in valid_levels, f"Invalid level '{level}' for '{prefix}'"

    def test_all_reasons_are_non_empty(self):
        for prefix, level, reason in HIGH_RISK_TOOLS:
            assert reason, f"Empty reason for '{prefix}'"
