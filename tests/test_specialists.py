"""多 Agent 协作（Supervisor 模式）测试。"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Specialist 数据模型测试
# ---------------------------------------------------------------------------


class TestSpecialistDefinitions:
    """测试 Specialist 配置定义。"""

    def test_specialists_registered(self):
        """所有 specialist 应在注册表中。"""
        from agent.specialists import SPECIALISTS, SPECIALISTS_LIST
        assert len(SPECIALISTS) == 4
        assert len(SPECIALISTS_LIST) == 4
        assert set(SPECIALISTS.keys()) == {"researcher", "coder", "analyst", "general"}

    def test_researcher_has_search_tools(self):
        """研究员应包含搜索相关工具前缀。"""
        from agent.specialists import RESEARCHER
        assert "tool_search_knowledge" in RESEARCHER.tool_prefixes
        assert "mcp_searxng_" in RESEARCHER.tool_prefixes
        assert "tool_add_memory" in RESEARCHER.tool_prefixes

    def test_coder_has_code_and_file_tools(self):
        """代码专家应包含执行和文件工具前缀。"""
        from agent.specialists import CODER
        assert "execute_code" in CODER.tool_prefixes
        assert "mcp_read_file" in CODER.tool_prefixes
        assert "mcp_write_file" in CODER.tool_prefixes

    def test_analyst_has_code_and_data_tools(self):
        """数据分析师应包含代码执行和文件读取工具。"""
        from agent.specialists import ANALYST
        assert "execute_code" in ANALYST.tool_prefixes
        assert "mcp_read_file" in ANALYST.tool_prefixes

    def test_general_allows_all_tools(self):
        """通用助手应设定空工具前缀（全部工具）。"""
        from agent.specialists import GENERAL
        assert GENERAL.tool_prefixes == []
        assert GENERAL.name == "general"

    def test_specialist_dataclass_fields(self):
        """验证 Specialist dataclass 各字段。"""
        from agent.specialists import Specialist
        s = Specialist(
            name="test",
            display_name="测试",
            description="测试用",
            tool_prefixes=["tool_a", "tool_b"],
            system_prompt_suffix="这是测试",
            icon="🧪",
        )
        assert s.name == "test"
        assert s.display_name == "测试"
        assert s.description == "测试用"
        assert s.tool_prefixes == ["tool_a", "tool_b"]
        assert s.system_prompt_suffix == "这是测试"
        assert s.icon == "🧪"

    def test_get_specialist_valid(self):
        """get_specialist() 应返回正确的 specialist。"""
        from agent.specialists import get_specialist
        sp = get_specialist("researcher")
        assert sp is not None
        assert sp.name == "researcher"
        assert sp.display_name == "研究员"

    def test_get_specialist_invalid(self):
        """get_specialist() 对无效名称返回 None。"""
        from agent.specialists import get_specialist
        assert get_specialist("nonexistent") is None
        assert get_specialist("") is None


# ---------------------------------------------------------------------------
# match_specialist_tools 测试
# ---------------------------------------------------------------------------


class TestMatchSpecialistTools:
    """测试 specialist 工具匹配。"""

    def test_match_by_exact_name(self):
        """精确名称应匹配。"""
        from agent.specialists import match_specialist_tools
        mock_tools = {
            "tool_a": MagicMock(name="tool_a"),
            "tool_b": MagicMock(name="tool_b"),
            "tool_c": MagicMock(name="tool_c"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        specialist = MagicMock()
        specialist.tool_prefixes = ["tool_a", "tool_b"]

        matched = match_specialist_tools(specialist, mock_tools)
        assert len(matched) == 2
        names = {t.name for t in matched}
        assert names == {"tool_a", "tool_b"}

    def test_match_by_prefix(self):
        """前缀匹配应覆盖所有同前缀工具。"""
        from agent.specialists import match_specialist_tools
        mock_tools = {
            "rpa_navigate": MagicMock(name="rpa_navigate"),
            "rpa_click": MagicMock(name="rpa_click"),
            "rpa_fill": MagicMock(name="rpa_fill"),
            "execute_code": MagicMock(name="execute_code"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        specialist = MagicMock()
        specialist.tool_prefixes = ["rpa_", "execute_code"]

        matched = match_specialist_tools(specialist, mock_tools)
        assert len(matched) == 4

    def test_empty_prefixes_returns_all(self):
        """空前缀列表应返回全部工具。"""
        from agent.specialists import match_specialist_tools
        mock_tools = {
            "tool_a": MagicMock(name="tool_a"),
            "tool_b": MagicMock(name="tool_b"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        specialist = MagicMock()
        specialist.tool_prefixes = []

        matched = match_specialist_tools(specialist, mock_tools)
        assert len(matched) == 2

    def test_no_match_returns_empty(self):
        """无匹配时应返回空列表。"""
        from agent.specialists import match_specialist_tools
        mock_tools = {
            "tool_a": MagicMock(name="tool_a"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        specialist = MagicMock()
        specialist.tool_prefixes = ["nonexistent_prefix"]

        matched = match_specialist_tools(specialist, mock_tools)
        assert len(matched) == 0

    def test_no_duplicates(self):
        """重叠前缀不应产生重复工具。"""
        from agent.specialists import match_specialist_tools
        mock_tools = {
            "tool_search_knowledge": MagicMock(name="tool_search_knowledge"),
            "search_web": MagicMock(name="search_web"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        specialist = MagicMock()
        # 两个前缀都能匹配同一个工具
        specialist.tool_prefixes = ["tool_search", "tool_search_knowledge"]

        matched = match_specialist_tools(specialist, mock_tools)
        names = [t.name for t in matched]
        # 不应有重复
        assert len(names) == len(set(names))

    def test_researcher_matches_kb_tools(self):
        """研究员应匹配知识库和搜索工具。"""
        from agent.specialists import RESEARCHER, match_specialist_tools
        mock_tools = {
            "tool_search_knowledge": MagicMock(name="tool_search_knowledge"),
            "tool_list_knowledge_sources": MagicMock(name="tool_list_knowledge_sources"),
            "tool_add_memory": MagicMock(name="tool_add_memory"),
            "execute_code": MagicMock(name="execute_code"),
            "mcp_searxng_web_search": MagicMock(name="mcp_searxng_web_search"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        matched = match_specialist_tools(RESEARCHER, mock_tools)
        names = {t.name for t in matched}
        assert "tool_search_knowledge" in names
        assert "mcp_searxng_web_search" in names
        assert "tool_add_memory" in names
        assert "execute_code" not in names

    def test_coder_excludes_kb_tools(self):
        """代码专家不应包含知识库搜索工具。"""
        from agent.specialists import CODER, match_specialist_tools
        mock_tools = {
            "execute_code": MagicMock(name="execute_code"),
            "mcp_read_file": MagicMock(name="mcp_read_file"),
            "tool_search_knowledge": MagicMock(name="tool_search_knowledge"),
        }
        for t in mock_tools.values():
            t.name = t._mock_name

        matched = match_specialist_tools(CODER, mock_tools)
        names = {t.name for t in matched}
        assert "execute_code" in names
        assert "mcp_read_file" in names
        assert "tool_search_knowledge" not in names


# ---------------------------------------------------------------------------
# Supervisor Prompt 测试
# ---------------------------------------------------------------------------


class TestSupervisorPrompt:
    """测试 Supervisor 提示词。"""

    def test_prompt_includes_all_specialists(self):
        """Supervisor prompt 应提到所有 4 个 specialist。"""
        from agent.specialists import SUPERVISOR_PROMPT
        assert "研究员" in SUPERVISOR_PROMPT
        assert "代码专家" in SUPERVISOR_PROMPT
        assert "数据分析师" in SUPERVISOR_PROMPT
        assert "通用助手" in SUPERVISOR_PROMPT

    def test_prompt_has_json_output_instruction(self):
        """Prompt 应要求 JSON 输出。"""
        from agent.specialists import SUPERVISOR_PROMPT
        assert "JSON" in SUPERVISOR_PROMPT.upper() or "json" in SUPERVISOR_PROMPT
        assert "specialist" in SUPERVISOR_PROMPT
        assert "reason" in SUPERVISOR_PROMPT


# ---------------------------------------------------------------------------
# Specialist system prompt suffix 测试
# ---------------------------------------------------------------------------


class TestSpecialistPromptSuffix:
    """测试 specialist 专属提示词后缀。"""

    def test_researcher_suffix_mentions_search(self):
        from agent.specialists import RESEARCHER
        assert "检索" in RESEARCHER.system_prompt_suffix or "搜索" in RESEARCHER.system_prompt_suffix

    def test_coder_suffix_mentions_code(self):
        from agent.specialists import CODER
        assert "代码" in CODER.system_prompt_suffix

    def test_analyst_suffix_mentions_data(self):
        from agent.specialists import ANALYST
        assert "数据" in ANALYST.system_prompt_suffix

    def test_general_has_no_suffix(self):
        from agent.specialists import GENERAL
        assert GENERAL.system_prompt_suffix == ""


# ---------------------------------------------------------------------------
# 集成：specialist 工具筛选在 graph context 中
# ---------------------------------------------------------------------------


class TestSpecialistInGraphContext:
    """测试 specialist 在 graph 上下文中的行为。"""

    @pytest.fixture
    def mock_state(self):
        return {
            "messages": [],
            "intent": "complex",
            "specialist": "",
            "specialist_task": "",
            "specialist_history": [],
            "selected_tools": [],
            "needs_rag": False,
            "tool_failures": 0,
            "tool_retries": 0,
            "denied_tool_calls": [],
            "session_costs": None,
            "last_turn_cost": None,
        }

    def test_specialist_state_defaults(self, mock_state):
        """验证 specialist 字段默认值。"""
        assert mock_state["specialist"] == ""
        assert mock_state["specialist_task"] == ""
        assert mock_state["specialist_history"] == []

    def test_specialist_field_set_in_state(self, mock_state):
        """验证 state 可设置 specialist 字段。"""
        mock_state["specialist"] = "researcher"
        mock_state["specialist_task"] = "查找产品定价策略"
        mock_state["specialist_history"].append({
            "specialist": "researcher",
            "reason": "需要查询知识库",
            "timestamp": "2025-01-01T00:00:00Z",
        })

        assert mock_state["specialist"] == "researcher"
        assert mock_state["specialist_task"] == "查找产品定价策略"
        assert len(mock_state["specialist_history"]) == 1
        assert mock_state["specialist_history"][0]["specialist"] == "researcher"
