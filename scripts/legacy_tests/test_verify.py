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
        MIMO_API_KEY, MIMO_MODEL, MIMO_FLASH_MODEL,
        DASHSCOPE_API_KEY,
    )
    assert MIMO_API_KEY, "MIMO_API_KEY not set"
    assert DASHSCOPE_API_KEY, "DASHSCOPE_API_KEY not set"
    from config import DASHSCOPE_EMBEDDING_DIM
    print(f"    Pro: {MIMO_MODEL}, Flash: {MIMO_FLASH_MODEL}, Embedding dim: {DASHSCOPE_EMBEDDING_DIM}")

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
    assert len(tools) == 17, f"Expected 17 skill tools, got {len(tools)}"

    skills_list = list_skills()
    assert len(skills_list) == 17

    for t in tools:
        assert t.name, "Tool missing name"
        assert t.description, f"Tool {t.name} missing description"
    print(f"    {len(tools)} skill tools loaded")

@check("agent core (get_core_tools + get_all_tools)")
def test_agent_core():
    from agent.core import (
        get_core_tools, get_all_tools,
    )
    core = get_core_tools()
    assert len(core) == 7, f"Expected 7 core tools, got {len(core)}"

    all_tools = get_all_tools()
    assert len(all_tools) == 24, f"Expected 24 total tools, got {len(all_tools)}"

    core_names = [t.name for t in core]
    expected = [
        "tool_search_knowledge", "tool_index_knowledge",
        "tool_add_memory", "tool_search_memory",
        "tool_forget_memory", "tool_list_memories",
        "tool_list_knowledge_sources",
    ]
    for name in expected:
        assert name in core_names, f"{name} missing from core tools"
    print(f"    Core: {len(core)} tools, All: {len(all_tools)} tools")

@check("MiMo LLM instantiation")
def test_mimo_adapter():
    from agent.core import create_mimo_llm
    from config import MIMO_MODEL
    llm = create_mimo_llm(model=MIMO_MODEL, temperature=0)
    assert llm is not None

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
    from skills.rpa_ziniao import (
        rpa_click, rpa_scroll, rpa_navigate, rpa_wait,
        rpa_fill_input, rpa_extract_tables,
        rpa_kill_process, rpa_update_core,
        rpa_close_store, rpa_query_campaign_spend,
    )
    rpa_tools = [
        rpa_click, rpa_scroll, rpa_navigate, rpa_wait,
        rpa_fill_input, rpa_extract_tables,
        rpa_kill_process, rpa_update_core,
        rpa_close_store, rpa_query_campaign_spend,
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

@check("RPA no-arg tools have valid schemas")
def test_no_arg_tools():
    from skills.rpa_ziniao import (
        rpa_start_browser, rpa_exit_client,
        rpa_store_list, rpa_page_summary,
    )
    for t in [rpa_start_browser, rpa_exit_client, rpa_store_list, rpa_page_summary]:
        assert t.name
        assert t.description
        schema = t.args_schema.model_json_schema()
        # no-arg tools should have empty or near-empty properties
        props = schema.get("properties", {})
        if props:
            print(f"    Note: {t.name} has properties: {list(props.keys())}")

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

@check("simple agent call (no tools needed)")
async def test_agent_call():
    from agent.graph import get_agent
    from langchain_core.messages import HumanMessage

    agent = await get_agent(context_summary="")
    config = {"configurable": {"thread_id": "test-verify-001"}}

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="你好，请用一句话介绍你自己")]},
        config=config,
    )

    msgs = result.get("messages", [])
    assert len(msgs) > 0, "No response messages"

    for msg in msgs:
        if msg.type == "ai" and msg.content:
            content_preview = str(msg.content)[:200].encode("ascii", errors="replace").decode()
            model_name = getattr(msg, "response_metadata", {}).get("model_name", "?")
            print(f"    Model: {model_name}")
            print(f"    Response: {content_preview}")

@check("agent call that triggers a tool call")
async def test_tool_call():
    from agent.graph import get_agent
    from langchain_core.messages import HumanMessage

    agent = await get_agent(context_summary="")
    config = {"configurable": {"thread_id": "test-verify-002"}}

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="搜索一下 Python 3.13 的新特性")]},
        config=config,
    )

    msgs = result.get("messages", [])
    assert len(msgs) > 0
    for msg in msgs:
        if msg.type == "ai":
            tc_count = len(getattr(msg, "tool_calls", []) or [])
            tool_names = [tc["name"] for tc in (getattr(msg, "tool_calls", []) or [])]
            print(f"    Tool calls: {tc_count} {tool_names}")
            if msg.content:
                preview = str(msg.content)[:150].encode("ascii", errors="replace").decode()
                print(f"    Content preview: {preview}")
        elif msg.type == "tool":
            preview = str(msg.content)[:100].encode("ascii", errors="replace").decode()
            print(f"    Tool result: {msg.name} -> {preview}")

print()
print("=" * 60)
p = results["passed"]
f = results["failed"]
t = p + f
print(f"Results: {p} passed, {f} failed, {t} total")
print("=" * 60)

if f > 0:
    sys.exit(1)
