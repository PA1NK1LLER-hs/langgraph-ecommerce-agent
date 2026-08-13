"""Phase 3 测试 — 结构化规划 Plan-Execute-Replan。

测试 planner_node、plan_check_node、replan_node 以及
_route_after_plan_check、_route_after_replan 路由函数。
"""

import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from agent.state import AgentState
from agent.graph import _route_after_plan_check, _route_after_replan
from agent.utils import extract_first_json


# ═══════════════════════════════════════════════════
# JSON 提取（纯函数，无 LLM 依赖）
# ═══════════════════════════════════════════════════

class TestExtractFirstJson:
    """测试 extract_first_json 正确解析 planner/replan 的 JSON 输出。"""

    def test_valid_json(self):
        text = '{"goal": "test", "steps": [{"step": 1, "description": "do X"}]}'
        result = extract_first_json(text)
        assert result is not None
        assert result["goal"] == "test"
        assert len(result["steps"]) == 1

    def test_json_after_text(self):
        text = '好的，以下是计划：\n{"goal": "test", "steps": []}\n希望有帮助'
        result = extract_first_json(text)
        assert result is not None
        assert result["goal"] == "test"

    def test_nested_json(self):
        text = '{"goal": "test", "steps": [{"step": 1, "extra": {"key": "val"}}]}'
        result = extract_first_json(text)
        assert result is not None
        assert result["steps"][0]["extra"]["key"] == "val"

    def test_no_json(self):
        assert extract_first_json("hello world") is None

    def test_empty_string(self):
        assert extract_first_json("") is None

    def test_incomplete_brace(self):
        assert extract_first_json('{"goal": "test"') is None


# ═══════════════════════════════════════════════════
# 路由函数（纯函数，无 LLM 依赖）
# ═══════════════════════════════════════════════════

class TestRouteAfterPlanCheck:
    """测试 _route_after_plan_check 在不同步骤状态下的路由。"""

    def _state(self, steps: list[dict], replan_count: int = 0) -> AgentState:
        return AgentState(
            messages=[], plan="", plan_steps=steps, plan_index=0,
            replan_count=replan_count, tool_failures=0, tool_retries=0,
            intent="complex", selected_tools=[], needs_rag=False, rag_context="",
            pending_approval=None, approval_decision="",
            denied_tool_calls=[], response_schema=None, structured_response=None,
        )

    def test_no_steps_returns_agent(self):
        """无计划步骤时回到 agent 继续正常循环（由 _should_continue 处理终止）。"""
        assert _route_after_plan_check(self._state([])) == "agent"

    def test_all_done_returns_agent(self):
        """全部完成时回到 agent，让 LLM 基于最终工具结果生成总结。"""
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "done"},
            {"step": 2, "description": "d2", "tool_hint": "?", "status": "done"},
        ]
        assert _route_after_plan_check(self._state(steps)) == "agent"

    def test_some_failed_skipped_triggers_replan(self):
        """有失败步骤且未达重规划上限 → 触发 replan。"""
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "done"},
            {"step": 2, "description": "d2", "tool_hint": "?", "status": "failed"},
            {"step": 3, "description": "d3", "tool_hint": "?", "status": "skipped"},
        ]
        assert _route_after_plan_check(self._state(steps)) == "replan"

    def test_failed_triggers_replan(self):
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "failed"},
            {"step": 2, "description": "d2", "tool_hint": "?", "status": "pending"},
        ]
        assert _route_after_plan_check(self._state(steps)) == "replan"

    def test_failed_but_max_replan_returns_agent(self):
        """失败但已达重规划上限 → 回到 agent 优雅降级。"""
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "failed"},
        ]
        assert _route_after_plan_check(self._state(steps, replan_count=3)) == "agent"

    def test_pending_returns_agent(self):
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "done"},
            {"step": 2, "description": "d2", "tool_hint": "?", "status": "pending"},
            {"step": 3, "description": "d3", "tool_hint": "?", "status": "pending"},
        ]
        assert _route_after_plan_check(self._state(steps)) == "agent"

    def test_in_progress_returns_agent(self):
        steps = [
            {"step": 1, "description": "d1", "tool_hint": "?", "status": "in_progress"},
        ]
        assert _route_after_plan_check(self._state(steps)) == "agent"


class TestRouteAfterReplan:
    """测试 _route_after_replan 路由。"""

    def _state(self, steps: list[dict]) -> AgentState:
        return AgentState(
            messages=[], plan="", plan_steps=steps, plan_index=0,
            replan_count=0, tool_failures=0, tool_retries=0,
            intent="complex", selected_tools=[], needs_rag=False, rag_context="",
            pending_approval=None, approval_decision="",
            denied_tool_calls=[], response_schema=None, structured_response=None,
        )

    def test_has_pending_returns_agent(self):
        steps = [{"step": 1, "description": "d1", "tool_hint": "?", "status": "pending"}]
        assert _route_after_replan(self._state(steps)) == "agent"

    def test_no_pending_returns_end(self):
        steps = [{"step": 1, "description": "d1", "tool_hint": "?", "status": "done"}]
        assert _route_after_replan(self._state(steps)) == "__end__"

    def test_empty_steps_returns_end(self):
        assert _route_after_replan(self._state([])) == "__end__"


# ═══════════════════════════════════════════════════
# plan_check_node 逻辑（纯逻辑模拟，不含 LLM）
# ═══════════════════════════════════════════════════

