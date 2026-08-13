"""图纯函数测试：is_tool_error + _should_continue + _should_plan + _track_failures。"""

from unittest.mock import MagicMock

from langchain_core.messages import HumanMessage, AIMessage

from agent.utils import is_tool_error
from agent.graph import _should_continue, _track_failures
from agent.state import AgentState


# ── is_tool_error ──


class TestIsToolError:
    def test_dict_error_status(self):
        # Use MagicMock for dict content (ToolMessage converts dict to str repr)
        msg = MagicMock()
        msg.content = {"status": "error", "message": "fail"}
        assert is_tool_error(msg) is True

    def test_dict_success_status(self):
        msg = MagicMock()
        msg.content = {"status": "success", "results": []}
        assert is_tool_error(msg) is False

    def test_json_string_error(self):
        msg = MagicMock()
        msg.content = '{"status": "error", "message": "failed"}'
        assert is_tool_error(msg) is True

    def test_json_string_success(self):
        msg = MagicMock()
        msg.content = '{"status": "success", "data": []}'
        assert is_tool_error(msg) is False

    def test_inline_error_pattern(self):
        msg = MagicMock()
        msg.content = 'something went wrong {"status":"error"} end'
        assert is_tool_error(msg) is True

    def test_compact_error_pattern(self):
        msg = MagicMock()
        msg.content = '{"status":"error","message":"x"}'
        assert is_tool_error(msg) is True

    def test_list_with_error_item(self):
        msg = MagicMock()
        msg.content = [{"status": "success"}, {"status": "error"}]
        assert is_tool_error(msg) is True

    def test_list_without_error(self):
        msg = MagicMock()
        msg.content = [{"status": "success"}, {"status": "ok"}]
        assert is_tool_error(msg) is False

    def test_malformed_json(self):
        msg = MagicMock()
        msg.content = "not a valid json at all {{{"
        assert is_tool_error(msg) is False

    def test_none_content(self):
        msg = MagicMock()
        msg.content = None
        assert is_tool_error(msg) is False

    def test_no_content_attr(self):
        msg = MagicMock(spec=[])
        assert is_tool_error(msg) is False


# ── _should_continue ──


def _make_state(messages, tool_failures=0, tool_retries=0, plan=""):
    return AgentState(
        messages=messages,
        plan=plan,
        tool_failures=tool_failures,
        tool_retries=tool_retries,
    )


class TestShouldContinue:
    def test_no_tool_calls_returns_end(self):
        msg = AIMessage(content="done")
        state = _make_state([HumanMessage(content="hi"), msg])
        assert _should_continue(state) == "__end__"

    def test_has_tool_calls_returns_tools(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {}, "id": "call_1", "type": "tool_call"}],
        )
        state = _make_state([HumanMessage(content="hi"), msg])
        assert _should_continue(state) == "tools"

    def test_failures_exceed_limit_returns_end(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {}, "id": "call_1", "type": "tool_call"}],
        )
        state = _make_state([HumanMessage(content="hi"), msg], tool_failures=3)
        assert _should_continue(state) == "__end__"

    def test_failure_without_retry_returns_reflect(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {}, "id": "call_1", "type": "tool_call"}],
        )
        state = _make_state(
            [HumanMessage(content="hi"), msg], tool_failures=1, tool_retries=0
        )
        assert _should_continue(state) == "reflect"

    def test_failure_with_retry_returns_tools(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {}, "id": "call_1", "type": "tool_call"}],
        )
        state = _make_state(
            [HumanMessage(content="hi"), msg], tool_failures=1, tool_retries=1
        )
        assert _should_continue(state) == "tools"


# ── _should_plan ──


# ── _track_failures ──


class TestTrackFailures:
    def test_tool_error_increments_failures(self):
        err_msg = MagicMock()
        err_msg.type = "tool"
        err_msg.content = {"status": "error", "message": "fail"}
        state = _make_state([HumanMessage(content="hi"), err_msg], tool_failures=0)
        result = _track_failures(state)
        assert result["tool_failures"] == 1

    def test_tool_success_resets_failures(self):
        ok_msg = MagicMock()
        ok_msg.type = "tool"
        ok_msg.content = {"status": "success"}
        state = _make_state(
            [HumanMessage(content="hi"), ok_msg], tool_failures=2, tool_retries=1
        )
        result = _track_failures(state)
        assert result["tool_failures"] == 0
        assert result["tool_retries"] == 0

    def test_no_tool_message_returns_defaults(self):
        state = _make_state(
            [HumanMessage(content="hi"), AIMessage(content="reply")],
            tool_failures=0, tool_retries=0,
        )
        result = _track_failures(state)
        assert result["tool_failures"] == 0
        assert result["tool_retries"] == 0

    def test_ai_message_ignored(self):
        ai_msg = AIMessage(content="let me try again")
        err_msg = MagicMock()
        err_msg.type = "tool"
        err_msg.content = {"status": "error"}
        state = _make_state(
            [HumanMessage(content="hi"), ai_msg, err_msg], tool_failures=0
        )
        result = _track_failures(state)
        assert result["tool_failures"] == 1

    def test_cumulative_failures(self):
        err_msg = MagicMock()
        err_msg.type = "tool"
        err_msg.content = {"status": "error"}
        state = _make_state([HumanMessage(content="hi"), err_msg], tool_failures=2)
        result = _track_failures(state)
        assert result["tool_failures"] == 3
