"""验证脚本 — 测试所有修改后的模块是否正确运行。"""
import asyncio
import sys
import traceback

sys.path.insert(0, "src")

results = {"passed": 0, "failed": 0}

def check(name):
    def decorator(fn):
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            results["passed"] += 1
            print(f"  PASS {name}")
        except Exception as e:
            results["failed"] += 1
            print(f"  FAIL {name}: {e}")
            traceback.print_exc()
    return decorator

print("=" * 60)
print("Stage 1: Module imports")
print("=" * 60)

@check("config")
def test_config():
    from config import (
        LLM_API_KEY, LLM_MODEL, LLM_FLASH_MODEL,
        EMBEDDING_API_KEY,
    )
    assert LLM_API_KEY, "LLM_API_KEY not set"
    assert EMBEDDING_API_KEY, "EMBEDDING_API_KEY not set"
    from config import EMBEDDING_MODEL
    print(f"    Model: {LLM_MODEL}, Flash: {LLM_FLASH_MODEL}, Embedding: {EMBEDDING_MODEL}")

@check("context module")
def test_context():
    from context import (
        load_user_context, format_context_summary, set_current_user, add_memory, search_memory, list_memories, forget_memory,
    )
    set_current_user("test_user")
    ctx = load_user_context("test_user")
    assert "memories" in ctx
    assert "count" in ctx
    summary = format_context_summary(ctx)
    assert isinstance(summary, str)
    # 验证新 API 函数存在
    assert callable(add_memory)
    assert callable(search_memory)
    assert callable(list_memories)
    assert callable(forget_memory)

@check("skills module (get_skill_tools + list_skills)")
def test_skills():
    from skills import get_skill_tools, list_skills
    tools = get_skill_tools()
    names = [t.name for t in tools]
    # 新架构：RPA 批量任务始终由独立 RPA MCP 进程提供（mcp_rpa_*），
    # 技能目录里只保留 execute_code + shimaotong，不再有进程内 rpa_*。
    assert "execute_code" in names, names
    assert "tool_shimaotong_submit" in names, names
    assert not any(n.startswith("rpa_") or n.startswith("amazon_") for n in names), names

    skills_list = list_skills()
    assert len(skills_list) == len(tools)

    for t in tools:
        assert t.name, "Tool missing name"
        assert t.description, f"Tool {t.name} missing description"
    print(f"    {len(tools)} skill tools loaded: {sorted(names)}")

@check("agent core (get_core_tools + get_all_tools)")
def test_agent_core():
    from agent.core import (
        get_core_tools, get_all_tools,
    )
    core = get_core_tools()
    core_names = [t.name for t in core]
    expected = [
        "tool_search_knowledge", "tool_index_knowledge",
        "tool_add_memory", "tool_search_memory",
        "tool_forget_memory", "tool_list_memories",
        "tool_list_knowledge_sources",
    ]
    for name in expected:
        assert name in core_names, f"{name} missing from core tools"
    # 工具总数会随 MCP 懒连接变化（Filesystem/SearXNG/RPA），这里只要求核心齐全。
    all_tools = get_all_tools()
    assert len(all_tools) >= len(expected), f"Expected at least {len(expected)} total tools, got {len(all_tools)}"
    print(f"    Core: {len(core)} tools, All: {len(all_tools)} tools")

@check("OpenAI client factory")
def test_client_factory():
    from agent.client_factory import get_async_openai_client
    client = get_async_openai_client()
    assert client is not None
    from config import LLM_FLASH_MODEL
    assert LLM_FLASH_MODEL, "LLM_FLASH_MODEL not set"

@check("agent state TypedDict")
def test_agent_state():
    from agent.state import AgentState
    from langchain_core.messages import HumanMessage
    state = AgentState(messages=[HumanMessage(content="hello")])
    assert len(state["messages"]) == 1

print()
print("=" * 60)
print("Stage 2: Tool schema verification")
print("=" * 60)

