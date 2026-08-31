"""pytest 配置 — sys.path 引导 + 通用 fixtures。"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv

# 将 src/ 加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── 先加载真实 .env 端点（DashScope/DeepSeek），再补测试默认值 ──
# 必须在 setdefault 之前 load_dotenv：否则 setdefault 会把假端点 api.test.local
# 先占位，而 config.py 里 load_dotenv(override=False) 无法覆盖已存在的值，
# 集成测试就拿不到真实的 embedding/LLM 端点（表现为 getaddrinfo failed）。
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── 在模块导入之前设置必需的测试环境变量 ──
# deps.py / config.py 在 import 时就会读取 os.getenv，必须在 import 前设好
# 这里用 setdefault：真实 .env 已加载的键不会被覆盖，仅补 .env 缺失的测试默认值
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-at-least-32-chars!")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "https://api.test.local")
os.environ.setdefault("EMBEDDING_API_KEY", "test-dashscope-key")
os.environ.setdefault("EMBEDDING_BASE_URL", "https://api.test.local")
os.environ.setdefault("POSTGRES_URL", "")
os.environ.setdefault("POSTGRES_SCHEMA", "langgraph")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "")
os.environ.setdefault("TAVILY_API_KEY", "")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("QDRANT_API_KEY", "")
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "password123")


@pytest.fixture
def mock_env(monkeypatch):
    """为需要环境变量的测试提供安全默认值。"""
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-at-least-32-chars")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.test.local")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.test.local")
    monkeypatch.setenv("POSTGRES_URL", "")
    monkeypatch.setenv("POSTGRES_SCHEMA", "langgraph")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password123")
    return monkeypatch


@pytest.fixture
def sample_human_message():
    """返回一个 Mock HumanMessage，content="你好"。"""
    msg = MagicMock()
    msg.type = "human"
    msg.content = "你好"
    return msg


@pytest.fixture
def sample_ai_message():
    """返回一个 Mock AIMessage，无 tool_calls。"""
    msg = MagicMock()
    msg.type = "ai"
    msg.content = "你好！有什么可以帮你的？"
    msg.tool_calls = []
    msg.additional_kwargs = {}
    return msg


@pytest.fixture
def sample_ai_with_tools():
    """返回一个 Mock AIMessage，带 tool_calls。"""
    msg = MagicMock()
    msg.type = "ai"
    msg.content = ""
    msg.tool_calls = [{"name": "search_knowledge", "args": {"query": "test"}}]
    msg.additional_kwargs = {}
    return msg


@pytest.fixture
def sample_tool_error():
    """返回一个 Mock ToolMessage，content 是状态为 error 的 dict。"""
    msg = MagicMock()
    msg.type = "tool"
    msg.name = "search_knowledge"
    msg.content = {"status": "error", "message": "API timeout"}
    return msg


@pytest.fixture
def sample_tool_success():
    """返回一个 Mock ToolMessage，content 是状态为 success 的 dict。"""
    msg = MagicMock()
    msg.type = "tool"
    msg.name = "search_knowledge"
    msg.content = {"status": "success", "results": [{"content": "test result"}]}
    return msg
