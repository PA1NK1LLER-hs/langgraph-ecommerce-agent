"""Phase 4 测试 — 结构化输出与类型安全响应。

测试 response_schemas 模块的 Pydantic Schema，验证 JSON 序列化/反序列化，
以及 state 中 structured_response 字段的正确性。
"""

import pytest
from pydantic import ValidationError

from agent.response_schemas import (
    TextResponse,
    TableResponse,
    ActionConfirmResponse,
    TaskPlanResponse,
    RESPONSE_SCHEMAS,
)


# ═══════════════════════════════════════════════════
# Pydantic Schema 验证
# ═══════════════════════════════════════════════════

class TestTextResponse:
    """测试 TextResponse Schema。"""

    def test_valid(self):
        r = TextResponse(content="Hello world")
        assert r.response_type == "text"
        assert r.content == "Hello world"

    def test_missing_content_fails(self):
        with pytest.raises(ValidationError):
            TextResponse()

    def test_serialization(self):
        r = TextResponse(content="Hello")
        d = r.model_dump()
        assert d == {"response_type": "text", "content": "Hello"}


class TestTableResponse:
    """测试 TableResponse Schema。"""

    def test_valid(self):
        r = TableResponse(
            title="Products",
            columns=["Name", "Price"],
            rows=[["Widget", 9.99], ["Gadget", 19.99]],
        )
        assert r.response_type == "table"
        assert r.title == "Products"
        assert len(r.columns) == 2
        assert len(r.rows) == 2

    def test_empty_rows(self):
        r = TableResponse(title="Empty", columns=["A"], rows=[])
        assert r.rows == []

    def test_serialization(self):
        r = TableResponse(title="T", columns=["A"], rows=[[1]])
        d = r.model_dump()
        assert d["response_type"] == "table"
        assert d["title"] == "T"


class TestActionConfirmResponse:
    """测试 ActionConfirmResponse Schema。"""

    def test_valid(self):
        r = ActionConfirmResponse(
            action="Delete file",
            tool_name="mcp_delete",
            tool_args={"path": "/tmp/x.txt"},
            risk_summary="这将永久删除文件",
        )
        assert r.response_type == "action_confirm"
        assert r.tool_name == "mcp_delete"

    def test_risk_summary_default(self):
        r = ActionConfirmResponse(
            action="Create file",
            tool_name="mcp_write_file",
            tool_args={"path": "/tmp/x.txt"},
        )
        assert r.risk_summary == ""  # 默认值

    def test_serialization(self):
        r = ActionConfirmResponse(
            action="Run",
            tool_name="execute_code",
            tool_args={"code": "print(1)"},
        )
        d = r.model_dump()
        assert d["response_type"] == "action_confirm"


class TestTaskPlanResponse:
    """测试 TaskPlanResponse Schema。"""

    def test_valid(self):
        r = TaskPlanResponse(
            goal="Analyze sales data",
            steps=[
                {"step": 1, "description": "Load data", "status": "done"},
                {"step": 2, "description": "Generate chart", "status": "pending"},
            ],
            estimated_time="2 minutes",
        )
        assert r.response_type == "task_plan"
        assert len(r.steps) == 2

    def test_empty_steps(self):
        r = TaskPlanResponse(goal="Nothing", steps=[])
        assert r.steps == []

    def test_estimated_time_default(self):
        r = TaskPlanResponse(goal="Test", steps=[])
        assert r.estimated_time == ""


# ═══════════════════════════════════════════════════
# Schema 注册表
# ═══════════════════════════════════════════════════

class TestResponseSchemasRegistry:
    """测试 RESPONSE_SCHEMAS 注册表。"""

    def test_all_types_registered(self):
        assert "text" in RESPONSE_SCHEMAS
        assert "table" in RESPONSE_SCHEMAS
        assert "action_confirm" in RESPONSE_SCHEMAS
        assert "task_plan" in RESPONSE_SCHEMAS

    def test_registered_models_are_pydantic(self):
        from pydantic import BaseModel
        for schema_type, model_cls in RESPONSE_SCHEMAS.items():
            assert issubclass(model_cls, BaseModel), f"{schema_type} should be a Pydantic model"

    def test_text_schema_instantiable(self):
        model = RESPONSE_SCHEMAS["text"]
        r = model(content="test")
        assert r.content == "test"

    def test_table_schema_instantiable(self):
        model = RESPONSE_SCHEMAS["table"]
        r = model(title="T", columns=["A"], rows=[])
        assert r.title == "T"


# ═══════════════════════════════════════════════════
# AgentState structured_response 字段兼容
# ═══════════════════════════════════════════════════

class TestStructuredResponseInState:
    """测试 structured_response 在 AgentState 中的兼容性。"""

    def test_state_accepts_structured_response(self):
        from agent.state import AgentState
        from langchain_core.messages import HumanMessage

        state = AgentState(
            messages=[HumanMessage(content="test")],
            plan="",
            plan_steps=[],
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
            response_schema={"type": "table"},
            structured_response={"response_type": "table", "title": "T", "columns": ["A"], "rows": []},
        )
        assert state["response_schema"] == {"type": "table"}
        assert state["structured_response"]["response_type"] == "table"

    def test_null_schema_by_default(self):
        from agent.state import AgentState
        state = AgentState(
            messages=[],
            plan="",
            plan_steps=[],
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
        assert state["response_schema"] is None
        assert state["structured_response"] is None


# ═══════════════════════════════════════════════════
# JSON 往返测试
# ═══════════════════════════════════════════════════

class TestJsonRoundTrip:
    """测试 Schema JSON 序列化/反序列化往返。"""

    def test_text_roundtrip(self):
        import json
        orig = TextResponse(content="Hello JSON")
        d = orig.model_dump()
        j = json.dumps(d)
        loaded = json.loads(j)
        assert loaded["response_type"] == "text"
        assert loaded["content"] == "Hello JSON"

    def test_table_roundtrip(self):
        import json
        orig = TableResponse(
            title="Data",
            columns=["id", "name"],
            rows=[[1, "Alice"], [2, "Bob"]],
        )
        j = json.dumps(orig.model_dump())
        loaded = json.loads(j)
        assert loaded["response_type"] == "table"
        assert loaded["columns"] == ["id", "name"]
        assert len(loaded["rows"]) == 2