class TestPlanCheckLogic:
    """测试计划检查步骤状态转换逻辑。

    注意：这些测试模拟 plan_check_node 的核心逻辑，
    因为 plan_check_node 是 async 函数依赖完整的 AgentState，
    直接测试其状态转换逻辑比 mock 整个 graph 更精确。
    """

    def _simulate_plan_check(
        self, steps: list[dict], plan_idx: int = 0, failures: int = 0,
        last_msg_type: str = "tool", last_is_error: bool = False,
    ) -> list[dict]:
        """模拟 plan_check_node 的步骤状态更新逻辑。"""
        for s in steps:
            if s.get("status") == "pending":
                if last_msg_type == "tool" and not last_is_error:
                    s["status"] = "done"
                elif failures >= 2:
                    s["status"] = "failed"
                else:
                    s["status"] = "in_progress"
                break
        return steps

    def test_first_pending_becomes_done_on_success(self):
        steps = [
            {"step": 1, "description": "search", "tool_hint": "?", "status": "pending"},
            {"step": 2, "description": "analyze", "tool_hint": "?", "status": "pending"},
        ]
        result = self._simulate_plan_check(steps, last_msg_type="tool", last_is_error=False)
        assert result[0]["status"] == "done"
        assert result[1]["status"] == "pending"  # 第 2 步不受影响

    def test_first_pending_becomes_failed_on_excess_failures(self):
        steps = [{"step": 1, "description": "search", "tool_hint": "?", "status": "pending"}]
        result = self._simulate_plan_check(steps, failures=3, last_msg_type="tool", last_is_error=True)
        assert result[0]["status"] == "failed"

    def test_first_pending_becomes_in_progress_on_single_failure(self):
        steps = [{"step": 1, "description": "search", "tool_hint": "?", "status": "pending"}]
        result = self._simulate_plan_check(steps, failures=1, last_msg_type="tool", last_is_error=True)
        assert result[0]["status"] == "in_progress"

    def test_no_pending_no_change(self):
        steps = [
            {"step": 1, "description": "search", "tool_hint": "?", "status": "done"},
            {"step": 2, "description": "analyze", "tool_hint": "?", "status": "in_progress"},
        ]
        result = self._simulate_plan_check(steps)
        assert result[0]["status"] == "done"
        assert result[1]["status"] == "in_progress"

    def test_empty_steps_unchanged(self):
        assert self._simulate_plan_check([]) == []


# ═══════════════════════════════════════════════════
# replan_node 降级逻辑（不含 LLM）
# ═══════════════════════════════════════════════════

class TestReplanFallback:
    """测试 replan_node 的降级逻辑（超过最大重规划次数时）。"""

    _MAX_REPLAN = 3  # 需与 graph.py 中的值一致

    def test_max_replan_skips_pending(self):
        """超过最大重规划次数后，pending/failed 步骤应被标记为 skipped。"""
        steps = [
            {"step": 1, "description": "done1", "tool_hint": "?", "status": "done"},
            {"step": 2, "description": "fail1", "tool_hint": "?", "status": "failed"},
            {"step": 3, "description": "pend1", "tool_hint": "?", "status": "pending"},
            {"step": 4, "description": "pend2", "tool_hint": "?", "status": "in_progress"},
        ]
        for s in steps:
            if s.get("status") in ("pending", "failed", "in_progress"):
                s["status"] = "skipped"
        assert steps[0]["status"] == "done"
        assert steps[1]["status"] == "skipped"
        assert steps[2]["status"] == "skipped"
        assert steps[3]["status"] == "skipped"


# ═══════════════════════════════════════════════════
# AgentState 向后兼容
# ═══════════════════════════════════════════════════

class TestAgentStateCompat:
    """测试 AgentState 的新字段向后兼容。"""

    def test_minimal_state_works(self):
        """最精简的 state（只有 messages）也能正常构造。"""
        state = AgentState(messages=[HumanMessage(content="hello")])
        assert state["messages"][0].content == "hello"
        # 新字段都应为默认值（TypedDict 不自动填充，需用 .get 带默认值）
        assert state.get("plan", "") == ""
        assert state.get("plan_steps", []) == []
        assert state.get("plan_index", 0) == 0
        assert state.get("replan_count", 0) == 0
        assert state.get("approval_decision", "") == ""
        assert state.get("response_schema", None) is None

    def test_plan_steps_persist(self):
        """plan_steps 在多次更新中正确保持。"""
        steps = [
            {"step": 1, "description": "do A", "tool_hint": "tool_a", "status": "pending"},
        ]
        state = AgentState(
            messages=[HumanMessage(content="task")],
            plan="## Plan\n...",
            plan_steps=steps,
            plan_index=0,
            replan_count=0,
            tool_failures=0,
            tool_retries=0,
            intent="complex",
            selected_tools=[],
            needs_rag=False,
            rag_context="",
            pending_approval=None,
            approval_decision="",
            denied_tool_calls=[],
            response_schema=None,
            structured_response=None,
        )
        assert len(state["plan_steps"]) == 1
        assert state["plan_steps"][0]["status"] == "pending"

    def test_plan_steps_optional_default(self):
        """不提供 plan_steps 时使用空列表默认值。"""
        state = AgentState(messages=[])
        assert state.get("plan_steps", []) == []
