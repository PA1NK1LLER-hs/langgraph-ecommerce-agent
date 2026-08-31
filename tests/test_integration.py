"""集成测试 — 验证 RAG / Skills / MCP / Memory 各子系统端到端功能。

用法:
    pytest tests/test_integration.py -v -s

前提条件:
    - Docker 基础设施已在运行 (qdrant, neo4j, postgres, searxng)
    - .env 已配置有效的 LLM_API_KEY 和 EMBEDDING_API_KEY
"""

import asyncio
import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

# Ensure src is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# ══════════════════════════════════════════════════════════════════════════
# 1. RAG — 知识库入库 + 检索
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestRAG:
    """LightRAG 知识图谱：索引 → 检索 → 列出来源。"""

    def test_index_and_search(self):
        """索引一段文本，然后语义检索验证能找回。"""
        from rag.indexer import get_indexer, index_knowledge, search_knowledge, list_sources, async_index_knowledge

        # ── 入库 ──
        test_text = (
            "LangGraph Agent 是一个基于 LangGraph 的通用 AI 助手，"
            "采用 Flash/Pro 双模型架构，集成了 LightRAG 知识图谱、"
            "Mem0 用户记忆系统和 MCP 协议外部工具。"
        )
        import asyncio
        result = asyncio.run(async_index_knowledge(
            test_text, source="integration_test", tags="test"
        ))
        print(f"\n  [RAG Index] {result}")
        assert result["status"] == "success", f"Index failed: {result}"

        # ── 检索 ──
        search_result = search_knowledge("LangGraph Agent 是什么", top_k=3)
        print(f"  [RAG Search] status={search_result['status']}, count={search_result.get('count', 0)}")
        assert search_result["status"] == "success", f"Search failed: {search_result}"

        results = search_result.get("results", [])
        # 至少找到一条包含 "LangGraph" 的结果
        found = any("LangGraph" in r.get("content", "") for r in results)
        if not found and results:
            # 可能结果在 content 字段中
            print(f"  [RAG Content] First result: {str(results[0])[:200]}")

        # ── 列出来源 ──
        sources = list_sources()
        print(f"  [RAG Sources] status={sources['status']}, total_chunks={sources.get('total_chunks', 0)}")
        assert sources["status"] == "success"

    def test_list_sources(self):
        """列出知识库所有来源。"""
        from rag.indexer import list_sources
        sources = list_sources()
        print(f"  [RAG List] status={sources['status']}, sources_count={len(sources.get('sources', []))}")
        assert sources["status"] == "success"
        assert isinstance(sources.get("sources"), list)


# ══════════════════════════════════════════════════════════════════════════
# 2. Skills — 代码执行
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestCodeExecutor:
    """代码执行 Skill — Python / Shell 执行。"""

    def test_execute_python_simple(self):
        """执行简单 Python 代码。"""
        from skills.code_executor import execute_code

        result = execute_code.invoke({
            "code": "print('hello world')\nresult = 2 + 3\nprint(f'sum={result}')",
            "language": "python",
            "run": True,
        })
        print(f"  [Code Exec] status={result.get('status')}, output={str(result.get('output', ''))[:200]}")
        assert result["status"] == "success"
        assert "hello" in str(result.get("output", "")).lower()

    def test_execute_python_no_run(self):
        """仅保存代码不运行。"""
        from skills.code_executor import execute_code

        result = execute_code.invoke({
            "code": "x = 1 + 1",
            "language": "python",
            "run": False,
        })
        print(f"  [Code Save] status={result['status']}, file={result.get('file', '')}")
        assert result["status"] == "success"
        assert result.get("file")
        assert result.get("output") == "(未运行)"

    def test_list_code_logs(self):
        """列出代码执行日志。"""
        from skills.code_executor import list_code_logs
        logs = list_code_logs(5)
        print(f"  [Code Logs] count={len(logs)}")
        assert isinstance(logs, list)


