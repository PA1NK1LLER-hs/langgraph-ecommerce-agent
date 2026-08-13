"""MCP Wrapper 测试：JSON Schema 转换 + 生命周期管理。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_wrapper.client import (
    MCPToolImporter,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    _json_schema_to_pydantic,
    _build_lc_tool,
)


# ═══════════════════════════════════════════════════════════════════════════
# _json_schema_to_pydantic — 各类 JSON Schema 模式
# ═══════════════════════════════════════════════════════════════════════════


class TestJsonSchemaToPydantic:
    """JSON Schema → Pydantic 类型转换单元测试。"""

    def test_empty_schema_returns_none(self):
        assert _json_schema_to_pydantic("test", {}) is None
        assert _json_schema_to_pydantic("test", None) is None

    def test_string_type(self):
        assert _json_schema_to_pydantic("test", {"type": "string"}) is str

    def test_integer_type(self):
        assert _json_schema_to_pydantic("test", {"type": "integer"}) is int

    def test_number_type(self):
        assert _json_schema_to_pydantic("test", {"type": "number"}) is float

    def test_boolean_type(self):
        assert _json_schema_to_pydantic("test", {"type": "boolean"}) is bool

    def test_null_type(self):
        from types import NoneType
        result = _json_schema_to_pydantic("test", {"type": "null"})
        assert result is NoneType

    def test_enum_string(self):
        schema = {"type": "string", "enum": ["red", "green", "blue"]}
        from typing import Literal
        result = _json_schema_to_pydantic("test", schema)
        assert result == Literal["red", "green", "blue"]

    def test_enum_single_value_fallback(self):
        schema = {"type": "string", "enum": ["only"]}
        from typing import Literal
        result = _json_schema_to_pydantic("test", schema)
        assert result == Literal["only"] or result is str

    def test_array_with_string_items(self):
        schema = {"type": "array", "items": {"type": "string"}}
        from typing import List
        result = _json_schema_to_pydantic("test", schema)
        assert result == List[str]

    def test_array_with_integer_items(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        from typing import List
        result = _json_schema_to_pydantic("test", schema)
        assert result == List[int]

    def test_flat_object(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("Person", schema)
        assert issubclass(model, BaseModel)
        assert "name" in model.model_fields
        assert "age" in model.model_fields
        assert model.model_fields["name"].is_required()
        assert not model.model_fields["age"].is_required()

    def test_object_with_description_in_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("SearchArgs", schema)
        assert issubclass(model, BaseModel)
        assert model.model_fields["query"].description == "搜索关键词"

    def test_default_value_preserved(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 10},
            },
        }
        model = _json_schema_to_pydantic("Args", schema)
        assert model.model_fields["count"].default == 10

    def test_ref_resolution(self):
        schema = {
            "type": "object",
            "properties": {
                "address": {"$ref": "#/$defs/Address"},
            },
            "required": ["address"],
            "$defs": {
                "Address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                    },
                    "required": ["street"],
                },
            },
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("User", schema)
        assert issubclass(model, BaseModel)
        assert "address" in model.model_fields

    def test_ref_with_definitions_keyword(self):
        schema = {
            "type": "object",
            "properties": {
                "item": {"$ref": "#/definitions/Item"},
            },
            "definitions": {
                "Item": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            },
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("Container", schema)
        assert issubclass(model, BaseModel)
        assert "item" in model.model_fields

    def test_anyof_union(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        from typing import Union
        result = _json_schema_to_pydantic("test", schema)
        assert result == Union[str, int]

    def test_oneof_union(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        from typing import Union
        result = _json_schema_to_pydantic("test", schema)
        assert result == Union[str, int]

    def test_anyof_single_type_becomes_optional(self):
        schema = {"anyOf": [{"type": "string"}]}
        from typing import Optional
        result = _json_schema_to_pydantic("test", schema)
        assert result == Optional[str]

    def test_allof_merge(self):
        schema = {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                {
                    "type": "object",
                    "properties": {"age": {"type": "integer"}},
                },
            ],
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("Merged", schema)
        assert issubclass(model, BaseModel)
        assert "name" in model.model_fields
        assert "age" in model.model_fields

    def test_nested_object(self):
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            "required": ["user"],
        }
        from pydantic import BaseModel
        model = _json_schema_to_pydantic("Root", schema)
        assert issubclass(model, BaseModel)
        assert "user" in model.model_fields

    def test_no_type_defaults_to_string(self):
        result = _json_schema_to_pydantic("test", {"description": "just a description"})
        assert result is str

    def test_object_without_properties_returns_none(self):
        result = _json_schema_to_pydantic("test", {"type": "object"})
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# MCPToolImporter — 生命周期
# ═══════════════════════════════════════════════════════════════════════════


class TestMCPToolImporterLifecycle:
    """MCPToolImporter 连接/断开生命周期测试。"""

    def test_init_stdio_config(self):
        config = StdioTransportConfig(command="python", args=["-m", "test"])
        importer = MCPToolImporter(config)
        assert importer.connected is False
        assert importer._session is None

    def test_init_streamable_http_config(self):
        config = StreamableHttpTransportConfig(url="http://test.local/mcp")
        importer = MCPToolImporter(config)
        assert importer.connected is False

    @pytest.mark.asyncio
    async def test_context_manager_protocol(self):
        """async with 协议正常运作。"""
        config = StdioTransportConfig(command="python", args=["-c", "print('hello')"])
        importer = MCPToolImporter(config)
        # 模拟已连接状态，connect 会 idempotent 返回，disconnect 会清理
        importer._session = MagicMock()
        importer._transport_ctx = MagicMock()
        async with importer:
            pass
        assert importer.connected is False
        assert importer._session is None

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self):
        config = StdioTransportConfig(command="echo", args=["test"])
        importer = MCPToolImporter(config)
        # 手动设置 session mock 模拟已连接
        importer._session = MagicMock()
        importer._connected = True
        # 再次 connect 应该直接返回
        await importer.connect()
        # 不应改变已有 session
        assert importer._session is not None

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent(self):
        config = StdioTransportConfig(command="echo", args=["test"])
        importer = MCPToolImporter(config)
        # 第一次 disconnect（session 为 None）
        await importer.disconnect()
        assert importer._session is None
        assert importer.connected is False
        # 第二次 disconnect 不应报错
        await importer.disconnect()
        assert importer.connected is False

    @pytest.mark.asyncio
    async def test_list_tools_raises_when_not_connected(self):
        config = StdioTransportConfig(command="echo", args=["test"])
        importer = MCPToolImporter(config)
        with pytest.raises(RuntimeError, match="not connected"):
            await importer.list_tools()

    @pytest.mark.asyncio
    async def test_import_tools_raises_when_not_connected(self):
        config = StdioTransportConfig(command="echo", args=["test"])
        importer = MCPToolImporter(config)
        with pytest.raises(RuntimeError, match="not connected"):
            await importer.import_tools()


# ═══════════════════════════════════════════════════════════════════════════
# _build_lc_tool — 工具包装
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildLcTool:
    """MCP Tool → LangChain @tool 适配测试。"""

    def test_tool_name_prefixed_with_mcp(self):
        mcp_tool = MagicMock()
        mcp_tool.name = "search"
        mcp_tool.description = "Search files"
        mcp_tool.input_schema = {"type": "object", "properties": {}}

        importer = MagicMock()
        importer._session = MagicMock()

        lc_tool = _build_lc_tool(mcp_tool, importer)
        assert lc_tool.name == "mcp_search"

    def test_tool_has_args_schema_when_input_schema_provided(self):
        mcp_tool = MagicMock()
        mcp_tool.name = "query"
        mcp_tool.description = "Query data"
        mcp_tool.input_schema = {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "查询字符串"},
            },
            "required": ["q"],
        }

        importer = MagicMock()
        importer._session = MagicMock()

        lc_tool = _build_lc_tool(mcp_tool, importer)
        assert lc_tool.args_schema is not None
        assert "q" in lc_tool.args_schema.model_fields

    def test_tool_func_returns_error_when_session_none(self):
        mcp_tool = MagicMock()
        mcp_tool.name = "test"
        mcp_tool.description = "Test"
        mcp_tool.input_schema = {}

        importer = MagicMock()
        importer._session = None  # 模拟断开连接

        lc_tool = _build_lc_tool(mcp_tool, importer)
        import asyncio
        result = asyncio.run(lc_tool.coroutine())
        assert result["status"] == "error"
        assert "closed" in result["message"]

    @pytest.mark.asyncio
    async def test_tool_func_calls_session_call_tool(self):
        mcp_tool = MagicMock()
        mcp_tool.name = "echo"
        mcp_tool.description = "Echo test"
        mcp_tool.input_schema = {}

        session = AsyncMock()
        content = MagicMock()
        content.text = "hello world"
        session.call_tool.return_value.content = [content]

        importer = MagicMock()
        importer._session = session

        lc_tool = _build_lc_tool(mcp_tool, importer)
        result = await lc_tool.coroutine(message="hello")
        assert result["status"] == "success"
        assert result["results"] == ["hello world"]
        session.call_tool.assert_called_once_with("echo", arguments={"message": "hello"})

    @pytest.mark.asyncio
    async def test_tool_func_handles_call_tool_exception(self):
        mcp_tool = MagicMock()
        mcp_tool.name = "faulty"
        mcp_tool.description = "Always fails"
        mcp_tool.input_schema = {}

        session = AsyncMock()
        session.call_tool.side_effect = RuntimeError("MCP server error")

        importer = MagicMock()
        importer._session = session

        lc_tool = _build_lc_tool(mcp_tool, importer)
        result = await lc_tool.coroutine()
        assert result["status"] == "error"
        assert "MCP server error" in result["message"]
