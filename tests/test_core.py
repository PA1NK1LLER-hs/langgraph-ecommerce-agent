"""create_llm() + core tool definitions tests."""

from unittest.mock import patch

from agent.core import (
    create_llm,
    get_core_tools,
    get_all_tools,
    set_session_prompt_tokens,
    set_context_window_tokens,
    get_context_remaining_tokens,
)


class TestCreateLLM:
    def test_basic_instantiation(self):
        with patch("agent.core.LLM_API_KEY", "test-key"), \
             patch("agent.core.LLM_BASE_URL", "https://api.test.local/v1"):
            llm = create_llm(model="test-model", temperature=0.3)
            assert llm is not None
            assert llm.model_name == "test-model"

    def test_default_headers_has_api_key(self):
        with patch("agent.core.LLM_API_KEY", "tp-test-key"), \
             patch("agent.core.LLM_BASE_URL", "https://api.test.local/v1"):
            llm = create_llm(model="test-model")
            headers = getattr(llm, "default_headers", {}) or {}
            assert "api-key" in headers

    def test_max_tokens_optional(self):
        with patch("agent.core.LLM_API_KEY", "test-key"), \
             patch("agent.core.LLM_BASE_URL", "https://api.test.local/v1"):
            llm = create_llm(model="test-model", temperature=0)
            assert llm is not None

    def test_max_tokens_set(self):
        with patch("agent.core.LLM_API_KEY", "test-key"), \
             patch("agent.core.LLM_BASE_URL", "https://api.test.local/v1"):
            llm = create_llm(model="test-model", temperature=0, max_tokens=1024)
            assert llm is not None
            assert llm.max_tokens == 1024


class TestCoreTools:
    def test_get_core_tools_count(self):
        tools = get_core_tools()
        # 基础工具 11 + RPA 提交工具 3（submit_rpa_*）+ RPA 查询工具 1（get_rpa_job_status） = 15
        assert len(tools) == 15

    def test_core_tool_names(self):
        names = [t.name for t in get_core_tools()]
        assert "tool_search_knowledge" in names
        assert "tool_index_knowledge" in names
        assert "tool_add_memory" in names
        assert "tool_search_memory" in names
        assert "tool_forget_memory" in names
        assert "tool_list_memories" in names
        assert "tool_list_knowledge_sources" in names

    def test_submit_rpa_tools_present(self):
        names = {t.name for t in get_core_tools()}
        assert "submit_rpa_query_campaign_spend" in names
        assert "submit_rpa_collect_amazon_review" in names
        assert "submit_rpa_update_track_table" in names

    def test_rpa_status_query_tool_present(self):
        """get_rpa_job_status 挂在 core tools（结果回流对话，只读查询）。"""
        names = {t.name for t in get_core_tools()}
        assert "get_rpa_job_status" in names

    def test_get_all_tools_includes_core(self):
        core_names = {t.name for t in get_core_tools()}
        all_names = {t.name for t in get_all_tools()}
        assert core_names <= all_names


class TestContextRemaining:
    """get_context_remaining 元认知工具（借鉴 Codex）。"""

    def teardown_method(self):
        set_session_prompt_tokens(None)

    def test_none_when_unknown(self):
        set_session_prompt_tokens(None)
        assert get_context_remaining_tokens() is None

    def test_remaining_computed(self):
        set_context_window_tokens(128000)
        set_session_prompt_tokens(8000)
        assert get_context_remaining_tokens() == 120000

    def test_floor_zero(self):
        set_context_window_tokens(100)
        set_session_prompt_tokens(500)
        assert get_context_remaining_tokens() == 0

    def test_tool_present_in_core_tools(self):
        names = {t.name for t in get_core_tools()}
        assert "tool_get_context_remaining" in names