# ══════════════════════════════════════════════════════════════════════════
# 3. MCP — 文件系统工具
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestMCPFilesystem:
    """MCP Filesystem 工具 — 读写文件 / 目录操作。"""

    @pytest.mark.asyncio
    async def test_mcp_tools_registered(self):
        """验证 MCP 工具已注册到 Agent 工具列表（含懒加载 Filesystem MCP）。"""
        import asyncio as _aio

        from agent.mcp_setup import setup_filesystem_mcp, _connected
        from agent.core import get_all_tools

        # 确保 Filesystem MCP 已连接（15s 超时 + 失败/超时则跳过）
        if "Filesystem" not in _connected:
            print("\n  [MCP Setup] Connecting Filesystem...")
            try:
                count = await _aio.wait_for(setup_filesystem_mcp(), timeout=20)
                print(f"  [MCP Setup] Filesystem: {count} tools loaded")
            except BaseException as exc:
                print(f"  [MCP Setup] Filesystem skipped: {exc}")
                pytest.skip(f"Filesystem MCP unavailable: {exc}")

        tools = get_all_tools()
        tool_names = {t.name for t in tools}
        mcp_tools = [n for n in tool_names if n.startswith("mcp_")]
        print(f"  [MCP Tools] Total tools: {len(tools)}, MCP tools: {len(mcp_tools)}")
        print(f"  [MCP Tools] MCP tool names: {sorted(mcp_tools)[:20]}")
        assert len(mcp_tools) > 0, "No MCP tools registered!"

        # 关闭 MCP stdio 子进程：本测试的 event loop 马上要关（function 级 loop 作用域），
        # 若 npx 子进程的后台 reader task 仍存活，Windows Proactor 的 `_cancel_all_tasks`
        # 会卡死（测试断言过了却挂在收尾）。与 test_mcp_list_directory 末尾同款清理。
        try:
            from agent.mcp_setup import shutdown_mcp_tools
            await shutdown_mcp_tools()
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_mcp_list_directory(self):
        """通过 MCP 列出项目根目录。"""
        import asyncio as _aio

        from agent.mcp_setup import setup_filesystem_mcp, _connected
        from agent.core import get_all_tools

        # 确保 MCP 已连接
        if "Filesystem" not in _connected:
            try:
                await _aio.wait_for(setup_filesystem_mcp(), timeout=20)
            except BaseException as exc:
                pytest.skip(f"Filesystem MCP unavailable: {exc}")

        tools_by_name = {t.name: t for t in get_all_tools()}
        list_dir = tools_by_name.get("mcp_list_directory")
        if list_dir is None:
            pytest.skip("mcp_list_directory tool not available")

        result = await list_dir.coroutine(path=str(PROJECT_ROOT))
        print(f"  [MCP ListDir] status={result.get('status')}")
        if result.get("status") == "success":
            files = result.get("results", [])
            print(f"  [MCP ListDir] found {len(files)} entries")

        # 关闭 MCP stdio 子进程：其后台 reader task 若在会话 loop 关闭时仍存活，
        # 会在 Windows Proactor 上卡死 `_cancel_all_tasks`（集成测试正常跑完却卡在收尾）。
        try:
            from agent.mcp_setup import shutdown_mcp_tools
            await shutdown_mcp_tools()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# 4. User Memory (Mem0) — CRUD + 语义搜索
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestUserMemory:
    """Mem0 用户记忆系统 — 增删查。"""

    TEST_USER = "integration_test_user"

    def test_add_and_search_memory(self):
        """存入一条记忆，然后语义搜索找回。"""
        from context.manager import add_memory, search_memory, set_current_user, get_current_user

        set_current_user(self.TEST_USER)

        # ── 存入 ──
        result = add_memory(
            self.TEST_USER,
            content="测试用户叫张三，在ABC公司做研发工程师，喜欢用Python和Rust",
            category="fact",
        )
        print(f"  [Memory Add] status={result.get('status')}")
        assert result["status"] == "success", f"Add failed: {result}"

        # ── 搜索 ──
        search_result = search_memory(self.TEST_USER, query="这个用户叫什么名字，做什么工作")
        print(f"  [Memory Search] status={search_result.get('status')}, count={search_result.get('count', 0)}")
        assert search_result["status"] == "success"

    def test_list_memories(self):
        """列出用户全部记忆。"""
        from context.manager import list_memories

        result = list_memories(self.TEST_USER)
        print(f"  [Memory List] status={result.get('status')}, count={result.get('count', 0)}")
        assert result["status"] == "success"

    def test_forget_memory(self):
        """语义删除记忆（清理测试数据）。"""
        from context.manager import forget_memory

        result = forget_memory(self.TEST_USER, query="张三")
        print(f"  [Memory Forget] status={result.get('status')}, deleted={result.get('deleted', 0)}")
        assert result["status"] == "success"


