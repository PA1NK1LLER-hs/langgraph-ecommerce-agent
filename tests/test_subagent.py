"""真 Sub-Agent（LangGraph 子图）改造测试。

覆盖 graph.py 模块级新增的纯函数与子图组装行为：
- _merge_session_costs / _extract_subagent_report（纯函数）
- _route_after_supervisor（supervisor 委派 → 子图 / 规划 / 直答）
- sub_check_approval（子代理内高风险自动拒绝，低风险放行）
- build_specialist_subgraph（stub 节点组装 → 编译 + ainvoke + 报告提取）

不依赖外部 LLM key；子图节点全部用 stub 替换。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.graph import (
    SUBAGENT_NAMES,
    _extract_subagent_report,
    _merge_session_costs,
    _route_after_classify,
    _route_after_rag,
    _route_after_supervisor,
    _sanitize_messages_for_api,
    build_specialist_subgraph,
    sub_check_approval,
)

# ---------------------------------------------------------------------------
# 纯函数：成本合并
# ---------------------------------------------------------------------------


class TestMergeSessionCosts:
    def test_merges_numeric_keys(self):
        a = {"prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.01,
             "llm_calls": 2, "latency_ms": 300.0}
        b = {"prompt_tokens": 30, "completion_tokens": 10, "cost_usd": 0.005,
             "llm_calls": 1, "latency_ms": 120.5}
        out = _merge_session_costs(a, b)
        assert out["prompt_tokens"] == 130
        assert out["completion_tokens"] == 60
        assert out["llm_calls"] == 3
        assert out["latency_ms"] == pytest.approx(420.5)

    def test_handles_missing_keys(self):
        """缺失键按 0 处理，不影响其他键。"""
        out = _merge_session_costs({}, {"prompt_tokens": 10, "llm_calls": 1})
        assert out["prompt_tokens"] == 10
        assert out["llm_calls"] == 1
        assert out["completion_tokens"] == 0

    def test_ignores_derived_total_tokens(self):
        """派生字段 total_tokens 不参与合并（会被求和导致重复计数）。"""
        out = _merge_session_costs(
            {"prompt_tokens": 5, "total_tokens": 999},
            {"prompt_tokens": 3, "total_tokens": 3},
        )
        assert "total_tokens" not in out


# ---------------------------------------------------------------------------
# 纯函数：子代理报告提取
# ---------------------------------------------------------------------------


class TestExtractSubagentReport:
    def test_extracts_last_plain_ai_message(self):
        msgs = [
            AIMessage(content="思考", tool_calls=[{"id": "t1", "name": "execute_code", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="t1", name="execute_code"),
            AIMessage(content="这是最终报告"),
        ]
        assert _extract_subagent_report(msgs) == "这是最终报告"

    def test_skips_ai_with_tool_calls(self):
        """带 tool_calls 的 AI 消息不算报告（跳过向后找）。"""
        msgs = [
            AIMessage(content="中间步骤", tool_calls=[{"id": "t1", "name": "x", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="t1", name="x"),
            AIMessage(content="最终报告"),
        ]
        assert _extract_subagent_report(msgs) == "最终报告"

    def test_empty_when_no_content(self):
        assert _extract_subagent_report([]) == ""
        assert _extract_subagent_report([AIMessage(content="")]) == ""


# ---------------------------------------------------------------------------
# supervisor 后路由
# ---------------------------------------------------------------------------


class TestRouteAfterSupervisor:
    def test_subagent_names_route_to_run_specialist(self):
        for name in SUBAGENT_NAMES:
            assert _route_after_supervisor({"specialist": name, "specialist_task": "任务"}) == "run_specialist"

    def test_general_with_task_routes_to_planner(self):
        assert _route_after_supervisor({"specialist": "general", "specialist_task": "任务"}) == "planner"

    def test_no_task_routes_to_agent(self):
        assert _route_after_supervisor({"specialist": "researcher", "specialist_task": ""}) == "agent"
        assert _route_after_supervisor({"specialist": "", "specialist_task": ""}) == "agent"


# ---------------------------------------------------------------------------
# 分类后路由：code 意图必须进 supervisor（否则 coder 子代理不可达）
# ---------------------------------------------------------------------------


class TestRouteAfterClassifyCodeIntent:
    def test_code_intent_routes_to_supervisor(self):
        """code 意图（写代码）→ supervisor，从而可委派给 coder 子代理。"""
        assert _route_after_classify({"intent": "code", "needs_rag": False}) == "supervisor"

    def test_code_with_rag_goes_query_rewrite_then_supervisor(self):
        """code + needs_rag → 先改写查询词，RAG 后 _route_after_rag 仍进 supervisor。"""
        assert _route_after_classify({"intent": "code", "needs_rag": True}) == "query_rewrite"
        assert _route_after_rag({"intent": "code"}) == "supervisor"

    def test_complex_still_supervisor(self):
        assert _route_after_classify({"intent": "complex", "needs_rag": False}) == "supervisor"
        assert _route_after_rag({"intent": "complex"}) == "supervisor"

    def test_other_intents_go_agent(self):
        assert _route_after_classify({"intent": "knowledge", "needs_rag": False}) == "agent"
        assert _route_after_classify({"intent": "trivial", "needs_rag": False}) == "agent"
        assert _route_after_rag({"intent": "knowledge"}) == "agent"


# ---------------------------------------------------------------------------
# 消息清理：孤儿 ToolMessage 必须丢弃（防 DeepSeek 400）
# ---------------------------------------------------------------------------


class TestSanitizeMessagesForApi:
    def _ai(self, tool_calls, content="思考"):
        return AIMessage(content=content, tool_calls=tool_calls)

    def test_keeps_valid_pair(self):
        """AI(tool_calls) + 对应 ToolMessage → 都保留。"""
        msgs = [
            self._ai([{"id": "a", "name": "execute_code", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="a", name="execute_code"),
        ]
        out = _sanitize_messages_for_api(msgs)
        assert len(out) == 2

    def test_drops_orphaned_toolmessage(self):
        """ToolMessage 的 AI tool_call 已被压缩/清理（无 surviving tool_call）→ 丢弃。
        否则 DeepSeek 400: role 'tool' must follow a preceding message with 'tool_calls'。
        """
        msgs = [
            AIMessage(content="总结上文"),
            ToolMessage(content="结果", tool_call_id="ghost", name="execute_code"),
        ]
        out = _sanitize_messages_for_api(msgs)
        assert all(not isinstance(m, ToolMessage) for m in out)
        assert len(out) == 1

    def test_flattens_orphaned_ai_toolcalls_keeps_text(self):
        """AI 的 tool_calls 全无 ToolMessage → 降级为纯文本（保留 content）。"""
        msgs = [self._ai([{"id": "x", "name": "mcp_read_file", "args": {}}], content="读文件")]
        out = _sanitize_messages_for_api(msgs)
        assert len(out) == 1
        assert isinstance(out[0], AIMessage)
        assert out[0].tool_calls in (None, [])
        assert "读文件" in out[0].content

    def test_mixed_toolcalls_flatten_and_drop_partial_toolmessage(self):
        """AI 有 2 个 tool_call，仅 1 个有 ToolMessage → AI 降级为文本，孤儿的 ToolMessage 一并丢弃。"""
        msgs = [
            self._ai([
                {"id": "c1", "name": "tool_search_knowledge", "args": {}},
                {"id": "c2", "name": "execute_code", "args": {}},
            ]),
            ToolMessage(content="kb 结果", tool_call_id="c1", name="tool_search_knowledge"),
        ]
        out = _sanitize_messages_for_api(msgs)
        assert all(not isinstance(m, ToolMessage) for m in out)
        assert len(out) == 1 and isinstance(out[0], AIMessage)
        assert out[0].tool_calls in (None, [])


# ---------------------------------------------------------------------------
# 子代理内审批门控
# ---------------------------------------------------------------------------


class TestSubCheckApproval:
    def test_high_risk_calls_auto_denied(self):
        """execute_code（高风险）→ 不 interrupt，直接拒绝并记录。"""
        msg = AIMessage(content="do", tool_calls=[
            {"id": "c1", "name": "execute_code", "args": {"code": "print(1)"}},
        ])
        out = _run_sync(sub_check_approval, {"messages": [msg]})
        assert out["approval_decision"] == "denied"
        assert out["denied_tool_calls"] == [{"id": "c1", "name": "execute_code", "args": {"code": "print(1)"}}]
        denied = [m for m in out["messages"] if isinstance(m, ToolMessage)]
        assert len(denied) == 1
        assert "denied" in denied[0].content  # status: denied 文案

    def test_mixed_calls_only_deny_risky(self):
        """高风险与低风险混合时，只拒绝高风险调用。"""
        msg = AIMessage(content="do", tool_calls=[
            {"id": "c1", "name": "execute_code", "args": {"code": "x"}},
            {"id": "c2", "name": "tool_search_knowledge", "args": {"query": "x"}},
        ])
        out = _run_sync(sub_check_approval, {"messages": [msg]})
        assert out["approval_decision"] == "denied"
        names = [d["name"] for d in out["denied_tool_calls"]]
        assert names == ["execute_code"]

    def test_low_risk_passes_through(self):
        """全低风险 → 返回空决定，放行到 tools 节点。"""
        msg = AIMessage(content="search", tool_calls=[
            {"id": "c3", "name": "tool_search_knowledge", "args": {}},
        ])
        out = _run_sync(sub_check_approval, {"messages": [msg]})
        assert out["approval_decision"] == ""

    def test_accumulates_existing_denied(self):
        """已有 denied_tool_calls 时追加而不是覆盖。"""
        msg = AIMessage(content="do", tool_calls=[
            {"id": "c1", "name": "execute_code", "args": {}},
        ])
        out = _run_sync(sub_check_approval, {
            "messages": [msg],
            "denied_tool_calls": [{"id": "prev", "name": "mcp_write_file", "args": {}}],
        })
        assert [d["id"] for d in out["denied_tool_calls"]] == ["prev", "c1"]


# ---------------------------------------------------------------------------
# 子图组装（stub 节点）
# ---------------------------------------------------------------------------


def _run_sync(coro_fn, state: dict):
    """在 pytest（无 asyncio plugin 强制）下运行 async 节点函数。"""
    import asyncio
    return asyncio.run(coro_fn(state))


async def _stub_agent(state):
    """首轮请求执行高风险工具，被拒后产出最终报告。"""
    if len(state["messages"]) <= 1:
        return {"messages": [AIMessage(content="需要执行", tool_calls=[
            {"id": "c1", "name": "execute_code", "args": {"code": "x"}},
        ])]}
    return {"messages": [AIMessage(content="最终报告：execute_code 需审批")]}


async def _stub_tools(state):
    return {"messages": []}


async def _stub_reflect(state):
    return {"messages": []}


def _stub_track(state):
    return {"tool_failures": 0, "tool_retries": 0}


class TestBuildSpecialistSubgraph:
    def test_compiles_without_checkpointer(self):
        sub = build_specialist_subgraph(
            agent_node=_stub_agent, tool_node=_stub_tools,
            reflect_node=_stub_reflect, track_failures=_stub_track,
        )
        # 子图必须禁用 checkpointer（compile(checkpointer=False) → 属性为 False），
        # 避免继承父图 saver 污染父线程 checkpoint
        assert sub.checkpointer is False

    def test_ainvoke_deny_loop_and_report(self):
        """高风险调用 → 子代理自动拒绝 → 子代理产出报告 → 子图结束。"""
        sub = build_specialist_subgraph(
            agent_node=_stub_agent, tool_node=_stub_tools,
            reflect_node=_stub_reflect, track_failures=_stub_track,
            check_approval_node=sub_check_approval,
        )
        import asyncio
        out = asyncio.run(sub.ainvoke(
            {"messages": [AIMessage(content="hi")], "specialist": "coder"},
            config={"recursion_limit": 60},
        ))
        # 被拒绝的调用已记录
        assert out["denied_tool_calls"] == [{"id": "c1", "name": "execute_code", "args": {"code": "x"}}]
        # 报告提取 = 最终 AI 消息
        assert _extract_subagent_report(out["messages"]) == "最终报告：execute_code 需审批"

    def test_initial_human_message_passthrough(self):
        """子图输入 messages 会被 add_messages 正确合并（HumanMessage 保留）。"""
        sub = build_specialist_subgraph(
            agent_node=_stub_agent, tool_node=_stub_tools,
            reflect_node=_stub_reflect, track_failures=_stub_track,
        )
        import asyncio
        out = asyncio.run(sub.ainvoke({
            "messages": [HumanMessage(content="请完成任务")],
            "specialist": "researcher",
        }))
        assert any(isinstance(m, HumanMessage) and m.content == "请完成任务" for m in out["messages"])