@check("RPA tools: every param has description")
def test_rpa_schemas():
    from skills.rpa.adapters import (
        rpa_query_campaign_spend, rpa_collect_amazon_review, rpa_update_track_table,
    )
    rpa_tools = [
        rpa_query_campaign_spend, rpa_collect_amazon_review, rpa_update_track_table,
    ]
    missing = []
    for t in rpa_tools:
        schema = t.args_schema.model_json_schema()
        for name, prop in schema.get("properties", {}).items():
            desc = prop.get("description", "")
            if not desc:
                missing.append(f"{t.name}.{name}")
    assert not missing, f"Missing descriptions: {missing}"
    print(f"    {len(rpa_tools)} RPA tools, all params have descriptions")

@check("general skills: every param has description")
def test_skill_schemas():
    from skills.code_executor import execute_code

    missing = []
    for t in [execute_code]:
        schema = t.args_schema.model_json_schema()
        for name, prop in schema.get("properties", {}).items():
            if name == "kwargs":
                continue
            desc = prop.get("description", "")
            if not desc:
                missing.append(f"{t.name}.{name}")
    assert not missing, f"Missing descriptions: {missing}"
    print("    code_executor skill, all params have descriptions")

@check("RPA batch tools have valid schemas")
def test_batch_tools():
    from skills.rpa.adapters import (
        rpa_query_campaign_spend, rpa_collect_amazon_review, rpa_update_track_table,
    )
    for t in [rpa_query_campaign_spend, rpa_collect_amazon_review, rpa_update_track_table]:
        assert t.name
        assert t.description
        t.args_schema.model_json_schema()

print()
print("=" * 60)
print("Stage 3: Agent graph building")
print("=" * 60)

@check("build agent graph")
def test_build_graph():
    from agent.graph import build_agent
    graph = build_agent(context_summary="")
    assert graph is not None

@check("tool dispatch removed from core.py")
def test_no_dispatch_tool():
    from agent.core import get_core_tools, get_all_tools
    core_names = [t.name for t in get_core_tools()]
    all_names = [t.name for t in get_all_tools()]
    assert "tool_dispatch_skill" not in core_names, "tool_dispatch_skill should be removed"
    assert "tool_list_skills" not in core_names, "tool_list_skills should be removed"
    # But skill tools themselves should be present
    assert "execute_code" in all_names
    print("    Old dispatch tools removed, new direct tools present")

print()
print("=" * 60)
print("Stage 4: Live agent interaction")
print("=" * 60)

@check("live agent interaction (greeting + tool call)")
async def test_live_agent():
    """两个 live 调用必须在同一个事件循环里跑：get_agent() 返回进程级单例，
    其内部资源（如 aiosqlite 的 Lock）绑定首个 asyncio.run 的 loop，
    再起一个 asyncio.run 会报 "Lock bound to a different event loop"。"""
    from agent.graph import get_agent
    from langchain_core.messages import HumanMessage

    agent = await get_agent(context_summary="")
    msgs = []

    # 1) 简单问候（无需工具）
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="你好，请用一句话介绍你自己")]},
        config={"configurable": {"thread_id": "test-verify-001"}},
    )
    for m in result.get("messages", []):
        if m.type == "ai" and m.content:
            msgs.append(m)
    assert msgs, "No response messages"

    # 2) 触发工具调用的查询（同一 loop 继续）
    result2 = await agent.ainvoke(
        {"messages": [HumanMessage(content="搜索一下 Python 3.13 的新特性")]},
        config={"configurable": {"thread_id": "test-verify-002"}},
    )
    tool_calls = 0
    for m in result2.get("messages", []):
        if m.type == "ai":
            tool_calls += len(getattr(m, "tool_calls", []) or [])
        elif m.type == "tool":
            tool_calls += 1
    print(f"    Tool interactions: {tool_calls}")

print()
print("=" * 60)
p = results["passed"]
f = results["failed"]
t = p + f
print(f"Results: {p} passed, {f} failed, {t} total")
print("=" * 60)

if f > 0:
    sys.exit(1)
