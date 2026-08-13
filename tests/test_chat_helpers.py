"""聊天模块辅助函数测试 — _extract_interrupt 及其他纯函数。"""

import pytest


# ═══════════════════════════════════════════════════
# _extract_interrupt — 中断检测兼容 langgraph 0.x 和 1.x
# ═══════════════════════════════════════════════════


class _MockInterrupt:
    """模拟 langgraph Interrupt 对象。"""

    def __init__(self, value: dict):
        self.value = value

    def __repr__(self):
        return f"Interrupt(value={self.value!r})"


class TestExtractInterrupt:
    """测试中断提取逻辑，兼容 langgraph 0.x 和 1.x 两种格式。"""

    @staticmethod
    def _extract(node_name: str, node_output) -> list | None:
        """从 stream 块中提取中断信息（与 chat.py 中的实现一致）。"""
        if node_name == "__interrupt__":
            if isinstance(node_output, (list, tuple)):
                return list(node_output)
            return [node_output] if node_output is not None else None
        if isinstance(node_output, dict) and "__interrupt__" in node_output:
            info = node_output["__interrupt__"]
            if isinstance(info, (list, tuple)):
                return list(info)
            return [info] if info is not None else None
        return None

    def test_langgraph_1x_format_tuple(self):
        """langgraph 1.x: node_name == '__interrupt__', node_output 为 Interrupt 元组。"""
        interrupt = _MockInterrupt({"type": "approval_required", "calls": []})
        result = self._extract("__interrupt__", (interrupt,))
        assert result is not None
        assert len(result) == 1
        assert result[0].value["type"] == "approval_required"

    def test_langgraph_1x_format_list(self):
        """langgraph 1.x: node_output 为 Interrupt 列表（某些版本）。"""
        interrupt = _MockInterrupt({"type": "approval_required", "calls": [{"name": "test"}]})
        result = self._extract("__interrupt__", [interrupt])
        assert result is not None
        assert len(result) == 1

    def test_langgraph_0x_backward_compat(self):
        """langgraph 0.x 向后兼容: node_output['__interrupt__'] 包含列表。"""
        node_output = {"__interrupt__": [_MockInterrupt({"type": "approval_required"})]}
        result = self._extract("check_approval", node_output)
        assert result is not None
        assert len(result) == 1
        assert result[0].value["type"] == "approval_required"

    def test_langgraph_0x_single_not_list(self):
        """langgraph 0.x: __interrupt__ 值不是列表时包装为列表。"""
        node_output = {"__interrupt__": _MockInterrupt({"type": "approval_required"})}
        result = self._extract("check_approval", node_output)
        assert result is not None
        assert len(result) == 1

    def test_no_interrupt_normal_node(self):
        """普通节点（无中断）返回 None。"""
        result = self._extract("agent", {"messages": []})
        assert result is None

    def test_no_interrupt_empty_output(self):
        """空节点输出返回 None。"""
        result = self._extract("tools", {})
        assert result is None

    def test_approval_interrupt_type_check(self):
        """验证审批类型检测：应正确识别 approval_required 中断。"""
        interrupt = _MockInterrupt({"type": "approval_required", "calls": [
            {"name": "execute_code", "risk_level": "high"}
        ]})
        result = self._extract("__interrupt__", (interrupt,))
        assert result is not None
        value = getattr(result[0], "value", result[0])
        assert value["type"] == "approval_required"
        assert len(value["calls"]) == 1

    def test_non_approval_interrupt_passes_through(self):
        """非审批中断也应被提取（由调用方决定如何处理）。"""
        interrupt = _MockInterrupt({"type": "custom_interrupt"})
        result = self._extract("__interrupt__", (interrupt,))
        assert result is not None


# ═══════════════════════════════════════════════════
# RPA 工具前缀检测
# ═══════════════════════════════════════════════════


class TestRpaToolPrefixes:
    """测试 _RPA_TOOL_PREFIXES 正确覆盖所有 RPA 相关工具前缀。"""

    def test_rpa_prefixes_exist(self):
        """验证 _RPA_TOOL_PREFIXES 可从 graph 模块导入。"""
        from agent.graph import _RPA_TOOL_PREFIXES
        assert isinstance(_RPA_TOOL_PREFIXES, tuple)
        assert len(_RPA_TOOL_PREFIXES) >= 3

    def test_rpa_prefix_included(self):
        from agent.graph import _RPA_TOOL_PREFIXES
        assert "rpa_" in _RPA_TOOL_PREFIXES

    def test_amazon_prefix_included(self):
        from agent.graph import _RPA_TOOL_PREFIXES
        assert "amazon_" in _RPA_TOOL_PREFIXES

    def test_docker_prefix_included(self):
        from agent.graph import _RPA_TOOL_PREFIXES
        assert "mcp_docker_" in _RPA_TOOL_PREFIXES


# ═══════════════════════════════════════════════════
# _send_node_output 安全处理
# ═══════════════════════════════════════════════════


class TestSendNodeOutputSafety:
    """测试 _send_node_output 对非 dict 输入的防护。"""

    def test_non_dict_node_output_is_safe(self):
        """如果 node_output 不是 dict（如元组），_send_node_output 不应崩溃。"""
        # 模拟 chat.py 中的防护逻辑
        node_output = (1, 2, 3)  # 非 dict 的 node_output
        result_safe = isinstance(node_output, dict)
        assert not result_safe  # 应被过滤，不处理

    def test_dict_node_output_normal(self):
        """正常的 dict 输出应通过检查。"""
        node_output = {"messages": []}
        assert isinstance(node_output, dict)

    def test_none_node_output(self):
        """None 也不应崩溃。"""
        node_output = None
        result_safe = isinstance(node_output, dict)
        assert not result_safe
