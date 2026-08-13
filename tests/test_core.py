"""create_llm() + core tool definitions tests."""

from unittest.mock import patch

from agent.core import create_llm, get_core_tools, get_all_tools


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
        assert len(tools) == 9

    def test_core_tool_names(self):
        names = [t.name for t in get_core_tools()]
        assert "tool_search_knowledge" in names
        assert "tool_index_knowledge" in names
        assert "tool_add_memory" in names
        assert "tool_search_memory" in names
        assert "tool_forget_memory" in names
        assert "tool_list_memories" in names
        assert "tool_list_knowledge_sources" in names

    def test_get_all_tools_includes_core(self):
        core_names = {t.name for t in get_core_tools()}
        all_names = {t.name for t in get_all_tools()}
        assert core_names <= all_names