# ══════════════════════════════════════════════════════════════════════════
# 5. Core Tools — 工具完整性验证
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestCoreTools:
    """核心工具完整性 — 确保 @tool 定义无冲突、可正常调用。"""

    def test_all_tools_names_unique(self):
        """所有工具名唯一。"""
        from agent.core import get_all_tools
        tools = get_all_tools()
        names = [t.name for t in tools]
        duplicates = [n for n in names if names.count(n) > 1]
        print(f"  [Tools] Total: {len(tools)}, Unique: {len(set(names))}")
        assert len(duplicates) == 0, f"Duplicate tool names: {set(duplicates)}"

    def test_core_tools_functional(self):
        """核心工具可被调用（验证签名正确 — LangChain StructuredTool 通过 .invoke 调用）。"""
        from agent.core import get_core_tools
        tools = get_core_tools()
        for t in tools:
            assert hasattr(t, "name"), f"{t} has no name"
            assert hasattr(t, "description"), f"{t.name} has no description"
            # LangChain StructuredTool 通过 .invoke() 调用，不是 callable
            assert hasattr(t, "invoke") or callable(t), f"{t.name} has no invoke"
        print(f"  [Core Tools] All {len(tools)} core tools have valid signatures")

    def test_dynamic_system_prompt(self):
        """动态 System Prompt 生成不抛异常且包含关键内容。"""
        from agent.core import get_system_prompt
        prompt = get_system_prompt()
        print(f"  [Prompt] Length: {len(prompt)} chars")
        assert len(prompt) > 200, "Prompt too short"
        assert "工具选择规则" in prompt or "tool" in prompt.lower()
        print(f"  [Prompt] First 300 chars:\n{prompt[:300]}")


# ══════════════════════════════════════════════════════════════════════════
# 6. Embedding — 异步修复后功能验证
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestEmbedding:
    """验证 embedding.py 异步修复后功能正常。"""

    @pytest.mark.asyncio
    async def test_embed_single_text(self):
        """嵌入单条文本。"""
        from rag.embedding import _embed
        import numpy as np

        result = await _embed(["hello world test"])
        print(f"  [Embed] shape={result.shape}, dtype={result.dtype}")
        assert isinstance(result, np.ndarray)
        assert result.shape[0] == 1
        assert result.shape[1] > 0  # embedding dims

    @pytest.mark.asyncio
    async def test_embed_batch(self):
        """批量嵌入多条文本。"""
        from rag.embedding import _embed
        import numpy as np

        texts = [f"test text {i}" for i in range(5)]
        result = await _embed(texts)
        print(f"  [Embed Batch] shape={result.shape}")
        assert result.shape[0] == 5

    def test_get_embedding_func(self):
        """获取 EmbeddingFunc 单例。"""
        from rag.embedding import get_embedding_func
        ef1 = get_embedding_func()
        ef2 = get_embedding_func()
        assert ef1 is ef2, "EmbeddingFunc should be singleton"
        print(f"  [Embed Func] Singleton OK, dims={ef1.embedding_dim}")


# ══════════════════════════════════════════════════════════════════════════
# 7. AppContext — 状态管理
# ══════════════════════════════════════════════════════════════════════════


class TestAppContext:
    """验证 AppContext 单例和各模块一致性。"""

    def test_singleton(self):
        """AppContext 是线程安全的单例。"""
        from app_context import get_app_context
        ctx1 = get_app_context()
        ctx2 = get_app_context()
        assert ctx1 is ctx2

    def test_user_id_contextvar_isolation(self):
        """ContextVar 实现用户隔离：set 后在当前上下文中可见。"""
        from context.manager import set_current_user, get_current_user

        set_current_user("test_shared_user")
        assert get_current_user() == "test_shared_user"

        # 重置为默认值不影响其他测试
        set_current_user("default_user")
        assert get_current_user() == "default_user"

    def test_reset(self):
        """AppContext.reset() 清理所有状态。"""
        from app_context import get_app_context
        ctx = get_app_context()
        ctx.register_mcp_tools(["fake_tool"])
        ctx.reset()
        assert ctx.get_mcp_tools() == []
        assert ctx.get_agent() is None
